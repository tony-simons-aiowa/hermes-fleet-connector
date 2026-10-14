"""Synthetic validation of browser display prerequisites and QR payload handling."""
from types import SimpleNamespace

import pytest

from fleet_connector import display
from fleet_connector.display import DisplayFailure


INVITATION_ID = "A" * 22
SECRET = "S" * 43
LINK = f"https://fixture.example.test/pair#v=1&i={INVITATION_ID}&s={SECRET}"


def test_validate_link_accepts_only_the_matching_pair_fragment():
    display.validate_link(LINK, INVITATION_ID)
    mutations = [
        LINK.replace(INVITATION_ID, "B" * 22),
        LINK.replace("https://", "http://"),
        LINK.replace("/pair#", "/other#"),
        LINK.replace("/pair#", "/pair?secret=leak#"),
        LINK + "&extra=1",
        LINK.replace("v=1", "v=2"),
        LINK.replace(SECRET, "short"),
        LINK + " ",
        "https://fixture.example.test/pair#v=1&i=" + INVITATION_ID + "&s=" + SECRET + "&s=" + SECRET,
    ]
    for malformed in mutations:
        with pytest.raises(DisplayFailure) as error:
            display.validate_link(malformed, INVITATION_ID)
        assert error.value.stage == "invalid_payload"


def test_display_capability_requires_gui_qr_tool_and_launch_agent(monkeypatch):
    monkeypatch.setattr(display.sys, "platform", "darwin")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(display.shutil, "which", lambda name: "/fixture/qrencode" if name == "qrencode" else None)
    monkeypatch.setattr(display.Path, "is_file", lambda self: str(self) == "/usr/bin/open")
    calls = []

    def aqua_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"Aqua\n")

    assert display.display_capability(runner=aqua_runner)
    assert calls[0][0] == ["/bin/launchctl", "managername"]
    assert calls[0][1]["timeout"] <= 5

    monkeypatch.setenv("SSH_TTY", "/dev/ttys001")
    calls.clear()
    assert not display.display_capability(runner=aqua_runner)
    assert calls == []


def test_linux_display_capability_requires_local_gui_browser_and_qrencode(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "linux")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    expected_socket = tmp_path / "wayland-0"
    x11_socket = display.Path("/tmp/.X11-unix/X0")
    monkeypatch.setattr(display.Path, "is_socket", lambda self: self in {expected_socket, x11_socket})
    tools = {"qrencode": "/usr/bin/qrencode", "chromium": "/usr/bin/chromium"}
    monkeypatch.setattr(display.shutil, "which", lambda name: tools.get(name))

    assert display.display_capability()
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert not display.display_capability()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    # A gateway service may inherit SSH launch metadata while retaining access
    # to the owner's real local display socket.
    assert display.display_capability()
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    assert not display.display_capability()


def test_linux_service_uses_real_display_from_systemd_user_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "linux")
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-credential")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    socket_path = display.Path("/run/user/1000/wayland-1")
    monkeypatch.setattr(display.Path, "is_dir", lambda self: self == socket_path.parent)
    monkeypatch.setattr(display.Path, "is_socket", lambda self: self == socket_path)
    tools = {"systemctl": "/usr/bin/systemctl", "qrencode": "/usr/bin/qrencode",
             "chromium": "/usr/sbin/chromium"}
    monkeypatch.setattr(display.shutil, "which", lambda name: tools.get(name))
    calls = []

    def manager_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0,
            stdout="DISPLAY=:0\nWAYLAND_DISPLAY=wayland-1\nXDG_RUNTIME_DIR=/run/user/1000\n"
                   "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\nSSH_TTY=/dev/pts/1\n")

    assert display.display_capability(runner=manager_runner)
    assert calls[0][0] == ["/usr/bin/systemctl", "--user", "show-environment"]
    assert calls[0][1]["timeout"] == 3
    assert "TELEGRAM_BOT_TOKEN" not in calls[0][1]["env"]


