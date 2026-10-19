"""Overleaf export: one compile, immutable bytes, ownership and finite lifetimes."""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import CancelledError, ThreadPoolExecutor
import hashlib
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import httpx
import pytest
from tests.support.paper_pdf import paper_pdf

pytestmark = pytest.mark.unit
PACKAGE = Path(__file__).resolve().parents[2] / 'packages/tofu-overleaf-mcp'
PROJECT = 'a' * 24
REMOTE_BUILD = 'compile-123'
REQUEST = {'schema_version': 1, 'owner_user_id': 7, 'project_id': PROJECT, 'request_id': 'b' * 32}
PDF = paper_pdf()
ARTIFACTS = {'output.pdf': PDF, 'output.synctex.gz': b'synthetic-synctex', 'output.log': b'This is pdfTeX\n**main.tex\nBuild complete'}


@pytest.fixture
def exports(monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    from tofu_overleaf_mcp import contract, export, server, transport
    return contract, export, server, transport


def test_unknown_tool_is_an_error_without_upstream_dispatch(exports):
    from types import SimpleNamespace
    from mcp.types import CallToolRequestParams, ListToolsResult
    _, _, server, _ = exports

    async def list_tools(ctx, params):
        return ListToolsResult(tools=[])

    async def call_tool(ctx, params):
        pytest.fail('An unknown tool must not reach upstream dispatch')

    service = server.create_server(None, SimpleNamespace(
        handle_list_tools=list_tools, handle_call_tool=call_tool),
        writes=SimpleNamespace())
    handler = service.get_request_handler('tools/call')
    result = asyncio.run(handler.handler(None, CallToolRequestParams(
        name='unknown_tool', arguments={})))
    assert result.is_error is True
    assert 'Unknown tool: unknown_tool' in result.content[0].text


class Body(httpx.AsyncByteStream):
    def __init__(self, value):
        self.value = value
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.value), 8192):
            yield self.value[offset:offset + 8192]

    async def aclose(self):
        self.closed = True


class Cloud:
    def __init__(self):
        self.requests = []
        self.streams = []
        self.artifacts = dict(ARTIFACTS)
        self.status = 'success'
        self.rows = [{'path': name, 'build': REMOTE_BUILD,
                     'url': f'/project/{PROJECT}/user/{"c" * 24}/build/{REMOTE_BUILD}/output/{name}'}
                    for name in self.artifacts]

    def handle(self, request):
        self.requests.append(request)
        if request.method == 'POST':
            assert request.headers['x-csrf-token'] == 'fixture-csrf'
            value = json.dumps({'status': self.status, 'outputFiles': self.rows,
                                'clsiServerId': 'fixture-clsi'}).encode()
        elif '/output/' in request.url.path:
            assert request.url.params['clsiserverid'] == 'fixture-clsi'
            value = self.artifacts[request.url.path.rsplit('/', 1)[1]]
        else:
            value = b'<meta name="ol-csrfToken" content="fixture-csrf">'
        stream = Body(value)
        self.streams.append(stream)
        return httpx.Response(200, stream=stream)

    def transport(self, module):
        return module.OverleafTransport('https://overleaf.test', lambda: {'Cookie': 'session=fixture-secret'},
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handle)))


def lease(manifest):
    return {key: manifest[key] for key in (*REQUEST, 'lease_token')}


def read_all(store, manifest, name):
    chunks = bytearray()
    while True:
        value = store.read({**lease(manifest), 'artifact': name, 'offset': len(chunks)})
        assert value['request_id'] == manifest['request_id']
        chunk = base64.b64decode(value['data'], validate=True)
        assert len(chunk) <= 65536
        chunks.extend(chunk)
        if value['eof']:
            return bytes(chunks)


