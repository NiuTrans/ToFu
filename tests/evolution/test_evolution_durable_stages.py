"""Durable-at-birth admission, retained executing evidence and one terminal owner."""
from copy import deepcopy
import json

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, finish

pytestmark = pytest.mark.unit


def test_stages_require_real_owned_birth_and_keep_inline_context(runtime):
    from lib.storage import get_storage_client
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve long exploration evidence.'}, user_id=OWNER)
    for task in [tasks[0]]:
        born = get_storage_client().query('task_results.replay_get', {'key': task['id'], 'user_id': OWNER})
        assert born['status'] == 'pending' and born['version'] > 0
        assert task['_inline_messages'] is True and not task.get('_transientRuntime')
        assert get_storage_client().query('task_results.replay_get', {'key': task['id'], 'user_id': OWNER + 1}) is None
    reviewed = finish(runtime, tasks[0], 'Durable checkpoint evidence retained.')
    assert reviewed['status'] == 'reviewing'
    born = get_storage_client().query('task_results.replay_get', {'key': tasks[1]['id'], 'user_id': OWNER})
    assert born['status'] == 'pending' and not tasks[1].get('_transientRuntime')


@pytest.mark.parametrize('fault', ['missing', 'foreign', 'wrong_task', 'terminal', 'zero_version', 'wrong_conversation'])
def test_unconfirmed_birth_never_dispatches_or_opens_an_episode(runtime, monkeypatch, fault):
    from lib.storage import get_storage_client
    from lib.evolution import list_episodes
    import lib.storage
    runs, _, tasks = runtime
    actual = get_storage_client
    class Client:
        def query(self, operation, payload, **options):
            value = actual().query(operation, payload, **options)
            if operation != 'task_results.replay_get' or 'deadline' not in options:
                return value
            if fault == 'missing': return None
            result = deepcopy(value)
            if not result: return value
            if fault == 'foreign': result['user_id'] = OWNER + 1
            elif fault == 'wrong_task': result['task_id'] = 'another-task'
            elif fault == 'terminal': result['status'] = 'done'
            elif fault == 'zero_version': result['version'] = 0
            elif fault == 'wrong_conversation': result['conv_id'] = 'another-conversation'
            return result
        def command(self, *args, **kwargs): return actual(write=True).command(*args, **kwargs)
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **_: Client())
    with pytest.raises(runs.EvolutionStateError, match='birth checkpoint'):
        runs.submit_run({'objective': 'Require acknowledged execution evidence.'}, user_id=OWNER)
    assert not tasks and not list_episodes(user_id=OWNER)


def test_running_tool_evidence_and_snapshot_use_existing_persistent_owners(runtime):
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager._sync import checkpoint_task_partial
    from lib.tasks_pkg.manager._events import append_event
    from lib.tasks_pkg.event_log import flush_pending
    from lib.tasks_pkg import request_inspector as ri
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Retain progress before terminal settlement.'}, user_id=OWNER)
    task = tasks[0]
    task['toolRounds'] = [{'status': 'done', 'llmRound': 1, 'tool_calls': [], 'toolContent': 'Exact controlled observation: 17 receipts 🧪'}]
    task['content'] = 'Partial controlled exploration evidence.'
    checkpoint_task_partial(task, force=True)
    record = get_storage_client().query('record.get', {'namespace': 'task_results', 'key': task['id']})
    value = record['value']
    assert value['status'] == 'running' and value['content'] == task['content']
    assert '17 receipts' in value['tool_rounds']
    append_event(task, {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': 1, 'model': 'controlled', 'label': 'Controlled checkpoint', 'messages': [{'role': 'user', 'content': 'Exact request before a crash 🧪'}], 'tools': []})
    flush_pending(task['id'])
    assert ri.get_request_payload(task['id'], 1)['messages'][0]['content'] == 'Exact request before a crash 🧪'


def test_required_receiver_is_the_only_terminal_progression_owner(runtime, monkeypatch):
    from lib.evolution import automation
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Deliver one durable terminal receipt.'}, user_id=OWNER)
    task = tasks[0]
    task['status'] = 'done'
    deliveries = []
    monkeypatch.setattr(task['_terminalDelivery'], 'receiver', lambda completed: deliveries.append(completed['id']))
    assert automation.enqueue_task_terminal(task) is True
    assert automation.enqueue_task_terminal(task) is True
    assert deliveries == [task['id']]


def test_unbound_terminal_callback_cannot_resume_a_preparing_request(runtime, monkeypatch):
    from lib.tasks_pkg import manager
    runs, _, tasks = runtime
    monkeypatch.setattr(manager, 'create_task', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('admission unavailable')))
    with pytest.raises(RuntimeError):
        runs.submit_run({'objective': 'Never progress from an unbound task.'}, user_id=OWNER, idempotency_key='unbound-birth')
    run = next(value for (namespace, _), value in runtime[1].rows.items() if namespace == 'evolution.runs.v1')
    with pytest.raises(runs.EvolutionStateError, match='run binding'):
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task={'id': 'unbound', '_userId': OWNER, 'status': 'done'})
    assert not tasks


def test_storage_free_embedded_tasks_outside_evolution_remain_storage_free(runtime):
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.manager._terminal import reject_unstarted_chat_task
    from lib.tasks_pkg.manager._sync import checkpoint_task_partial
    from lib.tasks_pkg.manager._events import append_event
    from lib.storage import get_storage_client
    task = create_task('', [{'role': 'user', 'content': 'Controlled embedded context'}], {}, user_id=OWNER, supersede=False, transient=True)
    try:
        task['content'] = 'Controlled transient output'
        checkpoint_task_partial(task, force=True)
        append_event(task, {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': 1, 'model': 'controlled', 'label': 'Controlled embedded snapshot', 'messages': [{'role': 'user', 'content': 'Controlled embedded context'}], 'tools': []})
        assert get_storage_client().query('task_results.replay_get', {'key': task['id'], 'user_id': OWNER}) is None
        assert get_storage_client().query('event.list', {'task_id': task['id'], 'after_sequence': -1, 'limit': 10}) == []
    finally:
        reject_unstarted_chat_task(task, 'controlled test cleanup', cause='test_cleanup')