def test_linux_browser_open_uses_allowlisted_binary_and_bootstrap_path(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "linux")
    monkeypatch.setattr(display.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-credential")
    monkeypatch.setenv("API_SERVER_KEY", "synthetic-api-key")
    monkeypatch.setattr(display.Path, "is_socket", lambda self: self == display.Path("/tmp/.X11-unix/X0"))
    calls = []
    monkeypatch.setattr(display.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))

    bootstrap = tmp_path / "open-fleet.html"
    display.open_browser(bootstrap)

    assert calls[0][0] == ["/usr/bin/chromium", "--incognito", bootstrap.resolve().as_uri()]
    assert calls[0][1]["stdin"] is display.subprocess.DEVNULL
    assert calls[0][1]["stdout"] is display.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is display.subprocess.DEVNULL
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["env"]["DISPLAY"] == ":0"
    assert "TELEGRAM_BOT_TOKEN" not in calls[0][1]["env"]
    assert "API_SERVER_KEY" not in calls[0][1]["env"]


def test_linux_browser_prefers_omarchy_private_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "linux")
    tools = {"chromium": "/usr/bin/google-chrome-stable",
             "omarchy-launch-browser": "/usr/bin/omarchy-launch-browser"}
    monkeypatch.setattr(display.shutil, "which", lambda name: tools.get(name))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("API_SERVER_KEY", "synthetic-api-key")
    monkeypatch.setattr(display.Path, "is_socket", lambda self: self == display.Path("/tmp/.X11-unix/X0"))
    calls = []
    monkeypatch.setattr(display.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))

    bootstrap = tmp_path / "private bootstrap.html"
    display.open_browser(bootstrap)

    assert calls[0][0] == ["/usr/bin/omarchy-launch-browser", "--private", bootstrap.resolve().as_uri()]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdin"] is display.subprocess.DEVNULL
    assert "API_SERVER_KEY" not in calls[0][1]["env"]


def test_linux_firefox_uses_private_window(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "linux")
    monkeypatch.setattr(display.shutil, "which", lambda name: "/usr/bin/firefox" if name == "firefox" else None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(display.Path, "is_socket", lambda self: self == display.Path("/tmp/.X11-unix/X0"))
    calls = []
    monkeypatch.setattr(display.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))

    bootstrap = tmp_path / "open-fleet.html"
    display.open_browser(bootstrap)

    assert calls[0][0] == ["/usr/bin/firefox", "--private-window", bootstrap.resolve().as_uri()]


def test_wsl_windows_path_is_child_only_and_edge_resolves_from_windows_path(monkeypatch):
    display._wsl_windows_tools.cache_clear()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-credential")
    original_path = display.os.environ["PATH"]
    monkeypatch.setattr(display, "_mounted_windows_cmd", lambda: "/mnt/c/Windows/System32/cmd.exe")
    calls = []

    def which(name, path=None):
        if name == "wslpath":
            return "/usr/bin/wslpath"
        if name == "powershell.exe" and "/fixture/windows/PowerShell" in (path or ""):
            return "/fixture/windows/PowerShell/powershell.exe"
        return None

    def is_dir(path):
        return str(path) in {"/fixture/windows/System32", "/fixture/windows/PowerShell"}

    def is_file(path):
        return str(path) in {
            "/fixture/windows/PowerShell/powershell.exe",
            "/fixture/edge/msedge.exe",
        }

    monkeypatch.setattr(display.shutil, "which", which)
    monkeypatch.setattr(display.Path, "is_dir", is_dir)
    monkeypatch.setattr(display.Path, "is_file", is_file)

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0].endswith("cmd.exe"):
            return SimpleNamespace(returncode=0,
                stdout="C:\\Windows\\System32;C:\\Windows\\System32\\WindowsPowerShell\\v1.0".encode("utf-16le"))
        if argv[:2] == ["/usr/bin/wslpath", "-u"]:
            paths = {
                "C:\\Windows\\System32": "/fixture/windows/System32",
                "C:\\Windows\\System32\\WindowsPowerShell\\v1.0": "/fixture/windows/PowerShell",
                "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe": "/fixture/edge/msedge.exe",
            }
            value = paths[argv[2]]
            return SimpleNamespace(returncode=0, stdout=value.encode())
        assert argv[0].endswith("powershell.exe")
        return SimpleNamespace(returncode=0,
            stdout=b"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe\n")

    monkeypatch.setattr(display.subprocess, "run", runner)
    try:
        edge, child_path, _ = display._wsl_windows_tools(original_path)
        assert edge == "/fixture/edge/msedge.exe"
        assert "/fixture/windows/System32" in child_path
        assert "/fixture/windows/PowerShell" in child_path
        assert display.os.environ["PATH"] == original_path
        assert all("TELEGRAM_BOT_TOKEN" not in kwargs["env"] for _, kwargs in calls)
    finally:
        display._wsl_windows_tools.cache_clear()