def test_one_compile_exports_immutable_same_build_bytes_without_shared_paths(exports):
    _, module, server, transport = exports
    cloud = Cloud()

    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            manifest = await store.build(REQUEST)
            assert manifest['version_verified'] is False and manifest['source_digest'] is None
            assert manifest['remote_build_id'] == REMOTE_BUILD
            cloud.artifacts['output.pdf'] = b'changed-after-export'
            assert read_all(store, manifest, 'document.pdf') == PDF
            assert (await store.build(REQUEST))['lease_token'] == manifest['lease_token']
            assert sum(r.method == 'POST' for r in cloud.requests) == 1
            for artifact in manifest['artifacts']:
                data = read_all(store, manifest, artifact['name'])
                assert artifact['size_bytes'] == len(data)
                assert artifact['sha256'] == hashlib.sha256(data).hexdigest()
            files = list(next(iter(store.entries.values())).files.values())
            assert store.release(lease(manifest))['released']
            assert not store.release(lease(manifest))['released']
            assert all(file.closed for file in files)
        assert all(stream.closed for stream in cloud.streams)
    asyncio.run(run())
    tools = server.export_tools()
    assert len(json.dumps([tool.model_dump(by_alias=True) for tool in tools])) < 16000
    assert all(tool.output_schema for tool in tools)
    assert next(t for t in tools if t.name == 'paper_build_bundle').annotations.read_only_hint is False


@pytest.mark.parametrize('mutate', [
    lambda rows: rows[1].update(build='different-build'),
    lambda rows: rows[1].update(url=rows[1]['url'].replace(REMOTE_BUILD, 'different-build')),
    lambda rows: rows[0].update(url='https://attacker.test' + rows[0]['url']),
    lambda rows: rows[0].update(url=rows[0]['url'].replace(PROJECT, 'd' * 24)),
    lambda rows: rows.append(dict(rows[0])),
])
def test_rejects_mixed_builds_other_projects_and_untrusted_urls_before_download(exports, mutate):
    contract, module, _, transport = exports
    cloud = Cloud()
    mutate(cloud.rows)

    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            with pytest.raises(contract.ExportError, match='export_output_identity'):
                await store.build(REQUEST)
            assert not store.entries
        assert not any('/output/' in request.url.path for request in cloud.requests)
    asyncio.run(run())


def test_failed_compile_does_not_export_the_old_pdf(exports):
    _, module, _, transport = exports
    cloud = Cloud()
    cloud.status = 'failure'

    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            manifest = await store.build(REQUEST)
            assert manifest['compile_status'] == 'failed'
            assert {item['name'] for item in manifest['artifacts']} == {'document.synctex.gz', 'build.log'}
            assert not any(r.url.path.endswith('output.pdf') for r in cloud.requests)
    asyncio.run(run())


