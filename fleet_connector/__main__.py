"""Explicit local setup, supervision, status, observation and scoped recovery."""
from __future__ import annotations
import argparse
import json
import os
import hashlib
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from .settings import NAME, load, root, normalise_origin, private_json
from . import VERSION
from .bridge import control

def check_stock(path: Path):
    """Check the public Hermes APIs, without requiring Git metadata or a release label."""
    source = path.resolve(strict=True)
    plugin_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(source), str(plugin_root))))
    result = subprocess.run(
        [sys.executable, "-c",
         "from fleet_connector.compat import require_hermes_apis; require_hermes_apis()"],
        env=env, cwd=source, capture_output=True, text=True, timeout=15)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else "required APIs could not be imported"
        raise ValueError(f"Hermes host does not expose required plugin capabilities: {reason}")
    return source

def check_policy(home: Path, source: Path):
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(source))
    result = subprocess.run([sys.executable, "-c",
        "import hermes_bootstrap; from hermes_cli.plugin_isolation import isolation_mode; print(isolation_mode())"],
        env=env, cwd=source, check=True, capture_output=True, text=True, timeout=15)
    if result.stdout.strip() != "in_process":
        raise ValueError("Connector service is incompatible with effective host isolation; native dashboard auth requires in_process and policy is preserved")

def configure(home: Path, args):
    backend_mode = getattr(args, "backend_mode", "owned_stock")
    source = (check_existing_backend(Path(args.hermes_root)) if backend_mode == "existing_gated"
              else check_stock(Path(args.hermes_root)))
    check_policy(home, source)
    owners, bot_id = args.owner, args.bot_id
    if args.from_telegram_request:
        request = json.loads((root(home) / "setup-request.json").read_text())
        if request["home"] != str(home) or time.time() >= request["expires_at"]:
            raise ValueError("Fresh genuine Telegram request required")
        owners, bot_id = [request["owner"]], request["bot_id"]
    telegram = bool(owners) or bot_id is not None
    if telegram and not getattr(args, "approve_owner_binding", False):
        raise ValueError("Telegram owner binding requires --approve-owner-binding")
    if telegram and (not owners or bot_id is None):
        raise ValueError("Telegram setup needs both an owner and a bot ID (or a fresh Telegram setup request)")
    cfg = {"home": str(home), "origin": normalise_origin(args.origin)}
    if telegram:
        cfg.update(owners=owners, bot_id=bot_id)
    cfg.update(port=args.port, backend_port=args.backend_port,
               hermes_root=str(source), display_name="Hermes Fleet", version=VERSION)
    if backend_mode == "existing_gated":
        cfg["backend_mode"] = backend_mode
    path = root(home) / "settings.json"
    if path.exists():
        if load(home) != cfg:
            raise ValueError("Existing connector configuration differs; preserve it and review changes first")
        return {"ok": True, "message": "Identical connector settings already installed."}
    if telegram and (any(type(v) is not int or v <= 0 for v in cfg["owners"]) or cfg["bot_id"] <= 0):
        raise ValueError("Numeric owner and bot IDs required")
    if args.port == args.backend_port or not all(1024 <= p <= 65535 for p in (args.port,args.backend_port)):
        raise ValueError("Distinct unprivileged ports required")
    private_json(path, cfg)
    return {"ok": True, "message": "Connector settings prepared. No service activated or invitation issued."}

def prepare_host(home: Path, args):
    source = check_existing_backend(Path(args.hermes_root))
    check_policy(home, source)
    if (root(home) / "settings.json").exists():
        raise ValueError("Existing owner configuration must be preserved")
    cfg = {"home": str(home), "origin": normalise_origin(args.origin), "owners": [], "bot_id": 0,
        "port": args.port, "backend_port": args.backend_port, "backend_mode": "existing_gated",
        "hermes_root": str(source), "display_name": "Hermes Fleet", "version": VERSION}
    if args.port == args.backend_port or not all(1024 <= p <= 65535 for p in (args.port,args.backend_port)):
        raise ValueError("Distinct unprivileged ports required")
    path = root(home) / "host-plan.json"
    if path.exists() and json.loads(path.read_text()) != cfg:
        raise ValueError("Different host plan already exists")
    private_json(path, cfg)
    return {"ok": True, "message": "Host plan prepared with no owner and no issuance authority."}

