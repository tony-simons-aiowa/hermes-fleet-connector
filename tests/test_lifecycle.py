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

def test_nonstock_source_refuses(tmp_path):
    import subprocess
    subprocess.run(["git","init",str(tmp_path)],check=True,capture_output=True)
    (tmp_path/"fixture").write_text("unrelated")
    subprocess.run(["git","-C",str(tmp_path),"add","."],check=True,capture_output=True)
    subprocess.run(["git","-C",str(tmp_path),"-c","user.name=Fixture","-c","user.email=fixture@example.test","commit","-m","fixture"],check=True,capture_output=True)
    with pytest.raises(ValueError):check_stock(tmp_path)
    with pytest.raises(ValueError):check_existing_backend(tmp_path)
    assert (tmp_path/"fixture").read_text()=="unrelated"

def test_dirty_supported_source_refuses(tmp_path,monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from fleet_connector.settings import STOCK_VERSION_SHAS
    supported_sha=STOCK_VERSION_SHAS["0.22.1"]
    def run(argv,**kwargs):
        return SimpleNamespace(stdout=(supported_sha+"\n") if "rev-parse" in argv else " M tracked.py\n")
    monkeypatch.setattr(subprocess,"run",run)
    with pytest.raises(ValueError):check_stock(tmp_path)
    def check_output(argv,**kwargs):
        return (supported_sha+"\n") if "rev-parse" in argv else " M tracked.py\n"
    monkeypatch.setattr(subprocess,"check_output",check_output)
    with pytest.raises(ValueError):check_existing_backend(tmp_path)
