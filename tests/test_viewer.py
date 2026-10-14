"""Synthetic tests for the private loopback browser viewer boundary."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import threading
import time

import pytest

from fleet_connector.display import DisplayFailure
from fleet_connector.viewer import PrivateViewer
from fleet_connector.viewer_page import SCRIPT


def request(viewer, path, *, method="GET", token=None, origin=None, body=None,
            content_type="application/json", host=None, extra_headers=None):
    headers = {"Host": host or viewer.address}
    headers.update(extra_headers or {})
    if token is not None:
        headers["Authorization"] = "Viewer " + token
    if origin is not None:
        headers["Origin"] = origin
    data = body
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request("http://" + viewer.address + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


@pytest.fixture
def viewer(tmp_path):
    opened = []
    value = PrivateViewer(origin="https://fixture.example.test",
                          observe=lambda: True, opener=opened.append,
                          encoder=lambda text: b"synthetic-png:" + text.encode(),
                          setup_seconds=30)
    value.start()
    try:
        yield value, opened
    finally:
        value.close()


def bootstrap(viewer):
    body = urllib.parse.urlencode({"grant": viewer.bootstrap}).encode("ascii")
    return request(viewer, "/bootstrap", method="POST", origin="null", body=body,
                   content_type="application/x-www-form-urlencoded")


def test_private_bootstrap_is_one_use_and_page_has_no_external_dependencies(viewer):
    value, opened = viewer
    assert len(opened) == 1
    path = opened[0]
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    source = path.read_text()
    assert "Content-Security-Policy" in source
    assert "default-src 'none'" in source
    assert "https://" not in source
    assert value.bootstrap not in str(opened)

    status, headers, body = bootstrap(value)
    assert status == 200
    page = body.decode()
    assert value.session in page
    assert "https://fixture.example.test" in page
    assert headers["Cache-Control"] == "no-store, max-age=0"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert not path.exists()
    assert value.bootstrap == ""

    replay, _, replay_body = bootstrap(value)
    assert replay == 403
    assert value.session not in replay_body.decode()


def test_viewer_handshake_diagnostics_localize_bootstrap_vs_paint_without_values(viewer):
    value, _ = viewer
    bootstrap_secret = value.bootstrap
    attacker_origin = "https://attacker.example"

    rejected, _, _ = request(value, "/bootstrap", method="POST", origin=attacker_origin,
                             body=urllib.parse.urlencode({"grant": bootstrap_secret}).encode("ascii"),
                             content_type="application/x-www-form-urlencoded")
    assert rejected == 403
    assert bootstrap(value)[0] == 200

    auth = dict(token=value.session, origin=value.local_origin)
    assert request(value, "/frame", **auth)[0] == 200
    bad_paint = json.dumps({"phase": "preflight", "visible": True,
                            "width": 20, "height": 100}).encode()
    assert request(value, "/event", method="POST", body=bad_paint, **auth)[0] == 400

    diagnostics = value.diagnostic_snapshot()
    assert diagnostics["bootstrap_state"] == "consumed"
    assert diagnostics["counts"] == {
        "bootstrap_post_seen": 2,
        "bootstrap_post_accepted": 1,
        "session_requests_accepted": 2,
        "frame_requests_accepted": 1,
        "close_requests_accepted": 0,
        "paint_event_posts": 1,
        "paint_events_accepted": 0,
    }
    assert diagnostics["rejections"] == {
        "bootstrap_origin_rejected": 1,
        "paint_event_rejected": 1,
    }
    serialized = json.dumps(diagnostics)
    for sensitive in (bootstrap_secret, value.session, value.address, value.local_origin, attacker_origin):
        assert sensitive not in serialized


def test_hidden_preflight_waits_for_foreground_but_hidden_invitation_cancels():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the embedded viewer script with a synthetic DOM")
    harness = f"""
