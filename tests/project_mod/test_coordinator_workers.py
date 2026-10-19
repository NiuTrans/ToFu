"""One human request creates independent workers under conserved source policy."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_project_dispatch_repository import context as context
from lib.conversations import coordinator_workers
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.service import ProjectDispatchService

# Keep the imported fixture's registered alias visible to static tooling.
__all__ = ('base_runtime',)

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(base_runtime, monkeypatch):
    r = base_runtime
    from lib.project_dispatch import repository
    from lib.conversations import coordinator_workers as workers
    r.task.update(_turnId='coordinator-turn', _attemptId='coordinator-attempt', _attended=True)
    r.task['config'].update(codeExecEnabled=True, autoApply=True)
    # Execution identity has no reusable API scopes. The accepted canonical
    # request is the authority; the adapter must not forge a human API principal.
    r.task['_principalContext']['scopes'] = []
    r.turns['coordinator-turn'] = {'conversationId': 'coordinator', 'turnId': 'coordinator-turn',
        'actor': 'assistant', 'parentTurnId': 'human-turn'}
    r.turns['human-turn'] = r.c.turn
    r.attempts['coordinator-attempt'] = {'conversationId': 'coordinator', 'turnId': 'coordinator-turn',
        'attemptId': 'coordinator-attempt', 'taskId': r.task['id'], 'status': 'running', 'createdAt': 1_000_000}
    r.c.repo.turn_get = lambda conv, turn, **kw: deepcopy(r.turns.get(turn))
    r.c.repo.attempt_get = lambda attempt, **kw: deepcopy(r.attempts.get(attempt))
    monkeypatch.setattr(repository, 'ProjectDispatchRepository', lambda owner, path: r.c.repo)
    monkeypatch.setattr(workers, 'ProjectDispatchRepository', lambda owner, path: r.c.repo)
    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']; trusted.checked()
            assert kwargs['principal'].kind == 'system' and request['inputActor'] == 'virtual_user'
            assert r.c.turn['projection']['content'] in request['inputTurn']['content']
            assert 'Assigned objective:' in request['inputTurn']['content']
            turn, attempt, task = (prefix + conv for prefix in ('turn-', 'attempt-', 'task-'))
            if attempt in r.attempts:
                return SimpleNamespace(value={})
            trusted.bind_execution(turn, attempt)
            r.turns[turn] = {'conversationId': conv, 'turnId': turn, 'actor': 'assistant',
                            'projectionRevision': 1, 'projection': {'content': ''}}
            r.attempts[attempt] = {'conversationId': conv, 'turnId': turn, 'attemptId': attempt,
                                  'taskId': task, 'status': 'running'}
            r.calls.append(conv)
            return SimpleNamespace(value={})
    r.service = ProjectDispatchService(r.c.repo, commands=Commands(),
        turn_get=r.c.repo.turn_get, attempt_get=r.c.repo.attempt_get)
    monkeypatch.setattr(workers, 'ProjectDispatchService', lambda repo: r.service)
    capture_worker_policy(r.task, r.task['config'], [
        {'function': {'name': name}} for name in ('run_command', 'read_files', 'write_file', 'spawn_agents', 'timer_create')], 'model-a')
    r.args = {'title': 'Implement one part', 'brief': 'Implement and verify the requested feature.', 'idempotency_key': 'part-one'}
    return r


def test_one_human_request_creates_and_starts_independent_worker_without_grant_click(runtime):
    r = runtime
    result = r.call('delegate_project_work', **r.args)
    assert result['dispatch'] == 'accepted', result
    assert result['conversation_id'] != r.task['convId'] and r.calls == [result['conversation_id']]
    row = r.c.repo.read(result['assignment_id'])
    assert row['request']['authorizationSource'] == r.c.request['authorizationSource']
    assert row['grant']['subject'] == 'user:7' and row['grant']['tenant'] == 'tenant-a'
    assert row['request']['profile']['mode'] == 'project'
    assert 'run_command' in row['request']['profile']['tools']
    assert not {'spawn_agents', 'timer_create'} & set(row['request']['profile']['tools'])
    assert r.task['_principalContext']['scopes'] == []
    replay = r.call('delegate_project_work', **r.args)
    assert replay == result and len(r.calls) == len(r.created) == 1
    assert 'error' in r.call('delegate_project_work', **{**r.args, 'brief': 'Different request'})
    assert len(r.calls) == len(r.created) == 1


@pytest.mark.parametrize('violation', ['synthetic', 'unattended', 'source', 'attempt', 'parent', 'plan', 'readonly', 'binding', 'no-policy', 'model-argument'])
def test_invalid_source_never_creates_conversation_or_grant(runtime, violation):
    r = runtime
    if violation == 'synthetic': r.task['_principalContext']['kind'] = 'system'
    if violation == 'unattended': r.task['_attended'] = False
    if violation == 'source': r.c.turn['actor'] = 'virtual_user'
    if violation == 'attempt': r.attempts['coordinator-attempt']['taskId'] = 'another-task'
    if violation == 'parent': r.turns['coordinator-turn']['parentTurnId'] = 'missing'
    if violation == 'plan': r.task['config']['planMode'] = True
    if violation == 'readonly': r.task['config']['readOnlyPaths'] = ['/project']
    if violation == 'binding': r.c.binding.value['revision'] = 2
    if violation == 'no-policy': r.task.pop('_coordinatorWorkerPolicy')
    if violation == 'model-argument': r.args['model'] = 'unapproved-model'
    # Pin the original live role before changing its epoch.
    r.task['_coordinatorDispatchScope'] = {'user_id': 7, 'project_path': '/project',
                                          'conversation_id': 'coordinator', 'binding_revision': 1}
    assert 'error' in r.call('delegate_project_work', **r.args)
    assert not r.created and not r.calls and not r.c.repo.list()


def test_reserved_allowance_blocks_extra_workers_and_confirmed_usage_releases_it(runtime, monkeypatch):
    r = runtime
    monkeypatch.setattr('runtime_guards.task_concurrency_hard_ceiling', lambda: 10)
    results = [r.call('delegate_project_work', **{**r.args, 'idempotency_key': str(i)}) for i in range(8)]
    assert all(item.get('dispatch') == 'accepted' for item in results), results
    denied = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'next'})
    assert 'reserved or exhausted' in denied['error'] and len(r.created) == 8
    first = r.c.repo.read(results[0]['assignment_id'])
    r.attempts[first['execution']['attemptId']]['status'] = 'completed'
    # This worker made no physical model call. Its confirmed unused reservation
    # can be assigned again; unfinished workers retain their whole allowance.
    admitted = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'next'})
    assert admitted['dispatch'] == 'accepted' and len(r.created) == 9


def test_wait_uses_terminal_notification_and_removes_subscription(runtime):
    from lib.agent_core import admission
    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id']); attempt = row['execution']['attemptId']
    ready = threading.Event(); real = admission.on_terminal
    # Worker settlement is real durable evidence in the injected repository;
    # notification alone is deliberately insufficient to declare completion.
    def subscribe(task, fn):
        unsubscribe = real(task, fn); ready.set(); return unsubscribe
    result = []
    from unittest.mock import patch
    with patch.object(admission, 'on_terminal', subscribe):
        thread = threading.Thread(target=lambda: result.append(r.call('await_project_workers',
            assignment_ids=[row['id']], timeout_seconds=2)))
        thread.start(); assert ready.wait(1)
        r.turns[row['execution']['turnId']]['projection'] = {'content': 'Verified result'}
        r.attempts[attempt]['status'] = 'completed'
        admission.fire_terminal_callbacks(worker['task_id'])
        thread.join(2)
    assert not thread.is_alive() and result[0]['wait'] == 'settled', result
    assert result[0]['workers'][0]['result_excerpt'] == 'Verified result'
    assert worker['task_id'] not in admission._term_callbacks
    assert len(r.calls) == 1


def test_timeout_and_stop_leave_no_callbacks_and_never_restart_workers(runtime):
    from lib.agent_core import admission
    r = runtime; worker = r.call('delegate_project_work', **r.args)
    args = {'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 0.01}
    assert r.call('await_project_workers', **args)['wait'] == 'timeout'
    assert worker['task_id'] not in admission._term_callbacks
    r.task['aborted'] = True
    assert r.call('await_project_workers', **args)['wait'] == 'cancelled'
    assert len(r.calls) == 1


def test_delegate_and_await_reach_handlers_through_ordinary_pipeline(runtime, monkeypatch):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS
    r = runtime
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *args, **kwargs: True)
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    messages = []
    def run(name, arguments):
        parsed = _mk_tc(name, name, len(messages), args=arguments)
        r.task['toolRounds'] = [parsed[5]]
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              COORDINATOR_TOOLS, messages, [], 0, 'model-a')
        assert parsed[5]['status'] == 'done', messages[-1]
    run('delegate_project_work', r.args)
    assert len(r.created) == len(r.calls) == 1
    row = r.c.repo.list()[0]
    r.attempts[row['execution']['attemptId']]['status'] = 'completed'
    run('await_project_workers', {'assignment_ids': [row['id']]})
    assert len(r.calls) == 1


def test_stop_of_original_request_prevents_new_grants_even_before_task_flag_catches_up(runtime):
    from lib.project_dispatch.repository import record_stop
    r = runtime
    r.c.now[0] = 1010
    record_stop(7, 'coordinator', 1005, client=r.c.client)
    assert 'stopped' in r.call('delegate_project_work', **r.args)['error']
    assert not r.created and not r.c.repo.list()


@pytest.mark.parametrize('limits', [{'max_tokens': 1000}, {'max_tokens': 0}, {'max_tool_calls': 10}])
def test_explicit_source_execution_budget_cannot_be_bypassed_with_default_worker_pool(runtime, limits):
    r = runtime; r.task['config']['executionLimits'] = limits
    assert 'explicit execution budget' in r.call('delegate_project_work', **r.args)['error']
    assert not r.created and not r.calls and not r.c.repo.list()


def test_worker_deadline_cannot_outlive_original_request(runtime):
    r = runtime; r.task['config']['executionLimits'] = {'deadline': 1005, 'max_tokens': None}
    first = r.call('delegate_project_work', **r.args)
    assert first['budget']['deadline'] == 1005
    r.c.now[0] = 1006
    assert 'deadline expired' in r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'late'})['error']
    assert len(r.created) == len(r.calls) == 1


def _worker_task(r):
    from lib.project_dispatch import admission
    import time
    r.c.now[0] = time.time()
    r.attempts['coordinator-attempt']['createdAt'] = int(r.c.now[0] * 1000)
    result = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(result['assignment_id'])
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']))
    config = trusted.capture({}, 7, trusted.principal, result['conversation_id'])
    config['_turnOwnerUserId'] = 7
    task = {'id': result['task_id'], 'convId': result['conversation_id'], 'config': config,
            'status': 'running', '_userId': 7, '_principalContext': trusted.principal.to_payload(),
            '_attended': False, '_turnId': row['execution']['turnId'], '_attemptId': row['execution']['attemptId'],
            'abort_event': threading.Event(), 'usage': {}}
    admission.bind_account(task)
    return task, row


def test_parallel_live_tools_share_one_account_but_uncertain_effect_blocks_more(runtime, monkeypatch):
    from lib.llm.llm_errors import AbortedError
    r = runtime; task, row = _worker_task(r); budget = task['_executionBudget']
    first = budget.begin_tool_attempt(task, 'first', 'read_files', 1)
    second = budget.begin_tool_attempt(task, 'second', 'read_files', 1)
    budget.finish_tool_attempt(first, phase='executor_returned')
    mutate = r.c.repo.mutate
    monkeypatch.setattr(r.c.repo, 'mutate', lambda *a: (_ for _ in ()).throw(OSError('lost settlement')))
    budget.finish_tool_attempt(second, phase='executor_returned')
    monkeypatch.setattr(r.c.repo, 'mutate', mutate)
    with pytest.raises(AbortedError, match='unconfirmed'):
        budget.begin_tool_attempt(task, 'third', 'write_file', 2)
    saved = r.c.repo.read(row['id'])['toolAttempts']
    assert saved[0]['phase'] == 'executor_returned' and saved[1]['phase'] == 'intent'


def test_worker_cannot_enlarge_its_frozen_capability_config(runtime):
    from lib.project_dispatch import admission
    from lib.llm.llm_errors import AbortedError
    worker, _ = _worker_task(runtime)
    worker['config']['schedulerEnabled'] = True
    with pytest.raises(AbortedError):
        admission.validate_task(worker)


def test_changed_delegation_source_invalidates_dependency_without_losing_read_evidence(runtime):
    from lib.project_dispatch.dependencies import _evidence
    r = runtime
    result = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(result['assignment_id'])
    r.turns['coordinator-turn']['parentTurnId'] = 'different-source'
    assert _evidence(r.c.repo, row)[:2] == ('invalid', 'scope_changed')
    viewed = r.call('get_project_assignment', assignment_id=row['id'])
    assert viewed['assignmentId'] == row['id'] and len(r.calls) == 1


def test_coordinator_review_is_exact_and_not_human_acceptance(runtime):
    r = runtime
    result = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(result['assignment_id']); execution = row['execution']
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1}
    arguments = {'assignment_id': row['id'], 'result_revision': 2, 'decision': 'accept', 'note': 'Evidence checked'}
    with pytest.raises(ValueError, match='exact current completed'):
        r.service.review_for_coordinator(r.task, scope, arguments)
    r.attempts[execution['attemptId']]['status'] = 'completed'
    r.turns[execution['turnId']]['projectionRevision'] = 2
    viewed = r.service.review_for_coordinator(r.task, scope, arguments)
    assert viewed['review']['actor'] == 'coordinator' and not viewed['canonicalDelivery']
    assert r.service.review_for_coordinator(r.task, scope, arguments) == viewed
    with pytest.raises(ValueError, match='immutable'):
        r.service.review_for_coordinator(r.task, scope, {**arguments, 'note': 'different review'})
    r.c.binding.value['revision'] = 2
    with pytest.raises(ValueError):
        r.service.review_for_coordinator(r.task, scope, arguments)


@pytest.mark.parametrize('violation', ['readonly', 'synthetic', 'unattended', 'source'])
def test_role_visible_delegation_keeps_request_authority_gates(runtime, violation):
    from tests.tasks_pkg.test_coordinator_tool_surface import assemble, names, wire

    r = runtime
    if violation == 'readonly':
        r.task['config']['readOnlyPaths'] = ['/project']
    elif violation == 'synthetic':
        r.task['_principalContext']['kind'] = 'system'
    elif violation == 'unattended':
        r.task['_attended'] = False
    else:
        r.c.turn['actor'] = 'virtual_user'
    tools = assemble(r.task)
    assert 'delegate_project_work' in names(wire(r.task, tools)['tools'])
    result = r.call('delegate_project_work', **r.args)
    assert 'error' in result
    assert not r.created and not r.calls and not r.c.repo.list()
