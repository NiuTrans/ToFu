"""Observed file hashes remain immutable review sources after model compaction."""
import hashlib
import json
import pytest

from lib.evolution import artifact_evidence as artifacts, records
from lib.evolution.contracts import EvolutionStateError
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_artifact_evidence import runtime as runtime, project_run, read_artifacts, no_reindex
from tests.evolution.test_evolution_runs import finish, OWNER

pytestmark = pytest.mark.unit


def test_compacted_fingerprints_reach_independent_review_without_live_file_reads(runtime, tmp_path):
    run, task = project_run(runtime, tmp_path)
    path = tmp_path/'candidate.py';path.write_text('original generation')
    first = artifacts.fingerprint(task, 'candidate.py')
    path.write_text('changed generation')
    second = artifacts.fingerprint(task, 'candidate.py')
    task['messages'] = [];task['toolRounds'] = []
    path.unlink()
    reviewing = finish(runtime, task, 'Inspect retained read receipts; file bytes are not execution proof.')
    assert reviewing['status'] == 'reviewing'
    reviewer_prompt = runtime[2][-1]['messages'][0]['content']
    assert 'kind=file_fingerprint' in reviewer_prompt
    assert 'does not prove that the experiment loaded those bytes' in reviewer_prompt
    from lib.evolution.stage_outcomes import read_metadata
    metadata = read_metadata(run['run_id'], task['id'], user_id=OWNER)
    assert metadata['artifact_evidence']['artifact_count'] == 2
    assert metadata['artifact_evidence']['raw_bytes'] == 0
    data = [json.loads(line) for line in read_artifacts(runtime[2][-1], reviewing).splitlines()]
    assert [row['sha256'] for row in data] == [second['sha256'], first['sha256']]
    assert [row['sequence'] for row in data] == [2, 1]
    assert all(row['kind'] == 'file_fingerprint' and 'content' not in row for row in data)


@pytest.mark.parametrize('damage', ['missing', 'hash', 'count', 'foreign_owner'])
def test_broken_fingerprint_chain_cannot_describe_complete_evidence(runtime, tmp_path, damage):
    run, task = project_run(runtime, tmp_path);(tmp_path/'candidate').write_text('bytes')
    artifacts.fingerprint(task, 'candidate')
    manifest = artifacts.freeze(run, task, user_id=OWNER)
    reference = manifest['fingerprint_log']
    key = artifacts._fingerprint_key(task['id'], reference['head'], OWNER)
    if damage == 'missing': runtime[1].rows.pop((artifacts.FINGERPRINTS, key))
    elif damage == 'hash': runtime[1].rows[(artifacts.FINGERPRINTS,key)]['fingerprint']['sha256'] = 'a'*64
    elif damage == 'count': manifest = dict(manifest, fingerprint_log=dict(reference, count=reference['count']+1))
    with pytest.raises(EvolutionStateError): artifacts.describe(manifest, OWNER + 1 if damage == 'foreign_owner' else OWNER)


@pytest.mark.parametrize('lost_ack', [False, True])
def test_unpublished_node_cannot_replace_manifest_and_lost_ack_is_exact(runtime, tmp_path, monkeypatch, lost_ack):
    run, task = project_run(runtime, tmp_path);(tmp_path/'candidate').write_text('bytes')
    command = runtime[1].command
    def write(operation, payload, command_id):
        if payload['namespace'] == artifacts.MANIFESTS:
            if lost_ack: command(operation, payload, command_id)
            raise StorageError('database_internal', 'controlled manifest acknowledgement loss')
        return command(operation, payload, command_id)
    monkeypatch.setattr(runtime[1], 'command', write)
    if lost_ack:
        result = artifacts.fingerprint(task, 'candidate')
        assert result['sha256'] == hashlib.sha256(b'bytes').hexdigest()
        manifest, _ = artifacts._manifest(run, task, OWNER)
        assert manifest['fingerprint_log']['count'] == 1
    else:
        with pytest.raises(StorageError): artifacts.fingerprint(task, 'candidate')
        manifest, _ = artifacts._manifest(run, task, OWNER)
        assert 'fingerprint_log' not in manifest
        assert any(namespace == artifacts.FINGERPRINTS for namespace, _ in runtime[1].rows)


def test_many_read_receipts_keep_manifest_small_and_do_not_capture_contents(runtime, tmp_path):
    run, task = project_run(runtime, tmp_path);path = tmp_path/'candidate'
    sizes = []
    for number in range(100):
        path.write_text(f'generation {number}')
        artifacts.fingerprint(task, 'candidate')
        manifest, _ = artifacts._manifest(run, task, OWNER)
        sizes.append(len(artifacts._json(manifest).encode()))
    assert max(sizes) - min(sizes) < 8
    frozen = artifacts.freeze(run, task, user_id=OWNER)
    descriptor = artifacts.describe(frozen, OWNER)
    assert descriptor['artifact_count'] == 100 and descriptor['raw_bytes'] == 0
    assert len(list(artifacts.lines(frozen, OWNER))) == 100
    nodes = [row for (namespace, _), row in runtime[1].rows.items() if namespace == artifacts.FINGERPRINTS]
    assert all('content' not in row['fingerprint'] for row in nodes)
    print('fingerprint receipt measurement:', json.dumps({'reads':100,
        'manifest_bytes':max(sizes), 'node_json_bytes':sum(len(artifacts._json(row).encode()) for row in nodes)}))


