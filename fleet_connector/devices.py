"""Device pairing store for Hermes Fleet: short-lived single-use invitations that are exchanged
for revocable, device-specific credentials.

Threat model (what this module enforces, in order of importance)

* An invitation is a bearer capability, so it is high-entropy (256-bit secret + 128-bit id),
  short-lived (60 s .. 15 min), single-use and scoped to ONE origin. Only SHA-256 digests of the
  secret are stored, so a database read never yields a usable invitation.
* Redemption is one SQLite ``BEGIN IMMEDIATE`` transaction that flips ``pending -> redeemed``
  with a compare-and-set ``UPDATE ... WHERE state='pending' AND expires_at > now`` and creates the
  device row in the same transaction. Two simultaneous redemptions (threads or processes) cannot
  both win; the loser sees ``AlreadyRedeemed``.
* ``preview`` never mutates an invitation (apart from counting wrong-secret attempts), so opening
  or inspecting a link cannot consume it or approve access.
* The state of an invitation is only revealed to a caller who proved knowledge of its secret.
  A wrong secret, an unknown id and a malformed id are indistinguishable (``InvalidInvitation``)
  so guessers learn nothing. After ``MAX_FAILED_ATTEMPTS`` wrong secrets for a known id the
  invitation is cancelled.
* A device credential is a separate 256-bit secret stored hashed. Revocation is immediate and also
  kills every live device session. Device sessions are short-lived and are NOT able to mint
  invitations or manage other devices (enforced by the routes, which require a non-device owner).

No secret, credential or invitation is ever logged or placed in an exception message.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# --- policy -----------------------------------------------------------------------------------

MIN_TTL_SECONDS = 60
DEFAULT_TTL_SECONDS = 600
MAX_TTL_SECONDS = 900
MAX_FAILED_ATTEMPTS = 5
MAX_PENDING_INVITATIONS = 20
DEVICE_SESSION_TTL_SECONDS = 3600
SCOPE_OPERATOR = "fleet:operator"
ALLOWED_SCOPES = (SCOPE_OPERATOR,)
_MAX_LABEL = 64

INVITATION_ID_BYTES = 16
INVITATION_SECRET_BYTES = 32
CREDENTIAL_PREFIX = "hfd1"
SESSION_PREFIX = "hfa1"

# --- universal login (v2 approval flow) policy ---
REQUEST_TTL_SECONDS = 600
CREDENTIAL_WINDOW_SECONDS = 600
POLL_INTERVAL_SECONDS = 2
MIN_POLL_GAP_SECONDS = 1.0
MATCH_CODE_DIGITS = 6
MAX_APPROVER_FAILURES = 10
APPROVER_WINDOW_SECONDS = 600
APPROVER_LOCK_SECONDS = 600
CLIENT_SECRET_MIN = 43
CLIENT_SECRET_MAX = 128

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invitations (
    id              TEXT PRIMARY KEY,
    secret_hash     BLOB NOT NULL,
    origin          TEXT NOT NULL,
    label           TEXT NOT NULL,
    scopes          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'redeemed', 'cancelled')),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    redeemed_at     INTEGER,
    device_id       TEXT,
    requires_approval INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices (
    id              TEXT PRIMARY KEY,
    credential_hash BLOB NOT NULL,
    label           TEXT NOT NULL,
    device_model    TEXT NOT NULL DEFAULT '',
    origin          TEXT NOT NULL,
    scopes          TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    last_used_at    INTEGER,
    revoked_at      INTEGER,
    invitation_id   TEXT,
    approved_by_platform TEXT,
    approved_by_user     TEXT
);
CREATE TABLE IF NOT EXISTS device_sessions (
    token_hash BLOB PRIMARY KEY,
    device_id  TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_sessions_device ON device_sessions(device_id);
CREATE TABLE IF NOT EXISTS pairing_requests (
    id                 TEXT PRIMARY KEY,
    invitation_id      TEXT NOT NULL UNIQUE,
    client_secret_hash BLOB NOT NULL,
    device_name        TEXT NOT NULL,
    device_model       TEXT NOT NULL DEFAULT '',
    match_code         TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    expires_at         INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'denied')),
    approved_at        INTEGER,
    device_id          TEXT,
    credential         TEXT,
    last_poll_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_pairing_requests_code ON pairing_requests(match_code);
CREATE TABLE IF NOT EXISTS approver_failures (
    approver TEXT NOT NULL,
    at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approver_failures ON approver_failures(approver, at);
CREATE TABLE IF NOT EXISTS approver_locks (
    approver     TEXT PRIMARY KEY,
    locked_until INTEGER NOT NULL
);
"""

