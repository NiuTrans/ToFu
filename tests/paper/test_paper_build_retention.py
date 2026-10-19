"""Retained build authority: real Sidecar, replay, owner fences and restart.

Byte fixtures test storage integrity, not PDF visual validity. The Research
native compiler/browser tests exercise actual PDF delivery above this boundary.
"""
from __future__ import annotations

import base64
import asyncio
import copy
import uuid

import pytest

from lib.paper.builds.artifact_repository import PaperBuildArtifactRepository
from lib.paper.builds.compiler import artifact_metadata
from lib.paper.builds.contract import limit
from lib.storage.errors import StorageError
from tests.storage import test_storage_authority_sql_contract as storage_support
from tests.api import test_api_v1_paper_builds as api_support
from tests.paper.test_paper_build_cache import _build, _cache

pytestmark = pytest.mark.unit
storage = storage_support.storage
app = api_support.app
scheduler = api_support.scheduler


def _repository(storage, owner=7):
    return PaperBuildArtifactRepository(owner, client_factory=lambda **_: storage.client)


def _call(storage, operation, payload):
    return storage.client.command('paper.artifact.' + operation, {**payload, 'user_id': 7}, uuid.uuid4().hex)


def _pin(storage, build, proof, revision=0, paper_hash="paper"):
    receipt = build.receipt
    reference = {key: receipt[key] for key in ('document_id', 'build_id')}
    reference.update(source_digest='c' * 64, manifest_digest=receipt['source_digest'],
                     pdf_sha256=receipt['artifacts'][0]['sha256'])
    payload = {'user_id': 7, 'paper_hash': paper_hash, 'lang': 'en', 'expected_revision': revision,
               'updated_at': 1, 'artifact_proof': proof,
               'workspace': {'compilation': {'mode': 'paper_build', 'status': 'passing', 'build': reference}}}
    return storage.client.command('research.workspace.put', payload, uuid.uuid4().hex)


def _lease(storage, proof, *, operation='retain', lease_id=None):
    return _call(storage, 'lease', {**proof, 'lease_id': lease_id or uuid.uuid4().hex, 'operation': operation})


def test_retained_bytes_and_reference_survive_sidecar_restart(storage, tmp_path):
    build = _build(tmp_path)
    repository = _repository(storage)
    with repository.prepare(build) as proof:
        _pin(storage, build, proof)
    # The context's discard cannot undo a committed reference.
    assert repository.latest(build.receipt['document_id']) == {'build_id': build.receipt['build_id']}
    storage.stop()
    storage.start()
    workspace = storage.client.query('research.workspace.get', {'user_id': 7, 'paper_hash': 'paper', 'lang': 'en'})
    assert workspace['revision'] == 1
    assert workspace['workspace']['compilation']['build']['build_id'] == proof['build_id']
    cache = _cache(tmp_path)
    repository.ensure_cached(cache, proof['document_id'], proof['build_id'])
    with cache.open_artifact(7, proof['document_id'], proof['build_id'], 'document.pdf') as (stream, meta):
        assert stream.read() == (build.directory / 'document.pdf').read_bytes()
        assert meta == build.receipt['artifacts'][0]
    assert cache.latest(7, proof['document_id']) is None, 'hydration must not advance a compiler success pointer'


def test_chunk_retry_is_exact_and_sealing_requires_receipt_digest(storage, tmp_path):
    build = _build(tmp_path)
    proof = {key: build.receipt[key] for key in ('document_id', 'build_id')}
    proof['upload_id'] = uuid.uuid4().hex
    request = {**proof, 'receipt': build.receipt}
    assert _call(storage, 'prepare', request)['next_sequence'] == 0
    data = (build.directory / 'document.pdf').read_bytes()
    chunk = {**proof, 'sequence': 0, 'data': base64.b64encode(data).decode()}
    assert _call(storage, 'append', chunk) == _call(storage, 'append', chunk) == {'next_sequence': 1}
    with pytest.raises(StorageError) as conflict:
        _call(storage, 'append', {**chunk, 'data': base64.b64encode(b'x' * len(data)).decode()})
    assert conflict.value.code == 'database_conflict'
    assert _call(storage, 'seal', proof) == _call(storage, 'seal', proof) == {'sealed': True}
    changed = copy.deepcopy(request)
    changed['receipt']['toolchain'] = 'different'
    with pytest.raises(StorageError) as immutable:
        _call(storage, 'prepare', changed)
    assert immutable.value.code == 'database_conflict'


def test_incomplete_or_corrupt_upload_cannot_publish(storage, tmp_path):
    build = _build(tmp_path)
    proof = {key: build.receipt[key] for key in ('document_id', 'build_id')}
    proof['upload_id'] = uuid.uuid4().hex
    _call(storage, 'prepare', {**proof, 'receipt': build.receipt})
    with pytest.raises(StorageError, match='incomplete'):
        _call(storage, 'seal', proof)
    _call(storage, 'append', {**proof, 'sequence': 0,
                            'data': base64.b64encode(b'x' * build.receipt['artifacts'][0]['size_bytes']).decode()})
    with pytest.raises(StorageError) as digest:
        _call(storage, 'seal', proof)
    assert digest.value.code == 'database_integrity'
    with pytest.raises(StorageError):
        _pin(storage, build, proof)
    assert _repository(storage).latest(proof['document_id']) is None