def pending_host(home: Path):
    cfg = json.loads((root(home) / "host-plan.json").read_text())
    if (cfg["home"] != str(home) or cfg.get("owners") != [] or cfg.get("bot_id") != 0
            or cfg.get("backend_mode") != "existing_gated"):
        raise ValueError("Invalid pending host plan")
    cfg["origin"] = normalise_origin(cfg["origin"])
    return cfg

def check_existing_backend(path: Path):
    return check_stock(path)

def serve(home: Path, cfg: dict):
    if not sys.version_info >= (3,14):
        raise ValueError("Use stock Hermes's Python 3.14 runtime")
    existing = cfg.get("backend_mode") == "existing_gated"
    source = (check_existing_backend(Path(cfg["hermes_root"])) if existing
              else check_stock(Path(cfg["hermes_root"])))
    check_policy(home, source)
    # Check both ports before launching; a taken port fails without stopping any service.
    for port in ((cfg["port"],) if existing else (cfg["port"], cfg["backend_port"])):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    import secrets, uvicorn
    from .service import Connector, make_app, start_control
    backend_token = ("hfcproxy." if existing else "") + secrets.token_urlsafe(32)
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(source),
               HERMES_DASHBOARD_SESSION_TOKEN=backend_token)
    # Do not inherit Desktop lifecycle ownership, account onboarding or agent approval bypasses.
    for key in list(env):
        if key.startswith("HERMES_DESKTOP") or key in {"HERMES_GUEST_ONBOARDING", "HERMES_YOLO", "HERMES_NON_INTERACTIVE"}:
            env.pop(key, None)
    process = None if existing else subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "serve", "--host", "127.0.0.1",
                                "--port", str(cfg["backend_port"]), "--isolated", "--skip-build"],
                                env=env, cwd=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    c = Connector(home, cfg, backend_token)
    server = uvicorn.Server(uvicorn.Config(make_app(c), host="127.0.0.1", port=cfg["port"],
        proxy_headers=False, access_log=False, log_level="error", ws_max_size=8*1024*1024))
    control_server = None
    path = None
    backend_died = threading.Event()
    # Uvicorn re-emits captured signals to the previous handler after shutdown.
    # Install an owner handler so that re-emission reaches our finally block.
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    lease_stop = threading.Event()
    lease_thread = None
    def stop_owned(_signal, _frame):
        server.should_exit = True
    for sig in previous_signals:
        signal.signal(sig, stop_owned)
    try:
        if existing:
            import hashlib
            def publish_lease():
                private_json(root(home) / "backend-lease.json", {"home": str(home), "pid": os.getpid(),
                    "expires_at": time.time() + 35, "token_hash": hashlib.sha256(backend_token.encode()).hexdigest()})
            publish_lease()
            def renew_lease():
                while not lease_stop.wait(5):
                    try:
                        publish_lease()
                    except OSError:
                        server.should_exit = True
                        return
            lease_thread = threading.Thread(target=renew_lease, daemon=True)
            lease_thread.start()
        control_server, path = start_control(c)
        private_json(root(home) / "process.json", {"pid": os.getpid(), "backend_pid": process.pid if process else None,
            "backend_owned": not existing, "version": VERSION})
        def watch():
            process.wait()
            backend_died.set()
            server.should_exit = True
        if process:
            threading.Thread(target=watch, daemon=True).start()
        server.run()
        if backend_died.is_set():
            raise ValueError("Stock child exited; connector supervisor must restart")
    finally:
        c.shutdown_display()
        if control_server:
            control_server.shutdown()
            control_server.server_close()
        if path:
            path.unlink(missing_ok=True)
        lease_stop.set()
        if lease_thread:
            lease_thread.join(timeout=2)
            (root(home) / "backend-lease.json").unlink(missing_ok=True)
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        (root(home) / "process.json").unlink(missing_ok=True)
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)