# Columns added after 0.1.x: (table, column, DDL). Existing rows keep the safe defaults
# (invitations stay v1 / no approval; devices have no recorded approver).
_MIGRATIONS = (
    ("invitations", "requires_approval", "INTEGER NOT NULL DEFAULT 0"),
    ("devices", "approved_by_platform", "TEXT"),
    ("devices", "approved_by_user", "TEXT"),
)


# --- errors (no payloads: messages must never carry secrets or identifiers) -------------------

class PairingError(Exception):
    """Base class. ``code`` is a stable machine word safe to return to clients."""
    code = "pairing_error"


class InvalidInvitation(PairingError):
    """Unknown id, malformed id, or wrong secret (deliberately indistinguishable)."""
    code = "invalid"


class InvitationExpired(PairingError):
    code = "expired"


class AlreadyRedeemed(PairingError):
    code = "already_used"


class InvitationCancelled(PairingError):
    code = "cancelled"


class TooManyInvitations(PairingError):
    code = "too_many_pending"


class InvalidCredential(PairingError):
    """Unknown, malformed, revoked or wrong device credential."""
    code = "invalid_credential"


class InvalidRequest(PairingError):
    code = "invalid_request"


class ApprovalRequired(PairingError):
    """A v1 redeem was attempted on an invitation that only the approval flow may consume."""
    code = "approval_required"


class ApprovalNotSupported(PairingError):
    """A v2 request was attempted on a v1 (no-approval) invitation."""
    code = "approval_not_supported"


class RateLimited(PairingError):
    code = "rate_limited"


class NoMatch(PairingError):
    """No pending request has that match code (or the code was malformed)."""
    code = "no_match"


class Locked(PairingError):
    """Too many wrong codes from one approver."""
    code = "locked"


# --- records ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Invitation:
    id: str
    origin: str
    label: str
    scopes: tuple
    created_by: str
    created_at: int
    expires_at: int
    state: str
    requires_approval: bool = False


@dataclass(frozen=True)
class IssuedInvitation:
    """Returned exactly once, at creation. ``secret`` is not recoverable afterwards."""
    invitation: Invitation
    secret: str


@dataclass(frozen=True)
class Device:
    id: str
    label: str
    device_model: str
    origin: str
    scopes: tuple
    created_at: int
    last_used_at: Optional[int]
    revoked_at: Optional[int]
    approved_by_platform: Optional[str] = None
    approved_by_user: Optional[str] = None


@dataclass(frozen=True)
class PairingRequest:
    """A pending (or settled) approval request. ``match_code`` is shown to owner and phone."""
    id: str
    invitation_id: str
    match_code: str
    device_name: str
    device_model: str
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class RequestStatus:
    """``status`` is pending | approved | delivered | denied | expired. ``credential`` is set
    only while ``approved`` (until first login or the delivery window closes)."""
    status: str
    expires_at: int
    device_id: Optional[str] = None
    device_label: Optional[str] = None
    scopes: tuple = ()
    credential: Optional[str] = None


@dataclass(frozen=True)
class RedeemedDevice:
    """Returned exactly once, at redemption. ``credential`` is not recoverable afterwards."""
    device: Device
    credential: str


@dataclass(frozen=True)
class DeviceSession:
    device_id: str
    label: str
    scopes: tuple
    access_token: str
    expires_at: int


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _clean_label(label: object, *, default: str) -> str:
    if not isinstance(label, str):
        return default
    cleaned = "".join(ch for ch in label if ch.isprintable()).strip()[:_MAX_LABEL]
    return cleaned or default


