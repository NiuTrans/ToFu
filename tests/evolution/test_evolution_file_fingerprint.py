"""Real project byte fingerprints stay bounded, cancellable and source scoped."""
import hashlib
import os
import threading
import pytest
from lib.evolution import artifact_evidence as artifacts
from lib.evolution.contracts import EvolutionContractError, EvolutionStateError
from tests.evolution.test_evolution_artifact_evidence import runtime as runtime, project_run, OWNER

pytestmark = pytest.mark.unit


def test_fingerprint_streams_binary_file_larger_than_snapshot_limit(runtime, tmp_path):
    run, task = project_run(runtime, tmp_path)
    data = bytes(range(256)) * 20000
    (tmp_path/'data.bin').write_bytes(data)
    writes = runtime[1].writes
    result = artifacts.declare(task, {'fingerprint_path':'data.bin'})
    assert result['sha256'] == hashlib.sha256(data).hexdigest() and result['bytes'] == len(data)
    assert result['algorithm'] == 'sha256' and result['run_id'] == run['run_id']
    assert runtime[1].writes > writes
    # Complete snapshots have no former fixed size cutoff.
    saved = artifacts.declare(task, {'path':'data.bin'})
    assert saved['bytes'] == len(data)


@pytest.mark.parametrize('path', ['../outside', '/absolute', '.env', 'secrets.json', 'directory', 'link', 'fifo'])
def test_fingerprint_keeps_private_path_and_regular_file_boundary(runtime, tmp_path, path):
    run, task = project_run(runtime, tmp_path)
    (tmp_path/'directory').mkdir()
    (tmp_path/'data').write_text('private project generation')
    (tmp_path/'link').symlink_to(tmp_path/'data')
    os.mkfifo(tmp_path/'fifo')
    with pytest.raises((EvolutionStateError, OSError)):
        artifacts.declare(task, {'fingerprint_path':path})


@pytest.mark.parametrize('mutation', ['write', 'stop', 'replace_executor'])
def test_changed_or_revoked_generation_never_returns_fingerprint(runtime, tmp_path, monkeypatch, mutation):
    run, task = project_run(runtime, tmp_path)
    path = tmp_path/'candidate.py';path.write_bytes(b'x'*2000000)
    read = artifacts.os.read
    changed = False
    def interfere(fd, size):
        nonlocal changed
        block = read(fd, size)
        if not changed and block:
            changed = True
            if mutation == 'write':
                path.write_bytes(b'y'*2000000)
            elif mutation == 'stop':
                task['abort_event'] = threading.Event();task['abort_event'].set()
            else:
                current = runtime[0].get_run(run['run_id'], user_id=OWNER)
                current['task_id'] = 'replacement-executor'
                from lib.evolution import records
                records.put_record(records.NAMESPACE_RUNS,
                    records.entity_key(run['run_id'], OWNER, kind='run'), current, kind='run')
        return block
    monkeypatch.setattr(artifacts.os, 'read', interfere)
    with pytest.raises(EvolutionStateError):
        artifacts.declare(task, {'fingerprint_path':'candidate.py'})


@pytest.mark.parametrize('extra', [{'path':'data'}, {'paths':['data']}, {'delivery':'Done'}, {'delivery_source':{'artifact_id':'a'*64,'content_sha256':'0'*64}}])
def test_fingerprint_cannot_be_combined_with_capture_or_delivery(runtime, tmp_path, extra):
    run, task = project_run(runtime, tmp_path)
    with pytest.raises(EvolutionContractError):
        artifacts.declare(task, {'fingerprint_path':'data', **extra})


def test_large_fingerprint_keeps_python_allocation_bounded(runtime, tmp_path):
    import time
    import tracemalloc
    run, task = project_run(runtime, tmp_path)
    (tmp_path/'empty').write_bytes(b'')
    assert artifacts.declare(task, {'fingerprint_path':'empty'})['sha256'] == hashlib.sha256(b'').hexdigest()
    block = b'\x17' * (1024 * 1024)
    expected = hashlib.sha256()
    with (tmp_path/'large.bin').open('wb') as output:
        for _ in range(64):
            output.write(block);expected.update(block)
    tracemalloc.start()
    try:
        started = time.perf_counter()
        result = artifacts.declare(task, {'fingerprint_path':'large.bin'})
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result['sha256'] == expected.hexdigest() and result['bytes'] == 64 * 1024 * 1024
    assert peak < 4 * 1024 * 1024
    print(f'64 MiB fingerprint: {elapsed*1000:.2f} ms; peak traced Python bytes: {peak}')


