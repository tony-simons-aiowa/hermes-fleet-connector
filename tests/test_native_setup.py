"""Real PluginContext, real PTB Update and native handler callbacks. No Telegram network."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from telegram import Update
from fleet_connector.bridge import register
from fleet_connector.settings import root

def test_real_registration_captures_authenticated_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME",str(tmp_path))
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    manifest=PluginManifest(name="hermes-fleet-connector",version="0.1.0",description="fixture",
                            path=str(Path(__file__).parents[1]),source="user")
    manager=PluginManager()
    context=PluginContext(manifest,manager)
    register(context)
    factories=manager.get_platform_handler_factories("telegram")
    assert len(factories)==1
    callbacks=[]
    native=NS(add_handler=lambda h:callbacks.append(h))
    allowed=[True]
    adapter=NS(_is_callback_user_authorized=lambda *a,**kw:allowed[0])
    factories[0][0](native,adapter)
    replies=[]
    async def reply(self,text,*a,**kw):replies.append(text)
    from telegram import Message
    monkeypatch.setattr(Message,"reply_text",reply)
    update=Update.de_json({"update_id":9,"message":{"message_id":5,"date":1800000000,
        "from":{"id":123,"is_bot":False,"first_name":"Fixture"},
        "chat":{"id":123,"type":"private","first_name":"Fixture"},"text":"/fleetsetup"}},None)
    asyncio.run(callbacks[0].callback(update,NS(bot=NS(id=456))))
    recorded=json.loads((root(tmp_path)/"setup-request.json").read_text())
    assert recorded["owner"]==123 and recorded["bot_id"]==456 and recorded["update_id"]==9
    assert not (root(tmp_path)/"devices.sqlite3").exists()
    assert "No invitation" in replies[-1]
    before=(root(tmp_path)/"setup-request.json").read_bytes()
    allowed[0]=False
    asyncio.run(callbacks[0].callback(update,NS(bot=NS(id=456))))
    assert (root(tmp_path)/"setup-request.json").read_bytes()==before

def test_managed_isolation_is_not_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME",str(tmp_path))
    (tmp_path/"config.yaml").write_text("plugins:\n  isolation: host\n")
    from fleet_connector.bridge import register
    with pytest.raises(RuntimeError):register(NS())
    assert "isolation: host" in (tmp_path/"config.yaml").read_text()
