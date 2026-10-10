"""Actual HTTPS archive transport: owned headers, no redirects, finite bytes/time."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import shutil
import ssl
import subprocess
import threading
import time

import pytest

from lib.identity import PrincipalContext
from lib.skills.automation.mtskills_cli import (
    MTSkillsCLI, MTSkillsCLIError, OwnedMTSkillsCredential, source_policy,
)

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    os.name != 'posix' or not shutil.which('node') or not shutil.which('openssl'),
    reason='Local HTTPS Node archive port requires POSIX/openssl')]


@pytest.fixture
def archive_port(tmp_path):
    key, certificate = tmp_path/'key.pem', tmp_path/'ca.pem'
    subprocess.run([shutil.which('openssl'),'req','-x509','-newkey','rsa:2048','-nodes',
        '-keyout',str(key),'-out',str(certificate),'-days','1','-subj','/CN=fixture-source',
        '-addext','subjectAltName=IP:127.0.0.1'],check=True,timeout=15,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    requests = []
    payload = bytes(range(256))*2
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Fixture HTTP request logs must not print credential headers.
        def do_GET(self):
            requests.append({'path':self.path,'headers':dict(self.headers)})
            source = self.path.split('/')[-2]
            if source=='redirect':
                self.send_response(302);self.send_header('Location','https://unrelated.invalid/steal')
                self.end_headers();return
            if source=='unauthorized':
                self.send_response(401);self.end_headers();return
            self.send_response(200)
            if source=='declared-large':self.send_header('Content-Length','1025')
            elif source=='slow':self.send_header('Content-Length','32')
            self.end_headers()
            try:
                if source=='stream-large':
                    self.wfile.write(payload*4)
                elif source=='slow':
                    for _ in range(32):
                        self.wfile.write(b'x');self.wfile.flush();time.sleep(0.06)
                else:self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError,ssl.SSLError):
                pass  # This fixture explicitly exercises client abort/disconnect.
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=False
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate,key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=0.02))
    thread.start()
    policy=source_policy()
    policy['environments']['prod']['origin']=f'https://127.0.0.1:{server.server_port}'
    policy['archives'].update(maximumBytes=1024,timeoutSeconds=3)
    cli=tmp_path/'unused-cli.mjs';cli.write_text('export {};')
    port=MTSkillsCLI(cli_path=cli,node_path=shutil.which('node'),policy=policy,tls_ca_path=certificate)
    principal=PrincipalContext.user(subject_id='archive-owner',owner_user_id=7,
        tenant_id='source-tenant',scopes={'skills:read'})
    credential=OwnedMTSkillsCredential(7,'source-tenant','prod','fixture-source-only-token')
    try:yield port,principal,credential,requests,payload
    finally:
        server.shutdown();thread.join();server.server_close()


def test_binary_archive_uses_only_owned_source_headers(archive_port,monkeypatch):
    port,principal,credential,requests,payload=archive_port
    monkeypatch.setenv('UNRELATED_API_KEY','unrelated-host-secret')
    monkeypatch.setenv('NODE_TLS_REJECT_UNAUTHORIZED','0')
    assert port.download_archive('source-123',principal=principal,credential=credential)==payload
    request=requests[0]
    assert request['path']=='/mcphub-api/skill/source-123/download?channel=cli'
    assert request['headers']['access-token']==credential.token
    assert request['headers']['X-MTSKILLS-SSO-Env']=='product'
    assert 'Authorization' not in request['headers']


def test_redirect_is_never_followed_with_credentials(archive_port):
    port,principal,credential,requests,_=archive_port
    with pytest.raises(MTSkillsCLIError):
        port.download_archive('redirect',principal=principal,credential=credential)
    assert len(requests)==1


def test_authentication_failure_is_owned_and_redacted(archive_port):
    port,principal,credential,_,_=archive_port
    with pytest.raises(MTSkillsCLIError) as failure:
        port.download_archive('unauthorized',principal=principal,credential=credential)
    assert failure.value.code=='skills_provider_authentication'
    assert credential.token not in str(failure.value)


@pytest.mark.parametrize('source',['declared-large','stream-large'])
def test_both_declared_and_streamed_archive_bytes_are_bounded(archive_port,source):
    port,principal,credential,_,_=archive_port
    with pytest.raises(MTSkillsCLIError) as failure:
        port.download_archive(source,principal=principal,credential=credential)
    assert failure.value.output_limited


def test_absolute_deadline_interrupts_continuously_arriving_bytes(archive_port):
    port,principal,credential,_,_=archive_port
    port._policy['archives']['timeoutSeconds']=1
    with pytest.raises(MTSkillsCLIError) as failure:
        port.download_archive('slow',principal=principal,credential=credential)
    assert failure.value.timed_out


def test_certificate_verification_remains_enabled(archive_port,monkeypatch):
    port,principal,credential,requests,_=archive_port
    port._tls_ca=None
    monkeypatch.setenv('NODE_TLS_REJECT_UNAUTHORIZED','0')
    with pytest.raises(MTSkillsCLIError):
        port.download_archive('source-123',principal=principal,credential=credential)
    assert requests==[]


@pytest.mark.parametrize('code',[64,77,124])
def test_archive_error_mapping_preserves_orthogonal_observations(archive_port,monkeypatch,code):
    port,principal,credential,_,_=archive_port
    def interrupted(*args,**kwargs):
        raise MTSkillsCLIError('skills_provider_interrupted','Controlled overlapping outcomes',
            timed_out=True,cancelled=True,exit_code=code)
    monkeypatch.setattr(port,'_invoke',interrupted)
    with pytest.raises(MTSkillsCLIError) as failure:
        port.download_archive('source-123',principal=principal,credential=credential)
    assert failure.value.timed_out and failure.value.cancelled
    assert failure.value.exit_code==code