const vm = require('vm');
const SCRIPT = {json.dumps(SCRIPT)};
(async () => {{
  const documentHandlers = {{}}, windowHandlers = {{}}, intervalHandlers = [];
  const sent = [], beacons = [];
  let phase = 'preflight', visibility = 'hidden';
  const makeElement = () => ({{textContent:'', src:'', naturalWidth:280, naturalHeight:280,
    disabled:false, checked:false, classList:{{add(){{}},remove(){{}},toggle(){{}}}},
    addEventListener(){{}}, removeAttribute(name){{if(name==='src')this.src='';}},
    decode:async()=>{{}} }});
  const elements = new Map();
  const document = {{
    get visibilityState(){{return visibility;}},
    getElementById(id){{
      if(id==='config') return {{textContent:JSON.stringify({{session:'synthetic-session',origin:'https://fixture.invalid'}})}};
      if(!elements.has(id)) elements.set(id,makeElement());
      return elements.get(id);
    }},
    addEventListener(name,fn){{documentHandlers[name]=fn;}}
  }};
  const sandbox = {{
    document,
    window:{{addEventListener(name,fn){{windowHandlers[name]=fn;}}}},
    history:{{pushState(){{}}}},
    navigator:{{sendBeacon(path){{beacons.push(path);return true;}}}},
    performance:{{now:()=>1000}},
    requestAnimationFrame(fn){{queueMicrotask(fn);}},
    setInterval(fn){{intervalHandlers.push(fn);return intervalHandlers.length;}},
    Blob:class{{}},
    fetch:async(path,options={{}})=>{{
      sent.push({{path,body:options.body}});
      if(path==='/frame') return {{ok:true,json:async()=>({{phase,png:'c3ludGhldGlj',remaining:20,observed:false}})}};
      return {{ok:true,json:async()=>({{ok:true}})}};
    }}
  }};
  vm.runInNewContext(SCRIPT,sandbox);
  const pause=()=>new Promise(resolve=>setTimeout(resolve,10));
  await pause();
  if(beacons.length!==0) throw Error('hidden preflight cancelled instead of waiting');
  if(sent.some(item=>item.path==='/event')) throw Error('hidden preflight incorrectly acknowledged paint');
  visibility='visible'; documentHandlers.visibilitychange(); await pause();
  if(!sent.some(item=>item.path==='/event'&&JSON.parse(item.body).phase==='preflight'))
    throw Error('visible preflight did not resume its paint acknowledgement');
  phase='qr'; intervalHandlers[0](); await pause();
  if(!sent.some(item=>item.path==='/event'&&JSON.parse(item.body).phase==='qr'))
    throw Error('visible invitation QR did not paint');
  visibility='hidden'; documentHandlers.visibilitychange();
  if(beacons.length!==1||beacons[0]!=='/close') throw Error('hidden invitation QR was not cancelled');
  process.stdout.write('visibility policy passed');
}})().catch(err=>{{console.error(err.message);process.exitCode=1;}});
"""
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "visibility policy passed"


@pytest.mark.parametrize("origin,host,form", [
    ("https://attacker.example", None, None),
    ("null", "127.0.0.1:1", None),
    ("null", None, "grant=sentinel&extra=1"),
])
def test_bootstrap_rejects_wrong_origin_host_and_extra_grant_fields(viewer, origin, host, form):
    value, _ = viewer
    supplied = form.encode("ascii") if form else urllib.parse.urlencode({"grant": value.bootstrap}).encode("ascii")
    status, _, body = request(value, "/bootstrap", method="POST", origin=origin,
                              host=host, body=supplied,
                              content_type="application/x-www-form-urlencoded")
    assert status in {400, 403}
    assert b"sentinel" not in body
    assert value.session.encode() not in body
    assert value.bootstrap
    assert not value.loaded


def test_session_routes_require_header_token_exact_origin_and_loopback_host(viewer):
    value, _ = viewer
    assert bootstrap(value)[0] == 200

    assert request(value, "/frame")[0] == 403
    assert request(value, "/frame", token="wrong")[0] == 403
    assert request(value, "/frame", token=value.session,
                   origin="https://attacker.example")[0] == 403
    assert request(value, "/frame", token=value.session,
                   host="127.0.0.1:1")[0] == 403

    status, headers, body = request(value, "/frame", token=value.session,
                                    origin=value.local_origin)
    assert status == 200
    assert headers["Cache-Control"] == "no-store, max-age=0"
    assert headers["Pragma"] == "no-cache"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    frame = json.loads(body)
    assert frame["phase"] == "preflight"
    assert base64.b64decode(frame["png"]) == b"synthetic-png:" + b"HERMES FLEET PRIVATE DISPLAY CHECK - NOT A PAIRING INVITATION"


@pytest.mark.parametrize("path,method,origin,extra", [
    ("/frame?secret=sentinel", "GET", "http://127.0.0.1", {}),
    ("/frame", "POST", "http://127.0.0.1", {}),
    ("/observe", "POST", "https://attacker.example", {}),
    ("/frame", "GET", "http://127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
])
def test_rejects_bad_method_origin_query_and_forwarding_headers(viewer, path, method, origin, extra):
    value, _ = viewer
    status, _, body = request(value, path, method=method, token=value.session,
                              origin=origin, body=b"{}" if method == "POST" else None,
                              extra_headers=extra)
    assert status in (400, 403, 404)
    assert b"sentinel" not in body


def test_event_requires_a_visible_minimum_size_paint_and_observations_are_explicit(viewer):
    value, _ = viewer
    auth = dict(token=value.session, origin=value.local_origin)
    bad = json.dumps({"phase": "preflight", "visible": True,
                      "width": 20, "height": 100}).encode()
    assert request(value, "/event", method="POST", body=bad, **auth)[0] == 400
    assert not value.loaded

    paint = json.dumps({"phase": "preflight", "visible": True,
                        "width": 320, "height": 480}).encode()
    assert request(value, "/event", method="POST", body=paint, **auth)[0] == 200
    assert value.loaded
    assert request(value, "/observe", method="POST",
                   body=b'{"phone":true}', **auth)[0] == 409
    assert not value.observed
    assert request(value, "/observe", method="POST",
                   body=b'{"phone":true,"private":true}', **auth)[0] == 200
    assert value.observed
    assert value.wait_ready(timeout=.1)


def test_body_size_cap_and_transfer_encoding_are_refused_without_echo(viewer):
    value, _ = viewer
    auth = dict(token=value.session, origin=value.local_origin)
    oversized = b"x" * 4097 + b"synthetic-secret"
    status, _, body = request(value, "/observe", method="POST", body=oversized, **auth)
    assert status == 400
    assert b"synthetic-secret" not in body

    status, _, body = request(value, "/observe", method="POST", body=b"{}", **auth,
                              extra_headers={"Transfer-Encoding": "chunked"})
    assert status == 400
    assert b"synthetic-secret" not in body


def test_failed_owner_observation_fails_closed_after_preflight_paint(viewer):
    value, _ = viewer
    value.observe = lambda: False
    auth = dict(token=value.session, origin=value.local_origin)
    paint = json.dumps({"phase": "preflight", "visible": True,
                        "width": 320, "height": 480}).encode()
    assert request(value, "/event", method="POST", body=paint, **auth)[0] == 200
    status, _, body = request(value, "/observe", method="POST",
                              body=b'{"phone":true,"private":true}', **auth)
    assert status == 503
    assert json.loads(body) == {"error": "observation_failed"}
    assert value.phase == "display_failed"
    assert not value.observed
    assert value.image is None


def test_slow_observation_does_not_block_frame_or_close_and_late_success_stays_closed(viewer):
    value, _ = viewer
    entered = threading.Event()
    release = threading.Event()
    responses = []

    def slow_observe():
        entered.set()
        assert release.wait(timeout=2)
        return True

    value.observe = slow_observe
    assert bootstrap(value)[0] == 200
    auth = dict(token=value.session, origin=value.local_origin)
    paint = json.dumps({"phase": "preflight", "visible": True,
                        "width": 320, "height": 480}).encode()
    assert request(value, "/event", method="POST", body=paint, **auth)[0] == 200

    def observe_request():
        responses.append(request(value, "/observe", method="POST",
                                 body=b'{"phone":true,"private":true}', **auth))

    observer = threading.Thread(target=observe_request)
    observer.start()
    assert entered.wait(timeout=1)

    started = time.monotonic()
    status, _, frame = request(value, "/frame", token=value.session,
                               origin=value.local_origin)
    frame_elapsed = time.monotonic() - started
    assert status == 200
    assert json.loads(frame)["phase"] == "preflight"
    assert frame_elapsed < .75

    started = time.monotonic()
    status, _, _ = request(value, "/close", method="POST", token=value.session,
                           origin=value.local_origin, body=b"{}")
    close_elapsed = time.monotonic() - started
    assert status == 200
    assert close_elapsed < .75
    assert value.phase == "cancelled"

    release.set()
    observer.join(timeout=2)
    assert not observer.is_alive()
    assert responses[0][0] == 409
    assert json.loads(responses[0][2]) == {"error": "closed"}
    assert value.observed is False
    assert value.image is None
    assert value.phase == "cancelled"


def test_close_marks_terminal_cancels_frame_and_close_erases_grants(viewer):
    value, _ = viewer
    old_session = value.session
    assert bootstrap(value)[0] == 200
    status, _, body = request(value, "/close", method="POST", token=old_session,
                              origin=value.local_origin, body=b"{}")
    assert status == 200
    assert value.phase == "cancelled"
    assert value.image is None
    status, _, frame = request(value, "/frame", token=old_session,
                               origin=value.local_origin)
    assert status == 200
    assert json.loads(frame)["phase"] == "cancelled"
    assert json.loads(frame)["png"] is None
    late_ack = json.dumps({"phase": "preflight", "visible": True,
                           "width": 320, "height": 480}).encode()
    status, _, ack = request(value, "/event", method="POST", body=late_ack,
                             token=old_session, origin=value.local_origin)
    assert status == 200
    assert json.loads(ack) == {"ok": False, "phase": "cancelled"}
    late_link = f"https://fixture.example.test/pair#v=1&i={'A' * 22}&s={'S' * 43}"
    with pytest.raises(DisplayFailure) as error:
        value.show_invitation(late_link, "A" * 22, value.wall() + 30)
    assert error.value.stage == "viewer_unavailable"
    assert value.image is None
    assert value.phase == "cancelled"
    assert "sentinel" not in body.decode()
    value.close()
    assert value.session == ""
    assert value.bootstrap == ""


def test_expiry_is_terminal_without_exposing_payload(viewer):
    value, _ = viewer
    value.deadline = value.clock() - 1
    status, _, body = request(value, "/frame", token=value.session,
                              origin=value.local_origin)
    assert status == 200
    assert value.phase == "expired"
    assert json.loads(body)["phase"] == "expired"


def test_expired_bootstrap_grant_and_disconnected_viewer_cannot_advance(viewer, capsys):
    value, _ = viewer
    stale_grant = value.bootstrap
    value.deadline = value.clock() - 1
    expired_body = urllib.parse.urlencode({"grant": stale_grant}).encode("ascii")
    status, _, body = request(value, "/bootstrap", method="POST", origin="null",
                              body=expired_body,
                              content_type="application/x-www-form-urlencoded")
    assert status == 403
    assert b"grant" not in body
    assert value.phase == "expired"

    # Exercise disconnection expiry through the public authenticated frame route.
    value.phase = "preflight"
    value.deadline = value.clock() + 30
    value.last_seen = value.clock() - value.heartbeat_seconds - 1
    status, _, frame = request(value, "/frame", token=value.session,
                               origin=value.local_origin)
    assert status == 200
    assert json.loads(frame)["phase"] == "disconnected"
    assert value.image is None
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_start_failure_cleans_up_and_reports_renderer_stage():
    def fail_open(_path):
        raise DisplayFailure("browser_launch_failed")

    value = PrivateViewer(origin="https://fixture.example.test", observe=lambda: True,
                          opener=fail_open, encoder=lambda _value: b"PNG")
    with pytest.raises(DisplayFailure) as error:
        value.start()
    assert error.value.stage == "browser_launch_failed"
    assert value._closed
    assert value.session == ""
    assert value.temp is not None
    assert not Path(value.temp.name).exists()
