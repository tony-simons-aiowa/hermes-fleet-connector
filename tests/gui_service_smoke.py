"""Owned launchd synthetic GUI harness; never reads the live Hermes home.

It opens real local browsers, requires authenticated decode/paint acknowledgements,
and checks real DOM lifecycle behavior. All QR data is fixed fixture.invalid data;
there is no issuer, backend, Telegram registration, phone or persistent credential.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from websockets.sync.client import connect
from fleet_connector.viewer import PrivateViewer

LINK = "https://fixture.invalid/pair#v=1&i=" + "A" * 22 + "&s=" + "B" * 43
ROOT = Path(os.environ["FLEET_GUI_EVIDENCE"]).resolve()
assert str(ROOT).startswith("/private/tmp/") or str(ROOT).startswith("/tmp/")
ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
os.umask(0o077)

def wait(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError("synthetic browser condition not observed")

class Browser:
    def __init__(self):
        self.profile = ROOT / "isolated-chrome-profile"
        self.process = None
        self.id = 0
        self.socket = None
    def open(self, path):
        if self.process is None:
            port_file = self.profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            self.process = subprocess.Popen([
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--disable-sync", "--disable-component-update", "--new-window", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            wait(port_file.exists)
            self.port = int(port_file.read_text().splitlines()[0])
            def page_target():
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                            f"http://127.0.0.1:{self.port}/json/list", timeout=3) as res:
                        self.targets = json.load(res)
                    return any(t.get("type") == "page" for t in self.targets)
                except OSError:
                    return False
            wait(page_target)
            target = next(t for t in self.targets if t.get("type") == "page")
            self.socket = connect(target["webSocketDebuggerUrl"], open_timeout=5)
            self.call("Page.enable")
        else:
            self.call("Page.navigate", {"url": path.as_uri()})
        self.call("Page.bringToFront")
    def call(self, method, params=None):
        self.id += 1
        self.socket.send(json.dumps({"id": self.id, "method": method, "params": params or {}}))
        while True:
            result = json.loads(self.socket.recv(timeout=8))
            if result.get("id") == self.id:
                if "error" in result:
                    raise AssertionError("synthetic browser command refused")
                return result.get("result", {})
    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise AssertionError("synthetic page evaluation failed")
        return result.get("result", {}).get("value")
    def screenshot(self, name):
        result = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        (ROOT / name).write_bytes(base64.b64decode(result["data"]))
    def close(self):
        if self.socket:
            self.socket.close()
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)

def show_fixture(viewer, browser=None, seconds=8):
    # Synthetic assertions deliberately do not attest a real phone/network/screen.
    if browser:
        browser.evaluate("document.getElementById('phone').checked=true;document.getElementById('private').checked=true;document.getElementById('continue').click();true")
    else:
        req=urllib.request.Request(viewer.local_origin + "/observe", method="POST",
            data=b'{"phone":true,"private":true}', headers={"Authorization":"Viewer "+viewer.session,
            "Origin":viewer.local_origin,"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=3) as res:
            assert res.status == 200
    wait(lambda: viewer.observed)
    viewer.show_invitation(LINK, "A" * 22, time.time() + seconds)
    wait(lambda: viewer.qr_painted)

def main():
    manager = subprocess.check_output(["/bin/launchctl", "managername"], text=True).strip()
    assert manager == "Aqua"
    results = {"manager": manager, "fixture_only": True, "issuer_calls": 0,
               "phone_actions": 0, "default_browser": None, "cases": []}
    default = PrivateViewer(origin="https://fixture.invalid", observe=lambda: True)
    try:
        default.start()
        wait(lambda: default.loaded)
        show_fixture(default)
        results["default_browser"] = dict(default.metrics)
        (ROOT / "production-browser.json").write_text(json.dumps({"manager": manager,
            "fixture_only": True, "issuer_calls": 0, "paint": dict(default.metrics)}) + "\n")
        default.finish("expired")
        time.sleep(1)
    finally:
        default.close()
    assert not default.bootstrap_path.exists() and default.image is None and not default.session
    browser = Browser()
    try:
        for name in ["close", "back", "expired", "redeemed", "disconnected", "retry", "slow_checks", "close_during_checks"]:
            observations = []
            def observe():
                observations.append(True)
                if name in {"slow_checks", "close_during_checks"}:
                    time.sleep(2)
                return True
            viewer = PrivateViewer(origin="https://fixture.invalid", observe=observe, opener=browser.open)
            try:
                viewer.start()
                wait(lambda: viewer.loaded)
                assert browser.evaluate("document.visibilityState==='visible'") is True
                assert browser.evaluate("location.search===''&&location.hash===''") is True
                assert browser.evaluate("document.cookie===''&&localStorage.length===0&&sessionStorage.length===0") is True
                if name == "close_during_checks":
                    browser.evaluate("document.getElementById('phone').checked=true;document.getElementById('private').checked=true;document.getElementById('continue').click();document.getElementById('close').click();true")
                    wait(lambda: viewer.phase == "cancelled")
                    time.sleep(2.2)
                    assert browser.evaluate("document.getElementById('status').textContent.startsWith('Closed')") is True
                    assert not viewer.observed
                    results["cases"].append({"case": name, "result": "passed", "paint": dict(viewer.metrics)})
                    continue
                if name == "slow_checks":
                    browser.evaluate("document.getElementById('phone').checked=true;document.getElementById('private').checked=true;document.getElementById('continue').click();document.getElementById('continue').click();true")
                    wait(lambda: viewer.observed)
                    assert len(observations) == 1
                    viewer.show_invitation(LINK, "A" * 22, time.time() + 10)
                    wait(lambda: viewer.qr_painted)
                else:
                    show_fixture(viewer, browser, seconds=1.5 if name == "expired" else 10)
                if name == "close":
                    browser.screenshot("synthetic-private-viewer.png")
                    browser.evaluate("document.getElementById('close').click();true")
                    wait(lambda: viewer.phase == "cancelled")
                elif name == "back":
                    browser.evaluate("history.back();true")
                    wait(lambda: viewer.phase == "cancelled")
                elif name == "expired":
                    wait(lambda: viewer.phase == "expired")
                    wait(lambda: browser.evaluate("!document.getElementById('qr').hasAttribute('src')"))
                elif name == "redeemed":
                    viewer.finish("redeemed")
                    wait(lambda: browser.evaluate("document.getElementById('status').textContent.includes('Invitation accepted')"))
                    assert browser.evaluate("!document.getElementById('qr').hasAttribute('src')") is True
                elif name == "disconnected":
                    viewer.close()
                    wait(lambda: browser.evaluate("!document.getElementById('qr').hasAttribute('src')"))
                else:
                    browser.evaluate("document.getElementById('close').click();true")
                    wait(lambda: viewer.phase == "cancelled")
                results["cases"].append({"case": name, "result": "passed", "paint": dict(viewer.metrics)})
            finally:
                viewer.close()
                assert not viewer.bootstrap_path.exists() and viewer.image is None and not viewer.session
    finally:
        browser.close()
    source = Path(__file__).resolve().parent.parent
    results["module_hashes"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (source / "fleet_connector").glob("*.py")}
    (ROOT / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    print("Synthetic service GUI: Safari paint plus eight headed-browser lifecycle cases PASS", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Safe classification only. No browser URLs, HTML, grants or images in logs.
        frames = [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                  for f in traceback.extract_tb(exc.__traceback__)[-3:]]
        failure = {"error_type": type(exc).__name__, "stage": getattr(exc, "stage", None), "fixture_only": True, "frames": frames}
        failure.update({k: getattr(exc, k, None) for k in ("cause_type", "native_exit", "native_code", "os_code")})
        (ROOT / "failure.json").write_text(json.dumps(failure) + "\n")
        print("Synthetic service GUI failed: " + type(exc).__name__, flush=True)
        raise SystemExit(1)
