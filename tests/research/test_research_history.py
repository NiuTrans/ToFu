"""Long-horizon evidence retains committed lineage, falsifiers and bounded pages."""
from copy import deepcopy
from tests.support.research_observations import observation_payload
import json

import pytest

from lib.research.history import NAMESPACE, ResearchHistoryRepository, scope_digest
from lib.research.persistence import research_direction_hash
from lib.research.program import (
    dependency_digests, normalize_program_fields, normalize_run_evidence,
    scientific_dependency_digest, readiness, claim_evidence_digest,
)
from lib.research.workspace import empty_workspace, load_workspace, save_workspace, load_history_page
from lib.storage.errors import StorageError
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit
DIRECTION = 'Long-horizon discriminating replications'


def observation(program, index, outcome='supported', *, audited=True):
    reference = f'metrics/{index}.json'
    payload = observation_payload(program['protocol'], reference, outcome)
    if not audited:
        payload.pop('scientific_observations')
    return {'id': f'run-{index}', 'task_id': f'task-{index}', 'status': 'passed',
            'execution_status': 'completed', 'scientific_outcome': outcome,
            **dependency_digests(program), 'artifact_ref': reference,
            'tool_receipts': [{'tool': 'run_command', 'status': 'done',
                'result_digest': f'receipt-{index}',
                'result_excerpt': json.dumps(payload)}]}


def draft_with_runs(count, *, negative=None):
    draft = empty_workspace(DIRECTION, 'en')
    draft['hypothesis'] = 'Compression preserves held-out accuracy'
    draft['protocol'].update(dataset='held-out', baseline='uncompressed',
                             primary_metric='accuracy', falsifier='accuracy worsens', random_seeds=[1,2,3])
    draft['runs'] = [observation(draft, i, 'refuted' if i == negative else 'supported')
                     for i in range(count)]
    return draft


