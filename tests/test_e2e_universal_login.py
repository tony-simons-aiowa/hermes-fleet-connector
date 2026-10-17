"""End-to-end universal login on a Telegram-free host. No mocks between layers.

Real pieces: settings.load on an origin-only settings file, the Connector, the FastAPI app (HTTP),
the control socket server (start_control), the control client, commands.make_handler and
cli.run. Only environment edges are replaced: the SPKI pin fetch (no real TLS endpoint), the
stock-backend probe, and the session environment (Hermes gateway context). Synthetic data only.
"""
import io
import re
import time
from argparse import Namespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from fleet_connector import cli, commands, settings
from fleet_connector.control import control, socket_path
from fleet_connector.pin import PinCache
from fleet_connector.service import Connector, make_app, start_control

ORIGIN = "https://fleet.example.test"
PIN = "Qk" * 21 + "Q"  # 43 base64url characters
CLIENT = "e" * 43
OTHER_CLIENT = "f" * 43


@pytest.fixture
def host(tmp_path):
    # Settings contain only an origin: no Telegram owners, no bot id.
    settings.private_json(settings.root(tmp_path) / "settings.json", {
        "home": str(tmp_path.resolve()), "origin": ORIGIN, "port": 9129, "backend_port": 9130,
        "display_name": "Fixture", "version": "0.2.0"})
    cfg = settings.load(tmp_path)
    assert "owners" not in cfg and "bot_id" not in cfg
    sock = socket_path(tmp_path)
    assert not sock.exists(), "control socket path must be unique to this temporary home"
    connector = Connector(tmp_path, cfg, "backend-fixture-only", pin_cache=PinCache(fetch=lambda origin: PIN))
    connector.backend_ok = lambda: True  # stock backend is not part of the pairing path
    server, path = start_control(connector)
    assert path == sock
    http = TestClient(make_app(connector), base_url=ORIGIN, client=("127.0.0.1", 40001))
    try:
        yield Host(tmp_path, connector, http)
    finally:
        server.shutdown()
        server.server_close()
        sock.unlink(missing_ok=True)
        try:
            sock.parent.rmdir()
        except OSError:
            pass


class Host:
    def __init__(self, home, connector, http):
        self.home, self.connector, self.http = home, connector, http
        self._last_poll = 0.0

    # -- identities, as the Hermes gateway would bind them -------------------------------------
    def chat(self, platform, user_id, chat_type="dm"):
        env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user_id,
               "HERMES_SESSION_CHAT_TYPE": chat_type}
        handler = commands.make_handler(lambda: self.home, env_fn=lambda name: env.get(name, ""))
        return handler

    def say(self, platform, user_id, text, chat_type="dm"):
        return self.chat(platform, user_id, chat_type)(text)

    def cli(self, command, **kw):
        out = io.StringIO()
        code = cli.run(Namespace(fleet_command=command, **kw), home=self.home, out=out,
                       stdin_isatty=False, which=lambda name: None)
        return code, out.getvalue()

    def claim_owner(self, platform, user_id):
        code, out = self.cli("claim")
        assert code == 0
        digits = re.search(r"/fleet claim (\d{6})", out).group(1)
        return digits, self.say(platform, user_id, f"claim {digits}")

    # -- phone side (HTTP) ----------------------------------------------------------------------
    def poll(self, request_id, client_secret=CLIENT):
        wait = 1.1 - (time.monotonic() - self._last_poll)
        if wait > 0:
            time.sleep(wait)  # the contract allows one poll per second per request
        self._last_poll = time.monotonic()
        return self.http.post("/api/fleet/pairing/request/status",
                              json={"request_id": request_id, "client_secret": client_secret})

    def pair_link(self):
        code, out = self.cli("pair")
        assert code == 0
        link = next(line for line in out.splitlines() if line.startswith(ORIGIN + "/pair#"))
        frag = parse_qs(urlsplit(link).fragment)
        return link, {key: values[0] for key, values in frag.items()}


def wrong_code(real):
    return "000000" if real != "000000" else "111111"


