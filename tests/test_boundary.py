import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from fastapi.testclient import TestClient
from fleet_connector.bridge import owner_event, control
from fleet_connector.service import Connector, make_app, start_control
from fleet_connector.settings import private_json, normalise_origin, root

ORIGIN = "https://fixture.example.test"

@pytest.fixture
def fixture(tmp_path):
    cfg = dict(home=str(tmp_path), origin=ORIGIN, owners=[123], bot_id=456,
               port=9129, backend_port=9130, display_name="Fixture")
    private_json(root(tmp_path) / "settings.json", cfg)
    c = Connector(tmp_path, cfg, "backend-fixture-only")
    client = TestClient(make_app(c), base_url=ORIGIN, client=("127.0.0.1", 33333))
    return c, client

def issue(c):
    i = c.store.create_invitation(origin=ORIGIN, created_by="fixture", ttl_seconds=60)
    return dict(invitation_id=i.invitation.id, secret=i.secret)

def paired(c, client):
    response = client.post("/api/fleet/pairing/redeem", json=issue(c))
    assert response.status_code == 201
    data = response.json()
    login = client.post("/auth/device-login", json={"device_credential": data["credential"]})
    assert login.status_code == 200
    return data

def test_preview_redeem_login_ticket_scope_revocation(fixture):
    c, client = fixture
    proof = issue(c)
    preview = client.post("/api/fleet/pairing/preview", json=proof)
    assert preview.status_code == 200
    assert preview.json()["gateway"]["origin"] == ORIGIN
    assert preview.json()["invitation"]["scopes"] == ["fleet:operator"]
    redeem = client.post("/api/fleet/pairing/redeem", json={**proof,"device_name":"iPhone","device_model":"Fixture"})
    data = redeem.json()
    assert data["credential"].startswith("hfd1.")
    assert client.post("/api/fleet/pairing/redeem",json=proof).status_code == 409
    login = client.post("/auth/device-login",json={"device_credential":data["credential"]})
    assert login.status_code == 200
    assert "Secure" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]
    assert isinstance(login.json()["expires_at"],str)
    ticket = client.post("/api/auth/ws-ticket")
    assert ticket.status_code == 200 and ticket.json()["ttl_seconds"] == 30
    assert data["credential"] not in ticket.text and c.backend_token not in ticket.text
    assert client.post("/api/fleet/pairing/invitations",json={}).status_code == 403
    assert client.get("/api/config").status_code == 403
    assert client.delete("/api/fleet/devices/"+data["device"]["id"]).status_code == 403
    assert client.post("/auth/device-revoke",json={"device_credential":data["credential"]}).status_code == 200
    assert client.post("/api/auth/ws-ticket").status_code == 401
    assert client.post("/auth/device-login",json={"device_credential":data["credential"]}).status_code == 401

@pytest.mark.parametrize("mutation", ["wrong_host","plain_http","cross_origin","large_body","malformed"])
def test_rejections_do_not_echo_secrets(fixture, mutation):
    c, client = fixture
    secret = issue(c)
    headers = {}
    url = "/api/fleet/pairing/preview"
    content = json.dumps(secret)
    if mutation == "wrong_host": headers["host"]="other.example.test"
    if mutation == "plain_http": url="http://fixture.example.test"+url
    if mutation == "cross_origin": headers["origin"]="https://other.example.test"
    if mutation == "large_body": content="a"*17000+secret["secret"]
    if mutation == "malformed": content=json.dumps({**secret,"unexpected_secret":secret["secret"]})
    headers["content-type"]="application/json"
    response=client.post(url,content=content,headers=headers)
    assert response.status_code in {426,403,413,422}
    assert secret["secret"] not in response.text and c.backend_token not in response.text

def test_credential_cannot_be_used_as_session(fixture):
    c,client=fixture
    data=paired(c,client)
    client.cookies.clear()
    client.cookies.set("__Host-hermes_session_at",data["credential"])
    assert client.post("/api/auth/ws-ticket").status_code == 401

def event(**kw):
    u=NS(id=123,is_bot=False)
    chat=NS(id=123,type="private")
    m=NS(from_user=u,chat=chat,text="/fleetqr")
    e=NS(message=m,effective_message=m,effective_user=u,effective_chat=chat,update_id=10)
    for key,value in kw.items():
        if key.startswith("message_"): setattr(m,key[8:],value)
        else: setattr(e,key,value)
    return e

@pytest.mark.parametrize("change", ["nonowner","group","forward","via_bot","internal","edited","sender_mismatch","no_update"])
def test_owner_metadata_denials(change):
    e=event()
    if change=="nonowner": e.effective_user.id=999
    if change=="group": e.effective_chat.type="group"
    if change=="forward": e.message.forward_origin=object()
    if change=="via_bot": e.message.via_bot=object()
    if change=="internal": e.internal=True
    if change=="edited": e.message=None
    if change=="sender_mismatch": e.message.from_user=NS(id=123,is_bot=False)
    if change=="no_update": e.update_id=None
    assert not owner_event(e,[123])

def test_actual_owner_metadata():
    assert owner_event(event(),[123])

def test_control_socket_authority_and_restart_replay(fixture):
    c,client=fixture
    server,path=start_control(c)
    try:
        assert path.stat().st_mode & 0o077 == 0
        denied=control(c.home,{"action":"issue","owner":999,"bot_id":456,"update_id":7,"explicit_issue":True})
        assert not denied["ok"]
        assert not c.store.list_pending_invitations()
        proof=dict(action="issue",owner=123,bot_id=456,update_id=7,explicit_issue=True)
        assert c.claim(proof)
        assert not c.claim(proof)
        other=Connector(c.home,c.cfg,"another-fixture-token")
        assert not other.claim(proof)
        assert not c.owner(dict(action="issue",local_operator=True))["ok"]
    finally:
        server.shutdown();server.server_close();path.unlink()

def test_display_failure_before_confirmation_does_not_mint(fixture):
    c,client=fixture
    # Fixture readiness stands in for physical network/display observations; never claims live evidence.
    c.readiness=lambda **kw: dict(ready=True,blockers=[])
    def broken(*args,**kwargs): raise OSError("fixture")
    c.viewer_factory=broken
    result=c.owner(dict(action="issue",owner=123,bot_id=456,update_id=9,explicit_issue=True))
    assert result["ok"] and "s=" not in result["message"]
    for _ in range(100):
        if not c._display_busy: break
        time.sleep(.01)
    assert c.last_display=="display_failed"
    assert not c.store.list_pending_invitations()

@pytest.mark.parametrize("origin", ["http://a.example.test","https://127.0.0.1","https://a","https://a.example.test/path","https://u:p@a.example.test","https://a.example.test#x","https://a..test"])
def test_invalid_origins(origin):
    with pytest.raises(ValueError): normalise_origin(origin)
