import argparse
import json
from pathlib import Path
import pytest
from fleet_connector.__main__ import configure, main, check_stock, check_existing_backend
from fleet_connector.settings import load, root, private_json

def args(source):
    return argparse.Namespace(hermes_root=str(source),origin="https://fixture.example.test",owner=[123],
        bot_id=456,port=9129,backend_port=9130,from_telegram_request=False,approve_owner_binding=True)

def test_setup_idempotency_mismatch_and_data_preservation(tmp_path,monkeypatch):
    from fleet_connector import __main__ as cli
    source=tmp_path/"source";source.mkdir()
    monkeypatch.setattr(cli,"check_stock",lambda p:source)
    monkeypatch.setattr(cli,"check_policy",lambda *a:None)
    sentinels={"config.yaml":b"unrelated: preserve\n","memory.db":b"unrelated fixture memory", "SOUL.md":b"preserve"}
    for name,data in sentinels.items():(tmp_path/name).write_bytes(data)
    configure(tmp_path,args(source))
    before=(root(tmp_path)/"settings.json").read_bytes()
    configure(tmp_path,args(source))
    assert (root(tmp_path)/"settings.json").read_bytes()==before
    change=args(source);change.port=9149
    with pytest.raises(ValueError):configure(tmp_path,change)
    for name,data in sentinels.items():assert (tmp_path/name).read_bytes()==data
    for name in ("devices.sqlite3","receipts.sqlite3"):(root(tmp_path)/name).write_bytes(b"fixture persistent state")
    (root(tmp_path)/"service.plist").write_bytes(b"prepared fixture")
    monkeypatch.setattr(cli,"control",lambda *a:(_ for _ in ()).throw(ConnectionRefusedError()))
    main(["--home",str(tmp_path),"rollback"])
    assert not (root(tmp_path)/"service.plist").exists()
    for name in ("devices.sqlite3","receipts.sqlite3"):assert (root(tmp_path)/name).read_bytes()==b"fixture persistent state"
    assert (root(tmp_path)/"settings.json").read_bytes()==before
    for name,data in sentinels.items():assert (tmp_path/name).read_bytes()==data

def test_expired_telegram_binding_refuses(tmp_path,monkeypatch):
    from fleet_connector import __main__ as cli
    monkeypatch.setattr(cli,"check_stock",lambda p:tmp_path)
    monkeypatch.setattr(cli,"check_policy",lambda *a:None)
    private_json(root(tmp_path)/"setup-request.json",dict(owner=123,bot_id=456,home=str(tmp_path),expires_at=0))
    a=args(tmp_path);a.from_telegram_request=True
    with pytest.raises(ValueError):configure(tmp_path,a)
    assert not (root(tmp_path)/"settings.json").exists()

def test_host_without_required_capabilities_refuses(tmp_path):
    with pytest.raises(ValueError, match="required plugin capabilities"):
        check_stock(tmp_path)
    with pytest.raises(ValueError, match="required plugin capabilities"):
        check_existing_backend(tmp_path)
def origin_only(source):
    return argparse.Namespace(hermes_root=str(source),origin="https://fixture.example.test",owner=None,
        bot_id=None,port=9129,backend_port=9130,from_telegram_request=False,approve_owner_binding=False)

def test_origin_only_setup_is_telegram_free_and_idempotent(tmp_path,monkeypatch):
    from fleet_connector import __main__ as cli
    source=tmp_path/"source";source.mkdir()
    monkeypatch.setattr(cli,"check_stock",lambda p:source)
    monkeypatch.setattr(cli,"check_policy",lambda *a:None)
    configure(tmp_path,origin_only(source))
    saved=json.loads((root(tmp_path)/"settings.json").read_text())
    assert "owners" not in saved and "bot_id" not in saved
    cfg=load(tmp_path)
    assert cfg["origin"]=="https://fixture.example.test"
    before=(root(tmp_path)/"settings.json").read_bytes()
    configure(tmp_path,origin_only(source))
    assert (root(tmp_path)/"settings.json").read_bytes()==before
    # A launchd plist for a complete Telegram-free config does not ask for pending setup.
    plan=cli.prepare_launchd(tmp_path,cfg)
    import plistlib
    assert "--pending-setup" not in plistlib.loads(Path(plan["plist"]).read_bytes())["ProgramArguments"]

def test_telegram_options_still_need_both_ids_and_approval(tmp_path,monkeypatch):
    from fleet_connector import __main__ as cli
    monkeypatch.setattr(cli,"check_stock",lambda p:tmp_path)
    monkeypatch.setattr(cli,"check_policy",lambda *a:None)
    half=origin_only(tmp_path);half.owner=[123]
    with pytest.raises(ValueError):configure(tmp_path,half)
    unapproved=args(tmp_path);unapproved.approve_owner_binding=False
    with pytest.raises(ValueError):configure(tmp_path,unapproved)
    assert not (root(tmp_path)/"settings.json").exists()

def test_load_still_rejects_malformed_telegram_fields(tmp_path):
    for extra in ({"owners":[0]},{"owners":"123"},{"owners":[True]},{"bot_id":"456"},{"bot_id":-1}):
        cfg=dict(home=str(tmp_path),origin="https://fixture.example.test",port=9129,backend_port=9130,**extra)
        private_json(root(tmp_path)/"settings.json",cfg)
        with pytest.raises(ValueError):load(tmp_path)
