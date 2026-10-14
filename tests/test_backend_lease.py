import hashlib
import os
import secrets
import time
from pathlib import Path
import pytest
from fleet_connector.backend_provider import BackendLeaseProvider
from fleet_connector.settings import root, private_json

def lease(directory,token,**change):
    value=dict(home=str(directory),pid=os.getpid(),expires_at=time.time()+30,
               token_hash=hashlib.sha256(token.encode()).hexdigest())
    value.update(change)
    private_json(root(directory)/"backend-lease.json",value)

def test_host_lease_uses_supported_session_contract(tmp_path):
    token="hfcproxy."+secrets.token_urlsafe(32)
    lease(tmp_path,token)
    provider=BackendLeaseProvider(tmp_path)
    session=provider.verify_session(access_token=token)
    assert session is not None and session.user_id=="service:fleet-connector"
    assert session.provider==provider.name and not session.refresh_token
    assert provider.verify_session(access_token="hfd1.fixture.fixture") is None
    assert provider.verify_session(access_token="hfa1.fixture.fixture") is None
    (root(tmp_path)/"backend-lease.json").unlink()
    assert provider.verify_session(access_token=token) is None

@pytest.mark.parametrize("change",["expired","foreign_profile","dead_process","wrong_token","unsafe_permissions"])
def test_lease_failures_refuse(tmp_path,change):
    token="hfcproxy."+secrets.token_urlsafe(32)
    values={}
    if change=="expired":values['expires_at']=0
    if change=="foreign_profile":values['home']=str(tmp_path/'other')
    if change=="dead_process":values['pid']=2**30
    if change=="wrong_token":values['token_hash']='0'*64
    lease(tmp_path,token,**values)
    if change=="unsafe_permissions":(root(tmp_path)/"backend-lease.json").chmod(0o644)
    assert BackendLeaseProvider(tmp_path).verify_session(access_token=token) is None
