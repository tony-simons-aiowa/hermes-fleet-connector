import json
import os
import argparse
from pathlib import Path
import pytest
from fleet_connector.__main__ import prepare_host, pending_host, prepare_launchd
from fleet_connector.settings import root
from fleet_connector.service import Connector

def test_pending_host_has_no_issuance_authority(tmp_path,monkeypatch):
    from fleet_connector import __main__ as cli
    monkeypatch.setattr(cli,'check_existing_backend',lambda p:tmp_path)
    monkeypatch.setattr(cli,'check_policy',lambda *a:None)
    args=argparse.Namespace(hermes_root=str(tmp_path),origin='https://fixture.example.test:8443',port=9129,backend_port=9119)
    prepare_host(tmp_path,args)
    cfg=pending_host(tmp_path)
    c=Connector(tmp_path,cfg,'hfcproxy.'+'A'*43)
    assert cfg['owners']==[] and cfg['bot_id']==0
    result=c.owner(dict(action='issue',owner=123,bot_id=456,update_id=5,explicit_issue=True))
    assert result['ok'] is False
    assert not c.store.list_pending_invitations()
    assert not (root(tmp_path)/'settings.json').exists()
    prepare_host(tmp_path,args) # idempotent
    value=prepare_launchd(tmp_path,cfg)
    import plistlib
    plist=plistlib.loads(Path(value['plist']).read_bytes())
    assert '--pending-setup' in plist['ProgramArguments']
    assert plist['EnvironmentVariables']['PATH']==os.environ.get('PATH','/usr/bin:/bin')

def test_tampered_pending_plan_cannot_bind_an_owner(tmp_path,monkeypatch):
    from fleet_connector import __main__ as cli
    monkeypatch.setattr(cli,'check_existing_backend',lambda p:tmp_path)
    monkeypatch.setattr(cli,'check_policy',lambda *a:None)
    prepare_host(tmp_path,argparse.Namespace(hermes_root=str(tmp_path),origin='https://fixture.example.test',port=9129,backend_port=9119))
    p=root(tmp_path)/'host-plan.json';cfg=json.loads(p.read_text());cfg['owners']=[123];p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):pending_host(tmp_path)