def test_stale_workspace_cas_preserves_prior_and_does_not_pin_new_upload(storage, tmp_path):
    first, second = _build(tmp_path), _build(tmp_path)
    repository = _repository(storage)
    with repository.prepare(first) as proof:
        _pin(storage, first, proof)
    with repository.prepare(second) as stale:
        with pytest.raises(StorageError) as conflict:
            _pin(storage, second, stale, revision=0)
        assert conflict.value.code == 'database_conflict'
        with pytest.raises(StorageError) as unavailable:
            _lease(storage, stale)
        assert unavailable.value.code == 'database_not_found'
    assert repository.latest(first.receipt['document_id']) == {'build_id': first.receipt['build_id']}
    assert _call(storage, 'prune', {})['progress']
    assert _lease(storage, proof)['retained']


def test_commit_followed_by_lost_ack_does_not_discard_the_manuscript(storage, tmp_path):
    build = _build(tmp_path)
    repository = _repository(storage)
    with pytest.raises(StorageError, match='lost acknowledgement'):
        with repository.prepare(build) as proof:
            _pin(storage, build, proof)
            raise StorageError('database_unavailable', 'lost acknowledgement')
    assert not _call(storage, 'prune', {})['progress']
    assert _lease(storage, proof)['retained']


def test_owner_filter_and_demand_driven_read_leases(storage, tmp_path):
    build = _build(tmp_path)
    with _repository(storage).prepare(build) as proof:
        _pin(storage, build, proof)
    lease_id = uuid.uuid4().hex
    _lease(storage, proof, lease_id=lease_id)
    with pytest.raises(StorageError) as denied:
        storage.client.query('paper.artifact.read', {**proof, 'user_id': 8, 'lease_id': lease_id, 'sequence': 0})
    assert denied.value.code == 'database_not_found'
    assert _repository(storage, owner=8).latest(proof['document_id']) is None
    for _ in range(127):
        _lease(storage, proof)
    assert _lease(storage, proof)['retained']
    _lease(storage, proof, operation='release', lease_id=lease_id)
    with pytest.raises(StorageError):
        _lease(storage, proof, operation='renew', lease_id=lease_id)
    with pytest.raises(StorageError):
        storage.client.query('paper.artifact.read', {**proof, 'user_id': 7, 'lease_id': lease_id, 'sequence': 0})


def test_uncommitted_chunk_reclamation_is_bounded_and_restartable(storage, tmp_path):
    build = _build(tmp_path)
    pdf = build.directory / 'document.pdf'
    pdf.write_bytes(b'x' * (limit('retained_chunk_bytes') * (limit('retained_gc_chunks') + 1)))
    build.receipt['artifacts'] = [artifact_metadata(pdf)]
    with _repository(storage).prepare(build):
        pass
    first = _call(storage, 'prune', {})
    assert first == {'progress': True, 'chunks_deleted': limit('retained_gc_chunks')}
    storage.stop()
    storage.start()
    assert _call(storage, 'prune', {}) == {'progress': True, 'chunks_deleted': 1}
    assert not _call(storage, 'prune', {})['progress']


def test_http_range_restores_retained_bytes_and_preserves_owner_boundary(app, storage, tmp_path, monkeypatch):
    from lib.paper.builds import artifact_repository
    monkeypatch.setattr(artifact_repository, '_storage', lambda **_: storage.client)
    build = _build(tmp_path)
    with _repository(storage).prepare(build) as proof:
        _pin(storage, build, proof)
    storage.stop()
    storage.start()
    url = f'/api/v4/paper/builds/{proof["document_id"]}/{proof["build_id"]}/artifacts/document.pdf'
    async def run():
        client = app.test_client()
        assert (await client.get(url, headers={'X-Test-Owner': '8'})).status_code == 404
        response = await client.get(url, headers={'X-Test-Owner': '7', 'Range': 'bytes=0-15'})
        assert response.status_code == 206
        assert await response.get_data() == (build.directory / 'document.pdf').read_bytes()[:16]
        assert response.headers['X-Artifact-SHA256'] == build.receipt['artifacts'][0]['sha256']
        assert 'private' in response.headers['Cache-Control']
    asyncio.run(run())


def test_schema_integral_float_size_uploads_and_seals(storage, tmp_path):
    build = _build(tmp_path)
    receipt = copy.deepcopy(build.receipt)
    for artifact in receipt['artifacts']:
        artifact['size_bytes'] = float(artifact['size_bytes'])
    proof = {key: receipt[key] for key in ('document_id', 'build_id')}
    proof['upload_id'] = uuid.uuid4().hex
    assert _call(storage, 'prepare', {**proof, 'receipt': receipt})['next_sequence'] == 0
    sequence = 0
    for artifact in receipt['artifacts']:
        data = (build.directory / artifact['name']).read_bytes()
        for offset in range(0, len(data), limit('retained_chunk_bytes')):
            chunk = data[offset:offset + limit('retained_chunk_bytes')]
            assert _call(storage, 'append', {**proof, 'sequence': sequence,
                         'data': base64.b64encode(chunk).decode()})['next_sequence'] == sequence + 1
            sequence += 1
    assert _call(storage, 'seal', proof) == {'sealed': True}
