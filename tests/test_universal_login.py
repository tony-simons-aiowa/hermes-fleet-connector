"""Universal login: HTTP endpoints, /pair page, control actions, owners wiring, pin computation.
Synthetic data only."""
import base64
import datetime
import hashlib
import re
import socket
import ssl
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from fleet_connector import VERSION
from fleet_connector import devices as fd
from fleet_connector.bridge import control
from fleet_connector.pin import PinCache, PinUnavailable, fetch_pin
from fleet_connector.service import Connector, make_app, start_control
from fleet_connector.settings import private_json, root

ORIGIN = "https://fleet.example.test"
PIN = "P" * 43
CLIENT = "c" * 43
OTHER = "d" * 43
LOCAL = {"platform": "local", "user_id": "host"}
TG = {"platform": "telegram", "user_id": "123"}
STRANGER = {"platform": "discord", "user_id": "999"}


@pytest.fixture
def env(tmp_path):
    cfg = dict(home=str(tmp_path), origin=ORIGIN, owners=[123], bot_id=456,
               port=9129, backend_port=9130, display_name="Fixture")
    private_json(root(tmp_path) / "settings.json", cfg)
    pins = PinCache(fetch=lambda origin: PIN)
    c = Connector(tmp_path, cfg, "backend-fixture-only", pin_cache=pins)
    c.backend_ok = lambda: True
    client = TestClient(make_app(c), base_url=ORIGIN, client=("127.0.0.1", 33333))
    return c, client


def invite(c, **kw):
    kw.setdefault("requires_approval", True)
    i = c.store.create_invitation(origin=ORIGIN, created_by="fixture", **kw)
    return dict(invitation_id=i.invitation.id, secret=i.secret)


def ask(client, proof, client_secret=CLIENT, **extra):
    return client.post("/api/fleet/pairing/request",
                       json={**proof, "client_secret": client_secret, "device_name": "Test Phone",
                             "device_model": "Model1,1", **extra})


# --- HTTP ---------------------------------------------------------------------------------------

def test_preview_reports_approval_and_protocols(env):
    c, client = env
    v2 = client.post("/api/fleet/pairing/preview", json=invite(c))
    v1 = client.post("/api/fleet/pairing/preview", json=invite(c, requires_approval=False))
    assert v2.status_code == v1.status_code == 200
    assert v2.json()["invitation"]["approval_required"] is True
    assert v1.json()["invitation"]["approval_required"] is False
    assert v2.json()["protocols"] == [1, 2]
    assert v2.headers["cache-control"] == "no-store"


def test_request_status_approve_deliver_flow(env):
    c, client = env
    proof = invite(c)
    r = ask(client, proof)
    assert r.status_code == 201
    body = r.json()
    assert re.fullmatch(r"\d{6}", body["request"]["match_code"])
    assert body["request"]["poll_interval"] == 2
    assert body["request"]["expires_at"] - body["request"]["server_time"] in range(599, 602)
    assert body["gateway"]["origin"] == ORIGIN
    assert r.headers["cache-control"] == "no-store"
    # idempotent replay
    again = ask(client, proof)
    assert again.status_code == 201 and again.json()["request"]["id"] == body["request"]["id"]
    status = {"request_id": body["request"]["id"], "client_secret": CLIENT}
    pending = client.post("/api/fleet/pairing/request/status", json=status)
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    # poll rate limit: a second call within a second
    assert client.post("/api/fleet/pairing/request/status", json=status).status_code == 429
    c.store.approve_by_code(code=body["request"]["match_code"], approver_platform="telegram",
                            approver_user_id="123")
    time.sleep(1.05)
    done = client.post("/api/fleet/pairing/request/status", json=status).json()
    assert done["status"] == "approved" and done["credential"].startswith("hfd1.")
    assert done["scopes"] == ["fleet:operator"] and done["device"]["label"] == "Test Phone"
    time.sleep(1.05)
    again_done = client.post("/api/fleet/pairing/request/status", json=status).json()
    assert again_done["credential"] == done["credential"]
    login = client.post("/auth/device-login", json={"device_credential": done["credential"]})
    assert login.status_code == 200
    time.sleep(1.05)
    final = client.post("/api/fleet/pairing/request/status", json=status)
    assert final.json() == {"status": "delivered"}
    assert done["credential"] not in final.text


