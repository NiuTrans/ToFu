"""Recovered swarm truth: checkpoint outcomes, scoped partial logs, safe restart."""

import json
from unittest.mock import Mock

import pytest

from lib.swarm.integration import _logs, _tools
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.tools.result_envelope import tool_result_error

pytestmark = pytest.mark.unit


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    task = {'id': 'read-turn', 'convId': 'conversation', '_userId': 7}
    row = {
        'swarm_key': 'conversation', 'conv_id': 'conversation',
        'task_id': 'spawn-turn', 'status': 'terminated',
        'config': {'user_id': 7}, 'agents': [],
    }
    loader = Mock(return_value=row)
    monkeypatch.setattr('lib.swarm.persistence.load_session', loader)
    monkeypatch.setattr(_tools, '_get_session', lambda key: None)
    monkeypatch.setattr(_logs, 'SWARM_OUTPUT_DIR', str(tmp_path))
    return task, row, loader, tmp_path


def _checkpoint(row, status, *, result=None, messages=None):
    checkpoint = {'agent_id': 'child', 'role': 'coder', 'status': status,
                  'result': result or {}, 'messages': messages or []}
    row['agents'] = [checkpoint]
    return checkpoint


def _log(root, task_id='spawn-turn', content='streamed reasoning, not a final answer'):
    path = root / task_id / 'child.log'
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def _get(task, agent_id='child'):
    return json.loads(_tools._handle_get_agent_result(
        {'agent_id': agent_id}, task_id=task['id'], task=task))


def _wait(task, ids=None):
    return json.loads(_tools._handle_await_agents(
        {'ids': ids or ['child'], 'mode': 'all'}, task_id=task['id'], task=task))


def test_verified_success_returns_canonical_answer_not_stream(recovery):
    task, row, loader, root = recovery
    _checkpoint(row, 'completed', result={'status': 'completed', 'final_answer': 'verified'})
    _log(root)
    result = _get(task)
    assert result['status'] == 'ok'
    assert result['agent_status'] == 'completed'
    assert result['final_answer'] == 'verified'
    assert 'partial_output' not in result
    assert result['source'] == 'persisted'
    batch = _wait(task)
    assert batch['completed'][0]['status'] == 'completed'
    assert batch['completed'][0]['result'] == 'verified'
    assert batch['unknown'] == []
    assert batch['partial'] == []
    loader.assert_called_with('conversation')


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'aborted', 'stalled', 'interrupted'])
def test_terminal_non_success_keeps_partial_and_actual_status(recovery, status):
    task, row, loader, root = recovery
    _checkpoint(row, status, result={'final_answer': 'useful unfinished work',
                                    'error_message': 'execution stopped'})
    _log(root)
    result = _get(task)
    assert result['agent_status'] == status
    assert result['final_answer'] == ''
    assert result['partial_output'] == 'useful unfinished work'
    assert result['error'] == 'execution stopped'
    batch = _wait(task)
    assert batch['completed'][0]['status'] == status
    assert batch['completed'][0]['result'] == ''
    assert batch['completed'][0]['partial_output'] == 'useful unfinished work'
    assert batch['still_running'] == []


@pytest.mark.parametrize('status', [None, '', 'unknown', 'pending', 'running', 'retrying'])
def test_unknown_or_lost_worker_never_promotes_text_to_success(recovery, status):
    task, row, loader, root = recovery
    row['status'] = 'running'  # restart / pre-rehydrate window
    _checkpoint(row, status, result={'status': 'completed', 'final_answer': 'stale text'})
    result = _get(task)
    assert result['agent_status'] == 'unknown'
    assert result['final_answer'] == ''
    assert result['partial_output'] == 'stale text'
    batch = _wait(task)
    assert batch['completed'] == []
    assert batch['unknown'] == ['child']
    assert batch['still_running'] == []
    assert batch['partial'][0]['result'] == ''
    assert not batch['timed_out']


@pytest.mark.parametrize('result,expected', [
    ({'status': 'failed', 'final_answer': 'bad'}, 'failed'),
    ({'status': 'cancelled', 'final_answer': 'bad'}, 'cancelled'),
    ({'status': 'running', 'final_answer': 'bad'}, 'unknown'),
    ({'status': 'completed', 'error_message': 'failure', 'final_answer': 'bad'}, 'failed'),
])
def test_conflicting_completed_checkpoint_cannot_claim_success(recovery, result, expected):
    task, row, loader, root = recovery
    _checkpoint(row, 'completed', result=result)
    payload = _get(task)
    assert payload['agent_status'] == expected
    assert payload['final_answer'] == ''
    assert payload['partial_output'] == 'bad'


