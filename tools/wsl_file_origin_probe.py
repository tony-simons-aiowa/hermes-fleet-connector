#!/usr/bin/env python3
"""Synthetic file-origin form probe for WSL2 -> Windows Edge localhost forwarding.

This sends only ``probe=synthetic``. It is not the Fleet viewer and contains no
pairing grant, QR, token, or external dependencies. Run only on the intended
WSL host; it binds an ephemeral listener to 127.0.0.1 and opens a temporary
file:// page in a dedicated Windows Edge InPrivate profile.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote


PROFILE_PREFIX = "fleet-file-origin-"
EXPECTED_BODY = b"probe=synthetic"
MAX_BODY = 256


def powershell(script: str, *, timeout: int = 20) -> str:
    command = shutil.which("powershell.exe") or shutil.which("powershell")
    if not command:
        raise RuntimeError("Windows PowerShell interop is unavailable")
    result = subprocess.run(
        [command, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError("Windows helper failed; inspect the host prerequisites and retry")
    return result.stdout.strip()


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_windows_profile() -> str:
    script = r"""
$ErrorActionPreference = 'Stop'
$dir = Join-Path $env:TEMP ('fleet-file-origin-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $dir | Out-Null
try {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($sid)
    $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
    [void]$acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $dir -AclObject $acl
    [Console]::Out.WriteLine($dir)
} catch {
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    throw
}
"""
    profile = powershell(script)
    if not profile or not PureWindowsPath(profile).name.startswith(PROFILE_PREFIX):
        raise RuntimeError("Windows did not return the dedicated test profile")
    return profile


def edge_pids() -> set[int]:
    script = r"""
$ErrorActionPreference = 'Stop'
$ids = @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | ForEach-Object { [int]$_.ProcessId })
[Console]::Out.WriteLine(($ids -join ','))
"""
    output = powershell(script)
    return {int(value) for value in output.split(",") if value.strip().isdigit()}


def profile_pids(profile: str) -> set[int]:
    profile_q = ps_quote(profile)
    script = rf"""
$ErrorActionPreference = 'Stop'
$profilePath = {profile_q}
$needle = '--user-data-dir=' + $profilePath
$ids = @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | Where-Object {{ $_.CommandLine -and $_.CommandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0 }} | ForEach-Object {{ [int]$_.ProcessId }})
[Console]::Out.WriteLine(($ids -join ','))
"""
    output = powershell(script)
    return {int(value) for value in output.split(",") if value.strip().isdigit()}


def launch_edge(profile: str, file_url: str) -> None:
    profile_q, url_q = ps_quote(profile), ps_quote(file_url)
    script = rf"""
$ErrorActionPreference = 'Stop'
$profile = {profile_q}
$url = {url_q}
$edge = $null
$keys = @(
 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
 'Registry::HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'
)
foreach ($keyPath in $keys) {{
 try {{ $key = Get-Item -LiteralPath $keyPath -ErrorAction Stop; $candidate = [string]$key.GetValue(''); if ($candidate -and (Test-Path -LiteralPath $candidate)) {{ $edge = $candidate; break }} }} catch {{}}
}}
if (-not $edge) {{ throw 'Edge executable was not resolved from its registered App Paths.' }}
$arguments = '--inprivate --new-window --user-data-dir="' + $profile + '" "' + $url + '"'
$process = Start-Process -FilePath $edge -ArgumentList $arguments -PassThru
[Console]::Out.WriteLine([int]$process.Id)
"""
    powershell(script)


def remove_windows_profile(profile: str) -> bool:
    profile_q = ps_quote(profile)
    prefix_q = ps_quote(PROFILE_PREFIX)
    script = rf"""
