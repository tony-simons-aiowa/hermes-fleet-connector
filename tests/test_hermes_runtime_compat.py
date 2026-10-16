"""Hermes loader, doctor, and native Telegram integration checks.

Set HERMES_STOCK_ROOT to an official source checkout or installed package root
to run these checks against real host APIs. No Git metadata or Telegram
credentials are required.
"""
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from fleet_connector.settings import NAME


def _hermes_host_root():
    raw = os.environ.get("HERMES_STOCK_ROOT")
    if not raw:
        pytest.skip("Set HERMES_STOCK_ROOT to an official Hermes install or source root")
    source = Path(raw).resolve(strict=True)
    from fleet_connector.__main__ import check_stock
    check_stock(source)
    return source


def _install_under_home(home, plugin_dir):
    installed = home / "plugins" / NAME
    installed.mkdir(parents=True)
    shutil.copy2(plugin_dir / "plugin.yaml", installed / "plugin.yaml")
    shutil.copy2(plugin_dir / "__init__.py", installed / "__init__.py")
    shutil.copytree(plugin_dir / "fleet_connector", installed / "fleet_connector")
    (home / "config.yaml").write_text(f"plugins:\n  enabled:\n    - {NAME}\n")


def test_hermes_manifest_loader_guard_and_doctor():
    source = _hermes_host_root()
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


def test_hermes_real_discovery_and_private_telegram_authorization(tmp_path, monkeypatch):
    _hermes_host_root()
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


@pytest.mark.parametrize("missing", [
    "register_platform_handler",
    "register_dashboard_auth_provider",
])
def test_plugin_context_missing_public_capability_refuses_before_registration(missing):
    from fleet_connector.bridge import register

    registrations = []
    context = NS(
        register_platform_handler=lambda *args: registrations.append(("handler", args)),
        register_dashboard_auth_provider=lambda *args: registrations.append(("auth", args)),
    )
    delattr(context, missing)
    with pytest.raises(RuntimeError, match=missing):
        register(context)
    assert registrations == []


def test_missing_private_telegram_auth_method_fails_closed(caplog):
    from fleet_connector.bridge import _private_callback_authorized

    assert not _private_callback_authorized(NS(), "123", "123")
    assert "lacks its callback authorization method" in caplog.text
