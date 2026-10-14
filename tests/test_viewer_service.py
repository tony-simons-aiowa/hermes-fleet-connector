"""Synthetic lifecycle tests for display-gated invitation issuance."""
from __future__ import annotations

import threading
import time
from urllib.parse import parse_qs, urlsplit

from fleet_connector.display import DisplayFailure
from fleet_connector.service import Connector
from fleet_connector.viewer import PrivateViewer


ORIGIN = "https://fixture.example.test"


class FakeViewer:
    def __init__(self, *, origin, observe, wait_result=True, on_show=None,
                 show_error=None):
        self.origin = origin
        self.observe = observe
        self.wait_result = wait_result
        self.on_show = on_show
        self.show_error = show_error
        self.phase = "preflight"
        self.lock = threading.RLock()
        self.qr_painted = False
        self.started = False
        self.closed = False
        self.finished = []
        self.shown = []

    def start(self):
        self.started = True
        return self

    def wait_ready(self):
        return self.wait_result

    def show_invitation(self, link, invitation_id, expires_at):
        self.shown.append((link, invitation_id, expires_at))
        if self.on_show:
            self.on_show(link, invitation_id, expires_at)
        if self.show_error:
            raise self.show_error
        self.phase = "qr"

    def _tick(self):
        return None

    def finish(self, phase):
        self.finished.append(phase)
        self.phase = phase

    def close(self):
        self.closed = True


def make_connector(tmp_path):
    cfg = dict(home=str(tmp_path), origin=ORIGIN, owners=[123], bot_id=456,
               port=9129, backend_port=9130, display_name="Synthetic fixture")
    return Connector(tmp_path, cfg, "synthetic-backend-token")


def proof(update_id=10):
    return dict(action="issue", owner=123, bot_id=456, update_id=update_id,
                explicit_issue=True)


def wait_until_idle(connector):
    deadline = time.monotonic() + 3
    while connector._display_busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert not connector._display_busy
    if connector.display_thread:
        connector.display_thread.join(timeout=.1)


def configure_ready(connector, *, responses=None):
    values = iter(responses or [True] * 10)

    def readiness(**_kwargs):
        ready = next(values, True)
        return {"ready": ready, "blockers": [] if ready else ["fixture_changed"]}

    connector.readiness = readiness


def test_no_invitation_exists_until_viewer_is_ready(tmp_path):
    connector = make_connector(tmp_path)
    configure_ready(connector)
    entered = threading.Event()
    release = threading.Event()

    class WaitingViewer(FakeViewer):
        def wait_ready(self):
            entered.set()
            release.wait(timeout=2)
            return False

    viewer = WaitingViewer(origin=ORIGIN, observe=lambda: True)
    connector.viewer_factory = lambda **_kwargs: viewer
    result = connector.owner(proof())
    assert result["ok"]
    assert entered.wait(timeout=1)
    assert not connector.store.list_pending_invitations()
    release.set()
    wait_until_idle(connector)
    assert not viewer.shown
    assert viewer.closed
    assert connector.last_display == "display_not_confirmed"
    assert not connector.store.list_pending_invitations()


def test_readiness_change_after_viewer_checks_prevents_issuance(tmp_path):
    connector = make_connector(tmp_path)
    configure_ready(connector, responses=[True, False])
    viewer = FakeViewer(origin=ORIGIN, observe=lambda: True)
    connector.viewer_factory = lambda **_kwargs: viewer

    assert connector.owner(proof())["ok"]
    wait_until_idle(connector)
    assert viewer.finished == ["display_failed"]
    assert not viewer.shown
    assert not connector.store.list_pending_invitations()
    assert connector.last_display == "readiness_changed"


def test_renderer_failure_cancels_only_the_issued_attempt(tmp_path):
    connector = make_connector(tmp_path)
    configure_ready(connector)
    before = connector.store.create_invitation(origin=ORIGIN, created_by="fixture-unrelated", ttl_seconds=60)
    viewer = FakeViewer(origin=ORIGIN, observe=lambda: True,
                        show_error=DisplayFailure("qr_encoding_failed"))
    connector.viewer_factory = lambda **_kwargs: viewer

    result = connector.owner(proof())
    assert result["ok"]
    wait_until_idle(connector)
    attempt_id = viewer.shown[0][1]
    assert attempt_id != before.invitation.id
    assert connector.invitation_state(attempt_id) == "cancelled"
    assert connector.invitation_state(before.invitation.id) == "pending"
    assert [item.id for item in connector.store.list_pending_invitations()] == [before.invitation.id]
    assert connector.last_display == "qr_encoding_failed"
    assert viewer.closed


