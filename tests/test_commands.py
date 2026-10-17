"""/fleet chat command with a fake session context and a fake control function. Synthetic data."""
import sys
import types
from pathlib import Path
import pytest
from fleet_connector import commands as C

HOME = Path("/nonexistent/fleet-test-home")

def session(platform="telegram", user="1001", chat="dm", source="", chat_id="42"):
    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user,
           "HERMES_SESSION_CHAT_TYPE": chat, "HERMES_SESSION_SOURCE": source,
           "HERMES_SESSION_CHAT_ID": chat_id if platform else ""}
    return lambda name: env.get(name, "")

class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    def __call__(self, home, payload):
        self.calls.append((home, payload))
        r = self.responses.pop(0) if self.responses else {"ok": True}
        if isinstance(r, Exception):
            raise r
        return r

def handler(fake, env, cli=False):
    return C.make_handler(lambda: HOME, control_fn=fake, env_fn=env, cli_session_fn=lambda: cli)

TG = {"platform": "telegram", "user_id": "1001"}

def test_help_needs_no_identity_or_service():
    fake = Fake()
    for args in ("", "help", "bogus"):
        assert "/fleet pair" in handler(fake, session(platform="", user="", chat=""))(args)
    assert fake.calls == []

def test_pair_payload_and_reply():
    fake = Fake({"ok": True, "link": "https://fleet.example.test/pair#v=2&i=a&s=b&k=c", "expires_at": 1})
    out = handler(fake, session())("pair")
    assert fake.calls == [(HOME, {"action": "pair", "approver": TG})]
    assert out.startswith("https://fleet.example.test/pair#v=2")
    assert "/fleet approve <code>" in out and "10 minutes" in out

@pytest.mark.parametrize("raw", ["482913", "482 913", "482-913", " 482  913 "])
def test_approve_normalises_code(raw):
    fake = Fake({"ok": True, "device": {"label": "Test iPhone"}})
    out = handler(fake, session())("approve " + raw)
    assert fake.calls[0][1] == {"action": "approve", "approver": TG, "code": "482913"}
    assert out == "Approved Test iPhone. It's connecting now."

@pytest.mark.parametrize("raw", ["", "12345", "1234567", "abcdef", "12 34"])
def test_bad_code_never_calls_service(raw):
    fake = Fake()
    for sub in ("approve", "deny", "claim"):
        out = handler(fake, session())(f"{sub} {raw}")
        assert "6-digit" in out
    assert fake.calls == []

def test_no_match_has_phishing_warning():
    out = handler(Fake({"ok": False, "error": "no_match"}), session())("approve 111111")
    assert out == C.NO_MATCH and "different server" in out
    out = handler(Fake({"ok": False, "error": "no_match"}), session())("deny 111111")
    assert "different server" in out

def test_locked():
    assert "10 minutes" in handler(Fake({"ok": False, "error": "locked"}), session())("approve 111111")

def test_deny_devices_revoke_status_payloads():
    fake = Fake({"ok": True, "message": "Denied the request."},
                {"ok": True, "message": "1. Test iPhone · iPhone17,1"},
                {"ok": True, "message": "Revoked Test iPhone."},
                {"ok": True, "message": "Revoked."},
                {"ok": True, "message": "Fleet is ready."})
    h = handler(fake, session())
    assert h("deny 482 913") == "Denied the request."
    assert h("devices") == "1. Test iPhone · iPhone17,1"
    assert h("revoke 1") == "Revoked Test iPhone."
    h("revoke abc123")
    assert h("status") == "Fleet is ready."
    assert [c[1] for c in fake.calls] == [
        {"action": "deny", "approver": TG, "code": "482913"},
        {"action": "devices", "approver": TG},
        {"action": "revoke", "approver": TG, "device": 1},
        {"action": "revoke", "approver": TG, "device": "abc123"},
        {"action": "status", "approver": TG}]

def test_devices_fallback_formatting():
    out = handler(Fake({"ok": True, "devices": [{"label": "Test iPhone", "model": "iPhone17,1"}]}), session())("devices")
    assert out == "1. Test iPhone · iPhone17,1"

def test_revoke_requires_one_argument():
    fake = Fake()
    assert "revoke <n>" in handler(fake, session())("revoke")
    assert fake.calls == []