@pytest.mark.parametrize('changed', [False, True])
def test_scope_comparison_uses_frozen_run_value_and_reports_actual_drift(runtime, tmp_path, changed):
    from tests.evolution.test_evolution_scientific_observations import protocol
    from tests.support.research_observations import synthetic_frozen_scope
    payload = b'precommitted candidate generation'
    expected = hashlib.sha256(payload).hexdigest()
    value = protocol();value['measurement_scope'] = synthetic_frozen_scope(value)
    value['measurement_scope']['candidate_digest'] = expected
    run = runtime[0].submit_run({'objective':'Validate fixed candidate bytes', 'project_path':str(tmp_path),
        'scientific_protocol':value, 'max_attempts':1}, user_id=OWNER)
    task = runtime[2][-1]
    actual = payload + b' changed' if changed else payload
    (tmp_path/'candidate.py').write_bytes(actual)
    writes = runtime[1].writes
    result = artifacts.declare(task, {'fingerprint_path':'candidate.py', 'fingerprint_scope_field':'candidate_digest'})
    assert result['expected_sha256'] == expected and result['scope_field'] == 'candidate_digest'
    assert result['sha256'] == hashlib.sha256(actual).hexdigest()
    assert result['matches_precommit'] is (not changed)
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['scientific_protocol'] == value
    assert runtime[1].writes > writes


def test_scope_check_refuses_absent_precommitment_and_caller_override(runtime, tmp_path):
    run, task = project_run(runtime, tmp_path)
    (tmp_path/'candidate.py').write_bytes(b'current bytes')
    args = {'fingerprint_path':'candidate.py','fingerprint_scope_field':'candidate_digest'}
    with pytest.raises(EvolutionStateError, match='frozen measurement scope'):
        artifacts.declare(task, args)
    with pytest.raises(EvolutionContractError):
        artifacts.declare(task, {**args, 'expected_sha256':'0'*64})
    with pytest.raises(EvolutionContractError):
        artifacts.declare(task, {'path':'candidate.py', 'fingerprint_scope_field':'candidate_digest'})


def test_fingerprint_cannot_mix_changed_precommitment_during_read(runtime, tmp_path, monkeypatch):
    from tests.evolution.test_evolution_scientific_observations import protocol
    from tests.support.research_observations import synthetic_frozen_scope
    from lib.evolution import records
    payload = b'x'*2000000
    value = protocol();value['measurement_scope'] = synthetic_frozen_scope(value)
    value['measurement_scope']['candidate_digest'] = hashlib.sha256(payload).hexdigest()
    run = runtime[0].submit_run({'objective':'Validate a frozen candidate', 'project_path':str(tmp_path),
        'scientific_protocol':value, 'max_attempts':1}, user_id=OWNER)
    task = runtime[2][-1];(tmp_path/'candidate.py').write_bytes(payload)
    read = artifacts.os.read
    changed = False
    def replace_scope(fd, size):
        nonlocal changed
        block = read(fd, size)
        if not changed and block:
            changed = True
            current = runtime[0].get_run(run['run_id'], user_id=OWNER)
            current['scientific_protocol']['measurement_scope']['candidate_digest'] = '0'*64
            records.put_record(records.NAMESPACE_RUNS,
                records.entity_key(run['run_id'], OWNER, kind='run'), current, kind='run')
        return block
    monkeypatch.setattr(artifacts.os, 'read', replace_scope)
    with pytest.raises(EvolutionStateError, match='frozen scope changed'):
        artifacts.declare(task, {'fingerprint_path':'candidate.py', 'fingerprint_scope_field':'candidate_digest'})
