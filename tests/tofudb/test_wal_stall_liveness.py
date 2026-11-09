"""A stalled WAL write never blocks lease renewals, claims or reads.

Native authorities on network volumes run Normal WAL durability. A commit used
to perform its WAL frame's physical write while holding the authority mutex,
so one stalled volume write held every lease renewal and claim behind it
(2026-11-07: renewals missed and 12 durable workers were fenced). The bound is
the worker lease contract (contracts/durable_worker_runtime_v1.json): a renewal
sent at the heartbeat interval must commit before the lease it extends expires.

Native only: SQLite and PostgreSQL commit through one log flush per commit, so
a stalled log device stalls every writer there by design.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.api,
              pytest.mark.skipif(sys.platform != 'linux', reason='LD_PRELOAD write shim')]


def _renewal_deadline_seconds() -> float:
    policy = json.loads((ROOT / 'contracts/durable_worker_runtime_v1.json').read_text())
    return (policy['workerLeaseMilliseconds'] - policy['heartbeatIntervalMilliseconds']) / 1000


@pytest.fixture
def stalled_native_authority(tmp_path, monkeypatch):
    from scripts.tofudb_perf_compare import TofuDbEngine, _OWNER_ID
    from serverctl_pkg.native_engine import verify_current
    from tests.support.native_engine_prerequisite import inherited_engine_refusal_skips

    compiler = shutil.which('cc')
    if not compiler:
        pytest.skip('C compiler required for the WAL write stall shim')
    binary = os.environ.get('TOFUDB_BIN')
    if not binary:
        pytest.skip('Set TOFUDB_BIN to a source-matched native engine')
    binary = Path(binary).resolve()
    explicit = os.environ.get('TOFU_TEST_TOFUDB_BIN')
    expected_sha256 = ''
    if explicit and Path(explicit).expanduser().resolve() == binary:
        expected_sha256 = os.environ['TOFUDB_EXPECTED_SHA256']
    else:
        with inherited_engine_refusal_skips():
            verify_current(binary, ROOT)
    shim = tmp_path / 'walstall.so'
    subprocess.run([compiler, '-shared', '-fPIC', '-O2', str(ROOT / 'tests/fixtures/wal_stall/walstall.c'),
                    '-o', str(shim), '-ldl'], check=True, timeout=60)
    trigger = tmp_path / 'wal-stall'
    monkeypatch.setenv('LD_PRELOAD', str(shim))
    monkeypatch.setenv('WAL_STALL_TRIGGER', str(trigger))
    engine = TofuDbEngine(binary, durability='normal', authority_dir_parent=tmp_path,
                          expected_sha256=expected_sha256)
    engine.start()
    try:
        yield engine._client, _OWNER_ID, trigger
    finally:
        trigger.unlink(missing_ok=True)
        engine.stop()


def _until(deadline: float, observe):
    while time.monotonic() < deadline:
        value = observe()
        if value:
            return value
        time.sleep(0.005)
    return None


def test_stalled_wal_write_never_blocks_renewals_claims_or_reads(stalled_native_authority):
    client, owner, trigger = stalled_native_authority
    now_ms = lambda: int(time.time() * 1000)
    client.command('record.put', {'namespace': 'liveness', 'key': 'read', 'value': {'answer': 42}},
                   'put-read')
    for task_id in ('held', 'queued'):
        client.command('worker_job.enqueue', {
            'task_id': task_id, 'user_id': owner, 'tenant_id': '', 'task_kind': task_id,
            'idempotency_key': task_id, 'payload': {}, 'priority': 0, 'available_at_ms': 0,
            'now_ms': now_ms()}, f'enqueue-{task_id}')
    held = client.command('worker_job.claim_next', {
        'worker_id': 'holder', 'task_kinds': ['held'], 'now_ms': now_ms(), 'lease_ms': 60_000,
        'retain_claim': True}, 'claim-held')
    assert held['taskId'] == 'held'

    results = {}

    def command(name, operation, payload):
        results[name] = client.command(operation, payload, f'stalled-{name}')

    bulk = threading.Thread(target=command, args=('bulk', 'record.put', {
        'namespace': 'bulk', 'key': 'workspace-meta', 'value': {'blob': 'x' * 65_536}}))
    renewal = threading.Thread(target=command, args=('renewal', 'worker_job.heartbeat', {
        'task_id': 'held', 'worker_id': 'holder', 'fencing_token': held['fencingToken'],
        'now_ms': now_ms(), 'lease_ms': 60_000, 'replay_cursor': 7}))
    claim = threading.Thread(target=command, args=('claim', 'worker_job.claim_next', {
        'worker_id': 'claimer', 'task_kinds': ['queued'], 'now_ms': now_ms(), 'lease_ms': 60_000,
        'retain_claim': True}))
    job = lambda task_id: client.query('worker_job.get', {'task_id': task_id, 'user_id': owner})
    trigger.touch()
    try:
        deadline = time.monotonic() + _renewal_deadline_seconds()
        bulk.start()
        entered = trigger.with_name(trigger.name + '.entered')
        assert _until(deadline, entered.exists), 'the bulk write must reach the stalled volume'
        renewal.start()
        claim.start()
        renewed = _until(deadline, lambda: job('held')['replayCursor'] == 7)
        claimed = _until(deadline, lambda: job('queued')['claimOwner'] == 'claimer')
        read = client.query('record.get', {'namespace': 'liveness', 'key': 'read'})
        ready = client.health(deadline=5.0).get('ready')
        still_stalled = trigger.exists() and bulk.is_alive()
    finally:
        trigger.unlink(missing_ok=True)
    for thread in (bulk, renewal, claim):
        thread.join(timeout=60)
    assert renewed, 'a lease renewal must commit while another WAL write is stalled'
    assert claimed, 'a claim must commit while another WAL write is stalled'
    assert read['value'] == {'answer': 42}
    assert ready is True, 'a stalled WAL write is not an authority wedge'
    assert still_stalled, 'the stalled command answers only after its own write'
    assert results['renewal']['ok'] is True
    assert results['claim']['taskId'] == 'queued'
    assert results['bulk']['version'] == 1
