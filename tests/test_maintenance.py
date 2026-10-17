import json
from types import SimpleNamespace

import httpx
import pytest

from fleet_connector.maintenance import backend_maintenance
from fleet_connector.service import Connector


@pytest.fixture
def backend(monkeypatch):
    requests = []
    replies = [dict(ok=True, pid=91, role="serve"), dict(ok=True, idle=True)]
    original = httpx.Client
    def handler(request):
        requests.append(request)
        value = replies.pop(0)
        return httpx.Response(value if isinstance(value, int) else 200, json={} if isinstance(value, int) else value)
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    connector = SimpleNamespace(backend="http://127.0.0.1:9919", backend_headers=lambda: {"Authorization": "Bearer synthetic-host-lease"})
    return connector, requests, replies


def test_private_inspection_uses_existing_host_lease_and_filters_backend_data(backend):
    c, requests, replies = backend
    replies[-1].update(detail="private session details", token="must-not-escape")
    result = backend_maintenance(c, {"local_operator": True})
    assert result == {"ok": True, "pid": 91, "role": "serve", "idle": True}
    assert [r.url.path for r in requests] == ["/api/host/identity", "/api/health/idle"]
    assert all(r.headers["Authorization"] == "Bearer synthetic-host-lease" for r in requests)


def test_only_local_owner_can_request_maintenance(backend):
    c, requests, _ = backend
    for value in [None, False, 1, "true"]:
        assert Connector.owner(c, {"action": "backend_maintenance", "local_operator": value})["ok"] is False
    assert not requests


@pytest.mark.parametrize("idle", [False, None])
def test_busy_or_unknown_backend_does_not_get_a_permit(backend, idle):
    c, requests, replies = backend
    replies[-1] = {"ok": False, "idle": idle, "token": "must-not-escape"}
    result = backend_maintenance(c, {"local_operator": True, "operation": "prepare", "expected_pid": 91})
    assert result == {"ok": False, "pid": 91, "role": "serve", "idle": idle}
    assert json.loads(requests[-1].content) == {"action": "prepare"}


def test_retirement_is_bound_to_exact_process_and_fixed_endpoints(backend):
    c, requests, replies = backend
    token = "synthetic-retirement-permit-123456"
    replies[-1] = {"ok": True, "idle": True, "token": token}
    assert backend_maintenance(c, {"local_operator": True, "operation": "prepare", "expected_pid": 91})["token"] == token
    replies.extend([dict(ok=True, pid=92, role="serve"), dict(ok=True)])
    assert not backend_maintenance(c, {"local_operator": True, "operation": "commit", "expected_pid": 91, "token": token})["ok"]
    assert len(requests) == 3


@pytest.mark.parametrize("status", [401, 403, 404, 503, 302])
def test_unavailable_auth_or_route_keeps_backend_running(backend, status):
    c, requests, replies = backend
    replies[0] = status
    result = backend_maintenance(c, {"local_operator": True})
    assert not result["ok"] and "left running" in result["message"]
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["commit", "cancel"])
def test_commit_and_cancel_forward_only_the_retirement_permit(backend, operation):
    c, requests, replies = backend
    token = "synthetic-retirement-permit-123456"
    replies[-1] = {"ok": True, "detail": "private"}
    result = backend_maintenance(c, {"local_operator": True, "operation": operation, "expected_pid": 91, "token": token})
    assert result == {"ok": True, "pid": 91, "role": "serve"}
    assert json.loads(requests[-1].content) == {"action": operation, "token": token}
