import json
import httpx
import pytest
from fastapi.testclient import TestClient

from test_boundary import fixture, paired
from fleet_connector import service


def upstream(monkeypatch, *, status=200, payload=None):
    seen = []
    original = httpx.AsyncClient
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {
            "schema": 1, "publishers": 1, "stale_publishers": 0, "sessions": []})
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw))
    return seen


def combined_config(home, enabled=True):
    (home / "config.yaml").write_text("plugins:\n  enabled: " +
        ("[hermes-fleet-connector, fleet-liveops]" if enabled else "[hermes-fleet-connector]") + "\n")
    dashboard = home / "plugins/hermes-fleet-connector/dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "manifest.json").write_text(json.dumps({"name": "fleet-liveops", "version": "0.3.0"}))


def test_snapshot_requires_device_session_and_keeps_pairing_identity(fixture, monkeypatch):
    c, client = fixture
    seen = upstream(monkeypatch)
    path = "/api/plugins/fleet-liveops/snapshot"
    assert client.get(path).status_code == 401 and seen == []
    device = paired(c, client)
    response = client.get(path, headers={"Authorization": "Bearer fixture-caller-secret"})
    assert response.status_code == 200 and response.json()["publishers"] == 1
    assert "fixture-caller-secret" not in str(seen[0].headers)
    assert device["credential"] not in str(seen[0].headers)
    assert response.headers["cache-control"] == "no-store"
    # Rebuild the seam over the same retained store, as an upgrade/restart would.
    other = service.Connector(c.home, c.cfg, "different-host-only-backend-token")
    assert other.instance == c.instance
    with TestClient(service.make_app(other), base_url=c.cfg["origin"], client=("127.0.0.1", 33333)) as renewed:
        assert renewed.post("/auth/device-login", json={"device_credential": device["credential"]}).status_code == 200
        assert other.store.verify_session(renewed.cookies.get("__Host-hermes_session_at")) is not None
        assert renewed.post("/auth/device-revoke", json={"device_credential": device["credential"]}).status_code == 200
        assert renewed.get(path).status_code == 401


@pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
def test_reporting_mutations_and_new_namespaces_remain_refused(fixture, monkeypatch, verb):
    c, client = fixture
    paired(c, client)
    seen = upstream(monkeypatch)
    assert getattr(client, verb)("/api/plugins/fleet-liveops/snapshot").status_code == 403
    assert client.post("/api/plugins/fleet-liveops/push/respond").status_code == 403
    assert client.get("/api/plugins/hermes-fleet-connector/snapshot").status_code == 403
    assert seen == []


@pytest.mark.parametrize("enabled,state", [(False, "disabled"), (True, "restart_required")])
def test_authenticated_missing_route_gets_safe_configuration_diagnosis(fixture, monkeypatch, enabled, state):
    c, client = fixture
    combined_config(c.home, enabled)
    paired(c, client)
    upstream(monkeypatch, status=404)
    response = client.get("/api/plugins/fleet-liveops/snapshot")
    assert response.status_code == 200
    assert response.json()["reporting"]["state"] == state
    assert response.json()["publishers"] == 0 and response.json()["sessions"] == []
    assert str(c.home) not in response.text and c.backend_token not in response.text


@pytest.mark.parametrize("status", [401, 403, 503])
def test_authentication_or_backend_failure_is_never_reclassified_as_installation(fixture, monkeypatch, status):
    c, client = fixture
    combined_config(c.home, False)
    paired(c, client)
    upstream(monkeypatch, status=status, payload={"error": "synthetic"})
    response = client.get("/api/plugins/fleet-liveops/snapshot")
    assert response.status_code == status and "reporting" not in response.json()


def test_legacy_404_without_verified_combined_installation_stays_404(fixture, monkeypatch):
    c, client = fixture
    paired(c, client)
    upstream(monkeypatch, status=404)
    assert client.get("/api/plugins/fleet-liveops/snapshot").status_code == 404
