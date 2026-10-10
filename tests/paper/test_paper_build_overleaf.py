"""Owned Overleaf selection, schema drift, artifact transfer and publication fences."""
from __future__ import annotations

import base64
import copy
from dataclasses import replace
from concurrent.futures import CancelledError
import hashlib
import threading
import time

import pytest

from lib.paper.builds import overleaf, overleaf_capabilities as capabilities, service
from lib.paper.builds.contract import schema
from lib.paper.builds.errors import BuildError
from tests.support import paper_builds as support
from tests.support.paper_pdf import paper_objects, pdf_from_objects, paper_pdf

pytestmark = pytest.mark.unit
scheduler = support.scheduler
PROJECT = 'a' * 24
SERVER = 'renamed-papers'
SELECTION = {'conv_id': 'c', 'project_id': PROJECT, 'mcp_server': SERVER}
REQUEST = {**SELECTION, 'source': 'overleaf', 'request_id': 'edit-1', 'subscriber_id': 'viewer-1'}



class Bridge:
    def __init__(self):
        self.catalog = []
        for role, specification in schema()['x-mcp-tools'].items():
            self.catalog.append(copy.deepcopy({'server_name': SERVER,
                'namespaced_name': f'mcp__{SERVER}__{specification["name"]}',
                'openai_def': {'function': {'parameters': {
                    **schema()['$defs'][specification['input']], '$defs': schema()['$defs']}}},
                'output_schema': {**schema()['$defs'][specification['output']], '$defs': schema()['$defs']},
                'read_only_hint': role == 'read', 'annotations': {'readOnlyHint': role == 'read'},
                'schema_hash': 'provider-controlled-constant',
                'meta': {'tofu/paper-build': {'schema_version': 1, 'role': role,
                                            'version_verified': False, 'remote_cancel': False}}}))
        self.calls = []
        self.exports = {}
        self.pdf = paper_pdf()
        self.mutate_manifest = lambda value: None
        self.mutate_chunk = lambda value: None
        self.on_read = lambda: None
        self.status = 'success'
        self.hold = None
        self.barrier = None
        self.read_started = threading.Event()
        self.active_reads = self.maximum_reads = 0
        self.lock = threading.Lock()

    def get_tool_catalog_snapshot(self):
        return copy.deepcopy(self.catalog)

    def call_tool_document(self, name, args, *, timeout, cancel=None):
        assert 0 < timeout <= 300
        with self.lock:
            self.calls.append((name, copy.deepcopy(args)))
        identity = {key: args[key] for key in ('schema_version', 'owner_user_id', 'project_id', 'request_id')}
        if name.endswith('__paper_build_bundle'):
            files = {'build.log': b'**main.tex\nBuild fixture', 'document.pdf': self.pdf}
            if self.status == 'failed':
                files.pop('document.pdf')
            self.exports[args['request_id']] = files
            result = {**identity, 'lease_token': 'b' * 64, 'remote_build_id': 'remote-build-1',
                'compile_status': self.status, 'source_digest': None, 'version_verified': False,
                'main_file': 'main.tex', 'expires_in_seconds': 120,
                'artifacts': [{'name': key, 'size_bytes': len(value),
                    'sha256': hashlib.sha256(value).hexdigest()} for key, value in files.items()]}
            self.mutate_manifest(result)
            return result
        if name.endswith('__paper_release_build'):
            self.exports.pop(args['request_id'], None)
            return {**identity, 'released': True}
        assert name.endswith('__paper_read_artifact')
        assert args['lease_token'] == 'b' * 64
        with self.lock:
            self.active_reads += 1
            self.maximum_reads = max(self.maximum_reads, self.active_reads)
        self.read_started.set()
        try:
            if (self.barrier is not None and args['artifact'] == 'document.pdf'
                    and args['offset'] < self.barrier.parties * 65536):
                self.barrier.wait(timeout=2)
            if self.hold is not None:
                while not self.hold.wait(0.01):
                    if cancel is not None and cancel.is_set():
                        raise CancelledError()
            self.on_read()
            data = self.exports[args['request_id']][args['artifact']]
            chunk = data[args['offset']:args['offset'] + 65536]
            result = {**identity, 'artifact': args['artifact'], 'offset': args['offset'],
                      'data': base64.b64encode(chunk).decode(), 'eof': args['offset'] + len(chunk) == len(data)}
            self.mutate_chunk(result)
            return result
        finally:
            with self.lock:
                self.active_reads -= 1


