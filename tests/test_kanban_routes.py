"""Kanban allowlist and event-stream proxy: exact routes/methods, session gate, ticket semantics."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from fleet_connector.service import Connector, make_app, kanban_allowed
from fleet_connector.settings import private_json, root

ORIGIN = "https://fixture.example.test"
HOST = {"host": "fixture.example.test", "x-forwarded-proto": "https"}
SEEN = []

class Backend(BaseHTTPRequestHandler):
    def handle_any(self):
        n = int(self.headers.get("content-length") or 0)
        SEEN.append((self.command, self.path, dict(self.headers), self.rfile.read(n)))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "leak=1")
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any
    def log_message(self, *a):
        pass

@pytest.fixture
def stack(tmp_path):
    SEEN.clear()
    http = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    cfg = dict(home=str(tmp_path), origin=ORIGIN, owners=[123], bot_id=456,
               port=9129, backend_port=http.server_address[1], display_name="Fixture")
    private_json(root(tmp_path) / "settings.json", cfg)
    c = Connector(tmp_path, cfg, "backend-fixture-only")
    client = TestClient(make_app(c), base_url=ORIGIN, client=("127.0.0.1", 33333))
    yield c, client
    http.shutdown()

def pair(c, client):
    i = c.store.create_invitation(origin=ORIGIN, created_by="fixture", ttl_seconds=60)
    data = client.post("/api/fleet/pairing/redeem", json=dict(invitation_id=i.invitation.id, secret=i.secret)).json()
    assert client.post("/auth/device-login", json={"device_credential": data["credential"]}).status_code == 200
    return data

K = "/api/plugins/kanban"
ALLOWED = [("GET", "/board"), ("GET", "/boards"), ("GET", "/assignees"), ("GET", "/orchestration"),
           ("PUT", "/orchestration"), ("POST", "/dispatch"), ("POST", "/links"), ("DELETE", "/links"),
           ("POST", "/tasks"), ("POST", "/tasks/bulk"), ("GET", "/tasks/t_624b81cd"),
           ("PATCH", "/tasks/t_624b81cd"), ("DELETE", "/tasks/t_624b81cd"),
           ("POST", "/tasks/t_624b81cd/comments"), ("POST", "/tasks/t_624b81cd/reclaim"),
           ("POST", "/tasks/t_624b81cd/specify"), ("POST", "/tasks/t_624b81cd/decompose"),
           ("POST", "/tasks/t_624b81cd/reassign")]

@pytest.mark.parametrize("method,path", ALLOWED)
def test_allowed_kanban_routes_proxy_with_backend_credentials_only(stack, method, path):
    c, client = stack
    pair(c, client)
    r = client.request(method, K + path + "?board=main", content=b"{}",
                       headers={"Authorization": "Bearer caller-secret", "X-Hermes-Session-Token": "caller-token",
                                "Cookie": "extra=caller-cookie; __Host-hermes_session_at=" + client.cookies["__Host-hermes_session_at"]})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "set-cookie" not in r.headers and r.headers["cache-control"] == "no-store"
    m, p, h, _ = SEEN[-1]
    h = {k.lower(): v for k, v in h.items()}
    assert (m, p) == (method, K + path + "?board=main")
    assert h["x-hermes-session-token"] == "backend-fixture-only"
    assert "authorization" not in h and "cookie" not in h

def test_existing_gated_backend_gets_bearer(stack):
    c, client = stack
    c.cfg["backend_mode"] = "existing_gated"
    pair(c, client)
    assert client.get(K + "/board").status_code == 200
    h = {k.lower(): v for k, v in SEEN[-1][2].items()}
    assert h["authorization"] == "Bearer backend-fixture-only" and "x-hermes-session-token" not in h

@pytest.mark.parametrize("method,path", [
    ("POST", "/board"), ("DELETE", "/board"), ("PUT", "/tasks"), ("GET", "/tasks"), ("PUT", "/tasks/t_1"),
    ("GET", "/tasks/t_1/comments"), ("GET", "/links"), ("GET", "/dispatch"), ("PATCH", "/orchestration"),
    ("POST", "/boards"), ("PATCH", "/boards/main"), ("DELETE", "/boards/main"), ("POST", "/boards/main/switch"),
    ("GET", "/config"), ("GET", "/profiles"), ("PATCH", "/profiles/x"), ("GET", "/stats"), ("GET", "/diagnostics"),
    ("GET", "/attachments/1"), ("GET", "/tasks/t_1/attachments"), ("POST", "/runs/1/terminate"),
    ("GET", "/tasks/t_1/log"), ("GET", "/events"), ("POST", "/estimate"),
    ("GET", "/tasks/../config"), ("GET", "/tasks/%2e%2e/config"), ("GET", "/tasks/a/b/comments"),
    ("GET", "/board/"), ("GET", "/tasks/t_1%2fcomments")])
def test_other_kanban_routes_and_methods_refused(stack, method, path):
    c, client = stack
    pair(c, client)
    r = client.request(method, K + path, content=b"{}")
    assert r.status_code == 403 and r.json()["error"] == "out_of_scope"
    assert SEEN == []

@pytest.mark.parametrize("path", ["/api/plugins/other/board", "/api/plugins/kanban", "/api/plugins/kanbanx/board",
                                  "/api/plugins/", "/api/config", "/api/plugins/kanban/../x/board"])
def test_non_kanban_plugin_paths_still_refused(stack, path):
    c, client = stack
    pair(c, client)
    r = client.get(path)
    assert r.status_code == 403 and r.json()["error"] == "out_of_scope" and SEEN == []

def test_previously_allowed_paths_unchanged(stack):
    c, client = stack
    pair(c, client)
    assert client.get("/api/cron/jobs").status_code == 200
    assert client.post("/api/cron/jobs/abc/pause").status_code == 200
    assert client.get("/api/cron/jobs/../x").status_code == 403

def test_unauthenticated_kanban_is_401_without_backend_contact(stack):
    _, client = stack
    for method, path in ALLOWED[:4]:
        assert client.request(method, K + path).status_code == 401
    assert client.get(K + "/config").status_code == 401
    assert SEEN == []

def test_kanban_allowed_unit():
    assert kanban_allowed(K + "/board", "GET") and not kanban_allowed(K + "/board", "POST")
    assert not kanban_allowed(K + "/tasks/..", "GET") and not kanban_allowed(K + "/tasks/a%2fb", "GET")

# ---- event stream -----------------------------------------------------------------------------

@pytest.fixture
def ws_backend():
    from websockets.asyncio.server import serve
    got, ready = [], threading.Event()
    box = {}
    async def handler(conn):
        got.append(conn.request.path)
        await conn.send(json.dumps({"events": [{"id": 5}]}))
        await asyncio.sleep(0.2)
    def run():
        loop = asyncio.new_event_loop()
        async def main():
            async with serve(handler, "127.0.0.1", 0) as srv:
                box["port"] = srv.sockets[0].getsockname()[1]
                box["stop"] = asyncio.Event()
                box["loop"] = loop
                ready.set()
                await box["stop"].wait()
        loop.run_until_complete(main())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    yield box["port"], got
    box["loop"].call_soon_threadsafe(box["stop"].set)

def ws_stack(tmp_path, port):
    cfg = dict(home=str(tmp_path), origin=ORIGIN, owners=[123], bot_id=456,
               port=9129, backend_port=port, display_name="Fixture")
    private_json(root(tmp_path) / "settings.json", cfg)
    c = Connector(tmp_path, cfg, "backend-fixture-only")
    return c, TestClient(make_app(c), base_url=ORIGIN, client=("127.0.0.1", 33333))

def ticket(client):
    return client.post("/api/auth/ws-ticket").json()["ticket"]

def test_kanban_events_ticket_forwarding_and_single_use(tmp_path, ws_backend):
    port, got = ws_backend
    c, client = ws_stack(tmp_path, port)
    pair(c, client)
    t = ticket(client)
    with client.websocket_connect(f"/api/plugins/kanban/events?since=7&board=ops_1&ticket={t}&evil=1", headers=HOST) as ws:
        assert json.loads(ws.receive_text()) == {"events": [{"id": 5}]}
    assert got == ["/api/plugins/kanban/events?token=backend-fixture-only&since=7&board=ops_1"]
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/plugins/kanban/events?since=7&ticket={t}", headers=HOST) as ws:
            ws.receive_text()
    assert e.value.code == 4401 and len(got) == 1

@pytest.mark.parametrize("query,code", [("", 4401), ("ticket=bogus", 4401), ("token=backend-fixture-only", 4401)])
def test_kanban_events_without_valid_ticket_refused(tmp_path, ws_backend, query, code):
    port, got = ws_backend
    c, client = ws_stack(tmp_path, port)
    pair(c, client)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/plugins/kanban/events?" + query, headers=HOST) as ws:
            ws.receive_text()
    assert e.value.code == code and got == []

def test_kanban_events_unpaired_cannot_mint_ticket_and_revoked_ticket_refused(tmp_path, ws_backend):
    port, got = ws_backend
    c, client = ws_stack(tmp_path, port)
    assert client.post("/api/auth/ws-ticket").status_code == 401
    data = pair(c, client)
    t = ticket(client)
    assert client.post("/auth/device-revoke", json={"device_credential": data["credential"]}).status_code == 200
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/plugins/kanban/events?ticket={t}", headers=HOST) as ws:
            ws.receive_text()
    assert e.value.code == 4401 and got == []

@pytest.mark.parametrize("extra", ["since=abc", "since=-1", "since=1&since=2x", "board=Bad/Slug", "board=..", "board=-x", "since="])
def test_kanban_events_invalid_params_refused(tmp_path, ws_backend, extra):
    port, got = ws_backend
    c, client = ws_stack(tmp_path, port)
    pair(c, client)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/plugins/kanban/events?{extra}&ticket={ticket(client)}", headers=HOST) as ws:
            ws.receive_text()
    assert e.value.code == 4400 and got == []

def test_kanban_events_requires_https_transport_and_origin(tmp_path, ws_backend):
    port, got = ws_backend
    c, client = ws_stack(tmp_path, port)
    pair(c, client)
    for headers in ({"host": "fixture.example.test"}, {**HOST, "origin": "https://evil.example"}):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(f"/api/plugins/kanban/events?ticket={ticket(client)}", headers=headers) as ws:
                ws.receive_text()
        assert e.value.code == 4403
    assert got == []

def test_chat_ws_still_proxies_through_shared_bridge(tmp_path, ws_backend):
    port, got = ws_backend
    c, client = ws_stack(tmp_path, port)
    pair(c, client)
    with client.websocket_connect(f"/api/ws?ticket={ticket(client)}", headers=HOST) as ws:
        ws.receive_text()
    assert got == ["/api/ws?token=backend-fixture-only"]
