"""Retained HTTP/SyncTeX reads survive cache pressure with bounded memory.

Real SQLite semantic operations own bytes and leases. Synthetic PDF bytes test
transport integrity here; the native browser journey covers rendered papers.
"""
from __future__ import annotations

import asyncio
import gc
import gzip
import hashlib
import json
import threading
import tracemalloc
import uuid
from contextlib import contextmanager

import pytest

from lib.paper.builds import artifact_repository, service
from lib.paper.builds.compiler import artifact_metadata
from lib.paper.builds.contract import limit
from lib.paper.builds.errors import BuildError
from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _paper_artifacts as artifacts
from tests.api import test_api_v1_paper_builds as api_support
from tests.paper import test_paper_build_retention_faults as retention_support
from tests.storage import test_storage_authority_sql_contract as sidecar_support
from tests.paper.test_paper_build_cache import _build, _cache
from tests.paper.test_paper_build_retention import _call, _pin, _repository
from tests.support.envelope import v1_get_json

pytestmark = pytest.mark.unit
app = api_support.app
scheduler = api_support.scheduler
storage = retention_support.storage
backend_storage = sidecar_support.storage


def _manuscript(tmp_path, data=None):
    build = _build(tmp_path)
    pdf = data if data is not None else b'%PDF-1.4\n' + bytes(range(256)) * 2200
    files = {'document.pdf': pdf, 'build.log': b'compiler output\n',
             'sources.json': json.dumps([{'path': 'main.tex'}]).encode(),
             'document.synctex.gz': gzip.compress(
                 b'SyncTeX Version:1\nInput:1:main.tex\n{2\n(1,5:6553600,13107200:655360,655360,0\n}\n')}
    for name, data in files.items():
        (build.directory / name).write_bytes(data)
    build.receipt['artifacts'] = [artifact_metadata(build.directory / name) for name in files]
    return build, pdf


@pytest.fixture
def retained(app, storage, scheduler, tmp_path, monkeypatch):
    class TrackedClient:
        def __init__(self):
            self.reads = []
            self.leases = []

        def query(self, operation, payload):
            if operation == 'paper.artifact.read':
                self.reads.append(payload['sequence'])
            return storage.client.query(operation, payload)

        def command(self, operation, payload, command_id):
            if operation == 'paper.artifact.lease':
                self.leases.append(payload.copy())
            return storage.client.command(operation, payload, command_id)

    client = TrackedClient()
    monkeypatch.setattr(artifact_repository, '_storage', lambda **_: client)
    build, data = _manuscript(tmp_path)
    with _repository(storage).prepare(build) as proof:
        _pin(storage, build, proof)
    scheduler.cache = _cache(tmp_path, records=1, size=16384)
    blocker = _build(tmp_path, document='f' * 64)
    scheduler.cache.publish(blocker)
    scheduler.cache.viewer(7, blocker.receipt['document_id'], blocker.receipt['build_id'],
                           {'viewer_id': uuid.uuid4().hex, 'operation': 'retain'})
    return build, data, client


def _url(build, suffix='artifacts/document.pdf'):
    return f'/api/v4/paper/builds/{build.receipt["document_id"]}/{build.receipt["build_id"]}/{suffix}'