def test_success_without_canonical_answer_does_not_substitute_log_or_messages(recovery):
    task, row, loader, root = recovery
    _checkpoint(row, 'completed', messages=[{'role': 'assistant', 'content': 'draft'}])
    result = _get(task)
    assert result['agent_status'] == 'completed'
    assert result['final_answer'] == ''
    assert result['partial_output'] == 'draft'
    _log(root, content='raw stream')
    result = _get(task)
    assert result['final_answer'] == ''
    assert result['partial_output'] == 'raw stream'


def test_log_only_same_task_is_partial_unknown_and_no_global_lookup(recovery):
    task, row, loader, root = recovery
    loader.return_value = None
    _log(root, content='foreign evidence')
    assert _get(task)['found'] is False
    _log(root, task['id'], content='local evidence')
    result = _get(task)
    assert result['source'] == 'disk'
    assert result['agent_status'] == 'unknown'
    assert result['final_answer'] == ''
    assert result['partial_output'] == 'local evidence'
    batch = _wait(task, ['child', 'missing'])
    assert batch['completed'] == []
    assert batch['unknown'] == ['child', 'missing']
    assert batch['partial'][0]['partial_output'] == 'local evidence'


@pytest.mark.parametrize('mismatch', ['owner', 'ownerless', 'conversation', 'tenant'])
def test_foreign_checkpoint_denies_evidence_without_fallback(recovery, mismatch):
    task, row, loader, root = recovery
    _checkpoint(row, 'completed', result={'final_answer': 'private'})
    _log(root, content='private log')
    _log(root, task['id'], content='also private')
    if mismatch == 'owner':
        row['config']['user_id'] = 8
    elif mismatch == 'ownerless':
        row['config'] = {}
    elif mismatch == 'conversation':
        row['conv_id'] = 'unrelated'
    else:
        row['config']['parent_cfg'] = {'_tenant_id': 'foreign'}
    payload = _get(task)
    assert not payload['found']
    assert tool_result_error(payload) is not None
    assert 'private' not in json.dumps(payload)


def test_authorized_cross_turn_read_ignores_newer_foreign_log(recovery):
    task, row, loader, root = recovery
    _checkpoint(row, 'interrupted')
    _log(root, content='authorized partial')
    _log(root, 'foreign-turn', content='private newer answer')
    result = _get(task)
    assert result['partial_output'] == 'authorized partial'
    assert result['agent_status'] == 'interrupted'
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('fault', ['decode', 'read', 'storage'])
def test_recovery_faults_are_safe_and_do_not_invent_success(recovery, monkeypatch, fault):
    task, row, loader, root = recovery
    _checkpoint(row, 'interrupted', messages=[{'role': 'assistant', 'content': 'saved draft'}])
    path = _log(root)
    if fault == 'decode':
        path.write_bytes(b'\xff\xfe')
    elif fault == 'read':
        monkeypatch.setattr('builtins.open', Mock(side_effect=PermissionError('private path')))
    else:
        loader.side_effect = RuntimeError('private storage details')
    result = _get(task)
    if fault == 'storage':
        assert result['status'] == 'error'
        assert tool_result_error(result) is not None
    else:
        assert result['agent_status'] == 'interrupted'
        assert result['final_answer'] == ''
        assert result['partial_output'] == 'saved draft'
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('agent_id', ['../child', '/child', 'x/child', 'x\\child', '\x00', '*'])
def test_recovery_rejects_path_traversal_and_glob_handles(recovery, agent_id):
    task, row, loader, root = recovery
    loader.return_value = None
    _log(root, task['id'], content='private')
    assert not _get(task, agent_id)['found']


def test_symlinked_evidence_is_not_followed(recovery):
    task, row, loader, root = recovery
    loader.return_value = None
    foreign = _log(root, 'foreign', content='private')
    local = root / task['id']
    local.mkdir()
    (local / 'child.log').symlink_to(foreign)
    assert not _get(task)['found']
    (local / 'child.log').unlink()
    local.rmdir()
    local.symlink_to(root / 'foreign', target_is_directory=True)
    assert not _get(task)['found']