@pytest.mark.parametrize('redirect', [False, True])
def test_configured_cross_origin_downloads_never_receive_the_session_cookie(exports, redirect):
    _, module, _, transport = exports
    cloud = Cloud()
    original = cloud.handle
    if not redirect:
        for row in cloud.rows:
            row['url'] = 'https://cdn.test' + row['url']

    def handle(request):
        if redirect and request.url.host == 'overleaf.test' and '/output/' in request.url.path:
            cloud.requests.append(request)
            return httpx.Response(302, headers={'Location': 'https://cdn.test' + request.url.path})
        return original(request)

    async def run():
        provider = transport.OverleafTransport('https://overleaf.test',
            lambda: {'Cookie': 'session=fixture-secret'}, output_origins=['https://cdn.test'],
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        async with module.ExportStore(provider) as store:
            manifest = await store.build(REQUEST)
            assert read_all(store, manifest, 'document.pdf') == PDF
        for request in cloud.requests:
            assert ('cookie' in request.headers) == (request.url.host == 'overleaf.test')
        assert any(request.url.host == 'cdn.test' for request in cloud.requests)
    asyncio.run(run())


def test_redirect_cannot_choose_a_credential_destination(exports):
    contract, module, _, transport = exports
    cloud = Cloud()
    original = cloud.handle

    def handle(request):
        if '/output/' in request.url.path:
            cloud.requests.append(request)
            return httpx.Response(302, headers={'Location': 'https://attacker.test' + request.url.path})
        return original(request)
    cloud.handle = handle

    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            with pytest.raises(contract.ExportError, match='export_output_identity'):
                await store.build(REQUEST)
            assert not store.entries
        assert all(request.url.host == 'overleaf.test' for request in cloud.requests)
    asyncio.run(run())


@pytest.mark.parametrize('replacement', [
    {'schema_version': True}, {'schema_version': 1.0}, {'schema_version': 2},
    {'owner_user_id': True}, {'owner_user_id': 0}, {'project_id': PROJECT + '\n'},
    {'request_id': 'a' * 33}, {'output_path': '/tmp/unowned.pdf'},
])
def test_build_contract_fails_before_any_remote_work(exports, replacement):
    contract, module, _, transport = exports
    cloud = Cloud()

    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            with pytest.raises(contract.ExportError, match='export_contract'):
                await store.build({**REQUEST, **replacement})
        assert not cloud.requests
    asyncio.run(run())


def test_legacy_zero_cache_hint_cannot_disable_authorized_export(exports):
    _, module, server, transport = exports

    async def run():
        async with module.ExportStore(Cloud().transport(transport), cache_bytes=0) as store:
            assert server.create_server(store, None)
            result = await store.build(REQUEST)
            assert read_all(store, result, 'document.pdf') == PDF
    asyncio.run(run())


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(schema_version=1.0),
    lambda value: value['artifacts'].append(dict(value['artifacts'][0])),
    lambda value: value['artifacts'][0].update(name='sources.json'),
    lambda value: value.update(compile_status='failed'),
    lambda value: value['artifacts'][0].update(name='build.log'),
])
def test_host_and_producer_reject_invalid_manifest_semantics(exports, mutate):
    from lib.paper.builds.contract import validate as host_validate
    from lib.paper.builds.errors import BuildError
    contract = exports[0]
    value = {**REQUEST, 'lease_token': 'e' * 64, 'remote_build_id': REMOTE_BUILD,
             'compile_status': 'success', 'version_verified': False, 'source_digest': None, 'main_file': 'main.tex',
             'expires_in_seconds': 120, 'artifacts': [{'name': 'document.pdf',
             'sha256': hashlib.sha256(PDF).hexdigest(), 'size_bytes': len(PDF)}]}
    mutate(value)
    with pytest.raises(contract.ExportError):
        contract.validate(value, 'PaperMcpManifestV1')
    with pytest.raises(BuildError):
        host_validate(value, 'PaperMcpManifestV1')


@pytest.mark.parametrize('replacement', [
    {'owner_user_id': 8}, {'project_id': 'd' * 24}, {'request_id': 'e' * 32}, {'lease_token': 'f' * 64},
])
def test_lease_requires_owner_project_request_and_token(exports, replacement):
    contract, module, _, transport = exports
    cloud = Cloud()

    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            manifest = await store.build(REQUEST)
            wrong = {**lease(manifest), **replacement}
            with pytest.raises(contract.ExportError, match='export_lease_missing'):
                store.read({**wrong, 'artifact': 'document.pdf', 'offset': 0})
            assert not store.release(wrong)['released']
            assert read_all(store, manifest, 'document.pdf') == PDF
    asyncio.run(run())


def test_expiry_and_shutdown_close_files_without_extending_a_lease(exports):
    contract, module, _, transport = exports
    now = [10.0]

    async def run():
        async with module.ExportStore(Cloud().transport(transport), clock=lambda: now[0]) as store:
            manifest = await store.build(REQUEST)
            files = list(next(iter(store.entries.values())).files.values())
            now[0] += 30
            again = await store.build(REQUEST)
            assert again['expires_in_seconds'] == manifest['expires_in_seconds'] - 30
            now[0] += manifest['expires_in_seconds']
            with pytest.raises(contract.ExportError, match='export_lease_missing'):
                store.read({**lease(manifest), 'artifact': 'document.pdf', 'offset': 0})
            assert all(file.closed for file in files)
            await store.build({**REQUEST, 'request_id': 'c' * 32})
            files = list(next(iter(store.entries.values())).files.values())
        assert all(file.closed for file in files)
        assert store.retirement.done()
    asyncio.run(run())


