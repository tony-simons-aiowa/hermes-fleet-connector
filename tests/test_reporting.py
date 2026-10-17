"""Synthetic live registries and private files; no real gateway or model calls."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from fleet_connector import reporting as R


def report(pid, boot, now=100, **extra):
    return {"schema": 1, "pid": pid, "written_at": now, "sessions": [{
        "id": f"fleet:{boot}:same-runtime", "session_key": "same-durable-key",
        "status": "working", "last_active": now, "subagents": []}], **extra}


def test_legacy_and_combined_schema_one_and_duplicate_preference(tmp_path):
    R.publish(tmp_path, report(1, "old"), "old")
    R.publish(tmp_path, report(1, "new", reporter=R.PLUGIN), "new")
    R.publish(tmp_path, report(2, "other"), "other")
    value = R.aggregate(tmp_path, 101)
    assert value["schema"] == 1 and value["publishers"] == 2
    assert {v["id"] for v in value["sessions"]} == {"fleet:new:same-runtime", "fleet:other:same-runtime"}
    assert (tmp_path / "new.json").stat().st_mode & 0o777 == 0o600


def test_stale_live_report_cannot_be_hidden_by_idle_process(tmp_path, monkeypatch):
    R.publish(tmp_path, report(1, "stale", 90), "stale")
    R.publish(tmp_path, {**report(2, "idle", reporter=R.PLUGIN), "sessions": []}, "idle")
    monkeypatch.setattr(R, "publisher_alive", lambda pid: pid == 1)
    value = R.aggregate(tmp_path, 101)
    assert value["stale_publishers"] == 1 and value["reporting"]["state"] == "stale"
    monkeypatch.setattr(R, "publisher_alive", lambda pid: False)
    value = R.aggregate(tmp_path, 101)
    assert value["publishers"] == 1 and value["sessions"] == []
    assert value["reporting"]["state"] == "healthy"


def test_unknown_zero_and_corrupt_future_or_link_are_not_healthy(tmp_path):
    assert R.aggregate(tmp_path, 100)["reporting"]["state"] == "restart_required"
    (tmp_path / "broken.json").write_text("{")
    (tmp_path / "push.json").write_text('{"enabled":true}')
    R.publish(tmp_path, report(1, "future", 200), "future")
    (tmp_path / "secret.txt").write_text(json.dumps(report(2, "symlink")))
    (tmp_path / "link.json").symlink_to(tmp_path / "secret.txt")
    assert R.aggregate(tmp_path, 100)["publishers"] == 0


def test_problem_report_cannot_be_hidden_by_healthy_legacy_file(tmp_path):
    R.publish(tmp_path, report(1, "old"), "old")
    R.publish(tmp_path, report(1, "new", reporter=R.PLUGIN,
                             reporting=R.diagnostic("unsupported", "runtime_api")), "new")
    R.publish(tmp_path, report(2, "other"), "other")
    value = R.aggregate(tmp_path, 101)
    assert value["publishers"] == 0 and value["sessions"] == []
    assert value["reporting"]["state"] == "unsupported"


def test_snapshot_caps_never_claim_complete_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_SESSIONS", 1)
    R.publish(tmp_path, report(1, "one"), "one")
    R.publish(tmp_path, report(2, "two"), "two")
    assert R.aggregate(tmp_path, 101)["reporting"]["state"] == "unavailable"


def registry(sessions, child_owner="parent"):
    calls = []
    server = NS(_sessions_lock=threading.RLock(), _sessions=sessions,
                _session_live_item=lambda sid, row: calls.append(sid) or {
                    "session_key": row.get("session_key", "parent"), "status": "idle",
                    "last_active": 100, "transport": "PRIVATE", "cwd": "PRIVATE"})
    children = NS(list_active_subagents=lambda: [{"owner_agent_session_id": child_owner,
        "subagent_id": "child", "goal": "Synthetic work", "agent": "PRIVATE", "owner_transport": "PRIVATE"}])
    return server, children, calls


def test_collector_uses_live_owner_and_strips_private_handles(tmp_path):
    server, children, calls = registry({"runtime": {}, "closed": {"_finalized": True}})
    value = R.collect_process_snapshot(server, children, 100, tmp_path)
    assert calls == ["runtime"]
    assert value["sessions"][0]["subagents"][0]["subagent_id"] == "child"
    assert "PRIVATE" not in json.dumps(value)
    assert "owner_agent_session_id" not in json.dumps(value)


def test_secondary_profile_scope_fails_before_reading_rows(tmp_path):
    server, children, calls = registry({"runtime": {}, "other": {"profile_home": str(tmp_path / "other")}})
    value = R.collect_process_snapshot(server, children, 100, tmp_path)
    assert value["sessions"] == [] and not calls
    assert value["reporting"]["reason"] == "multiplexed_profiles"


def test_ambiguous_durable_ownership_is_unavailable(tmp_path):
    server, children, _ = registry({"one": {}, "two": {}})
    assert R.collect_process_snapshot(server, children, 100, tmp_path)["reporting"]["state"] == "unavailable"


def test_snapshot_router_only_exposes_get_and_has_no_control_route(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "runtime_policy", lambda: ("healthy", None))
    monkeypatch.setattr(R, "launch_home", lambda: tmp_path)
    monkeypatch.setattr(R, "reporting_directory", lambda: tmp_path)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    async def publisher(root):
        R.publish(root, {**R.process_report([], time.time())}, R.BOOT_ID)
        await asyncio.Future()
    monkeypatch.setattr(R, "publisher_loop", publisher)
    app = FastAPI()
    app.include_router(R.router, prefix="/api/plugins/fleet-liveops")
    with TestClient(app) as client:
        value = client.get("/api/plugins/fleet-liveops/snapshot").json()
        assert value["publishers"] == 1 and value["sessions"] == []
        assert client.post("/api/plugins/fleet-liveops/snapshot").status_code == 405
        assert client.post("/api/plugins/fleet-liveops/push/respond").status_code == 404
    assert not (tmp_path / f"{R.BOOT_ID}.json").exists()


def test_duplicate_lifespan_imports_start_only_one_publisher(tmp_path, monkeypatch):
    count = []
    monkeypatch.setattr(R, "reporting_directory", lambda: tmp_path)
    async def publisher(root):
        count.append(1)
        await asyncio.Future()
    monkeypatch.setattr(R, "publisher_loop", publisher)
    async def run():
        async with R.lifespan(None):
            await asyncio.sleep(0)
            async with R.lifespan(None):
                await asyncio.sleep(0)
                assert len(count) == 1
    asyncio.run(run())


def test_dashboard_entry_loads_without_connector_registration():
    path = Path(__file__).parents[1] / "dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("fixture_dashboard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert [(r.path, r.methods) for r in module.router.routes] == [("/snapshot", {"GET"})]


def test_combined_aggregator_signals_old_in_memory_publishers_until_restart(tmp_path):
    R.publish(tmp_path, report(1, "old"), "old")
    value = R.aggregate(tmp_path, 101)
    assert value["publishers"] == 1  # Existing DEV19 can still observe during the transition.
    assert value["reporting"]["state"] == "restart_required"
    assert value["reporting"]["reason"] == "legacy_publishers"


def test_missing_live_api_cannot_be_disguised_by_an_empty_registry(tmp_path):
    server = NS(_sessions={}, _sessions_lock=threading.Lock())
    value = R.collect_process_snapshot(server, NS(list_active_subagents=lambda: []), 100, tmp_path)
    assert value["reporting"]["state"] == "unsupported"


def test_diagnostic_never_relays_arbitrary_file_metadata(tmp_path):
    R.publish(tmp_path, report(1, "bad", reporter=R.PLUGIN,
        reporting={"state": "unsupported", "reason": "PRIVATE-path-or-exception"}), "bad")
    value = R.aggregate(tmp_path, 101)
    assert value["reporting"]["reason"] == "invalid_report"
    assert "PRIVATE" not in json.dumps(value)


def test_real_hermes_runtime_exposes_checked_observation_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [fleet-liveops]\n")
    from tui_gateway import server
    from tools import delegate_tool_registry
    from fleet_connector.compat import require_reporting_apis
    require_reporting_apis(server, delegate_tool_registry)
    value = R.collect_process_snapshot(server, delegate_tool_registry, time.time(), tmp_path)
    assert value["reporting"]["state"] == "healthy"
    assert value["sessions"] == []


def test_effective_consent_and_host_policy_are_preserved(monkeypatch):
    import hermes_cli.plugins_cmd as commands
    import hermes_cli.plugin_isolation as isolation
    monkeypatch.setattr(commands, "_get_enabled_set", lambda: {R.CONSENT})
    monkeypatch.setattr(commands, "_get_disabled_set", lambda: set())
    monkeypatch.setattr(isolation, "isolation_mode", lambda: "in_process")
    assert R.runtime_policy() == ("healthy", None)
    monkeypatch.setattr(commands, "_get_disabled_set", lambda: {R.CONSENT})
    assert R.runtime_policy()[0] == "disabled"
    monkeypatch.setattr(commands, "_get_disabled_set", lambda: set())
    monkeypatch.setattr(isolation, "isolation_mode", lambda: "host")
    assert R.runtime_policy() == ("unsupported", "host_isolation")