def test_batch_mixes_truthful_success_failure_and_unknown(recovery):
    task, row, loader, root = recovery
    row['agents'] = [
        {'agent_id': 'ok', 'status': 'completed', 'result': {'final_answer': 'done'}},
        {'agent_id': 'bad', 'status': 'failed', 'result': {'final_answer': 'partial'}},
        {'agent_id': 'lost', 'status': 'running', 'result': {'final_answer': 'draft'}},
    ]
    payload = json.loads(_tools._handle_get_agent_result(
        {'agent_ids': ['ok', 'bad', 'lost', 'missing']}, task_id=task['id'], task=task))
    assert [p.get('agent_status') for p in payload['results']] == [
        'completed', 'failed', 'unknown', None]
    assert [p.get('final_answer') for p in payload['results']] == ['done', '', '', None]
    batch = _wait(task, ['ok', 'bad', 'lost', 'missing'])
    assert [(p['agent_id'], p['status']) for p in batch['completed']] == [
        ('ok', 'completed'), ('bad', 'failed')]
    assert batch['unknown'] == ['lost', 'missing']


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled', 'interrupted', 'unknown', None])
def test_restart_restores_actual_outcome_without_prose_as_final(monkeypatch, status):
    spec = SubTaskSpec(id='child', objective='work')
    master = MasterOrchestrator(task_id='restart', conv_id='', specs=[spec], user_id=7)
    scheduler = Mock()
    monkeypatch.setattr(master, '_build_scheduler', Mock(return_value=scheduler))
    monkeypatch.setattr(master, '_start_driver', Mock())
    reenqueue = Mock()
    monkeypatch.setattr(master, '_reenqueue_swarm_update', reenqueue)
    master.rehydrate_in_background([{
        'agent_id': 'child', 'status': status,
        'messages': [{'role': 'assistant', 'content': 'draft'}], 'result': {},
    }])
    expected = status or 'unknown'
    payload = master.get_agent_result('child')
    assert payload['status'] == expected
    assert payload['final_answer'] == ''
    assert payload['partial_output'] == 'draft'
    scheduler.add_specs.assert_not_called()
    assert reenqueue.call_count == int(status in ('completed', 'failed'))
    batch = master.await_agents(ids=['child'], timeout_seconds=0)
    if expected == 'unknown':
        assert batch['completed'] == []
        assert batch['unknown'] == ['child']
        assert batch['partial'][0]['partial_output'] == 'draft'
    else:
        assert batch['completed'][0]['status'] == expected
        assert batch['completed'][0]['result'] == ''
    assert batch['swarm_status'][0]['status'] == expected


def test_restart_keeps_explicit_success_and_resumes_only_known_nonterminal(monkeypatch):
    specs = [SubTaskSpec(id=sid, objective='work') for sid in ('ok', 'running')]
    master = MasterOrchestrator(task_id='restart', conv_id='', specs=specs, user_id=7)
    scheduler = Mock()
    monkeypatch.setattr(master, '_build_scheduler', Mock(return_value=scheduler))
    monkeypatch.setattr(master, '_start_driver', Mock())
    monkeypatch.setattr(master, '_reenqueue_swarm_update', Mock())
    master.rehydrate_in_background([
        {'agent_id': 'ok', 'status': 'completed', 'result': {'final_answer': 'verified'}},
        {'agent_id': 'running', 'status': 'running', 'messages': [{'role': 'assistant', 'content': 'draft'}]},
    ])
    assert master.get_agent_result('ok')['final_answer'] == 'verified'
    scheduler.add_specs.assert_called_once_with([specs[1]])
    assert master._resume_messages['running'][0]['content'] == 'draft'
    assert master._result_from_dict({}).status == 'unknown'


def test_running_and_timeout_guidance_names_explicit_poll_handles(monkeypatch):
    spec = SubTaskSpec(id='child', objective='work')
    master = MasterOrchestrator(task_id='live', conv_id='', specs=[spec], user_id=7)
    master._scheduler = Mock(_running={'child': Mock()}, _pending=[])
    running = master.get_agent_result('child')
    timeout = master._build_await_response(
        completed_ids=[], still_running=['child'], unknown=[], mode='all',
        timed_out=True, note='', awaited_ids=['child'])
    for text in (running['message'], timeout['note']):
        assert 'await_agents' not in text
        assert 'poll' in text and 'wait_s' in text and '60' in text
        assert 'snapshot' in text
    assert "id='child'" in running['message']
    assert 'agent handles in ids' in timeout['note']


