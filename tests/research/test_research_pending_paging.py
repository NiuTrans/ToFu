"""Production pending paging preserves all original sources past workspace limits."""
from copy import deepcopy
import pytest

from lib.research.action import apply_action_result
from lib.research.workspace import save_workspace, load_workspace, open_run_receipts, read_run_execution_state
from lib.research.receipt_pages import ReceiptPages
from lib.research.persistence import research_direction_hash
from lib.storage.errors import StorageError
from tests.research.test_research_pending_lineage import initial, receipt
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit
DIRECTION = 'Long jobs'
BINDINGS = [{'tool': 'poll', 'capability': 'compute.status', 'provider': 'cluster'}]


def reader(client):
    def open_history(run):
        return ReceiptPages(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en',
            run_id=run['id'], hypothesis_digest=run['hypothesis_digest'],
            protocol_digest=run['protocol_digest']).open_receipts(run['receipt_history'])
    return open_history


def apply(workspace, task, rows, client):
    return apply_action_result(action='experiment', workspace=workspace, task_id=task,
        receipts=rows, payload={'run': {'id': 'run'}}, bindings=BINDINGS,
        pending_receipt_reader=reader(client))


def save(workspace):
    return save_workspace(DIRECTION, 'en', workspace, expected_revision=workspace['revision'], user_id=1,
                          paginate_pending_run_id='run')


def test_more_than_64_polls_continue_and_survive_selected_storage_restart(storage):
    supervisor, _ = storage
    saved = save(initial())
    for n in range(80):
        saved = save(apply(saved, f'poll-{n}', [receipt('job-B', 'completed', 'poll', f'call-{n}')], supervisor.client))
        assert saved['runs'][0]['execution_status'] == 'running'
        assert saved['runs'][0]['tool_receipts'] == []
        assert saved['runs'][0]['receipt_history']['count'] == n + 3
        if n == 40:
            supervisor.stop(); supervisor.start(); saved = load_workspace(DIRECTION, user_id=1)
    with open_run_receipts(DIRECTION, user_id=1, run_id='run') as stream:
        rows = list(stream['receipts'])
    assert len(rows) == 82 and rows[0]['execution_observation']['job_id'] == 'job-A'
    assert [row['source_task_id'] for row in rows[2:]] == [f'poll-{n}' for n in range(80)]
    assert read_run_execution_state(DIRECTION, user_id=1, run_id='run')['execution_status'] == 'running'


def test_archived_same_source_replay_does_not_append_or_overwrite_facts(storage):
    supervisor, _ = storage
    saved = save(initial()); row = receipt('job-B', 'completed', 'poll', 'call')
    saved = save(apply(saved, 'same-task', [row], supervisor.client)); head = saved['runs'][0]['receipt_history']
    replay = deepcopy(row); replay['observed_at'] += 100
    reapplied = save(apply(saved, 'same-task', [replay], supervisor.client))
    assert reapplied['runs'][0]['receipt_history'] == head
    changed = dict(deepcopy(row), result_digest='changed')
    with pytest.raises(ValueError, match='source identity conflicts'):
        apply(reapplied, 'same-task', [row, changed], supervisor.client)
    assert load_workspace(DIRECTION, user_id=1) == reapplied


def test_failed_paging_commit_cannot_publish_prepared_sources(storage, monkeypatch):
    supervisor, _ = storage
    saved = save(initial())
    proposed = apply(saved, 'next', [receipt('job-B', 'completed', 'poll', 'call')], supervisor.client)
    command = supervisor.client.command
    def refuse(operation, *args, **kwargs):
        if operation == 'research.workspace.put': raise StorageError('database_conflict', 'injected refusal')
        return command(operation, *args, **kwargs)
    monkeypatch.setattr(supervisor.client, 'command', refuse)
    with pytest.raises(StorageError, match='injected refusal'): save(proposed)
    assert load_workspace(DIRECTION, user_id=1) == saved
    monkeypatch.setattr(supervisor.client, 'command', command)
    committed = save(proposed)
    assert committed['runs'][0]['receipt_history']['count'] == 3