@pytest.fixture
def owned(monkeypatch, tmp_path):
    from lib.conversations import repository, settings_store
    import lib.storage
    state = {'unrelated': 'preserve'}
    bridge = Bridge()

    def conversation(conv_id, *, user_id, **kw):
        return {'settings': copy.deepcopy(state)} if (conv_id, user_id) == ('c', 7) else None

    class Client:
        def query(self, operation, payload):
            assert operation == 'conversation.get'
            return {'metadata': {'settings': copy.deepcopy(state)}} if (
                payload['conv_id'], payload['user_id']) == ('c', 7) else None

        def command(self, operation, payload, command_id):
            assert payload['expected_settings'] == state
            assert payload['user_id'] == 7 and payload['conv_id'] == 'c'
            state.clear()
            state.update(copy.deepcopy(payload['updates']))
            return {'applied': True}

    monkeypatch.setattr(repository, 'get_conversation', conversation)
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: Client())
    monkeypatch.setattr(settings_store, '_publish_after_settings_write', lambda *a: None)
    monkeypatch.setattr(capabilities, '_bridge', lambda owner: bridge)
    monkeypatch.setattr(overleaf, '_bridge', lambda owner: bridge)
    from tests.support.paper_storage import isolated_paper_storage
    with isolated_paper_storage(tmp_path, monkeypatch):
        yield state, bridge


def select(**extra):
    return capabilities.select_source({**SELECTION, **extra}, owner_user_id=7)['source']


def execute(owned, scheduler, *, publish=None):
    state, bridge = owned
    document = overleaf.bind_overleaf_document(REQUEST, owner_user_id=7, settings=state)
    published = []
    result = overleaf.overleaf_execution(document, REQUEST, scheduler)(
        {'abort_event': threading.Event()}, publish or (lambda build, **extra: published.append(build) or {
            **extra, 'receipt': scheduler.cache.publish(build)}))
    return result, published


def test_common_document_selection_switches_sources_without_compiling_or_losing_grants(owned, tmp_path):
    from lib.paper.builds.documents import discover_documents, select_document
    state, bridge = owned
    selected = select_document({**SELECTION, 'source': 'overleaf', 'main_file': 'main.tex'}, owner_user_id=7)['selected']
    result = discover_documents({'conv_id': 'c'}, owner_user_id=7)
    assert result['documents'] == [selected] and result['selected'] == selected
    assert result['overleaf_servers'] == [SELECTION['mcp_server']] and result['diagnostics'] == []
    assert state['paperDocument'] == selected and not bridge.calls
    (tmp_path/'main.tex').write_text('\\documentclass{article}\\begin{document}Hi\\end{document}')
    state['projectPath'] = str(tmp_path)
    local = select_document({'conv_id': 'c', 'project_root': str(tmp_path), 'path': 'main.tex'}, owner_user_id=7)['selected']
    assert local['source'] == 'local' and len(state['paperOverleafSources']) == 1
    assert len(discover_documents({'conv_id': 'c'}, owner_user_id=7)['documents']) == 2
    capabilities.remove_source(SELECTION, owner_user_id=7)
    assert state['paperDocument'] == local and state['paperOverleafSources'] == []