def test_live_failed_result_keeps_lifecycle_status_in_tool_envelope(recovery, monkeypatch):
    from lib.swarm.protocol import SubAgentResult
    task, row, loader, root = recovery
    spec = SubTaskSpec(id='child', objective='work')
    master = MasterOrchestrator(task_id=task['id'], conv_id=task['convId'], specs=[spec], user_id=7)
    master._results_by_id['child'] = (spec, SubAgentResult(status='failed', final_answer='partial'))
    monkeypatch.setattr(_tools, '_get_session', lambda key: master)
    monkeypatch.setattr(_tools, 'add_session_alias', Mock())
    monkeypatch.setattr('lib.swarm.delivery.stage_tool_results', Mock())
    result = _get(task)
    assert result['status'] == 'ok'
    assert result['agent_status'] == 'failed'
    assert result['final_answer'] == ''
    assert result['partial_output'] == 'partial'


@pytest.mark.parametrize('mismatch', ['owner', 'conversation', 'tenant'])
def test_foreign_live_session_denies_without_reading_disk(recovery, monkeypatch, mismatch):
    from lib.swarm.protocol import SubAgentResult
    task, row, loader, root = recovery
    _log(root, task['id'], content='private fallback')
    spec = SubTaskSpec(id='child', objective='work')
    master = MasterOrchestrator(
        task_id=task['id'], conv_id=('foreign' if mismatch == 'conversation' else task['convId']),
        specs=[spec], user_id=(8 if mismatch == 'owner' else 7),
        parent_config={'_tenant_id': 'foreign'} if mismatch == 'tenant' else {})
    master._results_by_id['child'] = (spec, SubAgentResult(final_answer='private result'))
    monkeypatch.setattr(_tools, '_get_session', lambda key: master)
    for payload in (_get(task), _wait(task)):
        assert payload['status'] == 'error'
        assert tool_result_error(payload).code == 'swarm_scope_denied'
        assert 'private' not in json.dumps(payload)
    loader.assert_not_called()


def test_ownerless_task_cannot_read_current_task_log(recovery):
    task, row, loader, root = recovery
    task.pop('_userId')
    _log(root, task['id'], content='private')
    assert not _get(task)['found']
    loader.assert_not_called()


def test_no_id_recovery_never_discovers_or_waits_for_all(recovery):
    task, row, loader, root = recovery
    _checkpoint(row, 'completed', result={'final_answer': 'verified'})
    assert _tools._await_from_disk(task['id'], {}, task=task) is None
    loader.assert_not_called()


def test_exact_task_recovery_without_conversation_does_not_borrow_another_turn(recovery):
    task, row, loader, root = recovery
    task.pop('convId')
    row['task_id'] = task['id']
    _checkpoint(row, 'completed', result={'final_answer': 'verified'})
    assert _get(task)['final_answer'] == 'verified'
    loader.assert_called_with(task['id'])
    row['task_id'] = 'foreign'
    assert not _get(task)['found']


def test_repeated_restart_keeps_partial_text_separate(monkeypatch):
    checkpoint = {'agent_id': 'child', 'status': 'failed', 'result': {},
                  'messages': [{'role': 'assistant', 'content': 'useful partial'}]}
    for _ in range(2):
        master = MasterOrchestrator(
            task_id='restart', conv_id='', user_id=7,
            specs=[SubTaskSpec(id='child', objective='work')])
        monkeypatch.setattr(master, '_build_scheduler', Mock(return_value=Mock()))
        monkeypatch.setattr(master, '_start_driver', Mock())
        master.rehydrate_in_background([checkpoint])
        payload = master.get_agent_result('child')
        assert payload['status'] == 'failed'
        assert payload['final_answer'] == ''
        assert payload['partial_output'] == 'useful partial'
        assert checkpoint['result'] == {}


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_canonical_answer_does_not_read_or_repeat_stream(recovery, monkeypatch, status):
    task, row, loader, root = recovery
    _checkpoint(row, status, result={'status': status, 'final_answer': 'stored answer'})
    read_log = Mock(side_effect=AssertionError('canonical answer needs no raw stream'))
    monkeypatch.setattr('lib.swarm.integration._recovery._read_agent_log', read_log)
    result = _get(task)
    assert result['agent_status'] == status
    assert result['final_answer'] == ('stored answer' if status == 'completed' else '')
    if status == 'failed':
        assert result['partial_output'] == 'stored answer'
    else:
        assert 'partial_output' not in result
    read_log.assert_not_called()


def test_empty_log_does_not_hide_checkpoint_partial(recovery):
    task, row, loader, root = recovery
    _checkpoint(row, 'interrupted', messages=[{'role': 'assistant', 'content': 'saved draft'}])
    _log(root, content='\n')
    result = _get(task)
    assert result['agent_status'] == 'interrupted'
    assert result['partial_output'] == 'saved draft'
    assert result['final_answer'] == ''