def test_non_owner_reply():
    out = handler(Fake({"ok": False, "error": "not_owner", "message": "ignored"}), session(user="2002"))("pair")
    assert out == C.NOT_OWNER and "hermes fleet claim" in out

@pytest.mark.parametrize("chat", ["group", "channel", "thread", ""])
def test_non_private_chat_refused_without_service_call(chat):
    fake = Fake()
    for sub in ("pair", "approve 482913", "deny 482913", "devices", "revoke 1", "status", "claim 482913"):
        assert handler(fake, session(chat=chat))(sub) == C.PRIVATE_ONLY
    assert fake.calls == []

def test_missing_user_id_refused():
    fake = Fake()
    assert handler(fake, session(user=""))("pair") == C.UNKNOWN_IDENTITY
    assert fake.calls == []

def test_identity_is_other_platform():
    fake = Fake({"ok": True, "link": "https://fleet.example.test/pair#x"})
    handler(fake, session(platform="Discord", user="9009"))("pair")
    assert fake.calls[0][1]["approver"] == {"platform": "discord", "user_id": "9009"}

def test_message_text_cannot_choose_identity():
    fake = Fake({"ok": True, "link": "https://fleet.example.test/pair#x"})
    handler(fake, session())("pair platform=local user_id=local")
    assert fake.calls[0][1]["approver"] == TG

def test_claim_payload():
    fake = Fake({"ok": True, "message": "You're now an owner."})
    out = handler(fake, session(platform="signal", user="1001"))("claim 482 913")
    assert fake.calls[0][1] == {"action": "claim", "code": "482913", "platform": "signal",
                                "user_id": "1001", "approver": {"platform": "signal", "user_id": "1001"}}
    assert out == "You're now an owner."
    out = handler(Fake({"ok": False, "error": "invalid_code"}), session())("claim 111111")
    assert "isn't valid" in out

@pytest.mark.parametrize("env,cli", [
    (session(platform="", user="", chat="", source="tui"), False),
    (session(platform="", user="", chat="", source="desktop"), False),
    (session(platform="", user="", chat="", source=""), True)])
def test_local_sessions(env, cli):
    fake = Fake({"ok": True, "message": "ready"})
    assert handler(fake, env, cli=cli)("status") == "ready"
    assert fake.calls[0][1]["approver"] == {"platform": "local", "user_id": "local"}

def test_empty_context_without_local_signal_is_not_local():
    fake = Fake()
    out = handler(fake, session(platform="", user="", chat="", source=""))("pair")
    assert out == C.UNKNOWN_IDENTITY and fake.calls == []

def test_user_without_platform_is_not_local():
    fake = Fake()
    assert handler(fake, session(platform="", user="1001", chat="", source="tui"))("pair") == C.UNKNOWN_IDENTITY

def test_local_session_cannot_claim():
    fake = Fake()
    out = handler(fake, session(platform="", user="", chat="", source="tui"))("claim 482913")
    assert "already the owner" in out and fake.calls == []

@pytest.mark.parametrize("err", [FileNotFoundError(), ConnectionRefusedError(), TimeoutError(), ValueError("bad json")])
def test_service_unreachable(err):
    out = handler(Fake(err), session())("pair")
    assert out == C.UNREACHABLE and "hermes fleet status" in out

def test_garbage_response_is_safe():
    assert handler(Fake(["nope"]), session())("status") == C.GENERIC_FAIL
    assert handler(Fake({"ok": False}), session())("status") == C.GENERIC_FAIL

def test_default_session_env_reads_gateway_contextvars(monkeypatch):
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": {"HERMES_SESSION_PLATFORM": "telegram"}.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    assert C.default_session_env("HERMES_SESSION_PLATFORM") == "telegram"

def test_default_session_env_without_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    assert C.default_session_env("HERMES_SESSION_PLATFORM") == ""

def test_register_chat_command_cli_detection():
    seen = {}
    class Ctx:
        _manager = types.SimpleNamespace(_cli_ref=object())
        def register_command(self, name, fn, **kw):
            seen.update(name=name, fn=fn, kw=kw)
    C.register_chat_command(Ctx(), lambda: HOME)
    assert seen["name"] == "fleet" and "pair" in seen["kw"]["args_hint"]