@pytest.mark.parametrize('namespace', [artifacts.FINGERPRINTS, artifacts.MANIFESTS])
def test_false_positive_ack_never_returns_published_fingerprint(runtime, tmp_path, monkeypatch, namespace):
    run, task = project_run(runtime, tmp_path);(tmp_path/'candidate').write_text('bytes')
    command = runtime[1].command
    def write(operation, payload, command_id):
        return {'version':1} if payload['namespace'] == namespace else command(operation, payload, command_id)
    monkeypatch.setattr(runtime[1], 'command', write)
    with pytest.raises(StorageError, match='not exactly acknowledged'):
        artifacts.fingerprint(task, 'candidate')
    manifest, _ = artifacts._manifest(run, task, OWNER)
    assert 'fingerprint_log' not in manifest


def test_later_writer_preserves_exact_acknowledgement_of_earlier_receipt(runtime, tmp_path, monkeypatch):
    run, task = project_run(runtime, tmp_path)
    (tmp_path/'first').write_text('first');(tmp_path/'second').write_text('second')
    command = runtime[1].command;interleaved = [False]
    def write(operation, payload, command_id):
        ack = command(operation, payload, command_id)
        if payload['namespace'] == artifacts.MANIFESTS and not interleaved[0]:
            interleaved[0] = True
            artifacts.fingerprint(task, 'second')
        return ack
    monkeypatch.setattr(runtime[1], 'command', write)
    artifacts.fingerprint(task, 'first')
    manifest = artifacts.freeze(run, task, user_id=OWNER)
    assert manifest['fingerprint_log']['count'] == 2
    assert [json.loads(line)['path'] for line in artifacts.lines(manifest, OWNER)] == ['second', 'first']


def test_scoped_mismatch_survives_compaction_without_rewriting_frozen_protocol(runtime, tmp_path):
    from tests.evolution.test_evolution_scientific_observations import protocol
    from tests.support.research_observations import synthetic_frozen_scope
    frozen = protocol();frozen['measurement_scope'] = synthetic_frozen_scope(frozen)
    run = runtime[0].submit_run({'objective':'Read the frozen generation identity',
        'project_path':str(tmp_path), 'scientific_protocol':frozen, 'max_attempts':1}, user_id=OWNER)
    task = runtime[2][-1];(tmp_path/'candidate').write_text('different bytes')
    report = artifacts.fingerprint(task, 'candidate', scope_field='candidate_digest')
    assert report['matches_precommit'] is False
    task['messages'] = [];task['toolRounds'] = []
    manifest = artifacts.freeze(run, task, user_id=OWNER)
    retained = json.loads(next(artifacts.lines(manifest, OWNER)))
    assert retained['expected_sha256'] == frozen['measurement_scope']['candidate_digest']
    assert retained['sha256'] == report['sha256'] and retained['matches_precommit'] is False
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['scientific_protocol'] == frozen


def test_stop_during_node_write_does_not_publish_new_manifest_root(runtime, tmp_path, monkeypatch):
    import threading
    run, task = project_run(runtime, tmp_path);(tmp_path/'candidate').write_text('bytes')
    task['abort_event'] = threading.Event();command = runtime[1].command
    def write(operation, payload, command_id):
        ack = command(operation, payload, command_id)
        if payload['namespace'] == artifacts.FINGERPRINTS: task['abort_event'].set()
        return ack
    monkeypatch.setattr(runtime[1], 'command', write)
    with pytest.raises(EvolutionStateError, match='cancelled'): artifacts.fingerprint(task, 'candidate')
    manifest, _ = artifacts._manifest(run, task, OWNER)
    assert 'fingerprint_log' not in manifest


def test_parallel_file_reads_publish_every_receipt_without_overwriting_siblings(runtime, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    run, task = project_run(runtime, tmp_path)
    paths = [f'candidate-{number}' for number in range(32)]
    for path in paths: (tmp_path/path).write_text(path)
    with ThreadPoolExecutor(max_workers=8) as workers:
        reports = list(workers.map(lambda path: artifacts.fingerprint(task, path), paths))
    frozen = artifacts.freeze(run, task, user_id=OWNER)
    rows = [json.loads(line) for line in artifacts.lines(frozen, OWNER)]
    assert frozen['fingerprint_log']['count'] == len(paths)
    assert {row['sha256'] for row in rows} == {report['sha256'] for report in reports}
