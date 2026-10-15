"""Real TLS sockets and real stock Hermes process, with disposable profile and CA.

Run with HERMES_STOCK_ROOT pointing at a clean supported Hermes checkout. This does not
prove private Tailscale transport, production trust, Telegram delivery or a phone scan.
"""
import asyncio
import datetime
import ipaddress
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fleet_connector.service import Connector, make_app
from fleet_connector.settings import private_json, root

def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        return s.getsockname()[1]

@pytest.fixture(scope="module")
def network(tmp_path_factory):
    source=os.environ.get("HERMES_STOCK_ROOT")
    if not source:
        pytest.skip("Set HERMES_STOCK_ROOT for actual stock process tests")
    home=tmp_path_factory.mktemp("stock-process")
    bp,fp=port(),port()
    cfg=dict(home=str(home),origin=f"https://fixture.example.test:{fp}",owners=[123],bot_id=456,
             port=fp,backend_port=bp,display_name="TLS Fixture")
    private_json(root(home)/"settings.json",cfg)
    token=secrets.token_urlsafe(32)
    env=dict(os.environ,HERMES_HOME=str(home),PYTHONPATH=source,HERMES_DASHBOARD_SESSION_TOKEN=token)
    backend=subprocess.Popen([sys.executable,"-m","hermes_cli.main","serve","--host","127.0.0.1",
        "--port",str(bp),"--isolated","--skip-build"],cwd=source,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    c=Connector(home,cfg,token)
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"fixture.example.test")])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
        .not_valid_after(now+datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.example.test"),x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),critical=False)
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    certfile,keyfile=home/"fixture.pem",home/"fixture.key"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    keyfile.chmod(0o600)
    server=uvicorn.Server(uvicorn.Config(make_app(c),host="127.0.0.1",port=fp,proxy_headers=False,
        ssl_certfile=str(certfile),ssl_keyfile=str(keyfile),access_log=False,log_level="error"))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        with httpx.Client(timeout=1,trust_env=False) as probe:
            for _ in range(200):
                if backend.poll() is not None: pytest.fail("Stock backend exited during startup")
                try:
                    r=probe.get(c.backend+"/api/cron/jobs",headers={"X-Hermes-Session-Token":token})
                    if r.status_code==200 and server.started: break
                except httpx.HTTPError: pass
                time.sleep(.1)
            else: pytest.fail("Stock backend did not become authenticated-ready")
        ctx=ssl.create_default_context(cafile=str(certfile))
        with httpx.Client(base_url=f"https://127.0.0.1:{fp}",verify=ctx,trust_env=False,timeout=8,
                          headers={"Host":f"fixture.example.test:{fp}"}) as client:
            yield c,client,ctx
    finally:
        server.should_exit=True;thread.join(10)
        backend.terminate()
        try: backend.wait(timeout=12)
        except subprocess.TimeoutExpired: backend.kill();backend.wait(timeout=5)

def test_actual_stock_http_tls_ws_and_revocation(network):
    c,client,ctx=network
    issued=c.store.create_invitation(origin=c.cfg["origin"],created_by="fixture",ttl_seconds=60)
    proof=dict(invitation_id=issued.invitation.id,secret=issued.secret)
    assert client.post("/api/fleet/pairing/preview",json=proof).status_code==200
    data=client.post("/api/fleet/pairing/redeem",json=proof).json()
    assert client.post("/auth/device-login",json={"device_credential":data["credential"]}).status_code==200
    # Secure cookie domain belongs to the TLS transport address; the declared Host remains exact.
    ticket=client.post("/api/auth/ws-ticket").json()["ticket"]
    assert client.get("/api/cron/jobs").status_code==200
    assert client.get("/api/config").status_code==403
    async def rpc():
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        url=f"wss://127.0.0.1:{c.cfg['port']}/api/ws?ticket={ticket}"
        # websockets sets the transport Host; explicit override is not allowed as a duplicate.
        # A bounded fixture tunnel forwards TCP to loopback while retaining the declared DNS Host.
        url=f"wss://fixture.example.test:{c.cfg['port']}/api/ws?ticket={ticket}"
        async with connect(url,host="127.0.0.1",port=c.cfg["port"],ssl=ctx,proxy=None) as ws:
            await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"profiles.list","params":{}}))
            for _ in range(20):
                frame=json.loads(await asyncio.wait_for(ws.recv(),5))
                if frame.get("id")==1:
                    assert "result" in frame, frame
                    break
            else: pytest.fail("No real stock RPC response")
            c.store.revoke_device(data["device"]["id"])
            with pytest.raises(ConnectionClosed): await asyncio.wait_for(ws.recv(),3)
        with pytest.raises(InvalidStatus):
            async with connect(url,host="127.0.0.1",port=c.cfg["port"],ssl=ctx,proxy=None): pass
    asyncio.run(rpc())
    assert client.post("/api/auth/ws-ticket").status_code==401

def test_system_tls_refuses_fixture_ca(network):
    c,client,ctx=network
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"https://127.0.0.1:{c.cfg['port']}/fleet/health",trust_env=False,timeout=3)

def test_real_supervisor_sigterm_reaps_its_child(tmp_path):
    source=os.environ.get("HERMES_STOCK_ROOT")
    if not source:pytest.skip("Set HERMES_STOCK_ROOT for supervisor test")
    from fleet_connector.settings import private_json
    from fleet_connector.bridge import socket_path
    cp,bp=port(),port()
    cfg=dict(home=str(tmp_path),origin="https://fixture.example.test",owners=[123],bot_id=456,
        port=cp,backend_port=bp,hermes_root=source,version="0.1.0")
    private_json(root(tmp_path)/"settings.json",cfg)
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]))
    process=subprocess.Popen([sys.executable,"-m","fleet_connector","--home",str(tmp_path),"serve","--approve-activation"],
        env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    child=None
    try:
        for _ in range(200):
            if process.poll() is not None:pytest.fail("Supervisor failed to start")
            try:
                r=httpx.get(f"http://127.0.0.1:{cp}/fleet/health",trust_env=False,timeout=.2)
                if r.status_code==200:
                    child=json.loads((root(tmp_path)/"process.json").read_text())["backend_pid"]
                    break
            except (httpx.HTTPError,FileNotFoundError):pass
            time.sleep(.1)
        assert child is not None
        process.terminate();process.wait(timeout=20)
        assert process.returncode==0
        for p in (cp,bp):
            with socket.socket() as sock:assert sock.connect_ex(("127.0.0.1",p)) != 0
        with pytest.raises(ProcessLookupError):os.kill(child,0)
        assert not socket_path(tmp_path).exists()
        assert not (root(tmp_path)/"process.json").exists()
        assert (root(tmp_path)/"settings.json").exists()
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
        if child:
            try:os.kill(child,15)
            except ProcessLookupError:pass