def test_full_pairing_flow_without_telegram(host):
    # A messaging owner is added through the real claim flow (no Telegram anywhere).
    _, reply = host.claim_owner("discord", "1001")
    assert "owner" in reply.lower()

    # a. local pair -> v2 link
    link, frag = host.pair_link()
    assert frag["v"] == "2" and len(frag["i"]) == 22 and len(frag["s"]) == 43
    assert len(frag["k"]) == 43 and frag["k"] == PIN
    proof = {"invitation_id": frag["i"], "secret": frag["s"]}

    preview = host.http.post("/api/fleet/pairing/preview", json=proof)
    assert preview.status_code == 200
    assert preview.json()["invitation"]["approval_required"] is True

    v1 = host.http.post("/api/fleet/pairing/redeem", json={**proof, "device_name": "Test Phone"})
    assert v1.status_code == 403 and v1.json()["error"] == "approval_required"

    request = host.http.post("/api/fleet/pairing/request", json={
        **proof, "client_secret": CLIENT, "device_name": "Test Phone", "device_model": "Model1,1"})
    assert request.status_code == 201
    body = request.json()
    code = body["request"]["match_code"]
    request_id = body["request"]["id"]
    assert re.fullmatch(r"\d{6}", code)

    pending = host.poll(request_id)
    assert pending.status_code == 200 and pending.json()["status"] == "pending"

    # Someone holding only the link cannot collect anything.
    stolen = host.poll(request_id, OTHER_CLIENT)
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_request"

    # Wrong code from the messaging owner: S5 warning, request untouched.
    warning = host.say("discord", "1001", f"approve {wrong_code(code)}")
    assert "different server" in warning and "Approved" not in warning
    assert host.poll(request_id).json()["status"] == "pending"

    # Right code, with spaces as a person might type them.
    ok = host.say("discord", "1001", f"approve {code[:3]} {code[3:]}")
    assert ok.startswith("Approved Test Phone")

    approved = host.poll(request_id)
    assert approved.status_code == 200
    data = approved.json()
    assert data["status"] == "approved" and data["credential"].startswith("hfd1.")
    assert data["scopes"] == ["fleet:operator"]
    credential = data["credential"]

    login = host.http.post("/auth/device-login", json={"device_credential": credential})
    assert login.status_code == 200 and login.json()["ok"] is True

    delivered = host.poll(request_id)
    assert delivered.json() == {"status": "delivered"}

    listing = host.say("discord", "1001", "devices")
    assert "Test Phone" in listing and listing.startswith("1.")
    local_listing = host.cli("devices")[1]
    assert "Test Phone" in local_listing

    assert "revoked" in host.say("discord", "1001", "revoke 1").lower()
    again = host.http.post("/auth/device-login", json={"device_credential": credential})
    assert again.status_code in (401, 403) and again.json().get("error") == "invalid_credential"
    assert "No devices" in host.say("discord", "1001", "devices")

    # The link is single use.
    reuse = host.http.post("/api/fleet/pairing/request", json={
        **proof, "client_secret": OTHER_CLIENT, "device_name": "Other"})
    assert reuse.status_code in (409, 410)


def test_telegram_free_status_and_legacy_refusal(host):
    # Local access is ownership: nothing is "pending" just because there is no Telegram owner.
    status = control(host.home, {"action": "status", "approver": commands.LOCAL_APPROVER})
    assert status["ok"] is True and status["pin_computed"] is True and status["backend_ok"] is True
    assert "binding" not in status["message"].lower() and "owner_binding_pending" not in str(status)
    code, out = host.cli("status")
    assert code == 0 and "binding" not in out.lower()
    # Legacy Telegram owner proofs refuse cleanly; they never crash the service.
    refused = control(host.home, {"action": "issue", "owner": 123, "bot_id": 456, "update_id": 1,
                                  "explicit_issue": True})
    assert refused == {"ok": False, "message": "Owner authorization refused."}
    # The service is still alive afterwards.
    assert control(host.home, {"action": "status", "approver": commands.LOCAL_APPROVER})["ok"] is True


def test_claim_scoping_and_lockout(host):
    code, reply = host.claim_owner("discord", "1001")
    assert "owner" in reply.lower()
    # 1001 can now pair from a private chat.
    pair = host.say("discord", "1001", "pair")
    assert "/pair#v=2&i=" in pair
    # A different user and a group chat are refused.
    assert host.say("discord", "1002", "pair") == commands.NOT_OWNER
    assert host.say("discord", "1001", "pair", chat_type="group") == commands.PRIVATE_ONLY
    # The same code cannot be claimed twice (single use).
    assert "now an owner" not in host.say("discord", "1002", f"claim {code}")
    assert host.say("discord", "1002", "pair") == commands.NOT_OWNER

    # Five wrong attempts void a fresh code, even the right one afterwards.
    _, out = host.cli("claim")
    real = re.search(r"/fleet claim (\d{6})", out).group(1)
    bad = wrong_code(real)
    for _ in range(5):
        assert host.say("discord", "1003", f"claim {bad}")
    refused = host.say("discord", "1003", f"claim {real}")
    assert "now an owner" not in refused
    assert host.say("discord", "1003", "pair") == commands.NOT_OWNER
    assert [o["user_id"] for o in host.connector.owners.list_owners() if o["platform"] == "discord"] == ["1001"]


def test_non_owner_cannot_approve_even_with_the_right_code(host):
    host.claim_owner("discord", "1001")
    _, frag = host.pair_link()
    proof = {"invitation_id": frag["i"], "secret": frag["s"]}
    request = host.http.post("/api/fleet/pairing/request", json={
        **proof, "client_secret": CLIENT, "device_name": "Test Phone", "device_model": "Model1,1"}).json()
    code, request_id = request["request"]["match_code"], request["request"]["id"]

    assert host.say("discord", "1002", f"approve {code}") == commands.NOT_OWNER
    assert host.say("discord", "1002", f"deny {code}") == commands.NOT_OWNER
    # An owner's approval from a group chat is refused too.
    assert host.say("discord", "1001", f"approve {code}", chat_type="group") == commands.PRIVATE_ONLY
    # A different platform with the same user id is a different identity.
    assert host.say("slack", "1001", f"approve {code}") == commands.NOT_OWNER
    assert host.poll(request_id).json()["status"] == "pending"
    assert host.say("discord", "1001", f"approve {code}").startswith("Approved")
