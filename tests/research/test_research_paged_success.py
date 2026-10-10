"""Paged successful jobs publish full scientific evidence without trusting summaries."""
from copy import deepcopy
from dataclasses import replace
import json
import pytest

from lib.research.workspace import save_workspace, load_workspace, open_run_receipts
from lib.research.receipt_settlement import prepare_success_settlement
from lib.research.persistence import research_direction_hash
from lib.research.program import dependency_digests, normalize_run_evidence
from lib.research.science_reduction import bound_receipt_evidence
from lib.research.decisions import authoritative, receipt_outcome_consistent
from lib.storage.errors import StorageError
from tests.research.test_research_workspace_evidence_guard import storage as storage
from tests.research.test_research_decisions import result_workspace
from tests.research.test_research_pending_paging import save, apply, receipt, DIRECTION
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit
RULE = {'direction': 'higher', 'minimum_effect': 0, 'uncertainty_multiplier': 2}


def initial_science(outcome='supported'):
    workspace = result_workspace(outcome); workspace['direction'] = DIRECTION
    workspace['protocol']['decision_rule'] = deepcopy(RULE)
    scientific = receipt('job-B', 'completed', 'poll', 'science')
    scientific.update(source_task_id='original', artifact_ref='artifact:1',
        observation_excerpt=json.dumps(observation_payload(workspace['protocol'], 'artifact:1', outcome)))
    queued = receipt('job-A', 'running', 'submit', 'submit'); queued['source_task_id'] = 'original'
    queued['execution_observation']['provider_scope'] = 'provider:cluster'
    scientific['execution_observation']['provider_scope'] = 'provider:cluster'
    workspace['runs'] = normalize_run_evidence([dict(id='run', status='running', execution_status='running',
        tool_receipts=[queued, scientific], **dependency_digests(workspace))])
    return workspace


def prepare(client, candidate, prior):
    return prepare_success_settlement(client, candidate, prior, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en', run_id='run', revision=prior['revision'])


def complete(client, prior):
    return apply(prior, 'complete-A', [receipt('job-A', 'completed', 'poll', 'terminal')], client)


def commit(client, candidate, prior):
    return save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1,
                          success_settlement=prepare(client, candidate, prior))


def test_full_paged_success_counts_early_science_and_all_sources_after_restart(storage):
    supervisor, _ = storage; prior = save(initial_science())
    prior = save(apply(prior, 'many-polls', [receipt('job-B', 'completed', 'poll', f'poll-{n}')
                                           for n in range(63)], supervisor.client))
    saved = commit(supervisor.client, complete(supervisor.client, prior), prior)
    run = saved['runs'][0]
    assert run['execution_status'] == 'completed' and run['scientific_outcome'] == 'supported'
    assert 'artifact:1' in run['artifact_refs'] and authoritative(run)
    assert saved['decision']['status'] == 'retain'
    assert all(run['receipt_evidence']['checks'].values())
    assert run['receipt_evidence']['observed_receipts'] == 66
    assert saved['history']['source_index']['count'] == 66
    supervisor.stop(); supervisor.start()
    assert load_workspace(DIRECTION, user_id=1) == saved
    with open_run_receipts(DIRECTION, user_id=1, run_id='run') as stream:
        assert len(list(stream['receipts'])) == 66
    changed = deepcopy(saved); changed['hypothesis'] = 'Different hypothesis'
    changed = save_workspace(DIRECTION, 'en', changed, expected_revision=saved['revision'], user_id=1)
    assert changed['runs'][0]['validity'] == 'stale' and not authoritative(changed['runs'][0])