def test_http_ranges_and_conditions_read_only_needed_chunks_under_cache_pressure(app, retained, scheduler):
    build, data, tracked = retained
    size = len(data)
    owner = {'X-Test-Owner': '7'}
    before = scheduler.cache.prune()

    async def run():
        client = app.test_client()
        assert (await client.get(_url(build))).status_code == 401
        assert (await client.get(_url(build), headers={'X-Test-Owner': '8'})).status_code == 404
        assert (await client.get(_url(build), headers={**owner, 'X-Test-Scopes': 'read'})).status_code == 403
        etag = '"' + build.receipt['artifacts'][0]['sha256'] + '"'
        for method, headers, status in [('HEAD', {}, 200), ('HEAD', {'Range': 'bytes=1-2'}, 206),
                                        ('GET', {'If-None-Match': etag}, 304)]:
            tracked.reads.clear()
            response = await client.open(_url(build), method=method, headers={**owner, **headers})
            assert response.status_code == status
            assert await response.get_data() == b''
            assert tracked.reads == []
        chunk = limit('retained_chunk_bytes')
        for header, start, stop in [('bytes=0-0', 0, 1), ('bytes=-17', size - 17, size),
                                    (f'bytes={chunk-7}-{chunk+11}', chunk - 7, chunk + 12),
                                    (f'bytes={size-23}-', size - 23, size)]:
            tracked.reads.clear()
            response = await client.get(_url(build), headers={**owner, 'Range': header})
            assert response.status_code == 206
            assert response.headers['Content-Range'] == f'bytes {start}-{stop-1}/{size}'
            assert response.headers['ETag'] == etag
            assert response.headers['X-Artifact-SHA256'] == build.receipt['artifacts'][0]['sha256']
            assert 'private' in response.headers['Cache-Control']
            assert await response.get_data() == data[start:stop]
            assert tracked.reads == list(range(start // chunk, (stop - 1) // chunk + 1))
        for header in [f'bytes={size}-', 'bytes=0-2,8-9', 'bytes=invalid']:
            tracked.reads.clear()
            assert (await client.get(_url(build), headers={**owner, 'Range': header})).status_code == 416
            assert tracked.reads == []
        unchanged = await client.get(_url(build), headers={**owner, 'Range': 'bytes=1-3', 'If-Range': etag})
        assert unchanged.status_code == 206 and await unchanged.get_data() == data[1:4]
        changed = await client.get(_url(build), headers={**owner, 'Range': 'bytes=1-3', 'If-Range': '"old"'})
        assert changed.status_code == 200 and await changed.get_data() == data

    asyncio.run(run())
    assert scheduler.cache.prune() == before
    assert scheduler.cache.latest(7, build.receipt['document_id']) is None


def test_location_reads_only_manifest_and_synctex_without_pdf_or_source(retained, scheduler):
    build, data, tracked = retained
    result = service.locate_build(build.receipt['document_id'], build.receipt['build_id'],
        {'file': 'main.tex', 'line': 5}, owner_user_id=7, scheduler=scheduler)
    assert result['page'] == 2
    assert result['rects'][0]['h'] == pytest.approx(100 * 72 / 72.27)
    first = (len(data) + limit('retained_chunk_bytes') - 1) // limit('retained_chunk_bytes')
    assert tracked.reads == [first + 1, first + 2]
    with pytest.raises(BuildError) as wrong_file:
        service.locate_build(build.receipt['document_id'], build.receipt['build_id'],
            {'file': 'outside.tex', 'line': 5}, owner_user_id=7, scheduler=scheduler)
    assert wrong_file.value.code == 'source_path'


def test_damaged_cache_falls_back_to_retained_bytes_and_location(retained, scheduler, tmp_path):
    build, data, _ = retained
    scheduler.cache = _cache(tmp_path / 'damaged', size=2 * 1024 * 1024)
    scheduler.cache.publish(build)
    repository = artifact_repository.PaperBuildArtifactRepository(7)
    with scheduler.cache.lease(7, build.receipt['document_id'], build.receipt['build_id']) as cached:
        (cached.directory / 'document.pdf').write_bytes(b'truncated cache')
        (cached.directory / 'sources.json').write_bytes(b'{}')
    with repository.open_artifact(scheduler.cache, build.receipt['document_id'],
                                  build.receipt['build_id'], 'document.pdf') as (stream, metadata):
        assert stream.read() == data
        assert metadata['sha256'] == hashlib.sha256(data).hexdigest()
    assert service.locate_build(build.receipt['document_id'], build.receipt['build_id'],
        {'file': 'main.tex', 'line': 5}, owner_user_id=7, scheduler=scheduler)['page'] == 2


def test_cache_io_failure_does_not_block_owned_retained_bytes(retained, scheduler, monkeypatch):
    build, data, _ = retained

    @contextmanager
    def unavailable_cache():
        raise OSError('cache volume is unavailable')
        yield  # pragma: no cover

    monkeypatch.setattr(scheduler.cache, '_guard', unavailable_cache)
    with artifact_repository.PaperBuildArtifactRepository(7).open_artifact(
            scheduler.cache, build.receipt['document_id'], build.receipt['build_id'], 'document.pdf') as (stream, _):
        assert stream.read(32) == data[:32]


@pytest.mark.parametrize('encoded', ['{"data":NaN,"sha256":"bad"}',
                                    '{"data":"eA==","sha256":"bad"}'])
def test_corrupt_retained_chunks_fail_closed_and_release_admission(retained, storage, scheduler, monkeypatch, encoded):
    build, _, _ = retained
    key = f'7:{build.receipt["document_id"]}:{build.receipt["build_id"]}:0'
    changed = storage.client.connection.execute(
        'UPDATE storage_records SET value_json=? WHERE namespace=? AND record_key=?',
        (encoded, 'paper.build.chunk.v1', key))
    assert changed.rowcount == 1
    with pytest.raises(StorageError) as corrupt:
        with artifact_repository.PaperBuildArtifactRepository(7).open_artifact(
                scheduler.cache, build.receipt['document_id'], build.receipt['build_id'], 'document.pdf') as (stream, _):
            stream.read(1)
    assert corrupt.value.code == 'database_integrity'


def test_viewer_keeps_retired_build_readable_until_release_without_cache(app, retained, storage, tmp_path, monkeypatch):
    build, data, _ = retained
    now = [artifacts._now_ms()]
    monkeypatch.setattr(artifacts, '_now_ms', lambda: now[0])
    identity = uuid.uuid4().hex
    owner = {'X-Test-Owner': '7'}

    async def run():
        client = app.test_client()
        async def viewer(operation):
            response = await client.post(_url(build, 'viewers'), headers=owner,
                                        json={'viewer_id': identity, 'operation': operation})
            assert response.status_code == 200
            return (await v1_get_json(response))['retained']

        assert await viewer('retain')
        newer, _ = _manuscript(tmp_path)
        with _repository(storage).prepare(newer) as proof:
            _pin(storage, newer, proof, revision=1)
        now[0] += (limit('viewer_lease_seconds') - 1) * 1000
        assert await viewer('renew')
        now[0] += 2000
        assert not _call(storage, 'prune', {})['progress']
        response = await client.get(_url(build), headers={**owner, 'Range': 'bytes=-7'})
        assert response.status_code == 206 and await response.get_data() == data[-7:]
        assert not await viewer('release')
        assert not await viewer('renew')
        assert _call(storage, 'prune', {})['progress']
        assert (await client.get(_url(build), headers=owner)).status_code == 404

    asyncio.run(run())


def test_direct_reads_remain_chunked_without_rejecting_concurrent_build_lease(storage, tmp_path, monkeypatch):
    build, data = _manuscript(tmp_path, b'x' * (9 * 1024 * 1024))
    repository = _repository(storage)
    with repository.prepare(build) as proof:
        _pin(storage, build, proof)
    cache = _cache(tmp_path)
    digest = hashlib.sha256()
    tracemalloc.start()
    try:
        with repository.open_artifact(cache, proof['document_id'], proof['build_id'], 'document.pdf') as (stream, metadata):
            admission_peak = tracemalloc.get_traced_memory()[1]
            with repository.open_build(cache, proof['document_id'], proof['build_id']) as other:
                assert other.receipt['build_id'] == proof['build_id']
            while chunk := stream.read(limit('transfer_chunk_bytes')):
                digest.update(chunk)
        _, peak = tracemalloc.get_traced_memory()
        allocations = tracemalloc.take_snapshot().statistics('lineno')[:6] if peak >= 4 * 1024 * 1024 else []
    finally:
        tracemalloc.stop()
    assert digest.hexdigest() == metadata['sha256'] == hashlib.sha256(data).hexdigest()
    assert peak < 4 * 1024 * 1024, {'peak': peak, 'admission_peak': admission_peak,
                                  'allocations': [str(row) for row in allocations]}


def test_disconnect_joins_pending_read_before_releasing_lease(app, retained, scheduler, monkeypatch):
    from lib.file_serving import send_leased_artifact
    build, _, tracked = retained
    entered, finish = threading.Event(), threading.Event()
    original = tracked.query
    reading = [False]
    released_while_reading = []
    command = tracked.command

    def blocked_query(operation, payload):
        if operation == 'paper.artifact.read' and payload['sequence'] == 1:
            reading[0] = True
            entered.set()
            assert finish.wait(5)
            try:
                return original(operation, payload)
            finally:
                reading[0] = False
        return original(operation, payload)

    def observe_release(operation, payload, command_id):
        if operation == 'paper.artifact.lease' and payload['operation'] == 'release':
            released_while_reading.append(reading[0])
        return command(operation, payload, command_id)

    monkeypatch.setattr(tracked, 'query', blocked_query)
    monkeypatch.setattr(tracked, 'command', observe_release)

    repository = artifact_repository.PaperBuildArtifactRepository(7)
    async def run():
        async with app.test_request_context('/download'):
            lease = repository.open_artifact(
                scheduler.cache, build.receipt['document_id'], build.receipt['build_id'], 'document.pdf')
            response = await asyncio.to_thread(send_leased_artifact, lease, mimetype='application/pdf')
            async with response.response as stream:
                for _ in range(limit('retained_chunk_bytes') // limit('transfer_chunk_bytes')):
                    await anext(stream)
                pending = asyncio.create_task(anext(stream))
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    pending.cancel()
                    await asyncio.sleep(0)
                    assert not pending.done()
                    pending.cancel()
                    await asyncio.sleep(0)
                    assert not pending.done()
                    assert released_while_reading == []
                finally:
                    finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert released_while_reading == [False]

    asyncio.run(run())


def test_unstarted_response_collection_releases_its_owned_retention_lease(app, retained, scheduler, monkeypatch):
    from lib.file_serving import send_leased_artifact
    build, _, _ = retained

    released = threading.Event()
    repository = artifact_repository.PaperBuildArtifactRepository(7)
    original = repository._command
    def command(operation, payload):
        result = original(operation, payload)
        if operation == 'lease' and payload.get('operation') == 'release':
            released.set()
        return result
    monkeypatch.setattr(repository, '_command', command)
    async def run():
        async with app.test_request_context('/download'):
            lease = repository.open_artifact(
                scheduler.cache, build.receipt['document_id'], build.receipt['build_id'], 'document.pdf')
            response = await asyncio.to_thread(send_leased_artifact, lease, mimetype='application/pdf')
            assert not released.is_set()
            del response
            gc.collect()
            assert await asyncio.to_thread(released.wait, 3)

    asyncio.run(run())


def test_retained_stream_backend_parity(backend_storage, tmp_path):
    build, data = _manuscript(tmp_path)
    repository = _repository(backend_storage)
    with repository.prepare(build) as proof:
        _pin(backend_storage, build, proof)
    backend_storage.stop()
    backend_storage.start()
    cache = _cache(tmp_path)
    viewer = {'viewer_id': uuid.uuid4().hex, 'operation': 'retain'}
    assert repository.viewer(cache, proof['document_id'], proof['build_id'], viewer)['retained']
    with repository.open_artifact(cache, proof['document_id'], proof['build_id'], 'document.pdf') as (stream, _):
        start = limit('retained_chunk_bytes') - 3
        stream.seek(start)
        result = stream.read(11)
        result += stream.read(11 - len(result))
        assert result == data[start:start + 11]
    for operation in ('release', 'renew'):
        assert not repository.viewer(cache, proof['document_id'], proof['build_id'],
                                     {**viewer, 'operation': operation})['retained']
    assert cache.latest(7, proof['document_id']) is None