def test_independent_exports_keep_live_leases_without_cache_reservation(exports):
    _, module, _, transport = exports
    cloud = Cloud()

    async def run():
        async with module.ExportStore(cloud.transport(transport), cache_bytes=1) as store:
            first = await store.build(REQUEST)
            second = await store.build({**REQUEST, 'request_id': 'c' * 32})
            assert sum(r.method == 'POST' for r in cloud.requests) == 2
            assert read_all(store, first, 'document.pdf') == PDF
            assert read_all(store, second, 'document.pdf') == PDF
            store.release(lease(first))
            assert read_all(store, second, 'document.pdf') == PDF
    asyncio.run(run())


@pytest.mark.parametrize('finish', ['cancel', 'timeout', 'shutdown'])
def test_cancellation_timeout_and_shutdown_retire_network_and_partial_files(exports, finish):
    contract, module, _, transport = exports
    held = asyncio.Event()
    retired = []
    files = []

    class HeldBody(Body):
        async def __aiter__(self):
            yield b'x' * 65536
            held.set()
            await asyncio.Event().wait()

        async def aclose(self):
            retired.append(True)

    cloud = Cloud()
    original = cloud.handle

    def handle(request):
        if '/output/' in request.url.path:
            return httpx.Response(200, stream=HeldBody(b''))
        return original(request)
    cloud.handle = handle

    def file_factory(**kwargs):
        file = tempfile.TemporaryFile(**kwargs)
        files.append(file)
        return file

    async def run():
        async with module.ExportStore(cloud.transport(transport), file_factory=file_factory,
                compile_seconds=0.2 if finish == 'timeout' else 10) as store:
            task = asyncio.create_task(store.build(REQUEST))
            await asyncio.wait_for(held.wait(), 2)
            if finish == 'cancel':
                task.cancel()
            elif finish == 'shutdown':
                await store.aclose()
            with pytest.raises(TimeoutError if finish == 'timeout' else asyncio.CancelledError):
                await task
            assert retired and files and all(file.closed for file in files)
            assert not store.entries
    asyncio.run(run())


def test_oversized_chunk_fails_and_closes_partial_export(exports, monkeypatch):
    contract, module, _, transport = exports
    cloud = Cloud()
    files = []

    def file_factory(**kwargs):
        file = tempfile.TemporaryFile(**kwargs)
        files.append(file)
        return file

    original_limit = module.limit
    monkeypatch.setattr(module, 'limit', lambda name: 100 if name == 'transfer_chunk_bytes' else original_limit(name))

    async def run():
        async with module.ExportStore(cloud.transport(transport),
                                      file_factory=file_factory) as store:
            with pytest.raises(contract.ExportError, match='export_artifact_limit'):
                await store.build(REQUEST)
            assert files and all(file.closed for file in files)
            assert not store.entries
    asyncio.run(run())


def test_close_error_cannot_retain_other_artifacts_or_the_lease(exports):
    _, module, _, transport = exports
    files = []

    class CloseFault:
        def __init__(self, **kwargs):
            self.file = tempfile.TemporaryFile(**kwargs)
            files.append(self.file)

        def __getattr__(self, name):
            return getattr(self.file, name)

        def close(self):
            self.file.close()
            raise OSError('simulated buffered close error')

    async def run():
        async with module.ExportStore(Cloud().transport(transport), file_factory=CloseFault) as store:
            manifest = await store.build(REQUEST)
            assert store.release(lease(manifest))['released']
            assert files and all(file.closed for file in files)
            assert not store.entries
    asyncio.run(run())


@pytest.mark.parametrize('line, expected', [
    (b'**main.tex', 'main.tex'), (b'**"chapters/paper.tex"', 'chapters/paper.tex'),
    (b'**/compile/main.tex', 'main.tex'),
])
def test_manifest_identifies_the_main_file_from_the_same_compiler_log(exports, line, expected):
    _, module, _, transport = exports
    cloud = Cloud()
    cloud.artifacts['output.log'] = b'This is pdfTeX\n' + line + b'\nCompiled'
    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            manifest = await store.build({**REQUEST, 'main_file': expected})
            assert manifest['main_file'] == expected
    asyncio.run(run())


