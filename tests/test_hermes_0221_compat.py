"""Hermes v0.22.1 loader, doctor, and native Telegram integration checks.

Set HERMES_STOCK_ROOT to the clean official v0.22.1 checkout and put it first on
PYTHONPATH to run these checks against the real host APIs. No Telegram network
or account credentials are used.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from fleet_connector.settings import NAME, STOCK_VERSION_SHAS

EXPECTED_SHA = STOCK_VERSION_SHAS["0.22.1"]


def _official_0221():
    raw = os.environ.get("HERMES_STOCK_ROOT")
    if not raw:
        pytest.skip("Set HERMES_STOCK_ROOT to the clean official Hermes v0.22.1 checkout")
    source = Path(raw).resolve(strict=True)
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    if head != EXPECTED_SHA:
        pytest.skip(f"HERMES_STOCK_ROOT is {head}, not the v0.22.1 compatibility target")
    return source


def _install_under_home(home, plugin_dir):
    installed = home / "plugins" / NAME
    installed.mkdir(parents=True)
    shutil.copy2(plugin_dir / "plugin.yaml", installed / "plugin.yaml")
    shutil.copy2(plugin_dir / "__init__.py", installed / "__init__.py")
    shutil.copytree(plugin_dir / "fleet_connector", installed / "fleet_connector")
    (home / "config.yaml").write_text(f"plugins:\n  enabled:\n    - {NAME}\n")


def test_hermes_0221_manifest_loader_guard_and_doctor():
    source = _official_0221()
    from fleet_connector.__main__ import check_stock, check_existing_backend
    assert check_stock(source) == source
    assert check_existing_backend(source) == source

    from hermes_cli.plugins_manifest import parse_manifest_file, requires_hermes_error
    plugin_dir = Path(__file__).resolve().parents[1]
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "user", "")
    assert manifest is not None
    assert requires_hermes_error(manifest) is None

    from hermes_cli.plugin_validate import validate_plugin_dir
    report = validate_plugin_dir(plugin_dir)
    assert report.ok, report.failures
    checks = {name: (ok, detail) for name, ok, detail in report.checks}
    assert checks["requires_hermes"][0]
    assert checks["capability probe"][0]


def test_hermes_0221_real_discovery_and_private_telegram_authorization(tmp_path, monkeypatch):
    _official_0221()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123")
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    plugin_dir = Path(__file__).resolve().parents[1]
    _install_under_home(tmp_path, plugin_dir)

    from hermes_cli.plugins import PluginManager
    from hermes_cli.dashboard_auth.registry import list_providers, unregister_global_provider
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Message, Update

    manager = PluginManager()
    provider = None
    try:
        manager.discover_and_load()
        loaded = manager._plugins.get(NAME)
        assert loaded is not None and loaded.enabled and loaded.error is None
        factories = manager.get_platform_handler_factories("telegram")
        assert len(factories) == 1
        provider = next((p for p in list_providers() if p.name == "fleet-connector-backend"), None)
        assert provider is not None

        adapter = object.__new__(TelegramAdapter)
        adapter._authorization_check = None
        adapter._message_handler = None
        handlers = []
        factories[0][0](NS(add_handler=handlers.append), adapter)
        assert len(handlers) == 1
        replies = []

        async def reply_text(self, text, *args, **kwargs):
            replies.append(text)

        monkeypatch.setattr(Message, "reply_text", reply_text)
        update = Update.de_json({"update_id": 9, "message": {"message_id": 5, "date": 1800000000,
            "from": {"id": 123, "is_bot": False, "first_name": "Fixture"},
            "chat": {"id": 123, "type": "private", "first_name": "Fixture"}, "text": "/fleetsetup"}}, None)
        context = NS(bot=NS(id=456))
        import asyncio
        asyncio.run(handlers[0].callback(update, context))
        request = tmp_path / "plugin-data" / NAME / "setup-request.json"
        first = json.loads(request.read_text())
        assert first["owner"] == 123 and first["bot_id"] == 456
        assert "No invitation" in replies[-1]

        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS")
        asyncio.run(handlers[0].callback(update, context))
        assert json.loads(request.read_text()) == first
    finally:
        manager.unload()
        if provider is not None:
            unregister_global_provider(provider.name, provider)


@pytest.mark.parametrize("base_version,commit,dirty", [
    ("0.22.0", "b" * 40, False),
    ("0.22.1", EXPECTED_SHA, True),
    ("0.22.1", "b" * 40, False),
])
def test_real_loader_blocks_unsupported_or_dirty_runtime_before_registration(
        tmp_path, monkeypatch, base_version, commit, dirty):
    _official_0221()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    _install_under_home(tmp_path, Path(__file__).resolve().parents[1])

    from hermes_cli import version_info
    from hermes_cli.plugins import PluginManager
    from hermes_cli.dashboard_auth.registry import list_providers
    before = next((p for p in list_providers() if p.name == "fleet-connector-backend"), None)
    monkeypatch.setattr(version_info, "get_version_info",
                        lambda: NS(base_version=base_version, commit=commit, dirty=dirty))

    manager = PluginManager()
    try:
        manager.discover_and_load()
        loaded = manager._plugins.get(NAME)
        assert loaded is not None and loaded.error
        assert "Requires clean official Hermes" in loaded.error
        assert manager.get_platform_handler_factories("telegram") == []
        after = next((p for p in list_providers() if p.name == "fleet-connector-backend"), None)
        assert after is before
    finally:
        manager.unload()