def test_early_refutation_and_late_support_settle_as_inconclusive(storage):
    supervisor, _ = storage; prior = save(initial_science('refuted'))
    late = receipt('job-A', 'completed', 'poll', 'terminal')
    late['observation_excerpt'] = json.dumps(observation_payload(prior['protocol'], 'artifact:1', 'supported'))
    candidate = apply(prior, 'complete-A', [late], supervisor.client)
    saved = commit(supervisor.client, candidate, prior)
    run = saved['runs'][0]
    assert run['scientific_outcome'] == 'inconclusive' and saved['decision']['status'] == 'inconclusive'
    assert authoritative(run) and receipt_outcome_consistent(run, saved['protocol'])


@pytest.mark.parametrize('mutation', ['source', 'owner', 'revision', 'without_port', 'active'])
def test_success_projection_cannot_publish_without_exact_full_history_proof(storage, mutation):
    supervisor, _ = storage; prior = save(initial_science()); candidate = complete(supervisor.client, prior)
    port = prepare(supervisor.client, candidate, prior)
    if mutation == 'source': candidate['runs'][0]['tool_receipts'][0]['result_digest'] = 'changed'
    elif mutation == 'owner': port = replace(port, user_id=2)
    elif mutation == 'revision': port = replace(port, revision=port.revision+1)
    elif mutation == 'without_port': port = None
    else:
        candidate = apply(prior, 'complete-B', [receipt('job-B', 'completed', 'poll', 'terminal')], supervisor.client)
        candidate['runs'][0]['execution_status'] = 'completed'
        with pytest.raises(ValueError, match='complete completed job observations'):
            prepare(supervisor.client, candidate, prior)
        assert load_workspace(DIRECTION, user_id=1) == prior
        return
    with pytest.raises(ValueError):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1, success_settlement=port)
    assert load_workspace(DIRECTION, user_id=1) == prior


def test_raw_summary_or_changed_run_cannot_grant_scientific_authority(storage):
    supervisor, _ = storage; prior = save(initial_science())
    saved = commit(supervisor.client, complete(supervisor.client, prior), prior)
    forged = deepcopy(saved)
    copied = dict(deepcopy(prior['runs'][0]), id='forged')
    copied['receipt_evidence'] = deepcopy(saved['runs'][0]['receipt_evidence'])
    from lib.research.science_reduction import receipt_evidence_digest
    copied['receipt_evidence']['run_digest'] = receipt_evidence_digest(copied)
    forged['runs'].append(copied)
    with pytest.raises(ValueError, match='server-owned'):
        save_workspace(DIRECTION, 'en', forged, expected_revision=saved['revision'], user_id=1)
    changed = deepcopy(saved['runs'][0]); changed['tool_receipts'][0]['result_digest'] = 'changed'
    assert not bound_receipt_evidence(changed) and not authoritative(changed)
    wrong_protocol = deepcopy(saved['protocol']); wrong_protocol['decision_rule']['direction'] = 'lower'
    assert not receipt_outcome_consistent(saved['runs'][0], wrong_protocol)


def test_success_cas_refusal_does_not_publish_science_or_consumed_sources(storage, monkeypatch):
    supervisor, _ = storage; prior = save(initial_science()); candidate = complete(supervisor.client, prior)
    port = prepare(supervisor.client, candidate, prior); command = supervisor.client.command
    def refuse(operation, *args, **kwargs):
        if operation == 'research.workspace.put': raise StorageError('database_conflict', 'refused success CAS')
        return command(operation, *args, **kwargs)
    monkeypatch.setattr(supervisor.client, 'command', refuse)
    with pytest.raises(StorageError, match='refused success CAS'):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1, success_settlement=port)
    assert load_workspace(DIRECTION, user_id=1) == prior and prior['history']['source_index']['count'] == 0
    monkeypatch.setattr(supervisor.client, 'command', command)
    saved = save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1, success_settlement=port)
    assert saved['runs'][0]['scientific_outcome'] == 'supported'