def test_disabled_capabilities_preserve_selection_intent_and_bound_combined_discovery(owned, tmp_path):
    from lib.paper.builds.documents import discover_documents
    state, _ = owned
    source = select()
    selected = state['paperDocument']
    state['mcpEnabled'] = False
    result = discover_documents({'conv_id': 'c'}, owner_user_id=7)
    assert result['selected'] is None and state['paperDocument'] == selected
    assert any(row['code'] == 'selection_stale' for row in result['diagnostics'])
    state['mcpEnabled'] = True
    state['paperOverleafSources'] = [{**source, 'project_id': f'{number:024x}'} for number in range(16)]
    state['paperDocument'] = capabilities.source_document(state['paperOverleafSources'][-1])
    state['projectPath'] = str(tmp_path)
    for number in range(64):
        (tmp_path/f'paper{number}.tex').write_text('\\documentclass{article}')
    result = discover_documents({'conv_id': 'c'}, owner_user_id=7)
    assert len(result['documents']) == 64 and result['selected'] == state['paperDocument']
    assert result['selected'] in result['documents']
    assert any(row['code'] == 'discovery_limit' for row in result['diagnostics'])


def test_overleaf_document_http_scope_owner_selection_and_revocation(owned):
    import asyncio
    from quart import Quart, g, request
    from lib.api_keys import AuthContext
    from routes.api_v1.paper_builds import api_v1_paper_builds_bp
    state, bridge = owned
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True

    @app.before_request
    def principal():
        if request.headers.get('X-Owner'):
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=int(request.headers['X-Owner']),
                scopes=frozenset(request.headers.get('X-Scope', 'tasks').split(',')))

    app.register_blueprint(api_v1_paper_builds_bp)

    async def run():
        client = app.test_client()
        base = '/api/v1/paper/documents'
        for suffix in ('/select', '/overleaf/select', '/overleaf/remove'):
            assert (await client.post(base+suffix, json=SELECTION)).status_code == 401
            assert (await client.post(base+suffix, json=SELECTION, headers={'X-Owner': '7', 'X-Scope': 'read'})).status_code == 403
            assert (await client.post(base+suffix, json=SELECTION, headers={'X-Owner': '8'})).status_code == 404
        result = await client.post(base+'/select', json={**SELECTION, 'main_file': None}, headers={'X-Owner': '7'})
        assert result.status_code == 200
        selected = (await result.get_json())['selected']
        assert selected['source'] == 'overleaf' and state['paperDocument'] == selected
        reopened = await app.test_client().post(base+'/discover', json={'conv_id': 'c'}, headers={'X-Owner': '7'})
        assert (await reopened.get_json())['selected'] == selected and not bridge.calls
        result = await client.post(base+'/overleaf/remove', json=SELECTION, headers={'X-Owner': '7'})
        assert result.status_code == 200 and 'paperDocument' not in state
    asyncio.run(run())


def test_select_is_owner_scoped_non_compiling_and_independent_of_server_name(owned):
    state, bridge = owned
    source = select()
    assert source['main_file'] is None and state['unrelated'] == 'preserve'
    assert len(source['capabilities']) == 3 and not bridge.calls
    assert state['paperOverleafSources'] == [source]
    assert select() == source
    with pytest.raises(BuildError) as failure:
        capabilities.select_source(SELECTION, owner_user_id=8)
    assert failure.value.code == 'document_not_found'
    with pytest.raises(BuildError):
        capabilities.remove_source(SELECTION, owner_user_id=8)
    assert state['paperOverleafSources'] == [source]
    capabilities.remove_source(SELECTION, owner_user_id=7)
    assert state['paperOverleafSources'] == []


@pytest.mark.parametrize('payload', [
    {**REQUEST, 'project_id': 'f' * 24}, {**REQUEST, 'mcp_server': 'another'},
    {**REQUEST, 'main_file': 'other.tex'}, {**REQUEST, 'engine': 'xelatex'},
])
def test_request_cannot_change_the_selected_project_main_or_target(owned, payload):
    state, bridge = owned
    select(main_file='main.tex')
    with pytest.raises(BuildError):
        overleaf.bind_overleaf_document(payload, owner_user_id=7, settings=state)
    assert not bridge.calls