$ErrorActionPreference = 'Stop'
$target = [System.IO.Path]::GetFullPath({profile_q})
$temp = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
if (-not $target.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase)) {{ throw 'Refusing cleanup outside Windows TEMP.' }}
$name = [System.IO.Path]::GetFileName($target)
if (-not $name.StartsWith({prefix_q}, [StringComparison]::Ordinal) -or $name -notmatch '^fleet-file-origin-[0-9a-f]{{32}}$') {{ throw 'Refusing cleanup of an unrecognized test profile.' }}
$needle = '--user-data-dir=' + $target
$active = @(Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | Where-Object {{ $_.CommandLine -and $_.CommandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0 }})
if ($active.Count -gt 0) {{ [Console]::Out.WriteLine('ACTIVE'); exit 0 }}
Remove-Item -LiteralPath $target -Recurse -Force
[Console]::Out.WriteLine('REMOVED')
"""
    return powershell(script) == "REMOVED"


def windows_file_uri(path: Path) -> str:
    wslpath = shutil.which("wslpath")
    if not wslpath:
        raise RuntimeError("wslpath is unavailable")
    result = subprocess.run([wslpath, "-w", str(path)], capture_output=True, text=True,
                            timeout=5, check=False)
    if result.returncode:
        raise RuntimeError("Could not map the temporary probe file for Windows Edge")
    windows_path = result.stdout.strip().replace("\\", "/")
    if windows_path.startswith("//"):
        return "file:" + quote(windows_path, safe="/:$@!~*'(),-._")
    return "file:///" + quote(windows_path, safe="/:$@!~*'(),-._")


class ProbeServer(HTTPServer):
    allow_reuse_address = False
    timeout = 180


def make_page(origin: str) -> str:
    action = origin + "/probe"
    script = "document.getElementById('start').submit();"
    digest = __import__("base64").b64encode(hashlib.sha256(script.encode()).digest()).decode()
    return (
        "<!doctype html><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        f"script-src 'sha256-{digest}'; form-action {origin}; base-uri 'none'\">"
        "<title>Fleet file-origin probe</title><p>Synthetic file-origin check. No pairing data.</p>"
        f"<form id='start' method='post' action='{action}' enctype='application/x-www-form-urlencoded'>"
        "<input type='hidden' name='probe' value='synthetic'></form><script>" + script + "</script>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-profile", help="remove one previously generated, inactive test profile")
    args = parser.parse_args()
    kernel = Path("/proc/sys/kernel/osrelease").read_text(errors="ignore").lower()
    if "microsoft" not in kernel and "wsl" not in kernel:
        raise RuntimeError("Run this probe inside WSL2, not on Windows or macOS")
    if not shutil.which("powershell.exe") and not shutil.which("powershell"):
        raise RuntimeError("Windows PowerShell interop is unavailable")
    if args.cleanup_profile:
        profile = args.cleanup_profile
        if not re.fullmatch(r"fleet-file-origin-[0-9a-f]{32}", PureWindowsPath(profile).name):
            raise RuntimeError("Refusing cleanup: path is not a generated test-profile name")
        if profile_pids(profile):
            print("Refusing cleanup while that dedicated Edge profile is active; close its window normally first.")
            return 2
        if remove_windows_profile(profile):
            print("Dedicated test profile removed.")
            return 0
        print("Dedicated profile was not removed.")
        return 2

    temp_root = Path(tempfile.mkdtemp(prefix="fleet-file-origin-"))
    temp_root.chmod(0o700)
    html_path = temp_root / "probe.html"
    profile = None
    profile_removed = False
    keep_scratch = False
    baseline: set[int] = set()
    result: dict[str, object] = {}
    server: ProbeServer | None = None
    try:
        server = ProbeServer(("127.0.0.1", 0), None)
        host, port = server.server_address
        origin = f"http://{host}:{port}"
        page = make_page(origin)
        fd = os.open(html_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(page)
        baseline = edge_pids()
        profile = create_windows_profile()
        file_url = windows_file_uri(html_path)
        print("Probe: synthetic file:// form POST; no pairing grant or QR data.")
        print(f"WSL listener: 127.0.0.1:{port} (ephemeral); Edge PIDs at baseline: {len(baseline)}")
        launch_edge(profile, file_url)

        server.timeout = 180
        server.handle_request()
        result = getattr(server, "probe_metadata", {})
        if not result:
            print("No POST arrived before the 180 second timeout.")
            return 2
        print("POST metadata (body and Referer are not logged):")
        print(json.dumps(result, sort_keys=True))
        print("Expected: peer 127.0.0.1, exact Host, Origin null or absent, synthetic body accepted.")
        try:
            input("Close only the dedicated test Edge window normally, then press Enter here: ")
        except EOFError:
            print("No interactive terminal; profile cleanup will be conservative.")

        deadline = time.monotonic() + 45
        while profile_pids(profile) and time.monotonic() < deadline:
            time.sleep(1)
        after = edge_pids()
        preserved = baseline.issubset(after)
        print(f"Pre-existing Edge PIDs preserved: {preserved} (baseline {len(baseline)}, after {len(after)}).")
        if profile_pids(profile):
            print("The dedicated test profile is still in use; it was left intact. Close its window normally, then run the cleanup command printed below.")
            keep_scratch = True
        elif remove_windows_profile(profile):
            profile_removed = True
            print("Dedicated test profile removed after its Edge process exited.")
        else:
            print("Dedicated test profile was not removed; no process was terminated.")

        ok = (result.get("peer") == "127.0.0.1"
              and result.get("host") == f"127.0.0.1:{port}"
              and result.get("origin") in (None, "null")
              and result.get("synthetic_body_accepted") is True
              and preserved)
        print("This checks file-origin browser metadata only; it does not test the Fleet viewer, grant handling, pairing, or Hermes compatibility.")
        return 0 if ok else 1
    finally:
        if server is not None:
            server.server_close()
        if profile and not profile_removed and profile_pids(profile):
            keep_scratch = True
            print("Test Edge is still using its isolated profile; leaving it for normal window closure.")
        elif profile and not profile_removed:
            try:
                profile_removed = remove_windows_profile(profile)
            except Exception:
                print("Windows profile cleanup could not be verified; no Edge process was terminated.")
        if keep_scratch:
            print(f"Windows profile retained: {profile}")
            print(f"WSL scratch file retained until normal test-window closure: {html_path}")
        else:
            try:
                html_path.unlink(missing_ok=True)
                temp_root.rmdir()
            except OSError:
                print("The WSL scratch directory was not fully removed; inspect only the generated fleet-file-origin-* path.")


def handler_factory(server: ProbeServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_error(404)

        def do_POST(self):
            metadata = {
                "method": "POST",
                "path": self.path,
                "peer": self.client_address[0],
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "content_type": self.headers.get("Content-Type"),
            }
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise ValueError
                body = self.rfile.read(length)
            except Exception:
                body = b""
            metadata["synthetic_body_accepted"] = body == EXPECTED_BODY
            expected_host = f"127.0.0.1:{server.server_port}"
            expected_origin = self.headers.get("Origin") in (None, "null")
            metadata["loopback_headers_match"] = (
                metadata["peer"] == "127.0.0.1"
                and metadata["host"] == expected_host
                and expected_origin
                and self.path == "/probe"
            )
            server.probe_metadata = metadata
            success = bool(metadata["loopback_headers_match"] and metadata["synthetic_body_accepted"])
            body = ("<!doctype html><meta charset='utf-8'><title>Probe complete</title>"
                    f"<h1>{'PASS' if success else 'CHECK FAILED'}</h1>"
                    "<p>Synthetic file-origin probe complete. No pairing data was sent.</p>").encode()
            self.send_response(200 if success else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


if __name__ == "__main__":
    # Install the handler before main binds the listener.
    original_server = ProbeServer

    class ConfiguredProbeServer(original_server):
        def __init__(self, address, _handler):
            super().__init__(address, handler_factory(self))

    ProbeServer = ConfiguredProbeServer
    raise SystemExit(main())
