"""``hermes fleet ...`` terminal command. Running it on the host counts as local ownership."""
from __future__ import annotations
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import commands as C
from .control import control as _control

POLL_SECONDS = 2
PAIR_WAIT_SECONDS = 600

def add_arguments(parser) -> None:
    sub = parser.add_subparsers(dest="fleet_command", metavar="COMMAND")
    sub.add_parser("pair", help="get a link for your iPhone and approve it here")
    for name, hlp in (("approve", "approve the code your iPhone shows"), ("deny", "reject a request")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("code", nargs="+", help="6-digit match code")
    sub.add_parser("devices", help="list connected phones")
    p = sub.add_parser("revoke", help="disconnect a phone")
    p.add_argument("device", help="number from `devices`")
    sub.add_parser("claim", help="print a code to make a messaging account an owner")
    p = sub.add_parser("owners", help="list owners, or `owners remove <n>`")
    p.add_argument("action", nargs="?", choices=["remove"])
    p.add_argument("index", nargs="?")
    sub.add_parser("status", help="is Fleet ready?")
    sub.add_parser("backend-status", help="check the backend idle state through the local connector")
    from .reporting_setup import add_arguments as reporting_arguments
    reporting_arguments(sub.add_parser("reporting", help="inspect, configure or migrate read-only reporting"))

class _Run:
    def __init__(self, home, control_fn, out, stdin_isatty, input_fn, sleep, clock, which, runner):
        self.home, self.call, self.out = home, control_fn, out
        self.interactive, self.input = stdin_isatty, input_fn
        self.sleep, self.clock, self.which, self.runner = sleep, clock, which, runner

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def ask(self, action: str, **extra):
        try:
            return self.call(self.home, {"action": action, "approver": dict(C.LOCAL_APPROVER), **extra})
        except Exception:
            return None

    def simple(self, action: str, **extra) -> int:
        resp = self.ask(action, **extra)
        if resp is None:
            self.say(C.UNREACHABLE)
            return 1
        self.say(C.describe(action, resp, local=True))
        return 0 if resp.get("ok") else 1

    def code_cmd(self, action: str, parts) -> int:
        code = C.normalise_code(" ".join(parts))
        if code is None:
            self.say(f"Usage: hermes fleet {action} <code>  (the 6-digit code your iPhone shows)")
            return 2
        return self.simple(action, code=code)

    def pair(self) -> int:
        resp = self.ask("pair")
        if resp is None:
            self.say(C.UNREACHABLE)
            return 1
        link = resp.get("link") if resp.get("ok") else None
        if not isinstance(link, str) or not link:
            self.say(C.describe("pair", resp, local=True))
            return 1
        self.say("Open this link on your iPhone (it works once and expires in 10 minutes):")
        self.say(link)
        if self.which("qrencode"):
            try:
                # The link goes over stdin only, never argv.
                self.runner(["qrencode", "-t", "ANSIUTF8"], input=link.encode(), timeout=10, check=False)
            except Exception:
                pass
        if not self.interactive:
            self.say("Then approve the code Fleet shows with `hermes fleet approve <code>` "
                     "or `/fleet approve <code>` in a private chat with Hermes.")
            return 0
        self.say("Waiting for your iPhone (Ctrl-C to stop)...")
        seen: set[str] = set()
        deadline = self.clock() + PAIR_WAIT_SECONDS
        try:
            while self.clock() < deadline:
                pending = self.ask("pending")
                if pending is None:
                    self.say(C.UNREACHABLE)
                    return 1
                for req in (pending.get("requests") or []) if pending.get("ok") else []:
                    code = req.get("match_code") if isinstance(req, dict) else None
                    if not isinstance(code, str) or code in seen:
                        continue
                    seen.add(code)
                    name = C._text(req.get("device_name"), 100) or "iPhone"
                    model = C._text(req.get("device_model"), 60)
                    label = f"{name} ({model})" if model else name
                    try:
                        answer = self.input(f"iPhone request: {label} · code {C.group_code(code)}. Approve? [y/N] ")
                    except EOFError:
                        self.say(f"No answer. Approve with `hermes fleet approve {code}`.")
                        return 0
                    approve = answer.strip().lower() in ("y", "yes")
                    result = self.ask("approve" if approve else "deny", code=code)
                    if result is None:
                        self.say(C.UNREACHABLE)
                        return 1
                    self.say(C.describe("approve" if approve else "deny", result, local=True))
                    if approve and result.get("ok"):
                        return 0
                self.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            self.say("\nStopped waiting. Pending requests can still be approved with `hermes fleet approve <code>`.")
            return 130
        self.say("Timed out after 10 minutes. Run `hermes fleet pair` for a new link.")
        return 1

    def claim(self) -> int:
        resp = self.ask("claim_start")
        if resp is None:
            self.say(C.UNREACHABLE)
            return 1
        code = resp.get("code") if resp.get("ok") else None
        if not isinstance(code, str) or not code:
            self.say(C.describe("claim_start", resp, local=True))
            return 1
        self.say(f"Claim code: {C.group_code(code)}")
        self.say(f"In a private chat with Hermes on any platform, send: /fleet claim {code} (valid 10 minutes)")
        return 0

    def owners(self, action, index) -> int:
        if action == "remove":
            if not (index or "").isdigit():
                self.say("Usage: hermes fleet owners remove <n>")
                return 2
            return self.simple("owners_remove", index=int(index))
        return self.simple("owners")

def run(args, *, home: Path | None = None, control_fn=None, out=None, stdin_isatty=None,
        input_fn=input, sleep=time.sleep, clock=time.monotonic, which=shutil.which,
        runner=subprocess.run) -> int:
    if home is None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    if stdin_isatty is None:
        stdin_isatty = sys.stdin.isatty()
    r = _Run(home, control_fn or _control, out or sys.stdout, stdin_isatty, input_fn,
             sleep, clock, which, runner)
    cmd = getattr(args, "fleet_command", None)
    if cmd == "reporting":
        from .reporting_setup import run as reporting_run
        return reporting_run(args, home, out or sys.stdout)
    if cmd == "backend-status":
        response = r.ask("backend_maintenance", local_operator=True)
        if response is None or response.get("ok") is not True:
            r.say("Backend idle state unavailable; backend left running.")
            return 1
        state = "idle" if response.get("idle") is True else "busy" if response.get("idle") is False else "unknown"
        r.say(f"Backend {response['pid']}: {state}.")
        return 0
    if cmd == "pair":
        return r.pair()
    if cmd in ("approve", "deny"):
        return r.code_cmd(cmd, args.code)
    if cmd in ("devices", "status"):
        return r.simple(cmd)
    if cmd == "revoke":
        d = str(args.device)
        return r.simple("revoke", device=int(d) if d.isdigit() else d)
    if cmd == "claim":
        return r.claim()
    if cmd == "owners":
        return r.owners(getattr(args, "action", None), getattr(args, "index", None))
    r.say("Usage: hermes fleet pair | approve <code> | deny <code> | devices | revoke <n> | claim | owners [remove <n>] | status")
    return 2

def handler(args) -> None:
    code = run(args)
    if code:
        raise SystemExit(code)

def register_cli(ctx) -> None:
    ctx.register_cli_command(
        "fleet", help="Connect Hermes Fleet phones (pair, approve, devices, claim, status)",
        setup_fn=add_arguments, handler_fn=handler,
        description="Connect Hermes Fleet phones from this computer.")