def test_wsl_edge_opens_private_file_bootstrap_without_grant_in_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-credential")
    monkeypatch.setattr(display, "_wsl_windows_tools", lambda _path: (
        "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "/usr/bin:/mnt/c/Windows/System32", "/usr/bin/wslpath"))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0,
            stdout=b"\\\\wsl.localhost\\Ubuntu-26.04\\tmp\\fleet-private-viewer\\open-fleet.html")

    opened = []
    monkeypatch.setattr(display.subprocess, "run", runner)
    monkeypatch.setattr(display.subprocess, "Popen", lambda argv, **kwargs: opened.append((argv, kwargs)))
    bootstrap = tmp_path / "open-fleet.html"
    display.WSLWindowsDisplayBackend().open(bootstrap)

    assert opened[0][0][:3] == [
        "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "--inprivate", "--new-window",
    ]
    assert opened[0][0][3].startswith("file://wsl.localhost/Ubuntu-26.04/")
    assert "synthetic-secret" not in " ".join(opened[0][0])
    assert "TELEGRAM_BOT_TOKEN" not in opened[0][1]["env"]
    assert opened[0][1]["env"]["PATH"].endswith("/mnt/c/Windows/System32")


def test_wsl_is_detected_and_selects_windows_display_backend(monkeypatch):
    monkeypatch.setattr(display.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-26.04")
    monkeypatch.setattr(display.WSLWindowsDisplayBackend, "available", staticmethod(lambda: True))
    assert display._is_wsl()
    assert isinstance(display.current_backend(), display.WSLWindowsDisplayBackend)


def test_unsupported_platform_has_no_display_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "win32")
    assert not display.display_capability()
    with pytest.raises(DisplayFailure) as error:
        display.open_browser(tmp_path / "open-fleet.html")
    assert error.value.stage == "unsupported_platform"


@pytest.mark.parametrize("returncode,stdout,expected", [
    (0, b"\x89PNG\r\n\x1a\nfixture", b"\x89PNG\r\n\x1a\nfixture"),
    (1, b"", "qr_encoding_failed"),
    (0, b"not a png", "qr_encoding_failed"),
    (0, b"\x89PNG\r\n\x1a\n" + b"x" * (512 * 1024), "qr_encoding_failed"),
])
def test_encode_png_checks_tool_result_and_never_places_payload_in_argv(monkeypatch, returncode, stdout, expected):
    monkeypatch.setattr(display.shutil, "which", lambda name: "/fixture/qrencode")
    payload_seen = []

    def runner(argv, **kwargs):
        payload_seen.append((argv, kwargs.get("input")))
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    payload = "https://fixture.example.test/pair#s=synthetic-secret"
    if isinstance(expected, str):
        with pytest.raises(DisplayFailure) as error:
            display.encode_png(payload, runner=runner)
        assert error.value.stage == expected
    else:
        assert display.encode_png(payload, runner=runner) == expected
    assert payload_seen == [(["/fixture/qrencode", "-t", "PNG", "-o", "-"], payload.encode("ascii"))]


def test_encode_png_classifies_missing_encoder_and_runner_failure(monkeypatch):
    monkeypatch.setattr(display.shutil, "which", lambda _name: None)
    with pytest.raises(DisplayFailure) as error:
        display.encode_png(LINK)
    assert error.value.stage == "qr_encoder_unavailable"

    monkeypatch.setattr(display.shutil, "which", lambda _name: "/fixture/qrencode")

    def failed(*_args, **_kwargs):
        raise OSError("fixture failure")

    with pytest.raises(DisplayFailure) as error:
        display.encode_png(LINK, runner=failed)
    assert error.value.stage == "qr_encoding_failed"
