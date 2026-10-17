"""Owner registry for universal login.

Owners are ``(platform, user_id)`` pairs stored in the connector's own settings directory
(``owners.json``). Platform ``local`` (the host's own CLI/TUI) is always an owner and is never
stored. The legacy ``owners`` list of Telegram numeric ids in ``settings.json`` is migrated into
``telegram`` entries (``added_via: "migrated"``) the first time it is readable; ``settings.json``
itself is never rewritten, so 0.1.x code that reads it keeps working.

Claim codes let a new platform identity become an owner: ``start_claim`` (host-local only) makes a
6-digit single-use code valid for 10 minutes that dies after 5 wrong attempts. Only a digest of
the code is stored. Nothing here logs or raises with secrets.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .settings import private_json

LOCAL = "local"
CLAIM_TTL_SECONDS = 600
MAX_CLAIM_ATTEMPTS = 5
CLAIM_DIGITS = 6
FILE_NAME = "owners.json"


def _clean_platform(platform: object) -> Optional[str]:
    if not isinstance(platform, str) or not 1 <= len(platform) <= 32:
        return None
    text = platform.strip().lower()
    if not text or not all(ch.islower() or ch.isdigit() or ch in "_-" for ch in text):
        return None
    return text


def _clean_user(user_id: object) -> Optional[str]:
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        return None
    text = str(user_id).strip()
    if not 1 <= len(text) <= 128 or not all(ch.isprintable() for ch in text):
        return None
    return text


def _digest(code: str) -> str:
    return hashlib.sha256(("fleet-claim:" + code).encode()).hexdigest()


def normalise_code(code: object) -> Optional[str]:
    if not isinstance(code, (str, int)) or isinstance(code, bool):
        return None
    text = "".join(str(code).split())
    return text if len(text) == CLAIM_DIGITS and text.isascii() and text.isdigit() else None


class OwnerRegistry:
    def __init__(self, directory: Path, *, clock=time.time) -> None:
        self.directory = Path(directory)
        self._clock = clock
        self._thread_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.directory / FILE_NAME

    # -- storage -------------------------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory / (FILE_NAME + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("owners"), list):
            data["owners"] = []
        return data

    def _legacy_telegram_ids(self) -> Optional[list]:
        """Numeric ids from settings.json, or ``None`` when it cannot be read yet."""
        try:
            raw = json.loads((self.directory / "settings.json").read_text())
        except (OSError, ValueError):
            return None
        ids = raw.get("owners") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return []
        return [v for v in ids if type(v) is int and v > 0]

    def _state(self) -> tuple:
        """Load, migrating legacy owners once. Returns ``(data, dirty)``."""
        data = self._read()
        dirty = False
        if not data.get("migrated"):
            legacy = self._legacy_telegram_ids()
            if legacy is not None:
                have = {(o.get("platform"), o.get("user_id")) for o in data["owners"]}
                for value in legacy:
                    if ("telegram", str(value)) not in have:
                        data["owners"].append({
                            "platform": "telegram", "user_id": str(value),
                            "added_at": int(self._clock()), "added_via": "migrated"})
                data["migrated"] = True
                dirty = True
        return data, dirty

    def _save(self, data: dict) -> None:
        private_json(self.path, data)

    # -- queries -------------------------------------------------------------------------------

    def list_owners(self) -> list:
        """Stored owners in order (``local`` is implicit and not listed)."""
        with self._locked():
            data, dirty = self._state()
            if dirty:
                self._save(data)
            return [dict(o) for o in data["owners"]]

    def is_owner(self, platform: object, user_id: object) -> bool:
        p = _clean_platform(platform)
        if p is None:
            return False
        if p == LOCAL:
            return True
        u = _clean_user(user_id)
        if u is None:
            return False
        return any(o.get("platform") == p and o.get("user_id") == u for o in self.list_owners())

    # -- changes -------------------------------------------------------------------------------

    def remove_owner(self, index: int) -> Optional[dict]:
        """Remove the owner at 1-based ``index`` (as numbered by ``list_owners``).

        Removing the last stored owner is allowed: local access always remains.
        """
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        with self._locked():
            data, _ = self._state()
            if not 1 <= index <= len(data["owners"]):
                return None
            removed = data["owners"].pop(index - 1)
            self._save(data)
            return dict(removed)

    def start_claim(self) -> tuple:
        """Create a claim code. Returns ``(code, expires_at)``; replaces any earlier code."""
        code = f"{secrets.randbelow(10 ** CLAIM_DIGITS):0{CLAIM_DIGITS}d}"
        now = int(self._clock())
        with self._locked():
            data, _ = self._state()
            data["claim"] = {"digest": _digest(code), "expires_at": now + CLAIM_TTL_SECONDS,
                             "attempts": 0}
            self._save(data)
        return code, now + CLAIM_TTL_SECONDS

    def claim(self, code: object, platform: object, user_id: object) -> Optional[dict]:
        """Redeem a claim code. Returns the new/existing owner entry, or ``None`` when the code is
        wrong, expired, used up or the identity is invalid. Platform ``local`` is refused."""
        p = _clean_platform(platform)
        u = _clean_user(user_id)
        if p is None or u is None or p == LOCAL:
            return None
        normalised = normalise_code(code)
        now = int(self._clock())
        with self._locked():
            data, _ = self._state()
            claim = data.get("claim")
            if not isinstance(claim, dict) or claim.get("expires_at", 0) <= now:
                if claim is not None:
                    data.pop("claim", None)
                    self._save(data)
                return None
            if normalised is None or not hmac.compare_digest(_digest(normalised), claim["digest"]):
                claim["attempts"] = int(claim.get("attempts", 0)) + 1
                if claim["attempts"] >= MAX_CLAIM_ATTEMPTS:
                    data.pop("claim", None)
                self._save(data)
                return None
            data.pop("claim", None)  # single use
            for owner in data["owners"]:
                if owner.get("platform") == p and owner.get("user_id") == u:
                    self._save(data)
                    return dict(owner)
            entry = {"platform": p, "user_id": u, "added_at": now, "added_via": "claim"}
            data["owners"].append(entry)
            self._save(data)
            return dict(entry)