def test_expected_main_and_replayed_request_options_cannot_silently_change(exports):
    contract, module, _, transport = exports
    async def run():
        async with module.ExportStore(Cloud().transport(transport)) as store:
            with pytest.raises(contract.ExportError, match='overleaf_main_file_changed'):
                await store.build({**REQUEST, 'main_file': 'another.tex'})
            assert not store.entries
            await store.build({**REQUEST, 'main_file': 'main.tex'})
            with pytest.raises(contract.ExportError, match='export_request_mismatch'):
                await store.build(REQUEST)
    asyncio.run(run())


def test_unknown_main_cannot_publish_a_success_but_preserves_failed_compile_logs(exports):
    contract, module, _, transport = exports
    cloud = Cloud()
    cloud.artifacts['output.log'] = b'Compiler could not start'
    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            with pytest.raises(contract.ExportError, match='overleaf_main_file_unavailable'):
                await store.build(REQUEST)
            cloud.status = 'failure'
            manifest = await store.build(REQUEST)
            assert manifest['main_file'] is None and manifest['compile_status'] == 'failed'
            assert read_all(store, manifest, 'build.log') == b'Compiler could not start'
    asyncio.run(run())


@pytest.mark.api
@pytest.mark.parametrize('launcher', ['installed', 'catalog'])
def test_packaged_upstream_server_exports_through_real_mcp_stdio(tmp_path, monkeypatch, launcher, flask_app):
    """Opt-in interpreter has the independently installed extension + pinned upstream.

    Tofu's real MCP bridge talks to another process and publishes to its shared
    cache. The fixture owns the Overleaf HTTP service and conversation settings.
    """
    executable = os.environ.get('TOFU_OVERLEAF_TEST_PYTHON')
    if not executable:
        pytest.skip('Set TOFU_OVERLEAF_TEST_PYTHON to the installed extension interpreter')
    from lib.mcp.client import MCPBridge
    from lib.mcp.documents import MCPDocumentError
    from lib.paper.builds import overleaf, overleaf_capabilities, service
    from lib.paper.builds.documents import select_document
    from lib.paper.builds.cache import LocalBuildCache
    from lib.paper.builds.runtime import PreviewBuildScheduler
    from lib.conversations import settings_store

    cloud = Cloud()
    hold_pdf = threading.Event()
    held_pdf = threading.Event()
    retired_pdf = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            size = int(self.headers.get('Content-Length', 0))
            assert size < 4096
            body = self.rfile.read(size)
            result = cloud.handle(httpx.Request(self.command, base_url + self.path,
                                               headers=dict(self.headers), content=body))
            data = result.stream.value
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if '/output/output.pdf' in self.path and hold_pdf.is_set():
                hold_pdf.clear()
                self.wfile.write(data[:65536])
                self.wfile.flush()
                held_pdf.set()
                # The MCP cancellation must close this HTTP connection while
                # the response is incomplete, not merely stop the UI poller.
                self.connection.settimeout(5)
                if self.connection.recv(1) == b'':
                    retired_pdf.set()
                return
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    http_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http_server.daemon_threads = True
    base_url = f'http://127.0.0.1:{http_server.server_port}'
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    bridge = MCPBridge()
    settings = {}

    def owned_settings(conv_id, owner_user_id):
        assert (conv_id, owner_user_id) == ('c', REQUEST['owner_user_id'])
        return json.loads(json.dumps(settings))

    def update_settings(conv_id, mutate, *, user_id):
        candidate = owned_settings(conv_id, user_id)
        mutate(candidate); settings.clear(); settings.update(candidate)
        return json.loads(json.dumps(settings))

    monkeypatch.setattr(service, 'owned_settings', owned_settings)
    monkeypatch.setattr(settings_store, 'update_conversation_settings', update_settings)
    def owned_bridge(owner_user_id):
        assert owner_user_id == REQUEST['owner_user_id']
        return bridge
    monkeypatch.setattr(overleaf_capabilities, '_bridge', owned_bridge)
    monkeypatch.setattr(overleaf, '_bridge', owned_bridge)
    from lib.paper.builds import documents
    monkeypatch.setattr(documents, 'owned_settings', owned_settings)
    temporary = tmp_path / 'producer-temp'
    temporary.mkdir()
    try:
        from lib.mcp.registry import get_catalog_entry
        catalog = get_catalog_entry('overleaf')
        if launcher == 'catalog':
            # App installation and boot finish this existing preparation stage
            # before the MCP handshake; dependency resolution has its own budget.
            from lib.mcp.client import prewarm_vendored_launcher
            ready, detail = prewarm_vendored_launcher(catalog['command'])
            assert ready, detail
        bridge.connect_server('renamed-paper-service', {
            'command': executable if launcher == 'installed' else catalog['command'],
            'args': ['-m', 'tofu_overleaf_mcp.server'] if launcher == 'installed' else catalog['args'], 'enabled': True,
            'env': {'OVERLEAF_BASE_URL': base_url, 'OVERLEAF_SESSION': 'fixture-session',
                    'OVERLEAF_GIT_TOKEN': 'fixture-git', 'OVERLEAF_TEMP_DIR': str(tmp_path / 'git'),
                    'TMPDIR': str(temporary), 'TOFU_OVERLEAF_OUTPUT_ORIGINS': base_url},
        })

        def call(name, args):
            return bridge.call_tool_document('mcp__renamed-paper-service__' + name, args, timeout=10)

        manifest = call('paper_build_bundle', REQUEST)
        assert manifest['source_digest'] is None and not manifest['version_verified']
        assert (call('paper_build_bundle', REQUEST))['lease_token'] == manifest['lease_token']
        cloud.artifacts['output.pdf'] = b'newer-remote-output'
        received = bytearray()
        while True:
            chunk = call('paper_read_artifact', {**lease(manifest), 'artifact': 'document.pdf',
                                                'offset': len(received)})
            received.extend(base64.b64decode(chunk['data'], validate=True))
            if chunk['eof']:
                break
        assert bytes(received) == PDF
        assert sum(request.method == 'POST' for request in cloud.requests) == 1
        assert call('paper_release_build', lease(manifest))['released']
        with pytest.raises(MCPDocumentError):
            call('paper_read_artifact', {**lease(manifest), 'artifact': 'document.pdf', 'offset': 0})
        cloud.artifacts['output.pdf'] = PDF
        hold_pdf.set()
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(bridge.call_tool_document,
                'mcp__renamed-paper-service__paper_build_bundle',
                {**REQUEST, 'request_id': 'd' * 32}, timeout=10, cancel=cancel)
            try:
                assert held_pdf.wait(5)
            finally:
                cancel.set()
            with pytest.raises(CancelledError):
                future.result(timeout=5)
        assert retired_pdf.wait(5)
        # Cancellation releases admission as well as the transfer connection.
        next_manifest = call('paper_build_bundle', {**REQUEST, 'request_id': 'e' * 32})
        assert next_manifest['compile_status'] == 'success'
        call('paper_release_build', lease(next_manifest))
        # The same real MCP connection must also work through the application
        # document service and ordinary scheduler/cache, with no shared paths.
        selected = select_document({'conv_id': 'c', 'project_id': PROJECT,
            'mcp_server': 'renamed-paper-service', 'main_file': 'main.tex'}, owner_user_id=REQUEST['owner_user_id'])
        assert selected['selected']['source'] == 'overleaf'
        scheduler = PreviewBuildScheduler(LocalBuildCache(tmp_path/'host-cache'))
        try:
            accepted = service.start_preview({'source': 'overleaf', 'conv_id': 'c', 'project_id': PROJECT,
                'mcp_server': 'renamed-paper-service', 'main_file': 'main.tex',
                'request_id': 'application', 'subscriber_id': 'viewer'}, owner_user_id=REQUEST['owner_user_id'], scheduler=scheduler)
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                task = scheduler.production.runtime.get_owned(accepted['task_id'], user_id=REQUEST['owner_user_id'])
                if task['status'] not in ('pending', 'running'):
                    break
                threading.Event().wait(0.02)
            assert task['status'] == 'done', task.get('error')
            receipt = task['result']['receipt']
            assert receipt['target'] == 'overleaf' and receipt['main_file'] == 'main.tex'
            assert not receipt['version_verified'] and task['result']['location'] is None
            with scheduler.cache.open_artifact(REQUEST['owner_user_id'], receipt['document_id'], receipt['build_id'], 'document.pdf') as (stream, metadata):
                assert stream.read() == PDF and metadata['size_bytes'] == len(PDF)
        finally:
            scheduler.shutdown()
    finally:
        bridge.disconnect_all()
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=3)
    assert not list(temporary.iterdir())


