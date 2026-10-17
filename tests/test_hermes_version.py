"""Capability-based host checks for Gitless pip/Desktop/future Hermes installs."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from fleet_connector.__main__ import check_existing_backend, check_policy, check_stock


def _install_host(root: Path, *, missing=(), isolation="in_process", version="99.0.0") -> Path:
    root.mkdir(parents=True)
    missing = set(missing)
    package = root / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    methods = [
        f"    def {name}(self, *args, **kwargs): pass"
        for name in ("register_command", "register_cli_command",
                     "register_platform_handler", "register_dashboard_auth_provider")
        if name not in missing
    ]
    (package / "plugins.py").write_text("class PluginContext:\n" + "\n".join(methods) + "\n")
    if "isolation_mode" not in missing:
        (package / "plugin_isolation.py").write_text(
            f"def isolation_mode(): return {isolation!r}\n"
        )
    (package / "version_info.py").write_text(
        f"def get_version_info(): return {{'base_version': {version!r}, 'commit': None, 'dirty': True}}\n"
    )
    if "get_hermes_home" not in missing:
        (root / "hermes_constants.py").write_text(
            "from pathlib import Path\ndef get_hermes_home(): return Path('/tmp/hermes-fixture')\n"
        )
    (root / "hermes_bootstrap.py").write_text("# Minimal bootstrap import fixture.\n")
    return root


@pytest.mark.parametrize("install_shape,version", [
    ("venv/lib/python3.14/site-packages", "0.22.2"),
    ("Hermes.app/Contents/Resources/python/site-packages", "99.0.0"),
])
def test_gitless_install_shapes_accept_future_hosts_with_required_apis(tmp_path, install_shape, version):
    source = _install_host(tmp_path / install_shape, version=version)

    assert not (source / ".git").exists()
    assert check_stock(source) == source
    assert check_existing_backend(source) == source
    check_policy(tmp_path / "hermes-home", source)


@pytest.mark.parametrize("missing", [
    "register_command",
    "register_cli_command",
    "register_platform_handler",
    "register_dashboard_auth_provider",
])
def test_host_missing_required_plugin_context_method_refuses(tmp_path, missing):
    source = _install_host(tmp_path / "site-packages", missing=(missing,))

    with pytest.raises(ValueError, match=missing):
        check_stock(source)


@pytest.mark.parametrize("missing", ["get_hermes_home", "isolation_mode"])
def test_host_missing_required_hermes_module_api_refuses(tmp_path, missing):
    source = _install_host(tmp_path / "site-packages", missing=(missing,))

    with pytest.raises(ValueError, match="required plugin capabilities"):
        check_stock(source)


def test_dirty_source_tree_with_required_capabilities_is_allowed(tmp_path):
    source = _install_host(tmp_path / "source-install")
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    tracked = source / "tracked.py"
    tracked.write_text("before = True\n")
    subprocess.run(["git", "-C", str(source), "add", "tracked.py"], check=True, capture_output=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.test", "commit", "-m", "fixture",
    ], check=True, capture_output=True)
    tracked.write_text("after = True\n")

    assert check_stock(source) == source


def test_supported_api_does_not_override_in_process_requirement(tmp_path):
    source = _install_host(tmp_path / "host-install", isolation="host")
    assert check_stock(source) == source

    with pytest.raises(ValueError, match="effective host isolation"):
        check_policy(tmp_path / "hermes-home", source)


def test_manifest_has_minimum_hermes_version_without_upper_bound():
    manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    match = re.search(r'^requires_hermes:\s*["\']([^"\']+)["\']\s*$', manifest, re.MULTILINE)
    assert match
    assert match.group(1) == ">=0.21.6"