def _is_token(value: object, *, min_len: int, max_len: int) -> bool:
    if not isinstance(value, str) or not (min_len <= len(value) <= max_len):
        return False
    return all(ch.isalnum() or ch in "-_" for ch in value)


def _scopes_to_text(scopes: tuple) -> str:
    return json.dumps(sorted(scopes), separators=(",", ":"))


def _scopes_from_text(text: str) -> tuple:
    try:
        parsed = json.loads(text)
        return tuple(str(s) for s in parsed)
    except (ValueError, TypeError):
        return ()


class FleetDeviceStore:
    """SQLite-backed store. Safe for concurrent threads and processes (WAL + IMMEDIATE writes).

    ``clock`` is injectable so expiry is testable without sleeping.
    """

    def __init__(self, path: Path, *, clock=time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._init_lock = threading.Lock()
        self._initialised = False

    # -- plumbing ------------------------------------------------------------------------------

    def _now(self) -> int:
        return int(self._clock())

    def _ensure_ready(self) -> None:
        if self._initialised:
            return
        with self._init_lock:
            if self._initialised:
                return
            parent = self._path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            try:
                os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._migrate(conn)
            finally:
                conn.close()
            self._initialised = True

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        for table, column, ddl in _MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                # Another process migrated first.
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._ensure_ready()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        self._ensure_ready()
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # -- invitations ---------------------------------------------------------------------------

    def create_invitation(
        self, *, origin: str, created_by: str, label: object = "", ttl_seconds: object = None,
        scopes: tuple = (SCOPE_OPERATOR,), requires_approval: bool = False,
    ) -> IssuedInvitation:
        if not isinstance(origin, str) or not origin.startswith("https://"):
            raise InvalidRequest("origin must be https")
        if not scopes or any(s not in ALLOWED_SCOPES for s in scopes):
            raise InvalidRequest("unsupported scope")
        ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise InvalidRequest("ttl must be an integer")
        ttl = max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, ttl))
        now = self._now()
        invitation_id = secrets.token_urlsafe(INVITATION_ID_BYTES)
        secret = secrets.token_urlsafe(INVITATION_SECRET_BYTES)
        invitation = Invitation(
            id=invitation_id, origin=origin, label=_clean_label(label, default="Fleet device"),
            scopes=tuple(scopes), created_by=str(created_by)[:128], created_at=now,
            expires_at=now + ttl, state="pending", requires_approval=bool(requires_approval))
        with self._write() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM invitations WHERE state='pending' AND expires_at > ?",
                (now,)).fetchone()[0]
            if pending >= MAX_PENDING_INVITATIONS:
                raise TooManyInvitations("too many pending invitations")
            conn.execute(
                "INSERT INTO invitations (id, secret_hash, origin, label, scopes, created_by, "
                "created_at, expires_at, state, requires_approval) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
                (invitation.id, _digest(secret), invitation.origin, invitation.label,
                 _scopes_to_text(invitation.scopes), invitation.created_by, invitation.created_at,
                 invitation.expires_at, 1 if requires_approval else 0))
            self._purge_locked(conn, now)
        return IssuedInvitation(invitation=invitation, secret=secret)

    def _purge_locked(self, conn: sqlite3.Connection, now: int) -> None:
        """Drop invitations that ended long ago so the table cannot grow without bound."""
        horizon = now - 7 * 86400
        conn.execute("DELETE FROM invitations WHERE expires_at < ? AND state != 'redeemed'",
                     (horizon,))
        conn.execute("DELETE FROM device_sessions WHERE expires_at < ?", (now,))
        # Plaintext credentials never outlive their delivery window, even if nobody polls.
        conn.execute(
            "UPDATE pairing_requests SET credential=NULL "
            "WHERE credential IS NOT NULL AND approved_at <= ?", (now - CREDENTIAL_WINDOW_SECONDS,))
        conn.execute("DELETE FROM pairing_requests WHERE expires_at < ?", (horizon,))
        conn.execute("DELETE FROM approver_failures WHERE at < ?", (now - APPROVER_WINDOW_SECONDS,))
        conn.execute("DELETE FROM approver_locks WHERE locked_until < ?", (now,))

    def preview(
        self, *, invitation_id: object, secret: object, origin: Optional[str] = None,
    ) -> Invitation:
        """Validate without consuming. Raises a ``PairingError`` subclass on any problem."""
        row = self._verified_row(invitation_id, secret, consume=False, origin=origin)
        return self._invitation_from_row(row)

    def redeem(
        self, *, invitation_id: object, secret: object, device_name: object = "",
        device_model: object = "", origin: Optional[str] = None,
    ) -> RedeemedDevice:
        """Atomically consume the invitation and create the device. Exactly one caller can win."""
        row = self._verified_row(invitation_id, secret, consume=True, origin=origin)
        if row["requires_approval"]:
            raise ApprovalRequired("approval required")
        now = self._now()
        device_id = uuid.uuid4().hex
        secret_part = secrets.token_urlsafe(32)
        credential = f"{CREDENTIAL_PREFIX}.{device_id}.{secret_part}"
        label = _clean_label(device_name, default=row["label"])
        model = _clean_label(device_model, default="")
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE invitations SET state='redeemed', redeemed_at=?, device_id=? "
                "WHERE id=? AND state='pending' AND expires_at > ?",
                (now, device_id, row["id"], now)).rowcount
            if changed != 1:
                # Lost the race (or it expired between the check and the write): report precisely
                # what the winner left behind. The caller already proved knowledge of the secret.
                current = conn.execute(
                    "SELECT state, expires_at FROM invitations WHERE id=?", (row["id"],)).fetchone()
                if current is not None and current["state"] == "redeemed":
                    raise AlreadyRedeemed("already redeemed")
                if current is not None and current["state"] == "cancelled":
                    raise InvitationCancelled("cancelled")
                raise InvitationExpired("expired")
            conn.execute(
                "INSERT INTO devices (id, credential_hash, label, device_model, origin, scopes, "
                "created_at, invitation_id) VALUES (?,?,?,?,?,?,?,?)",
                (device_id, _digest(credential), label, model, row["origin"], row["scopes"], now,
                 row["id"]))
        device = Device(
            id=device_id, label=label, device_model=model, origin=row["origin"],
            scopes=_scopes_from_text(row["scopes"]), created_at=now, last_used_at=None,
            revoked_at=None)
        return RedeemedDevice(device=device, credential=credential)

    def _verified_row(
        self, invitation_id: object, secret: object, *, consume: bool,
        origin: Optional[str] = None, allow_redeemed: bool = False,
    ) -> sqlite3.Row:
        # Shape checks first: these never touch the database.
        if not _is_token(invitation_id, min_len=16, max_len=64) or not _is_token(
                secret, min_len=32, max_len=128):
            raise InvalidInvitation("invalid")
        now = self._now()
        presented = _digest(secret)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM invitations WHERE id=?", (invitation_id,)).fetchone()
            if row is None:
                raise InvalidInvitation("invalid")
            if not hmac.compare_digest(presented, bytes(row["secret_hash"])):
                failures = row["failed_attempts"] + 1
                state = row["state"]
                if state == "pending" and failures >= MAX_FAILED_ATTEMPTS:
                    state = "cancelled"
                conn.execute("UPDATE invitations SET failed_attempts=?, state=? WHERE id=?",
                             (failures, state, row["id"]))
                # The transaction must COMMIT the counter, so report outside the context.
                row = None
        if row is None:
            raise InvalidInvitation("invalid")
        # The secret is proven: only now may the state be revealed.
        if origin is not None and origin != row["origin"]:
            # Scoped to the intended gateway: a valid secret presented to a different origin is
            # indistinguishable from an invalid one (and does not consume it).
            raise InvalidInvitation("invalid")
        if row["state"] == "redeemed":
            if allow_redeemed:
                return row
            raise AlreadyRedeemed("already redeemed")
        if row["state"] == "cancelled":
            raise InvitationCancelled("cancelled")
        if row["expires_at"] <= now:
            raise InvitationExpired("expired")
        return row

    @staticmethod
    def _invitation_from_row(row: sqlite3.Row) -> Invitation:
        return Invitation(
            id=row["id"], origin=row["origin"], label=row["label"],
            scopes=_scopes_from_text(row["scopes"]), created_by=row["created_by"],
            created_at=row["created_at"], expires_at=row["expires_at"], state=row["state"],
            requires_approval=bool(row["requires_approval"]))

    # -- approval flow (universal login) -------------------------------------------------------

    @staticmethod
    def _clean_approver(platform: object, user_id: object) -> str:
        if (not isinstance(platform, str) or not 1 <= len(platform) <= 32
                or not all(ch.islower() or ch.isdigit() or ch in "_-" for ch in platform)):
            raise InvalidRequest("invalid approver")
        if (not isinstance(user_id, (str, int)) or isinstance(user_id, bool)
                or not 1 <= len(str(user_id)) <= 128
                or not all(ch.isprintable() for ch in str(user_id))):
            raise InvalidRequest("invalid approver")
        return f"{platform}:{user_id}"

    @staticmethod
    def normalise_code(code: object) -> Optional[str]:
        """Strip spaces; return the 6 digits or ``None`` when malformed."""
        if not isinstance(code, (str, int)) or isinstance(code, bool):
            return None
        text = "".join(str(code).split())
        return text if len(text) == MATCH_CODE_DIGITS and text.isascii() and text.isdigit() else None

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> PairingRequest:
        return PairingRequest(
            id=row["id"], invitation_id=row["invitation_id"], match_code=row["match_code"],
            device_name=row["device_name"], device_model=row["device_model"],
            created_at=row["created_at"], expires_at=row["expires_at"])

    def create_request(
        self, *, invitation_id: object, secret: object, client_secret: object,
        device_name: object = "", device_model: object = "", origin: Optional[str] = None,
    ) -> PairingRequest:
        """Consume a ``requires_approval`` invitation into exactly one pending request.

        Idempotent for the same invitation + client secret; another client secret loses with
        ``AlreadyRedeemed``. The invitation secret must be proven first.
        """
        if not _is_token(client_secret, min_len=CLIENT_SECRET_MIN, max_len=CLIENT_SECRET_MAX):
            raise InvalidRequest("invalid client secret")
        row = self._verified_row(invitation_id, secret, consume=True, origin=origin,
                                 allow_redeemed=True)
        if not row["requires_approval"]:
            raise ApprovalNotSupported("approval not supported")
        presented = _digest(client_secret)
        now = self._now()
        with self._write() as conn:
            inv = conn.execute("SELECT * FROM invitations WHERE id=?", (row["id"],)).fetchone()
            existing = conn.execute(
                "SELECT * FROM pairing_requests WHERE invitation_id=?", (row["id"],)).fetchone()
            if existing is not None:
                if hmac.compare_digest(presented, bytes(existing["client_secret_hash"])):
                    return self._request_from_row(existing)
                raise AlreadyRedeemed("already redeemed")
            if inv["state"] == "redeemed":
                raise AlreadyRedeemed("already redeemed")
            if inv["state"] == "cancelled":
                raise InvitationCancelled("cancelled")
            if inv["expires_at"] <= now:
                raise InvitationExpired("expired")
            taken = {r[0] for r in conn.execute(
                "SELECT match_code FROM pairing_requests WHERE state='pending' AND expires_at > ?",
                (now,))}
            code = None
            for _ in range(200):
                candidate = f"{secrets.randbelow(10 ** MATCH_CODE_DIGITS):0{MATCH_CODE_DIGITS}d}"
                if candidate not in taken:
                    code = candidate
                    break
            if code is None:
                raise TooManyInvitations("too many pending requests")
            request_id = secrets.token_urlsafe(16)
            label = _clean_label(device_name, default=inv["label"])
            model = _clean_label(device_model, default="")
            conn.execute(
                "INSERT INTO pairing_requests (id, invitation_id, client_secret_hash, device_name, "
                "device_model, match_code, created_at, expires_at, state) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (request_id, inv["id"], presented, label, model, code, now,
                 now + REQUEST_TTL_SECONDS))
            conn.execute(
                "UPDATE invitations SET state='redeemed', redeemed_at=? "
                "WHERE id=? AND state='pending'", (now, inv["id"]))
            created = conn.execute(
                "SELECT * FROM pairing_requests WHERE id=?", (request_id,)).fetchone()
        return self._request_from_row(created)

    def request_status(self, *, request_id: object, client_secret: object) -> RequestStatus:
        """Poll a request. Unknown id and wrong client secret are indistinguishable."""
        if (not _is_token(request_id, min_len=16, max_len=64)
                or not _is_token(client_secret, min_len=CLIENT_SECRET_MIN, max_len=CLIENT_SECRET_MAX)):
            raise InvalidRequest("invalid request")
        presented = _digest(client_secret)
        now = self._now()
        stamp = float(self._clock())
        with self._write() as conn:
            row = conn.execute("SELECT * FROM pairing_requests WHERE id=?", (request_id,)).fetchone()
            if row is None or not hmac.compare_digest(presented, bytes(row["client_secret_hash"])):
                raise InvalidRequest("invalid request")
            # Only a caller who proved the client secret learns of rate limiting.
            last = row["last_poll_at"]
            if last is not None and 0 <= stamp - last < MIN_POLL_GAP_SECONDS:
                raise RateLimited("rate limited")
            conn.execute("UPDATE pairing_requests SET last_poll_at=? WHERE id=?", (stamp, row["id"]))
            state = row["state"]
            if state == "denied":
                return RequestStatus("denied", row["expires_at"])
            if state == "pending":
                if row["expires_at"] <= now:
                    return RequestStatus("expired", row["expires_at"])
                return RequestStatus("pending", row["expires_at"])
            credential = row["credential"]
            if credential is not None and now >= row["approved_at"] + CREDENTIAL_WINDOW_SECONDS:
                conn.execute("UPDATE pairing_requests SET credential=NULL WHERE id=?", (row["id"],))
                credential = None
            if credential is None:
                return RequestStatus("delivered", row["expires_at"], device_id=row["device_id"])
            dev = conn.execute("SELECT label, scopes FROM devices WHERE id=?",
                               (row["device_id"],)).fetchone()
            if dev is None:
                return RequestStatus("delivered", row["expires_at"], device_id=row["device_id"])
            return RequestStatus(
                "approved", row["expires_at"], device_id=row["device_id"], device_label=dev["label"],
                scopes=_scopes_from_text(dev["scopes"]), credential=credential)

    def list_pending(self) -> list:
        now = self._now()
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM pairing_requests WHERE state='pending' AND expires_at > ? "
                "ORDER BY created_at ASC", (now,)).fetchall()
        return [self._request_from_row(r) for r in rows]

    def _settle_by_code(self, code: object, approver: str, *, approve: bool):
        """Shared by approve/deny. Returns ``(request_row, device_or_None)``; wrong codes count
        toward the approver lockout (committed even though the caller sees an error)."""
        now = self._now()
        normalised = self.normalise_code(code)
        outcome = None
        with self._write() as conn:
            lock = conn.execute("SELECT locked_until FROM approver_locks WHERE approver=?",
                                (approver,)).fetchone()
            if lock is not None and lock["locked_until"] > now:
                raise Locked("locked")
            row = None
            if normalised is not None:
                row = conn.execute(
                    "SELECT * FROM pairing_requests WHERE state='pending' AND expires_at > ? "
                    "AND match_code=?", (now, normalised)).fetchone()
            if row is None:
                conn.execute("INSERT INTO approver_failures (approver, at) VALUES (?,?)",
                             (approver, now))
                failures = conn.execute(
                    "SELECT COUNT(*) FROM approver_failures WHERE approver=? AND at > ?",
                    (approver, now - APPROVER_WINDOW_SECONDS)).fetchone()[0]
                if failures >= MAX_APPROVER_FAILURES:
                    conn.execute(
                        "INSERT OR REPLACE INTO approver_locks (approver, locked_until) VALUES (?,?)",
                        (approver, now + APPROVER_LOCK_SECONDS))
                    conn.execute("DELETE FROM approver_failures WHERE approver=?", (approver,))
                outcome = "no_match"
            elif not approve:
                changed = conn.execute(
                    "UPDATE pairing_requests SET state='denied' WHERE id=? AND state='pending'",
                    (row["id"],)).rowcount
                outcome = "ok" if changed == 1 else "no_match"
                result = (row, None)
            else:
                device_id = uuid.uuid4().hex
                credential = f"{CREDENTIAL_PREFIX}.{device_id}.{secrets.token_urlsafe(32)}"
                changed = conn.execute(
                    "UPDATE pairing_requests SET state='approved', approved_at=?, device_id=?, "
                    "credential=? WHERE id=? AND state='pending' AND expires_at > ?",
                    (now, device_id, credential, row["id"], now)).rowcount
                if changed != 1:
                    outcome = "no_match"
                else:
                    inv = conn.execute("SELECT * FROM invitations WHERE id=?",
                                       (row["invitation_id"],)).fetchone()
                    platform, _, user = approver.partition(":")
                    conn.execute(
                        "INSERT INTO devices (id, credential_hash, label, device_model, origin, "
                        "scopes, created_at, invitation_id, approved_by_platform, approved_by_user) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (device_id, _digest(credential), row["device_name"], row["device_model"],
                         inv["origin"], inv["scopes"], now, inv["id"], platform, user))
                    conn.execute("UPDATE invitations SET device_id=? WHERE id=?",
                                 (device_id, inv["id"]))
                    dev = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
                    result = (row, self._device_from_row(dev))
                    outcome = "ok"
        if outcome != "ok":
            raise NoMatch("no match")
        return result

    def approve_by_code(self, *, code: object, approver_platform: object,
                        approver_user_id: object) -> Device:
        """Approve the pending request showing ``code``. Exactly one approver can win; the
        device row is created in the same transaction. The credential stays in the request row
        for the phone to collect (it is never returned here)."""
        approver = self._clean_approver(approver_platform, approver_user_id)
        _, device = self._settle_by_code(code, approver, approve=True)
        return device

    def deny_by_code(self, *, code: object, approver_platform: object,
                     approver_user_id: object) -> PairingRequest:
        approver = self._clean_approver(approver_platform, approver_user_id)
        row, _ = self._settle_by_code(code, approver, approve=False)
        return self._request_from_row(row)

    def cancel_invitation(self, invitation_id: object) -> bool:
        """Owner-side cancel of a still-pending invitation. Returns whether one was cancelled."""
        if not _is_token(invitation_id, min_len=16, max_len=64):
            return False
        with self._write() as conn:
            return conn.execute(
                "UPDATE invitations SET state='cancelled' WHERE id=? AND state='pending'",
                (invitation_id,)).rowcount == 1

    def list_pending_invitations(self) -> list:
        now = self._now()
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM invitations WHERE state='pending' AND expires_at > ? "
                "ORDER BY created_at DESC", (now,)).fetchall()
        return [self._invitation_from_row(r) for r in rows]

    # -- devices -------------------------------------------------------------------------------

    @staticmethod
    def _split_credential(credential: object) -> Optional[str]:
        if not isinstance(credential, str) or len(credential) > 200:
            return None
        parts = credential.split(".")
        if len(parts) != 3 or parts[0] != CREDENTIAL_PREFIX:
            return None
        if not _is_token(parts[1], min_len=32, max_len=32) or not _is_token(
                parts[2], min_len=32, max_len=128):
            return None
        return parts[1]

    def _device_for_credential(self, conn: sqlite3.Connection, credential: object):
        device_id = self._split_credential(credential)
        if device_id is None:
            return None
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if not hmac.compare_digest(_digest(credential), bytes(row["credential_hash"])):
            return None
        return row

    def open_session(self, credential: object) -> DeviceSession:
        """Exchange a device credential for a short-lived session access token."""
        now = self._now()
        with self._write() as conn:
            row = self._device_for_credential(conn, credential)
            if row is None:
                raise InvalidCredential("invalid credential")
            token = f"{SESSION_PREFIX}.{row['id']}.{secrets.token_urlsafe(32)}"
            expires_at = now + DEVICE_SESSION_TTL_SECONDS
            conn.execute(
                "INSERT INTO device_sessions (token_hash, device_id, expires_at) VALUES (?,?,?)",
                (_digest(token), row["id"], expires_at))
            conn.execute("UPDATE devices SET last_used_at=? WHERE id=?", (now, row["id"]))
            # First successful login with this credential: the phone has it, drop the plaintext.
            conn.execute("UPDATE pairing_requests SET credential=NULL WHERE device_id=?",
                         (row["id"],))
            self._purge_locked(conn, now)
        return DeviceSession(
            device_id=row["id"], label=row["label"], scopes=_scopes_from_text(row["scopes"]),
            access_token=token, expires_at=expires_at)

    def verify_session(self, access_token: object) -> Optional[DeviceSession]:
        """Return the live session for an access token, or ``None`` (expired/unknown/revoked)."""
        if not isinstance(access_token, str) or not access_token.startswith(SESSION_PREFIX + "."):
            return None
        if len(access_token) > 200:
            return None
        now = self._now()
        with self._read() as conn:
            sess = conn.execute(
                "SELECT s.device_id, s.expires_at, d.label, d.scopes, d.revoked_at "
                "FROM device_sessions s JOIN devices d ON d.id = s.device_id "
                "WHERE s.token_hash = ?", (_digest(access_token),)).fetchone()
        if sess is None or sess["revoked_at"] is not None or sess["expires_at"] <= now:
            return None
        return DeviceSession(
            device_id=sess["device_id"], label=sess["label"],
            scopes=_scopes_from_text(sess["scopes"]), access_token=access_token,
            expires_at=sess["expires_at"])

    def verify_credential(self, credential: object) -> Optional[Device]:
        """Return the device for a valid, unrevoked credential, else ``None``."""
        with self._read() as conn:
            row = self._device_for_credential(conn, credential)
        return self._device_from_row(row) if row is not None else None

    def close_session(self, access_token: object) -> None:
        if not isinstance(access_token, str):
            return
        with self._write() as conn:
            conn.execute("DELETE FROM device_sessions WHERE token_hash = ?", (_digest(access_token),))

    def revoke_device(self, device_id: object) -> bool:
        """Revoke a device and every live session of it. Idempotent; False when unknown."""
        if not _is_token(device_id, min_len=32, max_len=32):
            return False
        now = self._now()
        with self._write() as conn:
            row = conn.execute("SELECT id, revoked_at FROM devices WHERE id=?",
                               (device_id,)).fetchone()
            if row is None:
                return False
            if row["revoked_at"] is None:
                conn.execute("UPDATE devices SET revoked_at=? WHERE id=?", (now, device_id))
            conn.execute("DELETE FROM device_sessions WHERE device_id=?", (device_id,))
        return True

    def revoke_with_credential(self, credential: object) -> bool:
        """Self-revocation by the holder of the credential. False if it was not valid."""
        device = self.verify_credential(credential)
        if device is None:
            return False
        return self.revoke_device(device.id)

    def list_devices(self, *, include_revoked: bool = False) -> list:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM devices " + ("" if include_revoked else "WHERE revoked_at IS NULL ")
                + "ORDER BY created_at DESC, rowid DESC").fetchall()
        return [self._device_from_row(r) for r in rows]

    @staticmethod
    def _device_from_row(row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"], label=row["label"], device_model=row["device_model"],
            origin=row["origin"], scopes=_scopes_from_text(row["scopes"]),
            created_at=row["created_at"], last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"], approved_by_platform=row["approved_by_platform"],
            approved_by_user=row["approved_by_user"])


# --- gateway identity -------------------------------------------------------------------------

def get_instance_id(directory: Path) -> str:
    """Stable per-installation identifier (32 hex chars), created once.

    It is NOT a secret and carries no information about the machine; it lets Fleet recognise the
    same gateway reached through a different address. Creation is race-safe (``O_EXCL``).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "fleet_instance_id"
    for _ in range(3):
        try:
            existing = path.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            existing = ""
        if _is_token(existing, min_len=32, max_len=32):
            return existing
        candidate = uuid.uuid4().hex
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(candidate)
        return candidate
    raise OSError("could not establish a gateway instance id")
