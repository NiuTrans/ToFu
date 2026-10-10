"""Retained-artifact rollback, quota, expiry and SQLite backup verification.

Uses the actual schema, SQLite session and operation handlers with an injected
pre-commit failure/clock. Real process/restart/HTTP tests live next door.
"""
from __future__ import annotations

import sqlite3
import tracemalloc
import threading
from types import SimpleNamespace

import pytest

from lib.paper.builds.compiler import artifact_metadata
from lib.paper.builds.contract import limit
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from tests.support.paper_storage import TransactionClient
from lib.storage_sidecar.operations_pkg import _paper_artifacts as artifacts
from tests.paper.test_paper_build_cache import _build, _cache
from tests.paper.test_paper_build_retention import _call, _lease, _pin, _repository

pytestmark = pytest.mark.unit


@pytest.fixture
def storage(tmp_path):
    client = TransactionClient(tmp_path / 'authority.sqlite')
    try:
        yield SimpleNamespace(client=client)
    finally:
        client.connection.close()


def test_retired_aggregate_budget_cannot_refuse_upload_or_evict_a_pinned_manuscript(storage, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_PAPER_BUILD_RETAINED_MAX_MIB', '1')
    first, second = _build(tmp_path), _build(tmp_path)
    for build in (first, second):
        pdf = build.directory / 'document.pdf'
        pdf.write_bytes(b'x' * (600 * 1024))
        build.receipt['artifacts'] = [artifact_metadata(pdf)]
    repository = _repository(storage)
    with repository.prepare(first) as proof:
        _pin(storage, first, proof)
    with repository.prepare(second) as second_proof:
        _pin(storage, second, second_proof, paper_hash="second-paper")
    assert _lease(storage, proof)['retained']
    assert not _call(storage, 'prune', {})['progress']


def test_workspace_rollback_rolls_back_new_pin_and_preserves_old_reference(storage, tmp_path):
    first, second = _build(tmp_path), _build(tmp_path)
    repository = _repository(storage)
    with repository.prepare(first) as proof:
        _pin(storage, first, proof)
    with repository.prepare(second) as candidate:
        storage.client.fail_commit = 'research.workspace.put'
        with pytest.raises(StorageError, match='before commit'):
            _pin(storage, second, candidate, revision=1)
        with pytest.raises(StorageError):
            _lease(storage, candidate)
        assert _lease(storage, proof)['retained']
    assert repository.latest(proof['document_id']) == {'build_id': proof['build_id']}
    workspace = storage.client.query('research.workspace.get', {'user_id': 7, 'paper_hash': 'paper', 'lang': 'en'})
    assert workspace['revision'] == 1
    assert workspace['workspace']['compilation']['build']['build_id'] == proof['build_id']


def test_expired_previous_version_survives_active_reader_then_reclaims(storage, tmp_path, monkeypatch):
    now = [1000000]
    monkeypatch.setattr(artifacts, '_now_ms', lambda: now[0])
    first, second = _build(tmp_path), _build(tmp_path)
    repository = _repository(storage)
    with repository.prepare(first) as proof:
        _pin(storage, first, proof)
    lease_id = 'd' * 32
    _lease(storage, proof, lease_id=lease_id)
    with repository.prepare(second) as candidate:
        _pin(storage, second, candidate, revision=1)
    now[0] += (limit('viewer_lease_seconds') - 1) * 1000
    _lease(storage, proof, operation='renew', lease_id=lease_id)
    now[0] += 2000
    assert not _call(storage, 'prune', {})['progress']
    _lease(storage, proof, operation='release', lease_id=lease_id)
    assert _call(storage, 'prune', {})['progress']
    with pytest.raises(StorageError):
        _lease(storage, proof)
    assert _lease(storage, candidate)['retained']


def test_sqlite_backup_contains_both_reference_and_immutable_bytes(storage, tmp_path):
    build = _build(tmp_path)
    with _repository(storage).prepare(build) as proof:
        _pin(storage, build, proof)
    path = tmp_path / 'backup.sqlite'
    with sqlite3.connect(path) as target:
        storage.client.connection.backup(target)
    backup = TransactionClient(path)
    try:
        repository = _repository(SimpleNamespace(client=backup))
        assert repository.latest(proof['document_id']) == {'build_id': proof['build_id']}
        cache = _cache(tmp_path)
        repository.ensure_cached(cache, proof['document_id'], proof['build_id'])
        with cache.open_artifact(7, proof['document_id'], proof['build_id'], 'document.pdf') as (stream, _):
            assert stream.read() == (build.directory / 'document.pdf').read_bytes()
    finally:
        backup.connection.close()


def test_restore_memory_is_chunk_bounded_and_encoding_growth_is_measured(storage, tmp_path):
    build = _build(tmp_path)
    pdf = build.directory / 'document.pdf'
    raw_bytes = 9 * 1024 * 1024
    with pdf.open('wb') as stream:
        for _ in range(raw_bytes // limit('retained_chunk_bytes')):
            stream.write(b'x' * limit('retained_chunk_bytes'))
    build.receipt['artifacts'] = [artifact_metadata(pdf)]
    repository = _repository(storage)
    with repository.prepare(build) as proof:
        _pin(storage, build, proof)
    encoded_bytes = storage.client.session.fetch_one(
        'SELECT SUM(LENGTH(value_json)) AS size FROM storage_records WHERE namespace = ?',
        ('paper.build.chunk.v1',))['size']
    assert raw_bytes * 4 // 3 <= encoded_bytes < raw_bytes * 1.34
    cache = _cache(tmp_path, size=12 * 1024 * 1024)
    tracemalloc.start()
    try:
        repository.ensure_cached(cache, proof['document_id'], proof['build_id'])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f'9 MiB restore buffered {peak} Python bytes'


def test_cancellation_during_retention_remains_an_abort_without_a_reference(storage, tmp_path, monkeypatch):
    from lib.llm_errors import AbortedError
    from lib.research.compilation import CompilationResult
    build = _build(tmp_path)
    active, cancel = threading.Event(), threading.Event()
    active.set()
    compiled = CompilationResult(7, build.receipt['document_id'], 'c' * 64, {}, active, cancel, build)
    original = storage.client.command
    def cancel_after_chunk(operation, payload, command_id):
        result = original(operation, payload, command_id)
        if operation == 'paper.artifact.append':
            cancel.set()
        return result
    monkeypatch.setattr(storage.client, 'command', cancel_after_chunk)
    repository = _repository(storage)
    with pytest.raises(AbortedError):
        with compiled.retain_artifacts(repository):
            pytest.fail('cancelled upload must not yield a commit witness')
    assert repository.latest(build.receipt['document_id']) is None
    assert _call(storage, 'prune', {})['progress']
