"""Owned local control socket client shared by the bridge, /fleet command and CLI."""
from __future__ import annotations
import hashlib
import json
import os
import socket
from pathlib import Path

def control(home: Path, payload: dict) -> dict:
    # macOS AF_UNIX sun_path is small; use the same short owned path on both sides.
    path = socket_path(home)
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(25)
        conn.connect(str(path))
        conn.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while b"\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data)

def socket_path(home: Path) -> Path:
    return Path(f"/tmp/hfc-{os.getuid()}-{hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]}") / "control.sock"
