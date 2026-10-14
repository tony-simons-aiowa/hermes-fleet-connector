"""Private protocol boundary in front of an unchanged, loopback stock Hermes server.

Owner operations exist only on an owner-only Unix socket. HTTP has its own explicit
device gate; no assumption that plugin routes inherit dashboard authorization.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import secrets
import socketserver
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from . import devices
from .settings import root
from .bridge import socket_path
from .display import DisplayFailure, display_capability
from .viewer import PrivateViewer, TERMINAL
from . import VERSION

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
ERRORS = {"invalid": 404, "expired": 410, "cancelled": 410, "already_used": 409,
          "invalid_credential": 401, "invalid_request": 400, "too_many_pending": 429}
SUMMARY = "Chat with the agents on this gateway, run and review their work, and manage them - the same access as signing in."
HTTP_PATHS = {"/api/media", "/api/cron/delivery-targets", "/api/cron/jobs",
              "/api/plugins/fleet-liveops/snapshot"}
# Kanban plugin routes the Fleet app uses, each with its exact allowed methods. Anything else
# under /api/plugins/kanban (config, attachments, runs, boards/{slug}/..., profiles, ...) stays refused.
_KB = "/api/plugins/kanban"
_ID = r"[A-Za-z0-9_-]{1,64}"
KANBAN_ROUTES = tuple((re.compile("^" + _KB + pattern + "$"), frozenset(methods)) for pattern, methods in (
    (r"/board", {"GET"}),
    (r"/boards", {"GET"}),
    (r"/assignees", {"GET"}),
    (r"/orchestration", {"GET", "PUT"}),
    (r"/dispatch", {"POST"}),
    (r"/links", {"POST", "DELETE"}),
    (r"/tasks", {"POST"}),
    (r"/tasks/bulk", {"POST"}),
    (r"/tasks/" + _ID, {"GET", "PATCH", "DELETE"}),
    (r"/tasks/" + _ID + r"/comments", {"POST"}),
    (r"/tasks/" + _ID + r"/(?:reclaim|specify|decompose|reassign)", {"POST"}),
))
KANBAN_EVENTS = _KB + "/events"
KANBAN_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
KANBAN_SINCE_RE = re.compile(r"^[0-9]{1,18}$")

def kanban_allowed(path: str, method: str) -> bool:
    if ".." in path or "%" in path:
        return False
    return any(rx.match(path) and method in methods for rx, methods in KANBAN_ROUTES)

class Proof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invitation_id: str = Field(min_length=22, max_length=22, pattern=r"^[A-Za-z0-9_-]+$")
    secret: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")

class Redeem(Proof):
    device_name: str = Field(default="", max_length=64)
    device_model: str = Field(default="", max_length=64)

class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_credential: str = Field(max_length=200)

def fail(code: str, status: int | None = None):
    return JSONResponse({"error": code, "detail": "Fleet request refused."},
                        status_code=status or ERRORS.get(code, 400), headers=NO_STORE)

class Connector:
    def __init__(self, home: Path, cfg: dict, backend_token: str, *, store=None):
        self.home, self.cfg = home.resolve(), cfg
        self.directory = root(home)
        self.store = store or devices.FleetDeviceStore(self.directory / "devices.sqlite3")
        self.backend_token = backend_token
        self.backend = f"http://127.0.0.1:{cfg['backend_port']}"
        self.instance = devices.get_instance_id(self.directory)
        self.lock = threading.Lock()
        self.tickets: dict[str, tuple[float, str]] = {}
        self.rate = deque()
        self.active = False
        self.viewer_factory = PrivateViewer
        self.viewer = None
        self.display_thread = None
        self.display_check = display_capability
        self._display_busy = False
        self.last_display = "not_requested"

    def gateway(self):
        return {"instance_id": self.instance, "display_name": self.cfg.get("display_name", "Hermes Fleet"),
                "origin": self.cfg["origin"]}

    def transport(self, request) -> bool:
        # Only a loopback TLS terminator may attest HTTPS. Direct HTTP remains refused.
        return (request.client is not None and request.client.host in {"127.0.0.1", "::1", "localhost"}
                and request.headers.get("host", "").lower() == urlsplit(self.cfg["origin"]).netloc
                and (request.url.scheme in {"https", "wss"}
                     or request.headers.get("x-forwarded-proto") == "https"))

    def session(self, request):
        token = request.cookies.get("__Host-hermes_session_at", "")
        session = self.store.verify_session(token)
        if session is None or session.scopes != (devices.SCOPE_OPERATOR,):
            return None
        return session

    def rate_ok(self):
        # A bounded shared budget cannot be bypassed with a forged forwarding header.
        now = time.monotonic()
        with self.lock:
            while self.rate and self.rate[0] < now - 60:
                self.rate.popleft()
            if len(self.rate) >= 60:
                return False
            self.rate.append(now)
            return True

    def readiness(self, *, network=True, observations=True):
        blockers = []
        if not self.cfg.get("owners"):
            blockers.append("owner_binding_pending")
        try:
            with httpx.Client(timeout=4, trust_env=False) as client:
                # A protected route proves the plugin-owned backend token is accepted.
                response = client.get(self.backend + "/api/cron/jobs",
                    headers=self.backend_headers())
                if response.status_code != 200 or not isinstance(response.json(), (dict, list)):
                    blockers.append("stock_backend_auth_unverified")
        except Exception:
            blockers.append("stock_backend_unavailable")
        if network:
            try:
                import ssl, urllib.request
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None
                import truststore
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirect(),
                    urllib.request.HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
                with opener.open(self.cfg["origin"] + "/fleet/health", timeout=5) as response:
                    body = json.loads(response.read(4097))
                    if body != {"connector": VERSION, "instance_id": self.instance}:
                        blockers.append("https_wrong_service")
            except Exception:
                blockers.append("private_https_unverified")
        if not self.display_check():
            blockers.append("private_display_unavailable")
        if observations:
            try:
                approval = json.loads((self.directory / "approval.json").read_text())
                digest = hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest()
                if (approval["config_hash"] != digest or time.time() >= approval["expires_at"]
                        or approval["pid"] != os.getpid() or not approval["phone_tls_verified"]
                        or not approval["private_display_verified"]):
                    blockers.append("current_owner_observation_required")
            except Exception:
                blockers.append("current_owner_observation_required")
        return {"ready": not blockers, "blockers": blockers, "pid": os.getpid(),
                "last_display": self.last_display}

    def backend_headers(self):
        if self.cfg.get("backend_mode") == "existing_gated":
            return {"Authorization": "Bearer " + self.backend_token}
        return {"X-Hermes-Session-Token": self.backend_token}

    def claim(self, proof: dict):
        path = self.directory / "receipts.sqlite3"
        # Files inherit process umask 077. Records survive plugin reinstall and removal.
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS receipts (bot INTEGER, owner INTEGER, update_id INTEGER, PRIMARY KEY(bot,owner,update_id))")
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT count(*) FROM receipts").fetchone()[0] >= 10000:
                return False
            try:
                conn.execute("INSERT INTO receipts VALUES (?,?,?)", (proof["bot_id"], proof["owner"], proof["update_id"]))
            except sqlite3.IntegrityError:
                return False
            return True

    def record_observations(self):
        from .settings import private_json
        private_json(self.directory / "approval.json", {
            "pid": os.getpid(), "expires_at": time.time() + 600,
            "config_hash": hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest(),
            "phone_tls_verified": True, "private_display_verified": True})

    def observe_from_viewer(self, expected_viewer=None):
        # Authenticated local page + explicit checkboxes supplement, never replace,
        # the original native owner request and current host transport checks.
        if not self.readiness(observations=False)["ready"]:
            return False
        viewer = expected_viewer if expected_viewer is not None else self.viewer
        if viewer is None or viewer is not self.viewer:
            return False
        with viewer.lock:
            viewer._tick()
            if not viewer.loaded or viewer.phase != "preflight":
                return False
            self.record_observations()
            return True

    def invitation_state(self, invitation_id):
        with self.store._read() as conn:
            row = conn.execute("SELECT state FROM invitations WHERE id=?", (invitation_id,)).fetchone()
        return row["state"] if row else "missing"

    def shutdown_display(self):
        viewer = self.viewer
        if viewer:
            viewer.finish("interrupted")
        if self.display_thread and self.display_thread is not threading.current_thread():
            self.display_thread.join(timeout=15)

    def owner(self, proof):
        action = proof.get("action")
        if action in {"status", "approve", "revoke"} and proof.get("local_operator") is True:
            # Same-UID, owner-only Unix socket is the legitimate local owner boundary.
            if action == "approve":
                if proof.get("phone_tls_verified") is not True or proof.get("private_display_verified") is not True:
                    return {"message": "Observations must be explicit.", "ok": False}
                self.record_observations()
                return {"ok": True, "message": "Owner observations recorded for this process for ten minutes."}
            if action == "revoke":
                return {"ok": self.store.revoke_device(proof.get("device_id")), "message": "Requested device revocation completed if the device existed."}
        elif (type(proof.get("owner")) is not int or proof["owner"] not in self.cfg["owners"]
              or type(proof.get("bot_id")) is not int or proof["bot_id"] != self.cfg["bot_id"]
              or type(proof.get("update_id")) is not int or proof["update_id"] < 0):
            return {"ok": False, "message": "Owner authorization refused."}
        if action == "status":
            status = self.readiness()
            message = "Fleet ready for /fleetqr."
            if not status["ready"]:
                if status["blockers"] == ["current_owner_observation_required"]:
                    message = "Send /fleetqr to open the private Fleet page and refresh the phone/screen checks there. No invitation is created before those checks."
                else:
                    message = "Fleet blocked: " + ", ".join(status["blockers"])
            return {**status, "message": message}
        if action == "devices":
            listed = self.store.list_devices()
            return {"ok": True, "message": "Active Fleet device IDs: " + (", ".join(d.id for d in listed) or "none")}
        if action != "issue" or proof.get("explicit_issue") is not True or proof.get("local_operator"):
            return {"ok": False, "message": "Explicit owner /fleetqr required."}
        status = self.readiness(observations=False)
        if not status["ready"]:
            return {"ok": False, "message": "Fleet blocked: " + ", ".join(status["blockers"])}
        with self.lock:
            if self._display_busy:
                return {"ok": False, "message": "A private QR display is already active."}
            if not self.claim(proof):
                return {"ok": False, "message": "This Telegram request was already handled or the receipt store is full."}
            self._display_busy = True
        def display():
            issued = None
            viewer = None
            try:
                viewer = self.viewer_factory(origin=self.cfg["origin"],
                    observe=lambda: self.observe_from_viewer(viewer))
                self.viewer = viewer
                viewer.start()
                self.last_display = "waiting_for_page_checks"
                if not viewer.wait_ready():
                    self.last_display = viewer.phase if viewer.phase in TERMINAL else "display_not_confirmed"
                    return
                if not self.readiness()["ready"]:
                    viewer.finish("display_failed")
                    self.last_display = "readiness_changed"
                    return
                issued = self.store.create_invitation(origin=self.cfg["origin"], created_by=f"telegram:{proof['owner']}", ttl_seconds=120)
                link = f"{self.cfg['origin']}/pair#v=1&i={issued.invitation.id}&s={issued.secret}"
                viewer.show_invitation(link, issued.invitation.id, issued.invitation.expires_at)
                self.last_display = "waiting_for_qr_paint"
                paint_deadline = time.monotonic() + 10
                while viewer.phase not in TERMINAL:
                    with viewer.lock:
                        viewer._tick()
                    if self.invitation_state(issued.invitation.id) == "redeemed":
                        viewer.finish("redeemed")
                    elif viewer.qr_painted:
                        self.last_display = "qr_painted_visibility_requires_owner"
                    elif time.monotonic() >= paint_deadline:
                        viewer.finish("display_failed")
                    time.sleep(.1)
                self.last_display = viewer.phase
            except Exception as exc:
                self.last_display = exc.stage if isinstance(exc, DisplayFailure) else "display_failed"
                if viewer:
                    viewer.finish("display_failed")
            finally:
                if issued:
                    try:
                        cancelled = self.store.cancel_invitation(issued.invitation.id)
                        if not cancelled and self.invitation_state(issued.invitation.id) != "redeemed":
                            self.last_display = "cancellation_unconfirmed"
                        elif not cancelled:
                            self.last_display = "redeemed"
                    except Exception:
                        self.last_display = "cancellation_unconfirmed"
                if viewer:
                    # Let the page retrieve terminal state, then destroy in-memory grants/images.
                    time.sleep(.8)
                    viewer.close()
                self.viewer = None
                with self.lock:
                    self._display_busy = False
        self.last_display = "opening_private_page"
        self.display_thread = threading.Thread(target=display, daemon=True)
        self.display_thread.start()
        return {"ok": True, "message": "Opening the private Fleet page on your MacBook. Confirm the phone/screen checks there; your two-minute QR appears only after the page works and those checks pass. No invitation or bearer link is sent here."}

def make_app(connector: Connector):
    c = connector
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if request.url.path == "/fleet/health":
            # Non-secret service identity; HTTPS probe uses this to avoid a wrong forwarding target.
            return JSONResponse({"connector": VERSION, "instance_id": c.instance}, headers=NO_STORE)
        if not c.transport(request):
            return fail("https_or_origin_required", 426)
        if request.headers.get("origin") not in (None, c.cfg["origin"]):
            return fail("cross_origin", 403)
        if not c.rate_ok():
            return fail("rate_limited", 429)
        # Bounded streaming read: do not trust Content-Length; do not reflect validation payloads.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return fail("body_too_large", 413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers.update(NO_STORE)
        return response

    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def validation(request, error):
        return fail("invalid_request", 422)

    @app.post("/api/fleet/pairing/preview")
    async def preview(body: Proof):
        try:
            i = c.store.preview(invitation_id=body.invitation_id, secret=body.secret, origin=c.cfg["origin"])
            return {"gateway": c.gateway(), "invitation": {"id": i.id, "label": i.label,
                    "scopes": list(i.scopes), "expires_at": i.expires_at, "server_time": int(time.time())},
                    "access": [{"scope": devices.SCOPE_OPERATOR, "summary": SUMMARY}]}
        except devices.PairingError as e:
            return fail(e.code)

    @app.post("/api/fleet/pairing/redeem", status_code=201)
    async def redeem(body: Redeem):
        try:
            d = c.store.redeem(**body.model_dump(), origin=c.cfg["origin"])
            return {"device": {"id": d.device.id, "label": d.device.label}, "credential": d.credential,
                    "gateway": c.gateway(), "scopes": list(d.device.scopes)}
        except devices.PairingError as e:
            return fail(e.code)

    @app.post("/auth/device-login")
    async def login(body: Credential):
        try:
            s = c.store.open_session(body.device_credential)
        except devices.PairingError as e:
            return fail(e.code)
        response = JSONResponse({"ok": True, "device_id": s.device_id,
            "expires_at": datetime.fromtimestamp(s.expires_at, timezone.utc).isoformat()}, headers=NO_STORE)
        response.set_cookie("__Host-hermes_session_at", s.access_token, max_age=3600, path="/",
                            secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/auth/device-revoke")
    async def self_revoke(body: Credential):
        if not c.store.revoke_with_credential(body.device_credential):
            return fail("invalid_credential")
        return {"ok": True}

    @app.post("/api/auth/ws-ticket")
    async def ticket(request: Request):
        s = c.session(request)
        if s is None:
            return fail("unauthenticated", 401)
        token = secrets.token_urlsafe(32)
        with c.lock:
            c.tickets = {k: v for k, v in c.tickets.items() if v[0] > time.monotonic()}
            if len(c.tickets) >= 1024:
                return fail("too_many_tickets", 429)
            c.tickets[token] = (time.monotonic() + 30, s.access_token)
        return {"ticket": token, "ttl_seconds": 30}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(path: str, request: Request):
        full = "/api/" + path
        if c.session(request) is None:
            return fail("unauthenticated", 401)
        if (full not in HTTP_PATHS and not (full.startswith("/api/cron/jobs/") and ".." not in full and "%" not in full)
                and not kanban_allowed(full, request.method)):
            return fail("out_of_scope", 403)
        # Caller cookies, Authorization, Host, redirects and Set-Cookie never pass through.
        headers = {**c.backend_headers(),
                   "Content-Type": request.headers.get("content-type", "application/json")}
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
                response = await client.request(request.method, c.backend + full,
                    params=request.query_params, content=await request.body(), headers=headers)
                if 300 <= response.status_code < 400 or len(response.content) > 8 * 1024 * 1024:
                    return fail("backend_response_refused", 502)
                return Response(response.content, status_code=response.status_code,
                                media_type=response.headers.get("content-type"), headers=NO_STORE)
        except httpx.HTTPError:
            return fail("backend_unavailable", 503)

    async def bridge(ws: WebSocket, upstream_url: str, *, validate=None):
        if not c.transport(ws) or ws.headers.get("origin") not in (None, c.cfg["origin"]):
            await ws.close(code=4403)
            return
        with c.lock:
            entry = c.tickets.pop(ws.query_params.get("ticket", ""), None)
        if entry is None or entry[0] <= time.monotonic() or c.store.verify_session(entry[1]) is None:
            await ws.close(code=4401)
            return
        if validate is not None:
            upstream_url = validate(ws, upstream_url)
            if upstream_url is None:
                await ws.close(code=4400)
                return
        from websockets.asyncio.client import connect
        try:
            async with connect(upstream_url, max_size=8 * 1024 * 1024, proxy=None) as upstream:
                await ws.accept()
                async def send_upstream():
                    while True:
                        data = await ws.receive_text()
                        # Check device revocation before EVERY RPC frame.
                        if len(data) > 8 * 1024 * 1024 or c.store.verify_session(entry[1]) is None:
                            await ws.close(code=4401)
                            return
                        await upstream.send(data)
                async def send_phone():
                    async for frame in upstream:
                        if c.store.verify_session(entry[1]) is None:
                            await ws.close(code=4401)
                            return
                        if isinstance(frame, str):
                            await ws.send_text(frame)
                async def watch_revocation():
                    while True:
                        await asyncio.sleep(0.5)
                        if c.store.verify_session(entry[1]) is None:
                            await ws.close(code=4401)
                            return
                tasks = [asyncio.create_task(f()) for f in (send_upstream, send_phone, watch_revocation)]
                try:
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            try:
                await ws.close(code=1011)
            except RuntimeError:
                pass

    @app.websocket("/api/ws")
    async def websocket(ws: WebSocket):
        await bridge(ws, c.backend.replace("http:", "ws:") + "/api/ws?token=" + c.backend_token)

    @app.websocket(KANBAN_EVENTS)
    async def kanban_events(ws: WebSocket):
        def validate(ws, base):
            # Only validated since/board are forwarded; the caller's ticket and anything else are dropped.
            q = ws.query_params
            extra = {}
            if "since" in q:
                if not KANBAN_SINCE_RE.match(q["since"]):
                    return None
                extra["since"] = q["since"]
            if "board" in q:
                if not KANBAN_BOARD_RE.match(q["board"]):
                    return None
                extra["board"] = q["board"]
            return base + ("&" + urlencode(extra) if extra else "")
        await bridge(ws, c.backend.replace("http:", "ws:") + KANBAN_EVENTS + "?token=" + c.backend_token,
                     validate=validate)
    return app

def start_control(c: Connector):
    path = socket_path(c.home)
    directory = path.parent
    if directory.is_symlink():
        raise ValueError("Symlinked control directory refused")
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("Unsafe control directory")
    # Never unlink another live service's socket.
    if path.exists():
        import socket
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise ValueError("Connector already running")
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(8)
            try:
                raw = self.rfile.readline(8193)
                if len(raw) > 8192:
                    return
                result = c.owner(json.loads(raw))
            except Exception:
                result = {"ok": False, "message": "Fleet owner operation failed."}
            self.wfile.write(json.dumps(result).encode() + b"\n")
    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    os.chmod(path, 0o600)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, path