def test_archival_and_scope_rebuild_keep_bound_paged_science(storage):
    supervisor, _ = storage; prior = save(initial_science())
    saved = commit(supervisor.client, complete(supervisor.client, prior), prior)
    candidate = deepcopy(saved)
    for n in range(23):
        candidate['runs'].append(normalize_run_evidence([dict(id=f'other-{n}', execution_status='completed',
            status='inconclusive', protocol_digest='f'*64, hypothesis_digest='f'*64,
            tool_receipts=[])])[0])
    archived = save_workspace(DIRECTION, 'en', candidate, expected_revision=saved['revision'], user_id=1)
    assert all(run['id'] != 'run' for run in archived['runs'])
    assert archived['history']['outcomes']['supported'] == 1 and archived['decision']['status'] == 'retain'
    changed = deepcopy(archived); changed['hypothesis'] = 'Other hypothesis'
    changed = save_workspace(DIRECTION, 'en', changed, expected_revision=archived['revision'], user_id=1)
    assert changed['history']['outcomes']['supported'] == 0
    restored = deepcopy(changed); restored['hypothesis'] = saved['hypothesis']
    restored = save_workspace(DIRECTION, 'en', restored, expected_revision=changed['revision'], user_id=1)
    assert restored['history']['outcomes']['supported'] == 1


def test_invalid_early_numbers_cannot_pass_after_late_success(storage):
    supervisor, _ = storage; original = initial_science()
    payload = json.loads(original['runs'][0]['tool_receipts'][1]['observation_excerpt'])
    payload['scientific_observations']['samples']['mean_difference'] = 999
    original['runs'][0]['tool_receipts'][1]['observation_excerpt'] = json.dumps(payload)
    prior = save(original)
    saved = commit(supervisor.client, complete(supervisor.client, prior), prior)
    assert saved['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert not any(saved['runs'][0]['receipt_evidence']['checks'].values())


@pytest.mark.parametrize('mutation', ['version', 'count', 'extra'])
def test_machine_summary_rejects_noncanonical_types_and_unknown_fields(mutation):
    from lib.research.science_reduction import normalize_receipt_evidence
    value = dict(version=1, run_digest='a'*64, scientific_outcome='inconclusive', outcome_consistent=False,
        observed_receipts=1, checks=dict.fromkeys(('baseline_fairness','uncertainty','seeds','validation_split'),False),
        verification_basis='reported_observation_consistency/v1')
    if mutation == 'version': value['version'] = True
    elif mutation == 'count': value['observed_receipts'] = 1.0
    else: value['unknown'] = True
    with pytest.raises(ValueError, match='Invalid'): normalize_receipt_evidence(value)


def test_oversized_early_scientific_packet_blocks_paged_success_audit(storage):
    from lib.research.action import _tool_receipts
    from tests.research.test_research_observation_capture import captured_row
    supervisor, _ = storage
    original = initial_science('refuted')
    payload = observation_payload(original['protocol'], 'artifact:1', 'refuted')
    payload['scientific_observations']['unknown_large_field'] = 'x' * 5000
    row, _ = captured_row(payload)
    captured = _tool_receipts([row])[0]
    captured.update(source_task_id='original', call_id='science', artifact_ref='artifact:1',
        execution_observation={'state':'completed','job_id':'job-B','provider_scope':'provider:cluster'})
    original['runs'][0]['tool_receipts'][1] = captured
    prior = save(original)
    supervisor.stop(); supervisor.start()
    prior = load_workspace(DIRECTION, user_id=1)
    late = receipt('job-A', 'completed', 'poll', 'terminal')
    late['observation_excerpt'] = json.dumps(observation_payload(prior['protocol'], 'artifact:1', 'supported'))
    saved = commit(supervisor.client, apply(prior, 'complete-A', [late], supervisor.client), prior)
    assert saved['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert not saved['runs'][0]['receipt_evidence']['outcome_consistent']
    assert not any(saved['runs'][0]['receipt_evidence']['checks'].values())
    with open_run_receipts(DIRECTION, user_id=1, run_id='run') as stream:
        assert any(row.get('observation_incomplete') is True for row in stream['receipts'])