def test_request_errors(env):
    c, client = env
    proof = invite(c)
    first = ask(client, proof)
    assert first.status_code == 201
    assert ask(client, proof, client_secret=OTHER).status_code == 409           # already_redeemed
    v1 = invite(c, requires_approval=False)
    refused = ask(client, v1)
    assert refused.status_code == 409 and refused.json()["error"] == "approval_not_supported"
    assert ask(client, {**proof, "secret": "x" * 43}).status_code == 404
    assert ask(client, proof, client_secret="short").status_code == 422
    # wrong secret and unknown id are the same answer
    bad = {"request_id": first.json()["request"]["id"], "client_secret": OTHER}
    unknown = {"request_id": "A" * 22, "client_secret": CLIENT}
    a = client.post("/api/fleet/pairing/request/status", json=bad)
    b = client.post("/api/fleet/pairing/request/status", json=unknown)
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (400, {"error": "invalid_request", "detail": "Fleet request refused."})


def test_v1_redeem_refuses_approval_invitation(env):
    c, client = env
    proof = invite(c)
    r = client.post("/api/fleet/pairing/redeem", json=proof)
    assert r.status_code == 403 and r.json()["error"] == "approval_required"
    assert ask(client, proof).status_code == 201     # not consumed by the refusal


def test_body_limit_and_origin_checks_apply(env):
    c, client = env
    big = client.post("/api/fleet/pairing/request", content=b"x" * 20000)
    assert big.status_code == 413
    cross = client.post("/api/fleet/pairing/request", json={}, headers={"origin": "https://evil.example.test"})
    assert cross.status_code == 403
    plain = TestClient(make_app(c), base_url="http://fleet.example.test", client=("127.0.0.1", 1))
    assert plain.get("/pair").status_code == 426


def test_pair_page_is_static_strict_and_secret_free(env):
    c, client = env
    r = client.get("/pair")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'")
    script = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    style = re.search(r"<style>(.*?)</style>", r.text, re.S).group(1)
    for text in (script, style):
        digest = base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp
    assert "unsafe" not in csp and "http" not in csp.replace("https", "")
    assert re.findall(r"""(?:src|href)=["']https?://""", r.text) == []
    assert "location.hash" in script
    assert "hermes-fleet-dev://pair#" in script and "hermes-fleet://pair#" in script
    assert "Open in Hermes Fleet" in r.text and "TestFlight" in r.text
    # no invitation material of any kind is rendered server-side
    issued = invite(c)
    assert issued["secret"] not in r.text and issued["invitation_id"] not in r.text
    assert r.headers["x-frame-options"] == "DENY"
    # reachable without a session; the generic proxy still refuses everything else
    assert client.get("/api/config").status_code == 401


# --- control actions ----------------------------------------------------------------------------

def act(c, action, approver=LOCAL, **kw):
    return c.owner({"action": action, "approver": approver, **kw})


def test_non_owner_is_refused_everywhere(env):
    c, client = env
    for action in ("pair", "pending", "approve", "deny", "devices", "revoke", "status", "claim_start",
                   "owners", "owners_remove"):
        r = act(c, action, STRANGER, code="123456", device=1, index=1)
        assert r["ok"] is False and r["error"] == "not_owner" and r["message"], action
    assert act(c, "pair", {"platform": "telegram"})["error"] == "not_owner"
    assert c.owner({"action": "pair"})["error"] == "not_owner"
    assert c.store.list_pending_invitations() == []


def test_migrated_telegram_owner_is_recognised(env):
    c, _ = env
    assert act(c, "pending", TG)["ok"] is True
    assert act(c, "pending", {"platform": "telegram", "user_id": 123})["ok"] is True
    assert act(c, "pending", {"platform": "discord", "user_id": "123"})["error"] == "not_owner"


