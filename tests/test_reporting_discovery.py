"""Exercise the real Hermes dashboard loader against isolated fixture directories."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_real_hermes_discovery_requires_reporting_consent_and_preserves_endpoint(tmp_path, monkeypatch):
    from hermes_cli import web_server_dashboard as dashboard
    from hermes_cli import plugin_isolation
    root = tmp_path / "plugins"
    package = root / "hermes-fleet-connector"
    source = Path(__file__).parents[1]
    shutil.copytree(source / "dashboard", package / "dashboard")
    shutil.copytree(source / "fleet_connector", package / "fleet_connector")
    monkeypatch.setattr(dashboard, "_dashboard_plugin_search_dirs", lambda: [(root, "user")])
    monkeypatch.setattr(plugin_isolation, "isolation_mode", lambda: "in_process")
    found = dashboard._discover_dashboard_plugins()
    assert len(found) == 1 and found[0]["name"] == "fleet-liveops"
    app = FastAPI()
    assert not dashboard._mount_one_plugin_api(app, found[0], {"hermes-fleet-connector"}, set())
    assert not dashboard._mount_one_plugin_api(app, found[0], {"fleet-liveops"}, {"fleet-liveops"})
    assert dashboard._mount_one_plugin_api(app, found[0], {"fleet-liveops"}, set())
    module = sys.modules["hermes_dashboard_plugin_fleet-liveops"]._module
    # The real loader imports our wrapper and composes its APIRouter lifespan.
    # Synthetic registry output keeps this test independent of running agents.
    monkeypatch.setattr(module, "reporting_directory", lambda: tmp_path)
    monkeypatch.setattr(module, "runtime_policy", lambda: ("healthy", None))
    monkeypatch.setattr(module, "launch_home", lambda: tmp_path)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    async def publisher(root):
        module.publish(root, module.process_report([], module.time.time()))
        await asyncio.Future()
    monkeypatch.setattr(module, "publisher_loop", publisher)
    # Generic scope/auth remains Hermes-owned; the connector's real device gate
    # is tested independently in test_reporting_proxy.py.
    app.dependency_overrides[dashboard._plugin_route_secret_scope] = lambda: None
    with TestClient(app) as client:
        value = client.get("/api/plugins/fleet-liveops/snapshot").json()
        assert value["schema"] == 1 and value["publishers"] == 1
        assert value["reporting"]["state"] == "healthy"
        assert client.post("/api/plugins/fleet-liveops/push/respond").status_code == 404
    assert not (tmp_path / f"{module.BOOT_ID}.json").exists()


def test_real_discovery_legacy_namespace_conflict_is_removed_only_outside_plugin_root(tmp_path, monkeypatch):
    from hermes_cli import web_server_dashboard as dashboard
    root = tmp_path / "plugins"
    for name in ("fleet-liveops", "hermes-fleet-connector"):
        path = root / name / "dashboard"
        path.mkdir(parents=True)
        (path / "manifest.json").write_text(json.dumps({"name": "fleet-liveops", "api": "plugin_api.py"}))
        (path / "plugin_api.py").write_text("# synthetic marker\n")
    monkeypatch.setattr(dashboard, "_dashboard_plugin_search_dirs", lambda: [(root, "user")])
    found = dashboard._discover_dashboard_plugins()
    assert len(found) == 1 and Path(found[0]["_dir"]).parent.name == "fleet-liveops"
    (root / "fleet-liveops").rename(root / "aaa-legacy-backup")
    found = dashboard._discover_dashboard_plugins()
    assert Path(found[0]["_dir"]).parent.name == "aaa-legacy-backup"
    (root / "aaa-legacy-backup").rename(tmp_path / "legacy-backup")
    found = dashboard._discover_dashboard_plugins()
    assert len(found) == 1 and Path(found[0]["_dir"]).parent.name == "hermes-fleet-connector"
