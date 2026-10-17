"""Observation-only, expiring snapshots. No controls, hooks, or new listener.

The dashboard component has its own consent name, fleet-liveops. Enabling it
does not register the connector's commands/auth provider in another profile.
Runtime internals are a checked optional capability, never a core override.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from fastapi import APIRouter
from .compat import require_reporting_apis

PLUGIN = "hermes-fleet-connector"
CONSENT = "fleet-liveops"
TTL_SECONDS = 8
MAX_BYTES = 2_000_000
MAX_FILES = 256
MAX_PUBLISHERS = 64
MAX_SESSIONS = 200
MAX_CHILDREN = 500
BOOT_ID = uuid.uuid4().hex
RESERVED_FILES = frozenset({"push.json", "push-tokens.json"})
SESSION_FIELDS = ("session_key", "title", "preview", "model", "started_at",
                  "last_active", "message_count", "status")
CHILD_FIELDS = ("subagent_id", "parent_id", "depth", "goal", "model",
                "started_at", "status", "tool_count", "last_tool")
STATES = frozenset({"healthy", "disabled", "unsupported", "unavailable", "stale", "restart_required"})
REASONS = frozenset({"profile_disabled", "host_isolation", "session_limit", "multiplexed_profiles",
    "ambiguous_session_owner", "subagent_limit", "publisher_limit", "invalid_report", "publisher_stalled",
    "no_publishers", "legacy_publishers", "runtime_api", "secondary_profile_host", "reporting_unavailable",
    "route_not_loaded"})


def diagnostic(state, reason=None):
    result = {"version": 1, "provider": PLUGIN, "state": state if state in STATES else "unavailable"}
    if reason:
        result["reason"] = reason if isinstance(reason, str) and reason in REASONS else "invalid_report"
    return result


def empty_snapshot(state, reason=None):
    return {"schema": 1, "publishers": 0, "stale_publishers": 0,
            "sessions": [], "reporting": diagnostic(state, reason)}


def runtime_policy():
    """Read effective Hermes consent/isolation, including managed policy."""
    from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
    from hermes_cli.plugin_isolation import isolation_mode
    if CONSENT in _get_disabled_set() or CONSENT not in _get_enabled_set():
        return "disabled", "profile_disabled"
    if isolation_mode() == "host" or os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return "unsupported", "host_isolation"
    return "healthy", None


def launch_home():
    try:
        from hermes_cli.config import get_process_hermes_home
    except ImportError:
        from hermes_constants import get_hermes_home as get_process_hermes_home
    return Path(get_process_hermes_home()).resolve()


def reporting_directory():
    from hermes_constants import get_default_hermes_root
    root = Path(get_default_hermes_root()) / CONSENT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Unsafe reporting directory")
    root.chmod(0o700)
    return root


def collect_process_snapshot(server, registry, now, home):
    """Only this backend's launch profile; multiplexed secondary homes fail closed.

    Never ask a plugin host for its empty registry, or call session activation,
    controls, inference, or a database-recency heuristic. Unknown secondary
    profile scope makes coverage incomplete instead of disclosing its rows.
    """
    try:
        require_reporting_apis(server, registry)
    except AttributeError:
        return process_report([], now, "unsupported", "runtime_api")
    home = Path(home).resolve()
    with server._sessions_lock:
        live = [(sid, dict(session)) for sid, session in server._sessions.items()
                if not session.get("_finalized")]
    if len(live) > MAX_SESSIONS:
        return process_report([], now, "unavailable", "session_limit")
    if any(session.get("profile_home") and Path(session["profile_home"]).resolve() != home
           for _, session in live):
        return process_report([], now, "unsupported", "multiplexed_profiles")
    items = [(sid, server._session_live_item(sid, session)) for sid, session in live]
    keys = [item.get("session_key") for _, item in items]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        return process_report([], now, "unavailable", "ambiguous_session_owner")
    children = registry.list_active_subagents()
    if len(children) > MAX_CHILDREN:
        return process_report([], now, "unavailable", "subagent_limit")
    by_owner = {}
    for child in children:
        owner = child.get("owner_agent_session_id")
        if owner in keys:
            by_owner.setdefault(owner, []).append(
                {key: child[key] for key in CHILD_FIELDS if key in child})
    rows = []
    for sid, item in items:
        row = {key: item[key] for key in SESSION_FIELDS if key in item}
        row["id"] = f"fleet:{BOOT_ID}:{sid}"
        row["subagents"] = by_owner.get(item["session_key"], [])
        rows.append(row)
    return process_report(rows, now)


def process_report(rows, now, state="healthy", reason=None):
    return {"schema": 1, "pid": os.getpid(), "written_at": now,
            "reporter": PLUGIN, "sessions": rows,
            "reporting": diagnostic(state, reason)}


def publish(root, payload, publisher=BOOT_ID):
    data = json.dumps(payload, allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("Reporting snapshot too large")
    temp = root / f".{publisher}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        temp.replace(root / f"{publisher}.json")
    finally:
        temp.unlink(missing_ok=True)


def publisher_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_report(path, now):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_BYTES:
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return None
    report = json.loads(data)
    if not isinstance(report, dict) or report.get("schema") != 1:
        return None
    written = report.get("written_at")
    if (not isinstance(written, (float, int)) or isinstance(written, bool)
            or not math.isfinite(written) or written > now):
        return None
    if type(report.get("pid")) is not int or report["pid"] <= 0:
        return None
    if not isinstance(report.get("sessions"), list):
        return None
    if "reporting" in report and not isinstance(report["reporting"], dict):
        return None
    return report


def aggregate(root, now):
    """Accept old schema-1 publishers too; prefer one combined report per PID.

    A leftover legacy file from the same live process cannot double its rows or
    conceal a new reporter's unsupported state. Never dedupe across processes
    by durable session key: different profiles may legitimately share a key.
    """
    reports = {}
    for index, path in enumerate(sorted(root.glob("*.json"))):
        if index >= MAX_FILES:
            return empty_snapshot("unavailable", "publisher_limit")
        if path.name in RESERVED_FILES:
            continue
        try:
            report = _read_report(path, now)
            if report is None:
                continue
            old = reports.get(report["pid"])
            rank = lambda p: (p.get("reporter") == PLUGIN, p["written_at"])
            if old is None or rank(report) > rank(old):
                reports[report["pid"]] = report
        except (OSError, ValueError, TypeError):
            continue
    rows, publishers, stale, legacy = [], 0, 0, 0
    problems = []
    for report in reports.values():
        age = now - report["written_at"]
        if age > TTL_SECONDS:
            stale += int(publisher_alive(report["pid"]))
            continue
        status = report.get("reporting", {})
        if len(report["sessions"]) > MAX_SESSIONS:
            problems.append(diagnostic("unavailable", "session_limit"))
            continue
        if status.get("state", "healthy") != "healthy":
            problems.append(status)
            continue
        publishers += 1
        legacy += int(report.get("reporter") != PLUGIN)
        for row in report["sessions"][:MAX_SESSIONS]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                problems.append(diagnostic("unavailable", "invalid_report"))
                continue
            try:
                activity = float(row.get("last_active") or 0)
            except (TypeError, ValueError):
                problems.append(diagnostic("unavailable", "invalid_report"))
                continue
            if row["id"].startswith("fleet:") and math.isfinite(activity):
                # Reapply the allowlist even to legacy files; never relay handles.
                clean = {key: row[key] for key in ("id", *SESSION_FIELDS) if key in row}
                children = row.get("subagents")
                if not isinstance(children, list) or len(children) > MAX_CHILDREN:
                    problems.append(diagnostic("unavailable", "invalid_report"))
                    continue
                clean["subagents"] = [{k: child[k] for k in CHILD_FIELDS if k in child}
                                      for child in children[:MAX_CHILDREN] if isinstance(child, dict)]
                rows.append(clean)
            else:
                problems.append(diagnostic("unavailable", "invalid_report"))
    if publishers > MAX_PUBLISHERS:
        return empty_snapshot("unavailable", "publisher_limit")
    if stale:
        state = diagnostic("stale", "publisher_stalled")
    elif problems:
        state = diagnostic("unsupported" if all(p.get("state") == "unsupported" for p in problems)
                           else "unavailable", problems[0].get("reason", "invalid_report"))
    elif legacy:
        state = diagnostic("restart_required", "legacy_publishers")
    elif publishers:
        state = diagnostic("healthy")
    else:
        state = diagnostic("restart_required", "no_publishers")
    # DEV19 must also see incomplete coverage, not a healthy partial/empty fleet.
    if problems:
        publishers, rows = 0, []
    rows.sort(key=lambda row: (row.get("status") == "idle" and not row.get("subagents"),
                              -float(row.get("last_active") or 0), row["id"]))
    if len(rows) > MAX_SESSIONS:
        return empty_snapshot("unavailable", "session_limit")
    return {"schema": 1, "publishers": publishers, "stale_publishers": stale,
            "sessions": rows, "reporting": state}


async def publisher_loop(root):
    while True:
        try:
            state, reason = runtime_policy()
            if state == "disabled":
                (root / f"{BOOT_ID}.json").unlink(missing_ok=True)
            elif state != "healthy":
                publish(root, process_report([], time.time(), state, reason))
            else:
                from tui_gateway import server
                from tools import delegate_tool_registry
                payload = await asyncio.to_thread(
                    collect_process_snapshot, server, delegate_tool_registry, time.time(), launch_home())
                publish(root, payload)
        except (ImportError, AttributeError):
            publish(root, process_report([], time.time(), "unsupported", "runtime_api"))
        except Exception:
            # The previous report expires; never log session content or pretend idle.
            pass
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    root = reporting_directory()
    # Shared across duplicate module imports in one process, not across profiles/processes.
    # The FD lock is released by the OS on a crash; no stale lock deletion or takeover.
    import fcntl
    lock = root / f".publisher-{os.getpid()}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    task = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield
            return
        task = asyncio.create_task(publisher_loop(root))
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            (root / f"{BOOT_ID}.json").unlink(missing_ok=True)
        os.close(fd)


router = APIRouter(lifespan=lifespan)


@router.get("/snapshot")
async def snapshot():
    # Existing Hermes dashboard authentication/consent gates remain outside this router.
    try:
        state, reason = runtime_policy()
        if state != "healthy":
            return empty_snapshot(state, reason)
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()).resolve() != launch_home():
            return empty_snapshot("unsupported", "secondary_profile_host")
        return await asyncio.to_thread(aggregate, reporting_directory(), time.time())
    except (ImportError, AttributeError):
        return empty_snapshot("unsupported", "runtime_api")
    except Exception:
        return empty_snapshot("unavailable", "reporting_unavailable")
