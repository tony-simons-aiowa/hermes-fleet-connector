"""Dependency contract and runtime smoke tests for Hermes' web extra."""
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from packaging.requirements import Requirement
from packaging.version import Version

from fleet_connector import VERSION
from fleet_connector.service import Connector, make_app
from fleet_connector.settings import private_json, root


def test_uvicorn_requirement_accepts_current_hermes_web_pin():
    manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    match = re.search(r'^\s*-\s*"(uvicorn[^"]+)"\s*$', manifest, re.MULTILINE)
    assert match

    requirement = Requirement(match.group(1))
    assert Version("0.41.0") in requirement.specifier
    assert Version("0.54.0") in requirement.specifier
    assert Version("0.55.0") not in requirement.specifier


def test_real_uvicorn_server_serves_connector_health(tmp_path):
    with socket.socket() as listener, socket.socket() as backend_listener:
        listener.bind(("127.0.0.1", 0))
        backend_listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        backend_port = backend_listener.getsockname()[1]

    config = {
        "home": str(tmp_path),
        "origin": f"https://fixture.example.test:{port}",
        "owners": [],
        "bot_id": 0,
        "port": port,
        "backend_port": backend_port,
        "display_name": "Uvicorn fixture",
    }
    private_json(root(tmp_path) / "settings.json", config)
    connector = Connector(tmp_path, config, "uvicorn-compat-fixture")
    server = uvicorn.Server(uvicorn.Config(
        make_app(connector),
        host="127.0.0.1",
        port=port,
        proxy_headers=False,
        access_log=False,
        log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    response = None

    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            for _ in range(200):
                try:
                    response = client.get(f"http://127.0.0.1:{port}/fleet/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Uvicorn did not serve the connector health endpoint")

        assert response is not None
        assert response.json() == {
            "connector": VERSION,
            "instance_id": connector.instance,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert not thread.is_alive()
