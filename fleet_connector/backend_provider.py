"""Supported dashboard session-provider seam for a host-only proxy lease.

This credential belongs to the connector, expires in seconds, and never leaves
the host. It is separate from the unchanged phone HFD1 protocol. A stopped
connector cannot authorize requests; no existing administrative key is read.
"""
import hashlib
import hmac
import json
import os
import stat
import time
from pathlib import Path
from hermes_cli.dashboard_auth.base import DashboardAuthProvider, Session, LoginStart, InvalidCodeError, RefreshExpiredError
from .settings import root

class BackendLeaseProvider(DashboardAuthProvider):
    name = "fleet-connector-backend"
    display_name = "Fleet connector host proxy"
    supports_password = False
    supports_token = False
    supports_session = True
    hidden = True

    def __init__(self, home: Path):
        self.home = home.resolve()

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("host-only connector lease")

    def complete_login(self, *, code, state, code_verifier, redirect_uri) -> Session:
        raise InvalidCodeError("host-only connector lease")

    def verify_session(self, *, access_token: str):
        if not isinstance(access_token, str) or not access_token.startswith("hfcproxy.") or len(access_token) != 52:
            return None
        path = root(self.home) / "backend-lease.json"
        try:
            mode = path.stat()
            if path.is_symlink() or mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
                return None
            lease = json.loads(path.read_text())
            if (lease["home"] != str(self.home) or lease["expires_at"] <= time.time()
                    or type(lease["pid"]) is not int or lease["pid"] <= 0):
                return None
            os.kill(lease["pid"], 0)
            if not hmac.compare_digest(lease["token_hash"], hashlib.sha256(access_token.encode()).hexdigest()):
                return None
            return Session(user_id="service:fleet-connector", email="", display_name=self.display_name,
                org_id="", provider=self.name, expires_at=int(lease["expires_at"]),
                access_token=access_token, refresh_token="")
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise RefreshExpiredError("host-only connector lease")

    def revoke_session(self, *, refresh_token: str):
        return None