def execution_draft(count, **options):
    """New dispatches precommit a rule; legacy archival fixtures stay legacy."""
    draft = draft_with_runs(0, **options)
    draft['protocol']['decision_rule'] = {'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}
    draft['runs'] = [observation(draft, i, 'refuted' if i == options.get('negative') else 'supported')
                     for i in range(count)]
    return draft


def save(draft, revision=None):
    return save_workspace(DIRECTION, 'en', draft, user_id=1,
                          expected_revision=draft['revision'] if revision is None else revision)


def test_many_batches_continue_beyond_working_document_boundary(storage):
    saved = save(draft_with_runs(24))
    for index in range(24, 96):
        saved['runs'].append(observation(saved, index))
        saved = save(saved)
        assert len(saved['runs']) < 24
    restored = load_workspace(DIRECTION, 'en', user_id=1)
    assert restored['history']['count'] + len(restored['runs']) == 96
    ids = {row['id'] for row in restored['runs']}
    page = load_history_page(DIRECTION, 'en', user_id=1)
    total = 0
    while True:
        assert len(page['runs']) <= 16
        assert page['revision'] == restored['revision']
        ids.update(row['id'] for row in page['runs'])
        total += len(page['runs'])
        if page['next_cursor'] is None:
            break
        page = load_history_page(DIRECTION, 'en', user_id=1, cursor=page['next_cursor'])
    assert total == restored['history']['count']
    assert ids == {f'run-{i}' for i in range(96)}


def test_archived_refutation_still_contests_claims_and_decision(storage):
    draft = draft_with_runs(24, negative=0)
    saved = save(draft)
    assert all(row['id'] != 'run-0' for row in saved['runs'])
    assert saved['history']['outcomes']['refuted'] == 1
    claim = {'id': 'claim', 'text': 'Compression preserves accuracy', 'status': 'supported',
             'evidence_refs': ['run-23'], **dependency_digests(saved)}
    claim['evidence_digest'] = claim_evidence_digest(claim, saved['runs'])
    saved['claims'] = [claim]
    restored = save(saved)
    assert restored['decision']['status'] == 'refine'
    assert restored['claims'][0]['status'] == 'contested'
    assert not readiness(restored)['ready']


def test_archival_relocation_preserves_scientific_counterevidence_snapshot(storage):
    draft = draft_with_runs(24, negative=0)
    before = scientific_dependency_digest(draft)
    saved = save(draft)
    assert scientific_dependency_digest(saved) == before


def test_old_missing_audit_cannot_be_hidden_by_archival(storage):
    draft = draft_with_runs(24)
    draft['runs'][0] = observation(draft, 0, audited=False)
    saved = save(draft)
    checks = {row['id']: row['ok'] for row in saved['manuscript_audit']['checks']}
    assert not checks['seeds'] and not checks['uncertainty']
    assert saved['history']['audit_failures']['seeds'] == 1


def test_changing_scope_replays_history_and_returning_scope_restores_counterevidence(storage):
    original = save(draft_with_runs(24, negative=0))
    changed = deepcopy(original)
    changed['protocol']['dataset'] = 'A genuinely different population'
    saved = save(changed)
    assert sum(saved['history']['outcomes'].values()) == 0
    assert saved['history']['scope'] == scope_digest(saved)
    assert saved['decision']['status'] == 'inconclusive'
    saved['protocol'] = original['protocol']
    restored = save(saved)
    assert restored['history']['outcomes']['refuted'] == 1
    assert restored['decision']['status'] == 'refine'


def test_workspace_body_cannot_forge_or_discard_history(storage):
    saved = save(draft_with_runs(24, negative=0))
    original = deepcopy(saved['history'])
    saved['history'] = {'head': '0' * 64, 'outcomes': {'supported': 99}}
    restored = save(saved)
    assert restored['history'] == original
    restored.pop('history')
    again = save(restored)
    assert again['history'] == original


def test_pending_and_explicitly_cited_observations_remain_in_working_basis(storage):
    draft = draft_with_runs(24)
    draft['runs'][0]['execution_status'] = 'running'
    draft['claims'] = [{'id': 'claim', 'text': 'Candidate scope', 'status': 'draft',
                        'evidence_refs': ['run-1', 'metrics/2.json', 'task-3']}]
    saved = save(draft)
    assert {'run-0', 'run-1', 'run-2', 'run-3'} <= {row['id'] for row in saved['runs']}
    assert saved['history']['count'] == 8


def test_language_and_owner_boundaries_prevent_history_leak(storage):
    save(draft_with_runs(24))
    assert load_history_page(DIRECTION, 'zh', user_id=1)['runs'] == []
    if storage[1] == 'tofudb':
        with pytest.raises(StorageError):
            load_history_page(DIRECTION, 'en', user_id=2)
    else:
        assert load_history_page(DIRECTION, 'en', user_id=2)['runs'] == []


def test_invalid_cursor_cannot_read_uncommitted_or_out_of_range_page(storage):
    saved = save(draft_with_runs(24))
    for cursor in (-1, 0, True, '8', 1, saved['history']['count'] + 1):
        with pytest.raises(ValueError):
            load_history_page(DIRECTION, 'en', user_id=1, cursor=cursor)


@pytest.mark.parametrize('failure', ['before_page', 'after_page', 'workspace_cas'])
def test_archive_failures_do_not_publish_unacknowledged_evidence(storage, monkeypatch, failure):
    import lib.research.workspace as workspace
    saved = save(draft_with_runs(23))
    client = storage[0].client

    class InjectedClient:
        def query(self, *args, **kwargs):
            return client.query(*args, **kwargs)
        def command(self, operation, payload, command_id):
            if operation == 'record.put' and payload['namespace'] == NAMESPACE:
                if failure == 'before_page':
                    raise StorageError('database_busy', 'Injected page admission failure')
                if failure == 'after_page':
                    client.command(operation, payload, command_id)
                    raise StorageError('database_timeout', 'Injected lost archive acknowledgement')
            if operation == 'research.workspace.put' and failure == 'workspace_cas':
                raise StorageError('database_conflict', 'Injected competing workspace CAS')
            return client.command(operation, payload, command_id)

    monkeypatch.setattr(workspace, '_storage', lambda **_: InjectedClient())
    candidate = deepcopy(saved)
    candidate['runs'].append(observation(candidate, 23))
    if failure == 'after_page':
        restored = save(candidate)
        assert restored['history']['count'] == 8
    else:
        with pytest.raises(StorageError) as caught:
            save(candidate)
        assert caught.value.code == ('database_busy' if failure == 'before_page' else 'database_conflict')
        assert load_workspace(DIRECTION, 'en', user_id=1) == saved
        assert load_history_page(DIRECTION, 'en', user_id=1)['runs'] == []
        monkeypatch.setattr(workspace, '_storage', lambda **_: client)
        resumed = save(candidate)
        assert resumed['history']['count'] == 8


def test_replayed_preparation_reuses_immutable_page(storage):
    saved = save(draft_with_runs(24))
    repo = ResearchHistoryRepository(storage[0].client, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en')
    original = draft_with_runs(24)
    from lib.research.history import compact_observations
    _, rows = compact_observations(original)
    again = repo.prepare(None, original, rows)
    assert again == saved['history']
    page = repo.read_chunk(again['head'], again['count'])
    assert page['runs'] == normalize_run_evidence(rows)


def test_archive_tampering_fails_integrity_instead_of_rewriting_evidence(storage):
    saved = save(draft_with_runs(24))
    repo = ResearchHistoryRepository(storage[0].client, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en')
    chunk = repo.read_chunk(saved['history']['head'], saved['history']['count'])
    chunk['runs'][0]['scientific_outcome'] = 'refuted'
    storage[0].client.command('record.put', {'namespace': NAMESPACE,
        'key': repo.prefix + saved['history']['head'], 'value': chunk, 'expected_version': 1},
        'fixture-corrupt-archive')
    with pytest.raises(StorageError) as caught:
        load_history_page(DIRECTION, 'en', user_id=1)
    assert caught.value.code == 'database_integrity'


def test_http_history_page_has_committed_revision_and_validated_cursor(storage):
    import server
    from tests.research.test_research_workspace import _run, _get_json
    saved = save(draft_with_runs(24))
    server.app.config['TESTING'] = True
    client = server.app.test_client()
    response = _run(client.get('/api/v4/research/workspace', query_string={
        'direction': DIRECTION, 'lang': 'en', 'history_cursor': '0'}))
    assert response.status_code == 200
    body = _get_json(response)
    assert body['history_page']['revision'] == saved['revision']
    assert body['history_page']['total'] == 8
    assert len(body['history_page']['runs']) == 8
    for invalid in ('-1', '1.5', 'bogus', '1'):
        bad = _run(client.get('/api/v4/research/workspace', query_string={
            'direction': DIRECTION, 'history_cursor': invalid}))
        assert bad.status_code == 400


def test_archive_schema_matches_workspace_page_and_chunk(storage):
    from pathlib import Path
    import jsonschema
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         'contracts/research_program_v1.schema.json').read_text())
    saved = save(draft_with_runs(24))
    jsonschema.validate(saved, schema)
    page = load_history_page(DIRECTION, 'en', user_id=1)
    jsonschema.validate(page, {'$defs': schema['$defs'], '$ref': '#/$defs/historyPage'})
    repo = ResearchHistoryRepository(storage[0].client, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en')
    chunk = repo.read_chunk(page['page'], page['total'])
    jsonschema.validate(chunk, {'$defs': schema['$defs'], '$ref': '#/$defs/historyChunk'})


def test_action_archives_full_legacy_window_before_any_agent_effect(storage, monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    from lib.research.context import project_workspace
    from tests.research.test_research_action import _epoch

    client = storage[0].client
    legacy = execution_draft(32, negative=0)
    # Simulate an acknowledged pre-archive workspace through the storage port.
    client.command('research.workspace.put', {'user_id': 1,
        'paper_hash': research_direction_hash(DIRECTION), 'lang': 'en',
        'expected_revision': 0, 'workspace': legacy, 'updated_at': 0}, 'legacy-seed')
    task = {'task_id': 'next-experiment', 'action': 'experiment', 'user_id': 1,
        'direction': DIRECTION, 'lang': 'en', 'expected_revision': 1,
        'confirm_external_writes': True, 'abort_event': threading.Event()}
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    monkeypatch.setattr(action, 'build_action_tool_epoch', lambda **_: (_epoch([]), [], []))

    def agent(task, current, *_):
        assert current['revision'] == 2
        assert len(current['runs']) == 16
        assert current['history']['count'] == 16
        assert project_workspace(current, 'experiment')['history']['outcomes']['refuted'] == 1
        return {'run': {'id': 'run-32', 'status': 'passed'}}, [{
            'tool': 'run_command', 'status': 'done', 'result_digest': 'result-32',
            'artifact_ref': 'metrics/32.json', 'result_excerpt': '{"scientific_outcome":"supported"}'}]

    agent_mock = Mock(side_effect=agent)
    monkeypatch.setattr(action, '_run_agent', agent_mock)
    action.run_research_action(task)
    agent_mock.assert_called_once()
    finished = owner.finish.call_args.kwargs['result']['workspace']
    assert finished['revision'] == 3
    assert len(finished['runs']) + finished['history']['count'] == 33
    assert finished['decision']['status'] == 'refine'


def test_preflight_fully_cited_completed_basis_refuses_new_effect(storage, monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    draft = execution_draft(32)
    draft['claims'] = [{'id': 'basis-a', 'text': 'First evidence subset', 'status': 'draft',
                        'evidence_refs': [row['id'] for row in draft['runs'][:24]]},
                       {'id': 'basis-b', 'text': 'Second evidence subset', 'status': 'draft',
                        'evidence_refs': [row['id'] for row in draft['runs'][24:]]}]
    saved = save(draft)
    task = {'task_id': 'no-room', 'action': 'experiment', 'user_id': 1,
        'direction': DIRECTION, 'lang': 'en', 'expected_revision': saved['revision'],
        'confirm_external_writes': True, 'abort_event': threading.Event()}
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    agent = Mock()
    monkeypatch.setattr(action, '_run_agent', agent)
    action.run_research_action(task)
    agent.assert_not_called()
    assert 'completed evidence basis is fully cited' in str(owner.finish.call_args)
    assert load_workspace(DIRECTION, 'en', user_id=1) == saved


def test_page_byte_budget_splits_large_observations_without_truncation(storage):
    from lib.research.history import empty_history
    from lib.research.program import MAX_PROGRAM_BYTES
    program = draft_with_runs(0)
    rows = []
    for index in range(4):
        row = observation(program, index)
        row['tool_receipts'] = [dict(row['tool_receipts'][0], call_id=str(i), result_excerpt='x' * 4000)
                                for i in range(64)]
        rows.append(normalize_run_evidence([row])[0])
    repo = ResearchHistoryRepository(storage[0].client, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en')
    prepared = repo.prepare(None, program, rows)
    chunks = [chunk for _, chunk in repo.iter_chunks(prepared)]
    assert len(chunks) == 2
    assert sum(len(chunk['runs']) for chunk in chunks) == 4
    for chunk in chunks:
        assert len(json.dumps(chunk, ensure_ascii=False).encode()) <= MAX_PROGRAM_BYTES
        assert len(chunk['runs'][0]['tool_receipts']) == 64
        assert len(chunk['runs'][0]['tool_receipts'][0]['result_excerpt']) == 4000


def test_large_receipts_archive_before_document_byte_ceiling(storage):
    draft = draft_with_runs(3, negative=0)
    for row in draft['runs']:
        row['tool_receipts'] = [dict(row['tool_receipts'][0], call_id=str(i), result_excerpt='x' * 4000)
                                for i in range(64)]
    saved = save(draft)
    assert len(saved['runs']) == 2
    assert saved['history']['count'] == 1
    assert saved['history']['outcomes']['refuted'] == 1
    page = load_history_page(DIRECTION, 'en', user_id=1)
    assert page['runs'][0]['id'] == 'run-0'
    assert len(page['runs'][0]['tool_receipts']) == 64


def test_stop_during_legacy_archive_preparation_never_enters_agent(storage, monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    import lib.research.workspace as workspace
    legacy = execution_draft(32)
    storage[0].client.command('research.workspace.put', {'user_id': 1,
        'paper_hash': research_direction_hash(DIRECTION), 'lang': 'en',
        'expected_revision': 0, 'workspace': legacy, 'updated_at': 0}, 'legacy-stop-seed')
    stop = threading.Event()
    task = {'task_id': 'stop-compaction', 'action': 'experiment', 'user_id': 1,
        'direction': DIRECTION, 'lang': 'en', 'expected_revision': 1,
        'confirm_external_writes': True, 'abort_event': stop}
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    original_save = workspace.save_workspace

    def stop_after_preparation(*args, **kwargs):
        prepared = original_save(*args, **kwargs)
        stop.set()
        return prepared

    monkeypatch.setattr(workspace, 'save_workspace', stop_after_preparation)
    agent = Mock()
    monkeypatch.setattr(action, '_run_agent', agent)
    action.run_research_action(task)
    agent.assert_not_called()
    owner.abort.assert_called_once_with('stop-compaction')
    saved = load_workspace(DIRECTION, 'en', user_id=1)
    assert len(saved['runs']) + saved['history']['count'] == 32


@pytest.mark.parametrize('after_commit', [False, True, 'different'])
def test_ambiguous_workspace_ack_requires_exact_committed_document(storage, monkeypatch, after_commit):
    import lib.research.workspace as workspace
    original = save(draft_with_runs(23))
    client = storage[0].client
    calls = []

    class LostAckClient:
        def query(self, *args, **kwargs):
            return client.query(*args, **kwargs)
        def command(self, operation, payload, command_id):
            if operation == 'research.workspace.put':
                calls.append(command_id)
                if after_commit:
                    committed = deepcopy(payload)
                    if after_commit == 'different':
                        committed['workspace']['stage'] = 'evidence'
                    client.command(operation, committed, command_id)
                raise StorageError('database_timeout', 'Injected ambiguous workspace ACK')
            return client.command(operation, payload, command_id)

    monkeypatch.setattr(workspace, '_storage', lambda **_: LostAckClient())
    draft = deepcopy(original)
    draft['runs'].append(observation(draft, 23))
    if after_commit is True:
        acknowledged = save(draft)
        assert acknowledged['revision'] == 2
        assert acknowledged['history']['count'] == 8
    else:
        with pytest.raises(StorageError) as caught:
            save(draft)
        assert caught.value.code == 'database_timeout'
        if after_commit == 'different':
            assert load_workspace(DIRECTION, 'en', user_id=1)['stage'] == 'evidence'
        else:
            assert load_workspace(DIRECTION, 'en', user_id=1) == original
            assert load_history_page(DIRECTION, 'en', user_id=1)['runs'] == []
    assert len(calls) == 1


@pytest.mark.parametrize('condition', ['unreceipted', 'stale'])
def test_archived_non_authoritative_refutation_does_not_invent_conflict(storage, condition):
    draft = draft_with_runs(24, negative=0)
    if condition == 'unreceipted':
        draft['runs'][0]['tool_receipts'] = []
    else:
        draft['runs'][0]['protocol_digest'] = '0' * 64
    saved = save(draft)
    assert saved['history']['outcomes']['refuted'] == 0
    assert saved['decision']['status'] == 'retain'


def test_consistent_archived_refutations_allow_scoped_negative_claim(storage):
    draft = draft_with_runs(24)
    draft['runs'] = [observation(draft, i, 'refuted') for i in range(24)]
    claim = {'id': 'negative', 'text': 'The tested intervention worsens accuracy',
        'status': 'supported', 'outcome_scope': 'refuted', 'evidence_refs': ['run-23'],
        **dependency_digests(draft)}
    normalized = normalize_program_fields(draft)
    claim['evidence_digest'] = claim_evidence_digest(claim, normalized['runs'])
    draft['claims'] = [claim]
    saved = save(draft)
    assert saved['decision']['status'] == 'refute'
    assert saved['claims'][0]['validity'] == 'current'
    assert saved['manuscript_audit']['passed']


def test_history_direction_alias_uses_the_acknowledged_direction_binding(storage):
    from urllib.parse import quote
    saved = save(draft_with_runs(24))
    aliased = load_history_page(quote(DIRECTION, safe=''), 'en', user_id=1)
    assert aliased['head'] == saved['history']['head']
    assert aliased['total'] == 8


def test_corrupt_stored_descriptor_is_integrity_failure(storage):
    draft = draft_with_runs(1)
    draft['history'] = {'head': 'forged'}
    storage[0].client.command('research.workspace.put', {'user_id': 1,
        'paper_hash': research_direction_hash(DIRECTION), 'lang': 'en',
        'expected_revision': 0, 'workspace': draft, 'updated_at': 0}, 'fixture-malformed-descriptor')
    with pytest.raises(StorageError) as caught:
        load_workspace(DIRECTION, 'en', user_id=1)
    assert caught.value.code == 'database_integrity'


def test_full_pending_working_basis_can_settle_existing_experiment(storage, monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    from tests.research.test_research_action import _epoch
    draft = execution_draft(32)
    for row in draft['runs']:
        row['execution_status'] = 'running'
    saved = save(draft)
    task = {'task_id': 'settle-pending', 'action': 'experiment', 'user_id': 1,
        'direction': DIRECTION, 'lang': 'en', 'expected_revision': saved['revision'],
        'confirm_external_writes': True, 'abort_event': threading.Event()}
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    monkeypatch.setattr(action, 'build_action_tool_epoch', lambda **_: (_epoch([]), [], []))
    monkeypatch.setattr(action, '_run_agent', lambda *_: ({'run': {'id': 'run-0', 'status': 'passed'}}, [{
        'tool': 'run_command', 'status': 'done', 'result_digest': 'settlement',
        'artifact_ref': 'metrics/0.json', 'result_excerpt': json.dumps(observation_payload(saved['protocol'],'metrics/0.json','refuted'))}]))
    action.run_research_action(task)
    settled = owner.finish.call_args.kwargs['result']['workspace']
    assert settled['revision'] == saved['revision'] + 1
    assert len(settled['runs']) == 31
    assert settled['history']['count'] == 1
    # The pending row already carried a supported measurement. Settlement
    # retains both that observation and its later refutation, not just the last.
    assert settled['history']['outcomes']['inconclusive'] == 1
    page = load_history_page(DIRECTION, 'en', user_id=1)
    assert page['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert len(page['runs'][0]['tool_receipts']) == 2
