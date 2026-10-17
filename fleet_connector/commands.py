"""Platform-neutral ``/fleet`` chat command.

Identity comes only from Hermes' trusted session context (the ``HERMES_SESSION_*``
variables the gateway binds around a plugin command) or from the local CLI/TUI
process. Message text and model output never choose the approver. The service
re-checks ownership; this module only decides *who is asking* and formats replies.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Callable

from .control import control as _control

log = logging.getLogger(__name__)

LOCAL_APPROVER = {"platform": "local", "user_id": "local"}
LOCAL_SOURCES = frozenset({"cli", "tui", "desktop"})

HELP = (
    "Hermes Fleet commands (private chat with Hermes):\n"
    "/fleet pair - get a link for your iPhone\n"
    "/fleet approve <code> - approve the code your iPhone shows\n"
    "/fleet deny <code> - reject a request\n"
    "/fleet devices - list connected phones\n"
    "/fleet revoke <n> - disconnect a phone\n"
    "/fleet claim <code> - become an owner (code from `hermes fleet claim`)\n"
    "/fleet status - is Fleet ready?"
)
NOT_OWNER = ("Only this Hermes owner can do that. On the computer running Hermes, run "
             "`hermes fleet claim` and send the code here as `/fleet claim <code>`.")
UNREACHABLE = ("Fleet connector isn't running on this computer. On the computer running "
               "Hermes, run `hermes fleet status`.")
NO_MATCH = ("No phone is waiting with that code. If your iPhone shows it, it may be connected "
            "to a different server — cancel on the phone and ask for a new link.")
PRIVATE_ONLY = "Use this command in a private chat with Hermes, not in a group or channel."
UNKNOWN_IDENTITY = ("Can't tell who is asking from here. Use a private chat with Hermes, or run "
                    "`hermes fleet` on the computer running Hermes.")
GENERIC_FAIL = "Fleet request refused."
LOCKED = "Too many wrong codes. Try again in 10 minutes."
PAIR_HELP = ("Open it on your iPhone, then send `/fleet approve <code>` with the code Fleet shows. "
             "Works once, expires in 10 minutes.")

def normalise_code(raw: str) -> str | None:
    code = re.sub(r"[\s-]", "", raw or "")
    return code if re.fullmatch(r"[0-9]{6}", code) else None

def group_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code

def _text(value, limit: int = 2000) -> str:
    return value[:limit] if isinstance(value, str) else ""

def describe(action: str, resp: dict | None, *, local: bool = False) -> str:
    """Turn a control response into reply text. Never echoes anything but safe fields."""
    if not isinstance(resp, dict):
        return GENERIC_FAIL
    err = resp.get("error")
    if not resp.get("ok"):
        if err in ("not_owner", "forbidden", "owner_required"):
            return NOT_OWNER
        if err == "no_match":
            return NO_MATCH
        if err == "locked":
            return LOCKED
        return _text(resp.get("message")) or GENERIC_FAIL
    message = _text(resp.get("message"))
    if action == "pair":
        link = _text(resp.get("link"), 4000)
        return f"{link}\n\n{PAIR_HELP}" if link else (message or GENERIC_FAIL)
    if action == "approve":
        device = resp.get("device") if isinstance(resp.get("device"), dict) else {}
        name = _text(device.get("label") or device.get("name") or "the phone", 200)
        return f"Approved {name}. It's connecting now."
    if action == "deny":
        return message or "Denied. That request was rejected."
    if action == "revoke":
        return message or "Revoked. That phone can no longer connect."
    if action == "devices":
        return message or _format_devices(resp.get("devices"))
    return message or "Done."

def _format_devices(devices) -> str:
    if not isinstance(devices, list) or not devices:
        return "No phones are connected yet."
    lines = []
    for i, d in enumerate(devices, 1):
        if not isinstance(d, dict):
            continue
        parts = [_text(d.get("label"), 100) or "Unnamed", _text(d.get("model"), 60)]
        if d.get("added"):
            parts.append(f"added {_text(str(d['added']), 40)}")
        if d.get("last_used"):
            parts.append(f"last used {_text(str(d['last_used']), 40)}")
        lines.append(f"{i}. " + " · ".join(p for p in parts if p))
    return "\n".join(lines) or "No phones are connected yet."

def default_session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return ""
    return get_session_env(name, "") or ""

def requester(env: Callable[[str], str], cli_session: Callable[[], bool]) -> dict:
    """Who is asking, from trusted context only.

    messaging: platform + user id + chat type bound by the gateway.
    local: no messaging identity at all AND a positive local signal (TUI/desktop source
           or the classic CLI registered with the plugin manager). An empty context
           alone (e.g. a plugin-host child process) is *not* local: fail closed.
    """
    platform = env("HERMES_SESSION_PLATFORM").strip().lower()
    user_id = env("HERMES_SESSION_USER_ID").strip()
    if platform and platform not in ("local", "cli", "tui", "desktop"):
        return {"kind": "messaging", "platform": platform, "user_id": user_id,
                "private": env("HERMES_SESSION_CHAT_TYPE").strip().lower() == "dm"}
    if platform or user_id or env("HERMES_SESSION_CHAT_ID").strip():
        return {"kind": "unknown"}
    if env("HERMES_SESSION_SOURCE").strip().lower() in LOCAL_SOURCES or cli_session():
        return {"kind": "local"}
    return {"kind": "unknown"}

def make_handler(home_fn: Callable[[], Path], *, control_fn=None, env_fn=None,
                 cli_session_fn: Callable[[], bool] = lambda: False):
    """Return the sync ``fn(raw_args) -> str`` handler (Hermes runs sync handlers on its pool)."""
    call = control_fn or _control
    env = env_fn or default_session_env

    def ask(payload: dict) -> dict | None:
        try:
            return call(home_fn(), payload)
        except Exception as exc:  # unreachable socket, bad JSON, timeouts
            log.debug("fleet control call failed: %s", type(exc).__name__)
            return None

    def handle(raw_args: str = "") -> str:
        try:
            parts = (raw_args or "").split()
            sub = parts[0].lower() if parts else "help"
            rest = parts[1:]
            if sub not in ("pair", "approve", "deny", "devices", "revoke", "status", "claim"):
                return HELP
            who = requester(env, cli_session_fn)
            if who["kind"] == "unknown":
                return UNKNOWN_IDENTITY
            if who["kind"] == "messaging":
                if not who["private"]:
                    return PRIVATE_ONLY
                if not who["user_id"]:
                    return UNKNOWN_IDENTITY
                approver = {"platform": who["platform"], "user_id": who["user_id"]}
            else:
                approver = dict(LOCAL_APPROVER)

            if sub == "claim":
                if who["kind"] == "local":
                    return "This session is already the owner. Run `hermes fleet claim` in a terminal to add a messaging account."
                code = normalise_code(" ".join(rest))
                if code is None:
                    return "Send `/fleet claim <code>` with the 6-digit code from `hermes fleet claim`."
                resp = ask({"action": "claim", "code": code, "approver": approver,
                            "platform": approver["platform"], "user_id": approver["user_id"]})
                if resp is None:
                    return UNREACHABLE
                if not resp.get("ok"):
                    return _text(resp.get("message")) or "That claim code isn't valid. Run `hermes fleet claim` for a new one."
                return _text(resp.get("message")) or "You're now an owner of this Hermes."

            if sub in ("pair", "devices", "status"):
                action, extra = sub, {}
            elif sub in ("approve", "deny"):
                code = normalise_code(" ".join(rest))
                if code is None:
                    return f"Send `/fleet {sub} <code>` with the 6-digit code your iPhone shows."
                action, extra = sub, {"code": code}
            elif sub == "revoke":
                if len(rest) != 1:
                    return "Send `/fleet revoke <n>` with the number from `/fleet devices`."
                action, extra = "revoke", {"device": int(rest[0]) if rest[0].isdigit() else rest[0]}
            else:
                return HELP
            resp = ask({"action": action, "approver": approver, **extra})
            if resp is None:
                return UNREACHABLE
            return describe(action, resp)
        except Exception:
            return GENERIC_FAIL
    return handle

def register_chat_command(ctx, home_fn: Callable[[], Path]) -> None:
    def cli_session() -> bool:
        # The classic CLI registers itself on the plugin manager; the gateway never does.
        manager = getattr(ctx, "_manager", None)
        return getattr(manager, "_cli_ref", None) is not None
    ctx.register_command(
        "fleet", make_handler(home_fn, cli_session_fn=cli_session),
        description="Connect Hermes Fleet phones: pair, approve, devices",
        args_hint="pair | approve <code> | deny <code> | devices | revoke <n> | claim <code> | status")
