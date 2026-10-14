"""Ephemeral authenticated loopback UI, separate from the connector/tailnet API.

Only a one-use grant appears in the private bootstrap file. QR images stay in
memory; page authorization uses a header token, never cookies, URLs or storage.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs
from .display import DisplayFailure, encode_png, open_browser, validate_link
from .viewer_page import PAGE, SCRIPT, STYLE

PREFLIGHT = "HERMES FLEET PRIVATE DISPLAY CHECK - NOT A PAIRING INVITATION"
TERMINAL = {"expired", "cancelled", "redeemed", "display_failed", "disconnected", "interrupted"}

class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            self.shutdown_request(request)
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
    def handle_error(self, *args):
        pass

class PrivateViewer:
    def __init__(self, *, origin, observe, opener=open_browser, encoder=encode_png,
                 clock=time.monotonic, wall=time.time, setup_seconds=180, heartbeat_seconds=6):
        self.origin, self.observe = origin, observe
        self.opener, self.encoder, self.clock, self.wall = opener, encoder, clock, wall
        self.setup_seconds, self.heartbeat_seconds = setup_seconds, heartbeat_seconds
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.bootstrap = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.phase = "preflight"
        self.loaded = self.observed = self.qr_painted = False
        self.observing = False
        self.last_seen = None
        self.deadline = self.clock() + setup_seconds
        self.failure_stage = None
        self.metrics = {}
        # Local, value-free counters for owner-run smoke diagnostics. Do not
        # record Host, Origin, token, grant, path, or request-body values.
        self._diagnostic_counts = {
            "bootstrap_post_seen": 0,
            "bootstrap_post_accepted": 0,
            "session_requests_accepted": 0,
            "frame_requests_accepted": 0,
            "close_requests_accepted": 0,
            "paint_event_posts": 0,
            "paint_events_accepted": 0,
        }
        self._diagnostic_rejections: dict[str, int] = {}
        self.image = None
        self.server = self.temp = self.bootstrap_path = None
        self._closed = False

    def start(self):
        stage = "qr_encoding_failed"
        try:
            self.image = self.encoder(PREFLIGHT)
            owner = self
            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.0"
                def setup(self):
                    super().setup()
                    self.connection.settimeout(3)
                def log_message(self, *args):
                    pass
                def send_error(self, code, message=None, explain=None):
                    owner.reply(self, code, {"error": "refused"})
                def do_GET(self):
                    owner.request(self, "GET")
                def do_POST(self):
                    owner.request(self, "POST")
                def do_OPTIONS(self):
                    owner.reply(self, 403, {"error": "refused"})
                def do_HEAD(self):
                    owner.reply(self, 403, {"error": "refused"})
            stage = "viewer_socket_failed"
            self.server = BoundedServer(("127.0.0.1", 0), Handler)
            self.address = f"127.0.0.1:{self.server.server_port}"
            self.local_origin = "http://" + self.address
            stage = "private_bootstrap_failed"
            # Use the platform's private temporary root; TemporaryDirectory
            # creates an owner-only directory and this code also chmods it.
            self.temp = tempfile.TemporaryDirectory(prefix="fleet-private-viewer-")
            directory = Path(self.temp.name)
            directory.chmod(0o700)
            self.bootstrap_path = directory / "open-fleet.html"
            script = "document.getElementById('start').submit();"
            sha = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            html = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                f"script-src 'sha256-{sha}'; form-action {self.local_origin}; base-uri 'none'\">"
                f'<title>Hermes Fleet</title><form id="start" method="post" action="{self.local_origin}/bootstrap">'
                f'<input type="hidden" name="grant" value="{self.bootstrap}">'
                '<button>Open private Fleet page</button></form><script>' + script + '</script>')
            fd = os.open(self.bootstrap_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(html)
            stage = "viewer_thread_failed"
            self.server_thread = threading.Thread(target=self.server.serve_forever,
                                                  kwargs={"poll_interval": .1}, daemon=True)
            self.server_thread.start()
            stage = "browser_launch_failed"
            self.opener(self.bootstrap_path)
            return self
        except Exception as exc:
            self.failure_stage = exc.stage if isinstance(exc, DisplayFailure) else stage
            self.finish("display_failed")
            self.close()
            error = DisplayFailure(self.failure_stage)
            error.cause_type = type(exc).__name__
            for key in ("native_exit", "native_code", "os_code"):
                setattr(error, key, getattr(exc, key, None))
            raise error from None

    def headers(self):
        script_sha = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        style_sha = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
        return {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; connect-src 'self'; img-src data:; "
                f"script-src 'sha256-{script_sha}'; style-src 'sha256-{style_sha}'; "
                "frame-ancestors 'none'; form-action 'none'; base-uri 'none'"}

    def reply(self, handler, code, value, *, html=False):
        data = value.encode() if html else json.dumps(value).encode()
        try:
            handler.send_response(code)
            for key, val in self.headers().items():
                handler.send_header(key, val)
            handler.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except (OSError, TimeoutError):
            pass

    def _valid_token(self, value):
        return isinstance(value, str) and len(value) == len(self.session) and hmac.compare_digest(value, self.session)

    def _diagnostic_increment(self, counter: str) -> None:
        with self.lock:
            self._diagnostic_counts[counter] += 1

    def _diagnostic_reject(self, category: str) -> None:
        with self.lock:
            self._diagnostic_rejections[category] = self._diagnostic_rejections.get(category, 0) + 1

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return local, sanitized handshake state for an owner smoke tool."""
        with self.lock:
            return {
                "phase": self.phase,
                "bootstrap_state": "consumed" if not self.bootstrap else "pending",
                "preflight_paint_ack": self.loaded,
                "qr_paint_ack": self.qr_painted,
                "counts": dict(self._diagnostic_counts),
                "rejections": dict(self._diagnostic_rejections),
            }

    def request(self, handler, method):
        if handler.client_address[0] != "127.0.0.1":
            self._diagnostic_reject("peer_rejected")
            return self.reply(handler, 403, {"error": "refused"})
        if handler.headers.get("Host") != self.address:
            self._diagnostic_reject("host_rejected")
            return self.reply(handler, 403, {"error": "refused"})
        if "?" in handler.path or "#" in handler.path:
            self._diagnostic_reject("request_target_rejected")
            return self.reply(handler, 403, {"error": "refused"})
        if handler.headers.get("X-Forwarded-For") or handler.headers.get("X-Forwarded-Proto"):
            self._diagnostic_reject("forwarded_header_rejected")
            return self.reply(handler, 403, {"error": "refused"})
        with self.lock:
            self._tick()
            if self._closed:
                return self.reply(handler, 410, {"error": "closed"})
        raw, body = b"", {}
        if method == "POST":
            if handler.path == "/bootstrap":
                self._diagnostic_increment("bootstrap_post_seen")
            try:
                size = int(handler.headers.get("Content-Length", "-1"))
                if size < 0 or size > 4096 or handler.headers.get("Transfer-Encoding"):
                    raise ValueError()
                raw = handler.rfile.read(size)
                if len(raw) != size:
                    raise ValueError()
                if handler.path != "/bootstrap":
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError()
            except Exception:
                if handler.path == "/bootstrap":
                    self._diagnostic_reject("bootstrap_body_rejected")
                return self.reply(handler, 400, {"error": "refused"})
        req_origin = handler.headers.get("Origin")
        if handler.path == "/bootstrap" and method == "POST":
            if req_origin not in (None, "null"):
                self._diagnostic_reject("bootstrap_origin_rejected")
                return self.reply(handler, 403, {"error": "refused"})
            if handler.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                self._diagnostic_reject("bootstrap_content_type_rejected")
                return self.reply(handler, 403, {"error": "refused"})
            try:
                fields = parse_qs(raw.decode("ascii"), strict_parsing=True)
                grant = fields.get("grant", [None])
            except Exception:
                fields, grant = {}, [None]
                self._diagnostic_reject("bootstrap_form_rejected")
            with self.lock:
                if (set(fields) != {"grant"} or len(grant) != 1 or not self.bootstrap
                        or not isinstance(grant[0], str) or not hmac.compare_digest(grant[0], self.bootstrap)
                        or self.phase in TERMINAL):
                    self._diagnostic_reject("bootstrap_grant_rejected")
                    return self.reply(handler, 403, {"error": "refused"})
                self.bootstrap = ""
                self.bootstrap_path.unlink(missing_ok=True)
                self._diagnostic_increment("bootstrap_post_accepted")
            config = json.dumps({"session": self.session, "origin": self.origin}).replace("<", "\\u003c")
            logo_path = Path(__file__).parent / "assets" / "fleet-wing.png"
            logo = "data:image/png;base64," + base64.b64encode(logo_path.read_bytes()).decode()
            html = PAGE.replace("@@CONFIG@@", config).replace("@@SCRIPT@@", SCRIPT).replace("@@STYLE@@", STYLE).replace("@@LOGO@@", logo)
            return self.reply(handler, 200, html, html=True)
        token = handler.headers.get("Authorization", "")
        token = token[7:] if token.startswith("Viewer ") else None
        if handler.path == "/close" and method == "POST" and token is None:
            token = body.get("session")
        if not self._valid_token(token):
            self._diagnostic_reject("session_auth_rejected")
            return self.reply(handler, 403, {"error": "refused"})
        if (req_origin not in (None, self.local_origin) if method == "GET"
                else req_origin != self.local_origin):
            self._diagnostic_reject("session_origin_rejected")
            return self.reply(handler, 403, {"error": "refused"})
        self._diagnostic_increment("session_requests_accepted")
        if handler.path == "/frame" and method == "GET":
            self._diagnostic_increment("frame_requests_accepted")
        elif handler.path == "/close" and method == "POST":
            self._diagnostic_increment("close_requests_accepted")
        elif handler.path == "/event" and method == "POST":
            self._diagnostic_increment("paint_event_posts")
        if handler.path == "/observe" and method == "POST":
            with self.lock:
                self._tick()
                if (not self.loaded or self.phase != "preflight" or self.observing
                        or body != {"phone": True, "private": True}):
                    return self.reply(handler, 409, {"error": "observations_required"})
                self.observing = True
                self.last_seen = self.clock()
            # TLS/backend verification can take seconds. Keep frame/close requests responsive.
            try:
                observed = self.observe() is True
            except Exception:
                observed = False
            with self.lock:
                self.observing = False
                self._tick()
                if self.phase in TERMINAL:
                    return self.reply(handler, 409, {"error": "closed"})
                self.observed = observed
                if not observed:
                    self.finish("display_failed")
                    return self.reply(handler, 503, {"error": "observation_failed"})
                self.changed.notify_all()
                return self.reply(handler, 200, {"ok": True})
        with self.lock:
            self._tick()
            if self.phase not in TERMINAL:
                self.last_seen = self.clock()
            if handler.path == "/frame" and method == "GET":
                return self.reply(handler, 200, self.frame())
            if handler.path == "/event" and method == "POST":
                if self.phase in TERMINAL:
                    return self.reply(handler, 200, {"ok": False, "phase": self.phase})
                if (body.get("phase") != self.phase or body.get("visible") is not True
                        or type(body.get("width")) is not int or body["width"] < 64
                        or type(body.get("height")) is not int or body["height"] < 64):
                    self._diagnostic_reject("paint_event_rejected")
                    return self.reply(handler, 400, {"error": "not_painted"})
                if self.phase == "preflight":
                    self.loaded = True
                elif self.phase == "qr":
                    self.qr_painted = True
                else:
                    return self.reply(handler, 409, {"error": "closed"})
                self._diagnostic_increment("paint_events_accepted")
                self.metrics = {"phase": self.phase, "visible": True,
                                "width": body["width"], "height": body["height"],
                                "browser_family": "Chromium" if "Chrome/" in handler.headers.get("User-Agent", "") else
                                    "Safari" if "Safari/" in handler.headers.get("User-Agent", "") else "other"}
                self.changed.notify_all()
                return self.reply(handler, 200, {"ok": True})
            if handler.path == "/close" and method == "POST":
                self.finish("cancelled")
                return self.reply(handler, 200, {"ok": True})
        return self.reply(handler, 404, {"error": "refused"})

    def frame(self):
        return {"phase": self.phase, "png": base64.b64encode(self.image).decode() if self.image else None,
                "remaining": max(0, int(self.deadline - self.clock())), "observed": self.observed}

    def _tick(self):
        if self.phase in TERMINAL:
            return
        if self.clock() >= self.deadline:
            self.finish("expired")
        elif self.last_seen is not None and self.clock() - self.last_seen > self.heartbeat_seconds:
            self.finish("disconnected")

    def wait_ready(self, *, timeout=180):
        end = self.clock() + timeout
        with self.changed:
            while self.phase not in TERMINAL and not (self.loaded and self.observed):
                self._tick()
                if self.clock() >= end:
                    return False
                self.changed.wait(.1)
            return self.phase not in TERMINAL and self.loaded and self.observed

    def show_invitation(self, link, invitation_id, expires_at):
        validate_link(link, invitation_id)
        image = self.encoder(link)
        with self.lock:
            self._tick()
            if not self.loaded or not self.observed or self.phase in TERMINAL:
                raise DisplayFailure("viewer_unavailable")
            self.deadline = self.clock() + max(0, expires_at - self.wall())
            self.image = image
            self.phase = "qr"
            self.changed.notify_all()

    def finish(self, phase):
        with self.lock:
            if self.phase not in TERMINAL:
                self.phase = phase
                self.image = None
                self.bootstrap = ""
                self.changed.notify_all()

    def close(self):
        with self.lock:
            if self._closed:
                return
            self.finish("interrupted")
            self._closed = True
            self.image = None
            self.bootstrap = self.session = ""
        if self.server:
            if hasattr(self, "server_thread"):
                self.server.shutdown()
                self.server_thread.join(timeout=3)
            self.server.server_close()
        if self.temp:
            self.temp.cleanup()
