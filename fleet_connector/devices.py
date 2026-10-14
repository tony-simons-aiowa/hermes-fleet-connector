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
    device_id       TEXT
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
    invitation_id   TEXT
);
CREATE TABLE IF NOT EXISTS device_sessions (
    token_hash BLOB PRIMARY KEY,
    device_id  TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_sessions_device ON device_sessions(device_id);
"""


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
            finally:
                conn.close()
            self._initialised = True

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
        scopes: tuple = (SCOPE_OPERATOR,),
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
            expires_at=now + ttl, state="pending")
        with self._write() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM invitations WHERE state='pending' AND expires_at > ?",
                (now,)).fetchone()[0]
            if pending >= MAX_PENDING_INVITATIONS:
                raise TooManyInvitations("too many pending invitations")
            conn.execute(
                "INSERT INTO invitations (id, secret_hash, origin, label, scopes, created_by, "
                "created_at, expires_at, state) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (invitation.id, _digest(secret), invitation.origin, invitation.label,
                 _scopes_to_text(invitation.scopes), invitation.created_by, invitation.created_at,
                 invitation.expires_at))
            self._purge_locked(conn, now)
        return IssuedInvitation(invitation=invitation, secret=secret)

    def _purge_locked(self, conn: sqlite3.Connection, now: int) -> None:
        """Drop invitations that ended long ago so the table cannot grow without bound."""
        horizon = now - 7 * 86400
        conn.execute("DELETE FROM invitations WHERE expires_at < ? AND state != 'redeemed'",
                     (horizon,))
        conn.execute("DELETE FROM device_sessions WHERE expires_at < ?", (now,))

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
        origin: Optional[str] = None,
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
            created_at=row["created_at"], expires_at=row["expires_at"], state=row["state"])

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
                + "ORDER BY created_at DESC").fetchall()
        return [self._device_from_row(r) for r in rows]

    @staticmethod
    def _device_from_row(row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"], label=row["label"], device_model=row["device_model"],
            origin=row["origin"], scopes=_scopes_from_text(row["scopes"]),
            created_at=row["created_at"], last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"])


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