@pytest.mark.parametrize('kind', ['output', 'input', 'disabled', 'version', 'duplicate', 'metadata'])
def test_live_capability_drift_invalidates_saved_binding_even_with_constant_provider_hash(owned, kind):
    state, bridge = owned
    select()
    row = bridge.catalog[0]
    if kind == 'output':
        row['output_schema']['properties']['remote_build_id'] = {'type': 'integer'}
    elif kind == 'input':
        row['openai_def']['function']['parameters']['additionalProperties'] = True
    elif kind == 'disabled':
        bridge.catalog.pop()
    elif kind == 'version':
        row['meta']['tofu/paper-build']['schema_version'] = True
    elif kind == 'duplicate':
        bridge.catalog.append(copy.deepcopy(row))
    else:
        row['meta']['tofu/paper-build']['remote_cancel'] = True
    with pytest.raises(BuildError) as failure:
        overleaf.bind_overleaf_document(REQUEST, owner_user_id=7, settings=state)
    assert failure.value.code == 'overleaf_capability'
    assert not bridge.calls


def test_imports_same_build_into_shared_cache_with_explicitly_unverified_version(owned, scheduler):
    state, bridge = owned
    select(main_file='main.tex')
    result, _ = execute(owned, scheduler)
    receipt = result['receipt']
    assert receipt['target'] == 'overleaf' and receipt['main_file'] == 'main.tex'
    assert receipt['remote_build_id'] == 'remote-build-1'
    assert not receipt['version_verified'] and receipt['source_digest'] is None
    assert result['location'] is None and result['pdf_url'].endswith('/document.pdf')
    with scheduler.cache.lease(7, receipt['document_id'], receipt['build_id']) as cached:
        assert (cached.directory / 'document.pdf').read_bytes() == bridge.pdf
    assert scheduler.cache.latest(7, receipt['document_id']) == receipt
    assert not bridge.exports and bridge.active_reads == 0
    assert 0 < bridge.maximum_reads <= min(2, scheduler.budget.workers)
    with pytest.raises(BuildError):
        with scheduler.cache.lease(8, receipt['document_id'], receipt['build_id']):
            pytest.fail('foreign owner received a PDF')


@pytest.mark.parametrize('field', ['request_id', 'offset', 'data', 'eof'])
def test_corrupt_chunks_release_the_lease_without_publishing(owned, scheduler, field):
    state, bridge = owned
    select()
    def mutate(value):
        value[field] = {'request_id': 'f' * 32, 'offset': value['offset'] + 1,
                        'data': base64.b64encode(b'corrupt').decode(), 'eof': not value['eof']}[field]
    bridge.mutate_chunk = mutate
    with pytest.raises(BuildError):
        execute(owned, scheduler, publish=lambda *a, **kw: pytest.fail('corrupt artifact published'))
    assert not bridge.exports and bridge.active_reads == 0
    assert not list(scheduler.cache.root.rglob('receipt.json'))


@pytest.mark.parametrize('corruption', ['empty_body', 'flate'])
def test_checksum_correct_invalid_pdf_preserves_the_previous_readable_build(owned, scheduler, corruption):
    _, bridge = owned
    select()
    previous, _ = execute(owned, scheduler)
    receipt = previous['receipt']
    previous_pdf = bridge.pdf
    if corruption == 'empty_body':
        bridge.pdf = b'%PDF-1.7\nnot a PDF body\n%%EOF\n'
    else:
        objects = paper_objects()
        objects[3] = b'<< /Length 3 /Filter /FlateDecode >>\nstream\nbad\nendstream'
        bridge.pdf = pdf_from_objects(objects)
    # The producer hashes these exact bytes, so transfer integrity succeeds.
    with pytest.raises(BuildError) as failure:
        execute(owned, scheduler, publish=lambda *a, **kw: pytest.fail('invalid PDF published'))
    assert failure.value.code == 'pdf_invalid'
    assert scheduler.cache.latest(7, receipt['document_id']) == receipt
    with scheduler.cache.lease(7, receipt['document_id'], receipt['build_id']) as cached:
        assert (cached.directory / 'document.pdf').read_bytes() == previous_pdf
    assert len(list(scheduler.cache.root.rglob('receipt.json'))) == 1
    assert not bridge.exports and bridge.active_reads == 0
    assert not list(scheduler.cache.root.rglob('.pdf-validation-*'))