def prepare_launchd(home: Path, cfg: dict):
    # Plist is a reviewable deliverable in connector data. No launchctl call is made here.
    import plistlib
    label = "dev.aiowa.fleet-connector." + hashlib.sha256(str(home).encode()).hexdigest()[:12]
    directory = root(home)
    value = {"Label": label,
        "ProgramArguments": [sys.executable, "-m", "fleet_connector", "--home", str(home), "serve", "--approve-activation"],
        "EnvironmentVariables": {"PYTHONPATH": str(Path(__file__).parent.parent), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
        "ProcessType": "Interactive", "Umask": 63,
        "StandardOutPath": str(directory / "supervisor.log"),
        "StandardErrorPath": str(directory / "supervisor-error.log")}
    # Only the staged host plan (owners == [] and bot_id == 0) needs --pending-setup. A
    # Telegram-free settings.json has neither key and is a complete configuration.
    if cfg.get("owners") == [] and cfg.get("bot_id") == 0:
        value["ProgramArguments"].append("--pending-setup")
    path = directory / "service.plist"
    if path.exists() and plistlib.loads(path.read_bytes()) != value:
        raise ValueError("Existing service plist differs; preserved for review")
    path.write_bytes(plistlib.dumps(value))
    path.chmod(0o600)
    return {"ok": True, "label": label, "plist": str(path), "message": "Supervisor prepared; not loaded."}

def _systemd_quote(value: str, *, exec_word: bool = True) -> str:
    """Quote one unit token; ExecStart expands dollar variables, Environment does not."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control characters are not supported in systemd service paths")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    if exec_word:
        value = value.replace("$", "$$")
    value = value.replace("%", "%%")
    return '"' + value + '"'

def _systemd_user_unit_content(home: Path, cfg: dict, label: str) -> str:
    args = [sys.executable, "-m", "fleet_connector", "--home", str(home),
            "serve", "--approve-activation"]
    if not cfg["owners"]:
        args.append("--pending-setup")
    lines = [
        "[Unit]",
        f"Description=Hermes Fleet connector ({label})",
        "After=graphical-session.target",
        "",
        "[Service]",
        "Type=simple",
        "UMask=0077",
        f"Environment={_systemd_quote('PYTHONPATH=' + str(Path(__file__).parent.parent), exec_word=False)}",
        f"Environment={_systemd_quote('PATH=' + os.environ.get('PATH', '/usr/bin:/bin'), exec_word=False)}",
        "ExecStart=" + " ".join(_systemd_quote(part) for part in args),
        "Restart=on-failure",
        "RestartSec=30",
        "",
    ]
    return "\n".join(lines)

def prepare_systemd_user(home: Path, cfg: dict):
    """Write a reviewable per-profile user unit; never load or start it."""
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        raise ValueError("Linux user-service support requires systemctl")
    label = "hermes-fleet-connector-" + hashlib.sha256(str(home).encode()).hexdigest()[:12]
    directory = root(home)
    if directory.is_symlink():
        raise ValueError("Symlinked connector state refused")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / (label + ".service")
    if path.is_symlink():
        raise ValueError("Symlinked service unit refused")
    content = _systemd_user_unit_content(home, cfg, label)
    if path.exists():
        if path.read_text() != content:
            raise ValueError("Existing systemd unit differs; preserved for review")
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as unit:
            unit.write(content)
            unit.flush()
            os.fsync(unit.fileno())
    path.chmod(0o600)
    return {"ok": True, "label": label, "unit": str(path),
            "message": "User service unit prepared for review; not linked, loaded or started."}

def prepare_service(home: Path, cfg: dict):
    if sys.platform == "darwin":
        return prepare_launchd(home, cfg)
    if sys.platform.startswith("linux"):
        return prepare_systemd_user(home, cfg)
    raise ValueError("Service supervision is supported only on macOS and Linux")

def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, help="Exact selected Hermes profile directory")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--hermes-root", required=True)
    setup.add_argument("--origin", required=True)
    setup.add_argument("--owner", type=int, action="append")
    setup.add_argument("--bot-id", type=int)
    setup.add_argument("--from-telegram-request", action="store_true")
    setup.add_argument("--approve-owner-binding", action="store_true",
                       help="required only with --owner/--bot-id/--from-telegram-request")
    setup.add_argument("--port", type=int, default=9129)
    setup.add_argument("--backend-port", type=int, default=9130)
    setup.add_argument("--backend-mode", choices=["owned_stock", "existing_gated"], default="owned_stock")
    plan = commands.add_parser("prepare-host", help="Stage an existing backend with no owner/issuance authority")
    plan.add_argument("--hermes-root", required=True)
    plan.add_argument("--origin", required=True)
    plan.add_argument("--port", type=int, default=9129)
    plan.add_argument("--backend-port", type=int, default=9119)
    commands.add_parser("status")
    commands.add_parser("backend-status", help="Inspect backend identity and idle state through the private owner socket")
    service = commands.add_parser("prepare-service")
    service.add_argument("--pending-setup", action="store_true")
    launch = commands.add_parser("serve")
    launch.add_argument("--approve-activation", action="store_true", required=True)
    launch.add_argument("--pending-setup", action="store_true")
    approve = commands.add_parser("observe")
    approve.add_argument("--phone-tls-verified", action="store_true", required=True)
    approve.add_argument("--private-display-verified", action="store_true", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("device_id")
    revoke.add_argument("--approve-revocation", action="store_true", required=True)
    commands.add_parser("rollback", help="Remove prepared supervision/observations only; retains devices, settings and replay records")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve(strict=True)
    if args.command == "setup":
        result = configure(home, args)
    elif args.command == "prepare-host":
        result = prepare_host(home, args)
    else:
        cfg = pending_host(home) if getattr(args, "pending_setup", False) else load(home)
        if args.command == "serve":
            serve(home, cfg)
            return 0
        if args.command == "prepare-service":
            result = prepare_service(home, cfg)
        elif args.command == "rollback":
            # Do not stop a service based on a stale PID; the owner stops/unloads its exact manager unit first.
            try:
                control(home, {"action": "status", "local_operator": True})
            except (OSError, ValueError):
                pass
            else:
                raise ValueError("Stop the exact connector service before rollback")
            unit_label = "hermes-fleet-connector-" + hashlib.sha256(str(home).encode()).hexdigest()[:12]
            unit_path = root(home) / (unit_label + ".service")
            if unit_path.is_symlink():
                raise ValueError("Symlinked systemd unit refused during rollback")
            if unit_path.exists() and unit_path.read_text() != _systemd_user_unit_content(home, cfg, unit_label):
                raise ValueError("Edited systemd unit differs; preserved during rollback")
            for name in ("service.plist", "approval.json", "process.json"):
                (root(home) / name).unlink(missing_ok=True)
            unit_path.unlink(missing_ok=True)
            result = {"ok": True, "message": "Prepared supervision removed; pairing data, replay records and settings retained."}
        else:
            payload = {"action": ("approve" if args.command == "observe" else "backend_maintenance" if args.command == "backend-status" else args.command), "local_operator": True}
            if args.command == "observe":
                payload.update(phone_tls_verified=True, private_display_verified=True)
            if args.command == "revoke":
                payload["device_id"] = args.device_id
            result = control(home, payload)
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError):
        # Never emit values of configuration or exception text containing secrets.
        print('{"ok":false,"message":"Connector operation refused; check compatibility, policy, profile, ports and private service readiness."}')
        raise SystemExit(2)
