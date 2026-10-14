"""OS-specific browser launch behind one local-display interface.

No QR payload or viewer grant enters argv or URLs. The short-lived bootstrap
file contains only the one-use viewer grant and is opened in the current user
session; the authenticated paint acknowledgement remains the success signal.
"""
from __future__ import annotations
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, quote, urlsplit
from .settings import normalise_origin

class DisplayFailure(RuntimeError):
    def __init__(self, stage="display_failed"):
        self.stage = stage
        super().__init__("Private Fleet page could not be displayed.")


class DisplayBackend:
    """Small OS adapter: report a local GUI capability and open one local file."""

    def available(self, *, runner=subprocess.run) -> bool:
        raise NotImplementedError

    def open(self, path: Path) -> None:
        raise NotImplementedError


class MacOSDisplayBackend(DisplayBackend):
    def available(self, *, runner=subprocess.run) -> bool:
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            return False
        if not shutil.which("qrencode") or not Path("/usr/bin/open").is_file():
            return False
        try:
            result = runner(["/bin/launchctl", "managername"], capture_output=True,
                            timeout=5, check=False)
            return result.returncode == 0 and result.stdout.strip() == b"Aqua"
        except (OSError, subprocess.SubprocessError):
            return False

    def open(self, path: Path) -> None:
        try:
            # Select Safari explicitly; HTML associations can point at editors.
            result = subprocess.run(["/usr/bin/open", "-b", "com.apple.Safari", str(path)],
                                    capture_output=True, timeout=10, check=False)
            if result.returncode:
                error = DisplayFailure("browser_launch_failed")
                error.native_exit = result.returncode
                code = re.search(rb"error\s+(-?\d+)", result.stderr, re.I)
                error.native_code = int(code.group(1)) if code else None
                raise error
        except (OSError, subprocess.SubprocessError) as exc:
            error = DisplayFailure("browser_launch_failed")
            error.os_code = getattr(exc, "errno", None)
            raise error from None


class LinuxDisplayBackend(DisplayBackend):
    """Use an installed browser in the logged-in X11/Wayland session."""

    BROWSERS = ("chromium", "chromium-browser", "google-chrome", "firefox")
    BROWSER_ENV = {
        "HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG",
        "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
        "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP",
        "XDG_DATA_DIRS", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_CACHE_HOME",
        "GDK_BACKEND", "MOZ_ENABLE_WAYLAND",
    }

    @staticmethod
    def browser() -> str | None:
        for name in LinuxDisplayBackend.BROWSERS:
            path = shutil.which(name)
            if path:
                return path
        return None

    @staticmethod
    def local_gui_session() -> bool:
        return LinuxDisplayBackend.has_local_gui(os.environ)

    @staticmethod
    def has_local_gui(env) -> bool:
        display = env.get("DISPLAY", "")
        # Accept only an X11 Unix-domain display with its local server socket.
        # A forwarded DISPLAY such as localhost:10.0 is not the owner's screen.
        match = re.fullmatch(r"(?:(?:unix/)?:(\d+)(?:\.\d+)?|/tmp/\.X11-unix/X(\d+))", display)
        if match:
            display_number = match.group(1) or match.group(2)
            if (Path("/tmp/.X11-unix") / f"X{display_number}").is_socket():
                return True
        wayland = env.get("WAYLAND_DISPLAY")
        runtime = env.get("XDG_RUNTIME_DIR")
        if not wayland or not runtime or not Path(runtime).is_dir():
            return False
        socket = Path(wayland)
        if not socket.is_absolute():
            socket = Path(runtime) / socket
        return socket.is_socket()

    @staticmethod
    def browser_environment(runner=subprocess.run) -> dict[str, str]:
        # A Telegram gateway process may carry bot/API credentials in its
        # environment. Query the user manager without forwarding those values,
        # and give the browser only an allowlist of desktop/runtime variables.
        env = {key: value for key, value in os.environ.items()
               if key in LinuxDisplayBackend.BROWSER_ENV or key.startswith("LC_")}
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return env
        try:
            result = runner([systemctl, "--user", "show-environment"], capture_output=True,
                            text=True, timeout=3, check=False, env=env)
            if result.returncode != 0:
                return env
            for line in result.stdout.splitlines():
                key, sep, value = line.partition("=")
                if sep and (key in LinuxDisplayBackend.BROWSER_ENV or key.startswith("LC_")):
                    env[key] = value
        except (OSError, subprocess.SubprocessError):
            pass
        return env

    def available(self, *, runner=subprocess.run) -> bool:
        # Inspect the actual local display socket rather than the process's
        # launch context. An owner service may inherit SSH flags while still
        # having access to the user's local X11/Wayland session; forwarded X11
        # displays are rejected because they have no local server socket.
        # The viewer still requires a browser paint acknowledgement before it
        # records either observation.
        if not self.has_local_gui(self.browser_environment(runner)):
            return False
        return bool(shutil.which("qrencode") and self.browser())

    def open(self, path: Path) -> None:
        browser = self.browser()
        if not browser:
            raise DisplayFailure("browser_unavailable")
        env = self.browser_environment()
        if not self.has_local_gui(env):
            raise DisplayFailure("display_unavailable")
        page = path.resolve().as_uri()
        # Omarchy's launcher selects its configured browser and applies the
        # desktop's private-window policy. It is specifically useful when the
        # gateway can reach the user's systemd manager but not the GUI env.
        omarchy_launcher = shutil.which("omarchy-launch-browser")
        if omarchy_launcher:
            argv = [omarchy_launcher, "--private", page]
        else:
            name = Path(browser).name.lower()
            private_flag = "--private-window" if "firefox" in name else "--incognito"
            argv = [browser, private_flag, page]
        try:
            # Do not use xdg-open: its default handler may be an editor, and its
            # success only says that a handler started. Paint is acknowledged
            # separately by the authenticated viewer page.
            # This process is intentionally not retained or terminated when the
            # invitation expires. The page clears its QR and displays the
            # terminal status; already-rendered browser content cannot be erased
            # by deleting the bootstrap file.
            subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True,
                             env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            error = DisplayFailure("browser_launch_failed")
            error.os_code = getattr(exc, "errno", None)
            raise error from None


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="ascii", errors="ignore")
    except OSError:
        return False
    return bool(re.search(r"microsoft|wsl", release, re.I))


