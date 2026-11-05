"""A deterministic native bound is refused once; transient capacity is replayed.

Drives the production storage.v2 client (``lib/storage/v2_client.py``) against
a real ``tofu-db serve``. A fixed per-request bound (a billing ledger page past
its scan bound) is classified where the engine enforces it, so the client
raises one non-retryable ``database_resource_exhausted`` carrying the bound's
literal instead of replaying it as load shedding. A capacity quota (the swarm
session capacity, docs/AGENT_LIFECYCLE.md) stays retryable and is replayed
under the same command identity. Engine-side classification is pinned by
``packages/tofu-db/src/resource_exhaustion_tests.rs``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.storage import v2_client as v2_module  # noqa: E402
from lib.storage.errors import StorageError, is_transient_storage_error  # noqa: E402
from tests.tofudb.test_tofudb_differential import (  # noqa: E402
    _OWNER_ID,
    _TENANT_ID,
    _TEST_SECRET,
    _resolve_tofudb_binary,
)

# serial: builds/spawns the native tofu-db engine (see test_tofudb_tofusql.py).
pytestmark = [pytest.mark.unit, pytest.mark.serial]

_ROOT = Path(__file__).resolve().parents[2]


def _contract_bound(table: str, field: str) -> int:
    contract = json.loads((_ROOT / 'contracts/tofudb_ir_v1.json').read_text(encoding='utf-8'))
    return next(entry[field] for entry in contract['logicalTables'] if entry['name'] == table)


@pytest.fixture(scope='module')
def daemon_port():
    binary = _resolve_tofudb_binary()
    data_dir = Path(tempfile.mkdtemp(prefix='tofudb-refusal-authority-'))
    projection_parent = Path(tempfile.mkdtemp(prefix='tofudb-refusal-projection-'))
    env = dict(os.environ)
    env['TOFU_STORAGE_TOKEN'] = _TEST_SECRET.decode('ascii')
    subprocess.run([str(binary), 'init', '--data-dir', str(data_dir)],
                   check=True, capture_output=True, text=True, timeout=120, env=env)
    process = subprocess.Popen(
        [str(binary), 'serve', '--data-dir', str(data_dir),
         '--owner-id', str(_OWNER_ID), '--tenant-id', str(_TENANT_ID),
         '--search-projection-dir', str(projection_parent / 'projection')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True)
    try:
        readiness = process.stdout.readline().strip()
        if not readiness:
            raise RuntimeError('tofu-db serve exited before readiness')
        yield int(json.loads(readiness)['port'])
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(projection_parent, ignore_errors=True)


@pytest.fixture
def client(daemon_port, monkeypatch):
    dispatched = []
    request = v2_module.StorageV2Session.request

    def counted(session, operation, *args, **kwargs):
        dispatched.append(operation)
        return request(session, operation, *args, **kwargs)

    monkeypatch.setattr(v2_module.StorageV2Session, 'request', counted)
    storage = v2_module.StorageV2Client(
        '127.0.0.1', daemon_port, _TEST_SECRET,
        owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=15.0)
    try:
        yield storage, dispatched
    finally:
        storage.close()


def test_a_deterministic_bound_is_refused_once_with_its_literal(client):
    storage, dispatched = client
    scan_rows = _contract_bound('billing_wallets_and_ledger', 'maximumListScanRows')
    page = {'user_id': 'billing-user', 'limit': 1, 'kinds': []}

    with pytest.raises(StorageError) as refused:
        storage.query('billing.ledger.list', dict(page, offset=scan_rows), deadline=15.0)

    error = refused.value
    assert (error.code, error.message, error.retryable, error.retry_after_ms) == (
        'database_resource_exhausted', 'billing ledger list scan exceeds its row bound',
        False, None)
    assert not is_transient_storage_error(error)
    assert dispatched == ['billing.ledger.list']
    assert storage.query('billing.ledger.list', dict(page, offset=scan_rows - 1),
                         deadline=15.0) == []


def test_a_bound_inside_a_domain_owner_refuses_a_command_once(client):
    # The paper owner raises its anchor bound as an io::Error deep inside the
    # transaction; the declared class must survive to the wire, and a command
    # (otherwise replayed on database_unavailable) must not be resent.
    storage, dispatched = client
    anchor_bytes = _contract_bound('paper_notes', 'maximumDocumentBytes')
    note = {'user_id': _OWNER_ID, 'id': 'oversized-anchor', 'paper_hash': 'paper',
            'note': 'n', 'created_at': 1, 'updated_at': 1}

    with pytest.raises(StorageError) as refused:
        storage.command('paper.note.create', dict(note, anchor={'text': 'x' * anchor_bytes}),
                        'oversized-anchor-create', deadline=15.0)

    error = refused.value
    assert (error.code, error.message, error.retryable) == (
        'database_resource_exhausted', 'paper note anchor exceeds its byte bound', False)
    assert dispatched == ['paper.note.create']
    storage.command('paper.note.create', dict(note, anchor={'text': 'x'}),
                    'bounded-anchor-create', deadline=15.0)


def test_a_capacity_quota_stays_retryable_and_is_replayed(client):
    storage, dispatched = client
    capacity = _contract_bound('swarm_sessions', 'maximumSessionsPerOwner')

    def save(index: int) -> None:
        storage.command('swarm.session.save', {
            'swarm_key': f'capacity-{index}', 'specs': [{'id': 'a'}],
            'config': {'user_id': _OWNER_ID}, 'now_ms': 1_000 + index,
        }, f'capacity-save-{index}', deadline=15.0)

    for index in range(capacity):
        save(index)
    dispatched.clear()

    with pytest.raises(StorageError) as refused:
        save(capacity)

    error = refused.value
    assert (error.code, error.message, error.retryable) == (
        'database_unavailable', 'database_resource_exhausted', True)
    assert is_transient_storage_error(error)
    assert len(dispatched) > 1 and set(dispatched) == {'swarm.session.save'}