def test_revocation_before_publication_prevents_a_finished_download_from_becoming_visible(owned, scheduler):
    state, bridge = owned
    select()
    bridge.on_read = lambda: state.update(paperOverleafSources=[])
    with pytest.raises(BuildError) as failure:
        execute(owned, scheduler, publish=lambda *a, **kw: pytest.fail('revoked build published'))
    assert failure.value.code == 'overleaf_binding'
    assert not bridge.exports and not list(scheduler.cache.root.rglob('receipt.json'))


def test_failed_cloud_compile_keeps_its_log_without_a_pdf(owned, scheduler):
    state, bridge = owned
    select()
    bridge.status = 'failed'
    result, _ = execute(owned, scheduler)
    assert result['receipt']['status'] == 'failed' and result['pdf_url'] is None
    assert result['receipt']['diagnostics'][0]['severity'] == 'error'
    assert [row['name'] for row in result['receipt']['artifacts']] == ['build.log']


def test_start_uses_existing_scheduler_and_last_viewer_cancels_all_transfer_work(owned, scheduler):
    state, bridge = owned
    select()
    bridge.hold = threading.Event()
    accepted = service.start_preview(REQUEST, owner_user_id=7, scheduler=scheduler)
    assert bridge.read_started.wait(3)
    assert scheduler.detach(accepted['task_id'], owner_user_id=7, subscriber_id=REQUEST['subscriber_id'])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = scheduler.production.runtime.get_owned(accepted['task_id'], user_id=7)
        if task['status'] not in ('pending', 'running'):
            break
        threading.Event().wait(0.01)
    assert task['status'] == 'aborted'
    assert not bridge.exports and bridge.active_reads == 0
    assert not list(scheduler.cache.root.rglob('receipt.json'))


@pytest.mark.parametrize('workers', [1, 2])
def test_transfer_idle_reuse_hint_keeps_receipt_and_retires_workers(owned, scheduler, workers):
    _, bridge = owned
    select()
    scheduler.budget = replace(scheduler.budget, workers=workers)
    bridge.barrier = threading.Barrier(2)
    result, _ = execute(owned, scheduler)
    assert result['receipt']['status'] == 'success'
    assert bridge.maximum_reads >= 2 and bridge.active_reads == 0
    assert not [thread for thread in threading.enumerate() if thread.name.startswith('paper-mcp-read')]


