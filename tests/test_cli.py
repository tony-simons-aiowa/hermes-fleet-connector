"""`hermes fleet ...` with fake control, fake clock, fake qrencode. Synthetic data only."""
import argparse
import io
from pathlib import Path
import pytest
from fleet_connector import cli
from fleet_connector.commands import LOCAL_APPROVER, UNREACHABLE, NO_MATCH

HOME = Path("/nonexistent/fleet-test-home")
LINK = "https://fleet.example.test/pair#v=2&i=inv&s=sec&k=pin"
LOCAL = LOCAL_APPROVER

class Fake:
    def __init__(self, handler):
        self.handler, self.calls = handler, []
    def __call__(self, home, payload):
        assert home == HOME
        self.calls.append(payload)
        r = self.handler(payload, len(self.calls))
        if isinstance(r, Exception):
            raise r
        return r

def parse(*argv):
    p = argparse.ArgumentParser()
    cli.add_arguments(p)
    return p.parse_args(list(argv))

class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s

def go(*argv, fake, tty=True, answers=(), qr=True, clock=None):
    out, runs, asked = io.StringIO(), [], []
    clock = clock or Clock()
    it = iter(answers)
    def input_fn(prompt):
        asked.append(prompt)
        try:
            v = next(it)
        except StopIteration:
            raise EOFError
        if isinstance(v, BaseException):
            raise v
        return v
    def runner(cmd, **kw):
        runs.append((cmd, kw))
    code = cli.run(parse(*argv), home=HOME, control_fn=fake, out=out, stdin_isatty=tty,
                   input_fn=input_fn, sleep=clock.sleep, clock=clock,
                   which=(lambda n: "/x/qrencode" if qr else None), runner=runner)
    return code, out.getvalue(), runs, asked

def pending(*reqs):
    return {"ok": True, "requests": [dict(match_code=c, device_name=n, device_model=m,
                                          created_at=1, expires_at=2) for c, n, m in reqs]}

def pair_service(pend_seq, approve=None):
    def h(payload, n):
        a = payload["action"]
        assert payload["approver"] == LOCAL
        if a == "pair":
            return {"ok": True, "link": LINK, "expires_at": 1}
        if a == "pending":
            return pend_seq[min(n - 2, len(pend_seq) - 1)]
        return approve or {"ok": True, "device": {"label": "Test iPhone"}}
    return h

def test_pair_flow_approve_with_qr_stdin_only():
    fake = Fake(pair_service([{"ok": True, "requests": []}, pending(("482913", "Test iPhone", "iPhone17,1"))]))
    code, out, runs, asked = go("pair", fake=fake, answers=["y"])
    assert code == 0
    assert LINK in out
    assert runs[0][0] == ["qrencode", "-t", "ANSIUTF8"] and runs[0][1]["input"] == LINK.encode()
    assert all(LINK not in part for cmd, _ in runs for part in cmd)
    assert asked == ["iPhone request: Test iPhone (iPhone17,1) · code 482 913. Approve? [y/N] "]
    assert [c["action"] for c in fake.calls] == ["pair", "pending", "pending", "approve"]
    assert fake.calls[-1] == {"action": "approve", "approver": LOCAL, "code": "482913"}
    assert "Approved Test iPhone" in out

def test_pair_flow_deny_then_next_request_then_approve():
    seq = [pending(("111111", "Other", "iPhone1,1")),
           pending(("111111", "Other", "iPhone1,1"), ("222222", "Mine", "iPhone17,1"))]
    fake = Fake(pair_service(seq, approve={"ok": True, "message": "Denied."}))
    code, out, _, asked = go("pair", fake=fake, answers=["", "yes"])
    actions = [(c["action"], c.get("code")) for c in fake.calls]
    assert ("deny", "111111") in actions and ("approve", "222222") in actions
    assert len(asked) == 2   # the denied code is not asked again
    assert code == 0

def test_pair_no_qrencode_link_only():
    fake = Fake(pair_service([pending(("482913", "T", ""))]))
    code, out, runs, asked = go("pair", fake=fake, answers=["n"], qr=False, clock=None)
    assert runs == [] and LINK in out
    assert "Approve?" in asked[0] and "()" not in asked[0]

def test_pair_non_interactive_prints_instructions_and_does_not_poll():
    fake = Fake(pair_service([]))
    code, out, runs, asked = go("pair", fake=fake, tty=False)
    assert code == 0 and asked == []
    assert [c["action"] for c in fake.calls] == ["pair"]
    assert "hermes fleet approve <code>" in out and "/fleet approve <code>" in out