def test_redeemed_invitation_is_preserved_when_viewer_closes(tmp_path):
    connector = make_connector(tmp_path)
    configure_ready(connector)
    redeemed = []

    def redeem_on_show(link, invitation_id, _expires_at):
        fields = parse_qs(urlsplit(link).fragment, strict_parsing=True)
        result = connector.store.redeem(invitation_id=invitation_id,
                                        secret=fields["s"][0], origin=ORIGIN)
        redeemed.append(result)

    viewer = FakeViewer(origin=ORIGIN, observe=lambda: True, on_show=redeem_on_show)
    connector.viewer_factory = lambda **_kwargs: viewer
    assert connector.owner(proof())["ok"]
    wait_until_idle(connector)
    invitation_id = viewer.shown[0][1]
    assert len(redeemed) == 1
    assert connector.invitation_state(invitation_id) == "redeemed"
    assert connector.last_display == "redeemed"
    assert viewer.closed


def test_unconfirmed_cancellation_is_reported_as_such(tmp_path, monkeypatch):
    connector = make_connector(tmp_path)
    configure_ready(connector)
    viewer = FakeViewer(origin=ORIGIN, observe=lambda: True,
                        show_error=DisplayFailure("renderer_failed"))
    connector.viewer_factory = lambda **_kwargs: viewer
    original_state = connector.invitation_state
    monkeypatch.setattr(connector.store, "cancel_invitation", lambda _invitation_id: False)

    assert connector.owner(proof())["ok"]
    wait_until_idle(connector)
    invitation_id = viewer.shown[0][1]
    assert original_state(invitation_id) == "pending"
    assert connector.last_display == "cancellation_unconfirmed"
    assert viewer.closed


def test_concurrent_request_is_refused_and_completed_update_cannot_be_replayed(tmp_path):
    connector = make_connector(tmp_path)
    configure_ready(connector)
    entered = threading.Event()
    release = threading.Event()

    class BlockingViewer(FakeViewer):
        def wait_ready(self):
            entered.set()
            release.wait(timeout=2)
            return False

    viewer = BlockingViewer(origin=ORIGIN, observe=lambda: True)
    connector.viewer_factory = lambda **_kwargs: viewer
    first = connector.owner(proof(update_id=77))
    assert first["ok"]
    assert entered.wait(timeout=1)
    second = connector.owner(proof(update_id=78))
    assert not second["ok"]
    assert "already active" in second["message"]
    release.set()
    wait_until_idle(connector)
    replay = connector.owner(proof(update_id=77))
    assert not replay["ok"]
    assert "already handled" in replay["message"]
    assert not connector.store.list_pending_invitations()

def test_observation_callback_cannot_switch_to_a_new_viewer(tmp_path):
    connector = make_connector(tmp_path)
    configure_ready(connector)
    previous = FakeViewer(origin=ORIGIN, observe=lambda: True)
    replacement = FakeViewer(origin=ORIGIN, observe=lambda: True)
    previous.loaded = replacement.loaded = True
    connector.viewer = replacement
    assert connector.observe_from_viewer(previous) is False
    assert not (connector.directory / "approval.json").exists()


def test_late_viewer_observation_cannot_refresh_after_close_or_change_owner_binding(tmp_path, monkeypatch):
    connector = make_connector(tmp_path)
    connector.readiness = lambda **_kwargs: {"ready": True, "blockers": []}
    viewer = FakeViewer(origin=ORIGIN, observe=lambda: True)
    viewer.loaded = True
    viewer.phase = "cancelled"
    connector.viewer = viewer
    writes = []
    monkeypatch.setattr(connector, "record_observations", lambda: writes.append(True))

    original_owners = list(connector.cfg["owners"])
    assert connector.observe_from_viewer() is False
    assert writes == []
    assert connector.cfg["owners"] == original_owners == [123]


def test_shutdown_display_interrupts_waiting_viewer_and_wakes_waiter(tmp_path):
    connector = make_connector(tmp_path)
    viewer = PrivateViewer(origin=ORIGIN, observe=lambda: True,
                           opener=lambda _path: None, encoder=lambda _payload: b"fixture-png")
    viewer.start()
    connector.viewer = viewer
    result = []
    waiter = threading.Thread(target=lambda: result.append(viewer.wait_ready(timeout=5)))
    connector.display_thread = waiter
    waiter.start()
    deadline = time.monotonic() + 1
    while not waiter.is_alive() and time.monotonic() < deadline:
        time.sleep(.005)

    connector.shutdown_display()
    try:
        assert not waiter.is_alive()
        assert result == [False]
        assert viewer.phase == "interrupted"
        assert not connector.store.list_pending_invitations()
    finally:
        viewer.close()