def _minimal_wsl_env(path: str) -> dict[str, str]:
    # Windows interop gets no Telegram or Hermes secrets. WSL_INTEROP is
    # forwarded only when present; WSL binfmt may work without it.
    env = {"PATH": path}
    for key in ("WSL_INTEROP", "WSL_DISTRO_NAME", "WSLENV", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _decode_cmd_output(raw: bytes) -> str:
    # cmd.exe /u writes redirected builtin-command output as UTF-16LE.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace").strip().lstrip("\ufeff")
    try:
        return raw.decode("utf-16le").strip().lstrip("\ufeff\x00")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace").strip()


def _mounted_windows_cmd() -> str | None:
    # Resolve the Windows system drive from mounted WSL drives; do not assume C:.
    try:
        roots = sorted(Path("/mnt").iterdir())
    except OSError:
        return None
    for root in roots:
        if not root.is_dir():
            continue
        candidate = root / "Windows" / "System32" / "cmd.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_path_executable(name: str, windows_dirs: list[str], *, path: str) -> str | None:
    for directory in windows_dirs:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name, path=path)


@lru_cache(maxsize=4)
def _wsl_windows_tools(process_path: str) -> tuple[str, str, str]:
    """Return (Edge Linux path, process-local PATH, wslpath executable).

    Windows PATH is read for this child environment only. It is never written
    back to os.environ, a shell profile, or a system configuration file.
    """
    cmd = shutil.which("cmd.exe", path=process_path) or _mounted_windows_cmd()
    wslpath = shutil.which("wslpath", path=process_path)
    if not cmd or not wslpath:
        raise DisplayFailure("windows_interop_unavailable")
    env = _minimal_wsl_env(process_path)
    try:
        result = subprocess.run([cmd, "/d", "/u", "/c", "echo %PATH%"],
                                capture_output=True, timeout=5, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        raise DisplayFailure("windows_path_unavailable") from None
    if result.returncode:
        raise DisplayFailure("windows_path_unavailable")
    windows_path = _decode_cmd_output(result.stdout)
    child_dirs = []
    for item in windows_path.split(";"):
        item = item.strip()
        if not item:
            continue
        try:
            converted = subprocess.run([wslpath, "-u", item], capture_output=True,
                                       timeout=3, check=False, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        if converted.returncode:
            continue
        directory = converted.stdout.decode("utf-8", errors="replace").strip()
        if directory and Path(directory).is_dir() and directory not in child_dirs:
            child_dirs.append(directory)
    child_path = os.pathsep.join([process_path, *child_dirs])
    child_env = _minimal_wsl_env(child_path)

    edge = _windows_path_executable("msedge.exe", child_dirs, path=child_path)
    if edge is None:
        powershell = _windows_path_executable("powershell.exe", child_dirs, path=child_path)
        if powershell is None:
            raise DisplayFailure("windows_edge_unavailable")
        query = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$keys = @(
 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
 'Registry::HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'
)
foreach ($keyPath in $keys) {
 try {
  $key = Get-Item -LiteralPath $keyPath -ErrorAction Stop
  $candidate = [string]$key.GetValue('')
  if ($candidate -and (Test-Path -LiteralPath $candidate)) { [Console]::Out.WriteLine($candidate); exit 0 }
 } catch {}
}
exit 1
"""
        try:
            result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-Command", query], capture_output=True, timeout=8,
                                    check=False, env=child_env)
        except (OSError, subprocess.SubprocessError):
            raise DisplayFailure("windows_edge_unavailable") from None
        if result.returncode:
            raise DisplayFailure("windows_edge_unavailable")
        windows_edge = result.stdout.decode("utf-8", errors="replace").strip()
        if not windows_edge:
            raise DisplayFailure("windows_edge_unavailable")
        try:
            converted = subprocess.run([wslpath, "-u", windows_edge], capture_output=True,
                                       timeout=3, check=False, env=child_env)
        except (OSError, subprocess.SubprocessError):
            raise DisplayFailure("windows_edge_unavailable") from None
        if converted.returncode:
            raise DisplayFailure("windows_edge_unavailable")
        edge = converted.stdout.decode("utf-8", errors="replace").strip()
    if not edge or not Path(edge).is_file():
        raise DisplayFailure("windows_edge_unavailable")
    return edge, child_path, wslpath


class WSLWindowsDisplayBackend(DisplayBackend):
    """Open the existing one-use file bootstrap in Windows Edge from WSL."""

    @staticmethod
    def available(*, runner=subprocess.run) -> bool:
        if not _is_wsl():
            return False
        try:
            _wsl_windows_tools(os.environ.get("PATH", ""))
            return True
        except DisplayFailure:
            return False

    def open(self, path: Path) -> None:
        try:
            edge, child_path, wslpath = _wsl_windows_tools(os.environ.get("PATH", ""))
            env = _minimal_wsl_env(child_path)
            result = subprocess.run([wslpath, "-w", str(path)], capture_output=True,
                                    timeout=3, check=False, env=env)
            if result.returncode:
                raise DisplayFailure("windows_path_unavailable")
            windows_path = result.stdout.decode("utf-8", errors="replace").strip().replace("\\", "/")
            if windows_path.startswith("//"):
                file_url = "file:" + quote(windows_path, safe="/:$@!~*'(),-._")
            else:
                file_url = "file:///" + quote(windows_path, safe="/:$@!~*'(),-._")
            # The argv contains only the owner-only bootstrap path. Its one-use
            # grant remains in the file and is POSTed in the form body.
            subprocess.Popen([edge, "--inprivate", "--new-window", file_url],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, env=env)
        except DisplayFailure:
            raise
        except (OSError, subprocess.SubprocessError):
            raise DisplayFailure("browser_launch_failed") from None


def current_backend() -> DisplayBackend | None:
    if sys.platform == "darwin":
        return MacOSDisplayBackend()
    if sys.platform.startswith("linux"):
        if _is_wsl():
            return WSLWindowsDisplayBackend()
        return LinuxDisplayBackend()
    return None

def validate_link(link: str, invitation_id: str) -> None:
    if not link.isascii() or len(link) > 2048 or any(ord(c) <= 32 for c in link):
        raise DisplayFailure("invalid_payload")
    try:
        parts = urlsplit(link)
        normalise_origin(f"{parts.scheme}://{parts.netloc}")
        fields = parse_qs(parts.fragment, strict_parsing=True)
        if (parts.path != "/pair" or parts.query or set(fields) != {"v", "i", "s"}
                or fields["v"] != ["1"] or fields["i"] != [invitation_id]
                or not re.fullmatch(r"[A-Za-z0-9_-]{22}", invitation_id)
                or len(fields["s"]) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", fields["s"][0])):
            raise DisplayFailure("invalid_payload")
    except (ValueError, KeyError):
        raise DisplayFailure("invalid_payload") from None

def display_capability(*, runner=subprocess.run) -> bool:
    # Capability is a prerequisite only. Authenticated paint acknowledgement is separate.
    backend = current_backend()
    if backend is None or not shutil.which("qrencode"):
        return False
    return backend.available(runner=runner)

def encode_png(payload: str, *, runner=subprocess.run) -> bytes:
    tool = shutil.which("qrencode")
    if tool is None:
        raise DisplayFailure("qr_encoder_unavailable")
    try:
        result = runner([tool, "-t", "PNG", "-o", "-"], input=payload.encode("ascii"),
                        capture_output=True, timeout=10, check=False)
        if (result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n")
                or len(result.stdout) > 512 * 1024):
            raise DisplayFailure("qr_encoding_failed")
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise DisplayFailure("qr_encoding_failed") from None

def open_browser(path: Path) -> None:
    backend = current_backend()
    if backend is None:
        raise DisplayFailure("unsupported_platform")
    backend.open(path)