def test_pair_eof_prints_instruction():
    fake = Fake(pair_service([pending(("482913", "T", "M"))]))
    code, out, _, _ = go("pair", fake=fake, answers=[])
    assert code == 0 and "hermes fleet approve 482913" in out
    assert [c["action"] for c in fake.calls] == ["pair", "pending"]

def test_pair_ctrl_c_exits_cleanly():
    fake = Fake(pair_service([{"ok": True, "requests": []}]))
    clock = Clock()
    def sleep(s):
        raise KeyboardInterrupt
    out = io.StringIO()
    code = cli.run(parse("pair"), home=HOME, control_fn=fake, out=out, stdin_isatty=True,
                   input_fn=input, sleep=sleep, clock=clock, which=lambda n: None, runner=None)
    assert code == 130 and "Stopped waiting" in out.getvalue()

def test_pair_times_out_after_ten_minutes_polling_every_two_seconds():
    fake = Fake(pair_service([{"ok": True, "requests": []}]))
    clock = Clock()
    code, out, _, _ = go("pair", fake=fake, clock=clock)
    polls = [c for c in fake.calls if c["action"] == "pending"]
    assert code == 1 and len(polls) == 300 and clock.t >= 600 and "Timed out" in out

def test_pair_unreachable_and_service_error():
    code, out, runs, _ = go("pair", fake=Fake(lambda p, n: FileNotFoundError()))
    assert code == 1 and UNREACHABLE in out and runs == []
    code, out, *_ = go("pair", fake=Fake(lambda p, n: {"ok": False, "message": "Not ready: no origin."}))
    assert code == 1 and "Not ready: no origin." in out and LINK not in out

def test_pair_mid_poll_service_loss():
    def h(p, n):
        return {"ok": True, "link": LINK} if n == 1 else ConnectionRefusedError()
    code, out, *_ = go("pair", fake=Fake(h))
    assert code == 1 and UNREACHABLE in out

def test_approve_deny_normalise_and_reject():
    fake = Fake(lambda p, n: {"ok": True, "device": {"label": "Test iPhone"}, "message": "m"})
    assert go("approve", "482", "913", fake=fake)[0] == 0
    assert fake.calls[0] == {"action": "approve", "approver": LOCAL, "code": "482913"}
    code, out, *_ = go("deny", "48-29-13", fake=fake)
    assert fake.calls[1]["code"] == "482913"
    n = len(fake.calls)
    code, out, *_ = go("approve", "12", fake=fake)
    assert code == 2 and len(fake.calls) == n

def test_approve_no_match_warning():
    code, out, *_ = go("approve", "111111", fake=Fake(lambda p, n: {"ok": False, "error": "no_match"}))
    assert code == 1 and NO_MATCH in out

def test_devices_status_revoke():
    fake = Fake(lambda p, n: {"ok": True, "message": f"reply-{p['action']}"})
    assert "reply-devices" in go("devices", fake=fake)[1]
    assert "reply-status" in go("status", fake=fake)[1]
    go("revoke", "2", fake=fake)
    go("revoke", "abc123", fake=fake)
    assert [c.get("device") for c in fake.calls[2:]] == [2, "abc123"]
    assert all(c["approver"] == LOCAL for c in fake.calls)

def test_claim():
    fake = Fake(lambda p, n: {"ok": True, "code": "482913", "expires_at": 1})
    code, out, *_ = go("claim", fake=fake)
    assert fake.calls == [{"action": "claim_start", "approver": LOCAL}]
    assert "/fleet claim 482913 (valid 10 minutes)" in out
    assert "In a private chat with Hermes on any platform, send:" in out

def test_owners_and_remove():
    fake = Fake(lambda p, n: {"ok": True, "message": "1. telegram 1001"})
    go("owners", fake=fake)
    go("owners", "remove", "1", fake=fake)
    assert [(c["action"], c.get("index")) for c in fake.calls] == [("owners", None), ("owners_remove", 1)]
    assert go("owners", "remove", fake=fake)[0] == 2

def test_no_subcommand_usage():
    fake = Fake(lambda p, n: {"ok": True})
    code, out, *_ = go(fake=fake)
    assert code == 2 and "hermes fleet pair" in out and fake.calls == []

def test_handler_exits_nonzero(monkeypatch):
    monkeypatch.setattr(cli, "run", lambda args: 3)
    with pytest.raises(SystemExit) as e:
        cli.handler(None)
    assert e.value.code == 3


def test_backend_status_uses_private_host_lease_without_browser_login():
    fake = Fake(lambda payload, n: {"ok": True, "pid": 91, "role": "serve", "idle": True})
    code, output, runs, asked = go("backend-status", fake=fake, tty=False)
    assert code == 0 and "Backend 91: idle" in output
    assert fake.calls[0]["action"] == "backend_maintenance" and fake.calls[0]["local_operator"] is True
    assert not runs and not asked