def test_all_chunks_start_without_window_and_anonymous_spool_closes(monkeypatch):
    from lib.paper.builds import overleaf
    import tempfile

    chunk_bytes = 65536
    body = b''.join(bytes([i]) * chunk_bytes for i in range(32))
    barrier = threading.Barrier(32, timeout=10)
    identity = dict(schema_version=1, owner_user_id=7, project_id='1' * 24, request_id='a' * 32)
    lease = dict(identity, lease_token='b' * 64, expires_in_seconds=30)
    active = peak = 0
    lock = threading.Lock()
    spools = []
    original = tempfile.TemporaryFile
    original_submit = overleaf.InstrumentedThreadPoolExecutor.submit
    futures = []

    def submit(pool, *args, **kwargs):
        future = original_submit(pool, *args, **kwargs)
        futures.append(future)
        return future

    def spool(*args, **kwargs):
        file = original(*args, **kwargs)
        spools.append(file)
        return file

    class Reader:
        def call_tool_document(self, tool, payload, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                offset = payload['offset']
                data = body[offset:offset + chunk_bytes]
                return dict(identity, artifact='document.pdf', offset=offset,
                    data=base64.b64encode(data).decode(), eof=offset + len(data) == len(body))
            finally:
                with lock:
                    active -= 1

    class Receiver:
        def __init__(self):
            self.chunks = []
        def accept(self, name, data):
            assert name == 'paper-artifact/document.pdf'
            self.chunks.append(base64.b64decode(data))

    monkeypatch.setattr(overleaf.tempfile, 'TemporaryFile', spool)
    monkeypatch.setattr(overleaf.InstrumentedThreadPoolExecutor, 'submit', submit)
    receiver = Receiver()
    started = time.monotonic()
    overleaf._download(Reader(), 'read', lease,
        [{'name': 'document.pdf', 'size_bytes': len(body)}], receiver,
        cancel=threading.Event(), workers=1)
    assert b''.join(receiver.chunks) == body
    assert peak == 32 and active == 0
    assert len(futures) == 32 and all(future.result() is None for future in futures)
    assert len(spools) == 1 and spools[0].closed
    assert not any(t.name.startswith('paper-mcp-read') for t in threading.enumerate())
    print(f'32 chunk reads, {len(body)} bytes: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers, 0 spool FDs')


def test_unstarted_chunk_failure_drains_siblings_and_closes_spool(monkeypatch):
    from lib.paper.builds import overleaf
    import tempfile

    identity = dict(schema_version=1, owner_user_id=7, project_id='1' * 24, request_id='a' * 32)
    lease = dict(identity, lease_token='b' * 64, expires_in_seconds=30)
    barrier = threading.Barrier(3, timeout=10)
    original_start = threading.Thread.start
    original_spool = tempfile.TemporaryFile
    starts = 0
    offsets = []
    spools = []
    lock = threading.Lock()

    def start(thread):
        nonlocal starts
        if thread.name.startswith('paper-mcp-read'):
            starts += 1
            if starts == 2:
                raise RuntimeError('unstarted chunk read')
        return original_start(thread)

    def spool(*args, **kwargs):
        file = original_spool(*args, **kwargs)
        spools.append(file)
        return file

    class Reader:
        def call_tool_document(self, tool, payload, **kwargs):
            offset = payload['offset']
            with lock:
                offsets.append(offset)
            barrier.wait()
            return dict(identity, artifact='document.pdf', offset=offset,
                data=base64.b64encode(b'x' * 65536).decode(), eof=offset == 3 * 65536)

    class Receiver:
        def accept(self, *args):
            pass

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(overleaf.tempfile, 'TemporaryFile', spool)
    with pytest.raises(BuildError) as caught:
        overleaf._download(Reader(), 'read', lease,
            [{'name': 'document.pdf', 'size_bytes': 4 * 65536}], Receiver(),
            cancel=threading.Event(), workers=1)
    assert caught.value.code == 'overleaf_unavailable'
    assert sorted(offsets) == [0, 2 * 65536, 3 * 65536]
    assert len(spools) == 1 and spools[0].closed
    assert not any(t.name.startswith('paper-mcp-read') for t in threading.enumerate())


def test_transfer_freezes_leased_artifact_metadata_before_worker_entry():
    from lib.paper.builds import overleaf

    identity = dict(schema_version=1, owner_user_id=7, project_id='1' * 24, request_id='a' * 32)
    lease = dict(identity, lease_token='b' * 64, expires_in_seconds=30)
    artifact = {'name': 'document.pdf', 'size_bytes': 2 * 65536}
    received = []

    class Reader:
        def call_tool_document(self, tool, payload, **kwargs):
            artifact['name'] = 'build.log'
            artifact['size_bytes'] = 0
            return dict(identity, artifact='document.pdf', offset=payload['offset'],
                data=base64.b64encode(b'x' * 65536).decode(), eof=payload['offset'] == 65536)

    class Receiver:
        def accept(self, name, data):
            received.append((name, len(base64.b64decode(data))))

    overleaf._download(Reader(), 'read', lease, [artifact], Receiver(),
                       cancel=threading.Event(), workers=1)
    assert received == [('paper-artifact/document.pdf', 65536)] * 2
