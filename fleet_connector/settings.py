"""Only connector-owned state is written. Existing Hermes configuration is read-only."""
from __future__ import annotations
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

NAME = "hermes-fleet-connector"
STOCK_SHA = "818c13be1dc4fd28987e1e881a9408224afd4535"
AUDITED_GATEWAY_SHA = "ecd22716c35c6993797808cfd9c05feb8fa0193f"

def root(home: Path) -> Path:
    return home.resolve() / "plugin-data" / NAME

def normalise_origin(raw: str) -> str:
    p = urlsplit(raw)
    host = p.hostname or ""
    if (p.scheme != "https" or p.username or p.password or p.path not in ("", "/")
            or p.query or p.fragment or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
            or "." not in host or host.endswith(".localhost") or host == "localhost"
            or any(not x or x.startswith("-") or x.endswith("-") for x in host.split("."))):
        raise ValueError("Trusted HTTPS DNS origin required")
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("DNS hostname required")
    port = p.port
    return "https://" + host.lower() + (f":{port}" if port not in (None, 443) else "")

def private_json(path: Path, value: dict) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Symlinked connector state refused")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def load(home: Path) -> dict:
    path = root(home) / "settings.json"
    if path.is_symlink() or root(home).is_symlink():
        raise ValueError("Symlinked connector state refused")
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise ValueError("Connector settings must be owner-only")
    cfg = json.loads(path.read_text())
    if cfg.get("home") != str(home.resolve()):
        raise ValueError("Profile mismatch")
    cfg["origin"] = normalise_origin(cfg["origin"])
    if (not isinstance(cfg.get("owners"), list) or not cfg["owners"]
            or any(type(v) is not int or v <= 0 for v in cfg["owners"])
            or type(cfg.get("bot_id")) is not int or cfg["bot_id"] <= 0):
        raise ValueError("Exact numeric owner and bot account required")
    ports = (cfg.get("port"), cfg.get("backend_port"))
    if any(type(x) is not int or not 1024 <= x <= 65535 for x in ports) or ports[0] == ports[1]:
        raise ValueError("Distinct unprivileged ports required")
    return cfg