def test_paged_completion_still_requires_complete_settlement_publication(storage):
    supervisor, _ = storage; saved = save(initial())
    candidate = apply(saved, 'complete-A', [receipt('job-A', 'completed', 'poll', 'call')], supervisor.client)
    with pytest.raises(ValueError, match='settlement requires complete evidence reduction'):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=saved['revision'], user_id=1)
    assert load_workspace(DIRECTION, user_id=1) == saved


@pytest.mark.parametrize('already_paged', [False, True])
def test_production_worker_automatically_pages_and_continues_pending_run(storage, monkeypatch, already_paged):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    from lib.research.program import dependency_digests
    supervisor, _ = storage
    draft = initial()
    draft['protocol'].update(decision_rule={'direction': 'higher', 'minimum_effect': 0, 'uncertainty_multiplier': 2},
        random_seeds=[1, 2, 3], dataset='held-out', primary_metric='accuracy', baseline='reference')
    draft['runs'][0].update(dependency_digests(draft))
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1,
                           paginate_pending_run_id='run' if already_paged else None)
    owner = SimpleNamespace(mark_running=Mock(), finish=Mock(), abort=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    from tests.research.test_research_action import _epoch
    monkeypatch.setattr(action, 'build_action_tool_epoch', lambda **kwargs:
        (_epoch([]), BINDINGS, []))
    agent = Mock(side_effect=lambda task, *args:
        ({'run': {'id': 'run'}}, [receipt('job-B', 'completed', 'poll', task['task_id'])]))
    monkeypatch.setattr(action, '_run_agent', agent)
    actual_apply = action.apply_action_result
    def indexed_apply(**kwargs):
        assert callable(kwargs.get('pending_progress_reader'))
        def no_full_scan(run):
            raise AssertionError('production indexed polling must not scan old receipt pages')
        kwargs['pending_receipt_reader'] = no_full_scan
        return actual_apply(**kwargs)
    monkeypatch.setattr(action, 'apply_action_result', indexed_apply)
    for n in range(2):
        task = dict(task_id=f'worker-{n}', direction=DIRECTION, lang='en', user_id=1,
                    action='experiment', expected_revision=saved['revision'],
                    confirm_external_writes=True, abort_event=threading.Event())
        action.run_research_action(task)
        result = owner.finish.call_args.kwargs['result']
        saved = result['workspace']
        assert saved['runs'][0]['receipt_history']['count'] == n + 3
        assert saved['runs'][0]['tool_receipts'] == []
        assert saved['runs'][0]['execution_status'] == 'running'
        assert task['_committedWorkspaceRevision'] == saved['revision']
    assert agent.call_count == 2
    terminal = 'failed' if already_paged else 'completed'
    agent.side_effect = lambda task, *args: ({'run': {'id': 'run'}},
        [receipt('job-A', terminal, 'poll', task['task_id'])])
    task = dict(task, task_id='terminal-worker', expected_revision=saved['revision'])
    action.run_research_action(task)
    result = owner.finish.call_args.kwargs['result']
    assert result['workspace']['runs'][0]['execution_status'] == terminal
    if already_paged:
        assert result['workspace']['runs'][0]['scientific_outcome'] == 'inconclusive'
        assert not result['workspace']['runs'][0]['artifact_refs']


@pytest.mark.parametrize('terminal', ['running', 'completed', 'failed'])
def test_pending_dedup_and_job_state_share_one_verified_history_pass(storage, terminal):
    from contextlib import contextmanager
    supervisor, _ = storage; saved = save(initial())
    delegate = reader(supervisor.client); observations = {'opens':0, 'rows':0}
    @contextmanager
    def once(run):
        observations['opens'] += 1
        assert observations['opens'] == 1, 'one logical action must not scan the same history twice'
        with delegate(run) as rows:
            def counted():
                for row in rows:
                    observations['rows'] += 1
                    yield row
            yield counted()
    updated = apply_action_result(action='experiment', workspace=saved, task_id='next',
        receipts=[receipt('job-A', terminal, 'poll', 'call')], payload={'run':{'id':'run'}},
        bindings=BINDINGS, pending_receipt_reader=once)
    assert updated['runs'][0]['execution_status'] == terminal
    assert observations == {'opens':1,'rows':2}