def test_pair_builds_v2_link_with_pin(env):
    c, client = env
    r = act(c, "pair", TG)
    assert r["ok"] and isinstance(r["expires_at"], int) and r["message"]
    m = re.fullmatch(re.escape(ORIGIN) + r"/pair#v=2&i=([A-Za-z0-9_-]{22})&s=([A-Za-z0-9_-]{43})&k=" + PIN, r["link"])
    assert m
    inv = c.store.preview(invitation_id=m.group(1), secret=m.group(2), origin=ORIGIN)
    assert inv.requires_approval and inv.created_by == "telegram:123"
    assert inv.expires_at - int(time.time()) in range(590, 601)
    # the same link drives the request path end to end
    assert ask(client, {"invitation_id": m.group(1), "secret": m.group(2)}).status_code == 201


def test_pair_fails_without_pin_and_issues_nothing(env):
    c, _ = env
    def boom(origin):
        raise PinUnavailable("x")
    c.pins = PinCache(fetch=boom)
    r = act(c, "pair")
    assert r["ok"] is False and r["error"] == "pin_unavailable" and r["message"]
    assert "k=" not in str(r) and c.store.list_pending_invitations() == []


def test_pending_approve_deny_devices_revoke(env):
    c, client = env
    link = act(c, "pair")["link"]
    i, s = re.search(r"i=([^&]+)&s=([^&]+)", link).groups()
    req = ask(client, {"invitation_id": i, "secret": s}).json()["request"]
    pending = act(c, "pending", TG)
    assert pending["requests"] == [{"match_code": req["match_code"], "device_name": "Test Phone",
        "device_model": "Model1,1", "created_at": pending["requests"][0]["created_at"], "expires_at": req["expires_at"]}]
    wrong = "000000" if req["match_code"] != "000000" else "111111"
    r = act(c, "approve", TG, code=wrong)
    assert r == {"ok": False, "error": "no_match", "message": r["message"]} and "different server" in r["message"]
    ok = act(c, "approve", TG, code=" ".join([req["match_code"][:3], req["match_code"][3:]]))
    assert ok["ok"] and ok["device"]["label"] == "Test Phone" and "connecting" in ok["message"]
    assert "credential" not in str(ok)
    d = act(c, "devices", LOCAL)
    assert d["devices"][0]["number"] == 1 and d["devices"][0]["id"] == ok["device"]["id"]
    assert c.store.list_devices()[0].approved_by_platform == "telegram"
    assert act(c, "revoke", LOCAL, device=1)["ok"]
    assert act(c, "devices")["devices"] == []
    assert act(c, "revoke", LOCAL, device=1)["error"] == "no_such_device"


def test_deny_and_revoke_by_id_and_text_number(env):
    c, client = env
    for n in range(2):
        proof = invite(c)
        ask(client, proof, client_secret=chr(97 + n) * 43)
    codes = [p["match_code"] for p in act(c, "pending")["requests"]]
    assert act(c, "deny", LOCAL, code=codes[0])["ok"]
    device = act(c, "approve", LOCAL, code=codes[1])["device"]
    assert act(c, "revoke", LOCAL, device=device["id"])["ok"]
    assert act(c, "revoke", LOCAL, device="nope")["error"] == "no_such_device"
    assert act(c, "revoke", LOCAL, device=True)["error"] == "no_such_device"


def test_approve_lockout_reports_locked(env):
    c, client = env
    ask(client, invite(c))
    for _ in range(fd.MAX_APPROVER_FAILURES):
        assert act(c, "approve", TG, code="000000")["error"] in {"no_match"}
    r = act(c, "approve", TG, code="000000")
    assert r["error"] == "locked" and r["message"]
    assert act(c, "approve", LOCAL, code="000000")["error"] == "no_match"   # per approver


def test_status_summary(env):
    c, _ = env
    r = act(c, "status", TG)
    assert r["ok"] and r["origin"] == ORIGIN and r["pin_computed"] is True
    assert r["backend_ok"] is True and r["version"] == VERSION == "0.3.0" and r["message"]


