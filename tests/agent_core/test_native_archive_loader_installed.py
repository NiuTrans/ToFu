"""Real pinned archive client executes and reuses one sealed image without extraction."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import shutil
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.native_process import NativeProcessClient,Budget,NativeConfigurationError
from tests.model_routing.test_native_route import principal
from tests.support.native_process_probe import config

pytestmark=pytest.mark.api


def test_shared_archive_keeps_principals_and_descriptor_lifetimes_independent(tmp_path):
    source=os.environ.get('TOFU_NATIVE_ARCHIVE_LOADER_TEST_ARCHIVE')
    if not source:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_artifact import seal_archive
    from lib.identity import PrincipalContext
    manifest=verify(Path(source),run=True)
    archive=tmp_path/'native.tar.gz';shutil.copyfile(source,archive)
    bodies=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            wire=b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    async def run():
        bounds=Budget(1,16<<20,65536,65536,5)
        image=NativeArchiveImage(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],budget=bounds)
        clients=[NativeProcessClient.from_shared_archive(image=image,
            principal=PrincipalContext.user(subject_id=str(owner),owner_user_id=owner),budget=bounds)
            for owner in (7,8,9)]
        async def execute(client):
            settings=config(f'http://127.0.0.1:{server.server_port}/chat',str(client.principal.owner_user_id))
            settings.update(owner_user_id=client.principal.owner_user_id,tools={})
            return await client.run(settings,callbacks={},working_directory=tmp_path)
        try:
            with patch('lib.agent_core.native_artifact.seal_archive',wraps=seal_archive) as seal:
                results=await asyncio.gather(*(execute(client) for client in clients[:2]))
                assert all(result['status']=='done' for result in results)
                assert seal.call_count==1
                descriptors=[client._image.result() for client in clients[:2]]
                assert os.fstat(descriptors[0].fileno()).st_ino==os.fstat(descriptors[1].fileno()).st_ino
                archive.unlink();await image.close()
                assert all(not descriptor.closed for descriptor in descriptors)
                assert (await execute(clients[1]))['status']=='done'
                with pytest.raises(NativeConfigurationError):await execute(clients[2])
                assert len(bodies)==3
                await clients[0].close()
                assert descriptors[0].closed and not descriptors[1].closed
        finally:
            for client in clients:await client.close()
            await image.close()
        assert all(client.active_runs==0 for client in clients)
        assert all(descriptor.closed for descriptor in descriptors)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:asyncio.run(run())
    finally:server.shutdown();thread.join();server.server_close()


def test_installed_archive_client_reuses_exact_image_after_archive_removal(tmp_path):
    source=os.environ.get('TOFU_NATIVE_ARCHIVE_LOADER_TEST_ARCHIVE')
    if not source:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    manifest=verify(Path(source),run=True)
    archive=tmp_path/'native.tar.gz';shutil.copyfile(source,archive)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    bodies=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            wire=('data: '+json.dumps({'choices':[{'delta':{'content':'done'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    settings=config(f'http://127.0.0.1:{server.server_port}/chat','archive-run');settings['tools']={}
    async def run():
        client=NativeProcessClient.from_archive(archive=archive,sha256=digest,application_version=manifest['build']['application_version'],principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
        image=None
        try:
            for index in range(2):
                result=await client.run(settings,callbacks={},working_directory=tmp_path)
                assert result['status']=='done'
                if index==0:
                    image=client._image.result()
                    archive.unlink()
                else:assert client._image.result() is image
        finally:await client.close()
        assert image.closed and client.active_runs==0
        assert list(tmp_path.iterdir())==[]
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:asyncio.run(run())
    finally:server.shutdown();thread.join();server.server_close()
    assert len(bodies)==2


def test_installed_archive_wrong_version_and_pin_refuse_before_any_process(tmp_path):
    source=os.environ.get('TOFU_NATIVE_ARCHIVE_LOADER_TEST_ARCHIVE')
    if not source:pytest.skip('explicit verified native archive required')
    archive=Path(source);digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    async def run():
        for pin,version in [('0'*64,'fixture'),(digest,'wrong-version')]:
            client=NativeProcessClient.from_archive(archive=archive,sha256=pin,application_version=version,principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
            try:
                settings=config('http://127.0.0.1:1/chat','refused');settings['tools']={}
                with pytest.raises(NativeConfigurationError):await client.run(settings,callbacks={},working_directory=tmp_path)
                assert client.active_runs==0
            finally:await client.close()
    asyncio.run(run())