def test_32_independent_exports_start_and_retain_every_lease(exports):
    _, module, _, _ = exports

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        files = []

        class Transport:
            async def compile(self, project_id, write_artifact):
                nonlocal calls
                calls += 1
                if calls == 32:
                    entered.set()
                await release.wait()
                async def chunks(data):
                    for offset in range(0, len(data), 65536):
                        yield data[offset:offset + 65536]
                await write_artifact('document.pdf', chunks(PDF))
                await write_artifact('build.log', chunks(b'**main.tex\n'))
                return 'compile-123', 'success'

        def file_factory(**kwargs):
            file = tempfile.TemporaryFile(**kwargs)
            files.append(file)
            return file

        started = time.monotonic()
        async with module.ExportStore(Transport(), cache_bytes=0, file_factory=file_factory) as store:
            tasks = [asyncio.create_task(store.build({**REQUEST,
                'project_id': f'{i:024x}', 'request_id': f'{i + 1:032x}'})) for i in range(32)]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert len(store.entries) == 32
            finally:
                release.set()
            manifests = await asyncio.gather(*tasks)
            assert len(store.entries) == 32
            assert all(read_all(store, manifest, 'document.pdf') == PDF for manifest in manifests)
            assert len(files) == 64
        assert all(file.closed for file in files)
        print(f'32 exports: {(time.monotonic() - started) * 1000:.2f} ms; 64 artifact FDs retired')
    asyncio.run(run())