def test_claim_and_owner_management(env):
    c, _ = env
    assert act(c, "claim_start", TG)["error"] == "not_owner"       # local only
    start = act(c, "claim_start", LOCAL)
    code = start["code"]
    assert start["ok"] and re.fullmatch(r"\d{6}", code) and isinstance(start["expires_at"], int)
    bad = c.owner({"action": "claim", "approver": STRANGER, "code": "000000" if code != "000000" else "111111",
                   "platform": "discord", "user_id": "999"})
    assert bad["ok"] is False and bad["error"] == "invalid_code" and bad["message"]
    local_try = c.owner({"action": "claim", "approver": LOCAL, "code": code, "platform": "local", "user_id": "x"})
    assert local_try["ok"] is False
    good = c.owner({"action": "claim", "approver": STRANGER, "code": code, "platform": "discord", "user_id": "999"})
    assert good["ok"] and good["owner"] == {"platform": "discord", "user_id": "999"}
    assert act(c, "pending", STRANGER)["ok"]                         # now an owner
    owners = act(c, "owners", LOCAL)["owners"]
    assert [(o["number"], o["platform"], o["added_via"]) for o in owners] == [(1, "telegram", "migrated"), (2, "discord", "claim")]
    assert act(c, "owners_remove", STRANGER, index=9)["error"] == "no_such_owner"
    assert act(c, "owners_remove", LOCAL, index=2)["ok"]
    assert act(c, "pending", STRANGER)["error"] == "not_owner"


def test_legacy_actions_still_work(env):
    c, _ = env
    assert c.owner({"action": "devices", "owner": 123, "bot_id": 456, "update_id": 1})["ok"] is True
    assert c.owner({"action": "revoke", "local_operator": True, "device_id": "x" * 32})["ok"] is False
    assert c.owner({"action": "issue", "owner": 999, "bot_id": 456, "update_id": 1, "explicit_issue": True})["ok"] is False


def test_control_actions_over_the_real_socket(env):
    c, client = env
    server, path = start_control(c)
    try:
        r = control(c.home, {"action": "pair", "approver": LOCAL})
        assert r["ok"] and r["link"].startswith(ORIGIN + "/pair#v=2&")
        r = control(c.home, {"action": "pair", "approver": STRANGER})
        assert r["error"] == "not_owner"
    finally:
        server.shutdown()
        server.server_close()
        path.unlink()


# --- pin ----------------------------------------------------------------------------------------

@pytest.fixture
def tls_server(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fleet.example.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1)).sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "c.pem", tmp_path / "k.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    stop = threading.Event()
    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except Exception:
                pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    expected = base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).decode().rstrip("=")
    yield sock.getsockname()[1], expected
    stop.set()
    t.join(2)
    sock.close()


def test_pin_matches_independently_computed_spki_hash(tls_server):
    port, expected = tls_server
    pin = fetch_pin("127.0.0.1", port)
    assert pin == expected and len(pin) == 43 and "=" not in pin


def test_pin_unavailable_on_refused_or_non_tls():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(PinUnavailable):
        fetch_pin("127.0.0.1", port, timeout=1)
    plain = socket.socket()
    plain.bind(("127.0.0.1", 0))
    plain.listen(1)
    threading.Thread(target=lambda: plain.accept()[0].close(), daemon=True).start()
    with pytest.raises(PinUnavailable) as caught:
        fetch_pin("127.0.0.1", plain.getsockname()[1], timeout=1)
    plain.close()
    assert "127.0.0.1" not in str(caught.value)


def test_pin_cache_ten_minutes_and_failures_not_cached():
    now = [0.0]
    calls = []
    def fetch(origin):
        calls.append(origin)
        if len(calls) == 1:
            raise PinUnavailable("x")
        return PIN
    cache = PinCache(fetch=fetch, clock=lambda: now[0])
    with pytest.raises(PinUnavailable):
        cache.get(ORIGIN)
    assert cache.get(ORIGIN) == PIN and len(calls) == 2
    now[0] = 599
    assert cache.get(ORIGIN) == PIN and len(calls) == 2
    now[0] = 601
    assert cache.get(ORIGIN) == PIN and len(calls) == 3