def test_duplicate_request_waiter_cancel_does_not_cancel_producer(exports):
    _, module, _, _ = exports

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        class Transport:
            async def compile(self, project_id, write_artifact):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                async def chunks():
                    yield b'**main.tex\n'
                await write_artifact('build.log', chunks())
                return 'compile-123', 'failed'

        async with module.ExportStore(Transport()) as store:
            producer = asyncio.create_task(store.build(REQUEST))
            await entered.wait()
            abandoned = asyncio.create_task(store.build(REQUEST))
            survivor = asyncio.create_task(store.build(REQUEST))
            await asyncio.sleep(0)
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
            assert not producer.done()
            release.set()
            original, shared = await asyncio.gather(producer, survivor)
            assert original['lease_token'] == shared['lease_token']
            assert calls == 1
    asyncio.run(run())


def test_real_file_creation_failure_cleans_only_its_export(exports):
    contract, module, _, transport = exports
    fail = False

    def factory(**kwargs):
        if fail:
            raise OSError('injected real disk failure')
        return tempfile.TemporaryFile(**kwargs)

    async def run():
        nonlocal fail
        async with module.ExportStore(Cloud().transport(transport), file_factory=factory) as store:
            first = await store.build(REQUEST)
            fail = True
            with pytest.raises(contract.ExportError, match='export_storage_unavailable'):
                await store.build({**REQUEST, 'request_id': 'c' * 32})
            assert len(store.entries) == 1
            assert read_all(store, first, 'document.pdf') == PDF
    asyncio.run(run())


def test_export_execution_accepts_default_and_long_explicit_deadlines(exports):
    _,module,_,transport=exports
    async def run():
        for seconds in (None,420):
            cloud=Cloud()
            async with module.ExportStore(cloud.transport(transport),compile_seconds=seconds) as store:
                assert store.compile_seconds==seconds
                manifest=await store.build(REQUEST)
                assert read_all(store,manifest,'document.pdf')==PDF
    asyncio.run(run())


def test_complete_cloud_build_log_above_old_quota_is_exported(exports):
    _,module,_,transport=exports
    cloud=Cloud();body=b'cloud progress\n'*100000+b'**main.tex\ncomplete log tail\n'
    assert len(body)>1024*1024;cloud.artifacts['output.log']=body
    async def run():
        async with module.ExportStore(cloud.transport(transport)) as store:
            manifest=await store.build(REQUEST)
            assert read_all(store,manifest,'build.log')==body
    asyncio.run(run())
