"""Real command/task/budget boundaries over injected authority and fake providers."""
from copy import deepcopy
import threading
import time
from types import SimpleNamespace

import pytest

from tests.project_mod.test_project_dispatch_repository import context as context
from lib.project_dispatch import admission
from lib.project_dispatch.service import ProjectDispatchService
from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(context, monkeypatch):
    c = context
    c.request['budget']['deadline'] = time.time() + 300
    c.request['budget']['maxTokens'] = 10000
    monkeypatch.setattr(admission, 'repository_for', lambda binding: c.repo)
    assignment = c.repo.authorize(c.request, c.principal, 'grant')
    binding = c.repo.admission(assignment['id'])
    trusted = admission.TrustedDispatch(binding)

    c.repo.attempt_get = lambda *a, **kw: {'attemptId': 'worker-attempt', 'turnId': 'worker-turn',
        'conversationId': 'recipient', 'taskId': 'task-a', 'status': 'running'}
    def task(task_id='task-a'):
        config = trusted.capture({}, 7, trusted.principal, 'recipient')
        config.update(_turnOwnerUserId=7, _turnId='worker-turn', _attemptId='worker-attempt')
        c.repo.bind_execution(assignment['id'], 'worker-turn', 'worker-attempt')
        return {'id': task_id, 'convId': 'recipient', 'config': config, 'status': 'running',
                '_userId': 7, '_principalContext': trusted.principal.to_payload(),

                '_turnId': 'worker-turn', '_attemptId': 'worker-attempt',
                '_attended': False, 'abort_event': threading.Event(), 'usage': {}}
    return SimpleNamespace(c=c, assignment=assignment, binding=binding, trusted=trusted, task=task)


def test_separate_project_system_identity_does_not_relax_generic_user_contract(runtime):
    from lib.conversation_sync.execution_identity import capture_execution_identity, restore_execution_identity
    r = runtime
    with pytest.raises(PermissionError):
        capture_execution_identity({}, 7, r.trusted.principal)
    t = r.task()
    principal, attended = restore_execution_identity(t['config'])
    assert principal.kind == 'system' and principal.scopes == {'chat'}
    assert attended is False
    assert '_conversationExecutionIdentity' not in t['config']
    # No public config can turn a user principal into this system identity.
    public = capture_execution_identity(t['config'], 7, r.c.principal)
    assert admission.KEY not in public


@pytest.mark.parametrize('mutation', [
    lambda t: t['config'].pop(admission.KEY),
    lambda t: t['config'].__setitem__('executionLimits', {'max_tokens': None, 'max_tool_calls': None, 'deadline': None}),
    lambda t: t.__setitem__('_attended', True),
    lambda t: t.__setitem__('_principalContext', PrincipalContext.user(subject_id='user:7', owner_user_id=7).to_payload()),
    lambda t: t.__setitem__('_parentTaskId', 'parent'),
    lambda t: t['config'].__setitem__('humanGuidanceEnabled', True),
    lambda t: t.__setitem__('_attemptId', 'different'),
])
def test_mutated_live_carrier_never_gains_more_authority(runtime, mutation):
    t = runtime.task()
    admission.bind_account(t)
    mutation(t)
    with pytest.raises((PermissionError, AbortedError, ValueError)):
        admission.validate_task(t)


def test_unbound_direct_invocation_and_account_replacement_fail_closed(runtime):
    t = runtime.task()
    with pytest.raises(AbortedError, match='bound conserved'):
        admission.validate_task(t)
    admission.bind_account(t)
    assert admission.validate_task(t) == runtime.binding
    t['_executionBudget'] = None
    with pytest.raises(AbortedError):
        admission.validate_task(t)


def _lease(t):
    from lib.agent_core.execution_budget import model_budget_admission
    return model_budget_admission(t)(input_tokens=4, max_output_tokens=6)


def test_physical_requests_retries_and_unknown_usage_share_conserved_budget(runtime):
    r = runtime; t = r.task(); admission.bind_account(t)
    first = _lease(t); first.prepare_input({'messages': []}); first.mark_dispatched()
    first.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    second = _lease(t); second.mark_dispatched(); second.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    third = _lease(t)
    with pytest.raises(AbortedError):
        third.mark_dispatched()
    third.settle(unsent=True)
    rows = r.c.repo.read(r.assignment['id'])['modelAttempts']
    assert len(rows) == 2 and sum(row['reported_tokens'] for row in rows) == 20


def test_permission_change_after_reservation_denies_physical_send(runtime):
    r = runtime; t = r.task(); admission.bind_account(t)
    lease = _lease(t)
    r.c.repo.revoke(r.assignment['id'], 1)
    with pytest.raises((AbortedError, ValueError)):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == []


@pytest.mark.parametrize('same_task_id', [False, True])
def test_new_account_cannot_replay_uncertain_occurrence_or_replenish_budget(runtime, same_task_id):
    r = runtime; t = r.task(); admission.bind_account(t)
    _lease(t).mark_dispatched()  # response/settlement lost

    original = deepcopy(r.c.repo.read(r.assignment['id'])['modelAttempts'])
    restored = r.task('task-a' if same_task_id else 'task-b')
    admission.bind_account(restored)
    lease = _lease(restored)
    with pytest.raises(AbortedError):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == original
    with pytest.raises(AbortedError):
        _lease(restored).mark_dispatched()


def test_no_tools_catalog_actual_custom_handler_and_removed_key_are_denied(runtime, monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as assembly
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    r = runtime; t = r.task(); admission.bind_account(t)
    monkeypatch.setattr(assembly, '_assemble_tool_list', lambda *a, **kw: pytest.fail('Broad catalog was assembled'))
    assert assembly.assemble_round_tools(t['config'], t, {}) == ([], False)
    for remove in (False, True):
        if remove:
            t['config'].pop(admission.KEY)
        with pytest.raises(AbortedError):
            _execute_tool_one(t, {}, 'run_command', 'tc', {}, 0, {}, {}, None, False)


def test_service_cancel_uses_existing_attempt_and_review_never_implies_delivery(runtime):
    r = runtime
    class Commands:
        def __init__(self): self.aborts = []
        def abort_attempt(self, attempt, owner): self.aborts.append((attempt, owner))
    commands = Commands()
    r.c.repo.bind_execution(r.assignment['id'], 'worker-turn', 'worker-attempt')
    service = ProjectDispatchService(r.c.repo, commands=commands,
        turn_get=lambda *a, **kw: {'turnId': 'worker-turn', 'conversationId': 'recipient', 'actor': 'assistant',
            'projectionRevision': 5, 'projection': {'content': 'report'}},
        attempt_get=lambda *a, **kw: {'attemptId': 'worker-attempt', 'turnId': 'worker-turn',
            'conversationId': 'recipient', 'status': 'completed', 'taskId': 'task-a'})
    accepted = service.review(r.assignment['id'], principal=r.c.principal,
        decision='accept', note='verified report', result_revision=5, expected_revision=1)
    assert accepted['review']['decision'] == 'accept'
    assert accepted['canonicalDelivery'] is False
    service.revoke(r.assignment['id'], principal=r.c.principal, expected_revision=1)
    service.revoke(r.assignment['id'], principal=r.c.principal, expected_revision=1)
    assert commands.aborts == [('worker-attempt', 7)] * 2


def test_real_command_birth_input_provenance_budget_report_review_and_duplicate_retry(runtime, monkeypatch):
    import lib.conversation_sync.command_service as module
    from lib.conversation_sync.task_start import start_conversation_attempt_executor
    import lib.conversation_sync.task_start as startup
    r = runtime
    pairs = {}; tasks = {}; launches = []
    def create_pair(conv, *, command_id, config, input_projection, **kw):
        assert kw['input_actor'] == 'virtual_user'
        assert kw['require_lane_idle'] and kw['reject_if_human_queued']
        assert input_projection['_initiator'] == 'brain'
        assert '_goalObjective' not in config
        if command_id in pairs:
            return {**deepcopy(pairs[command_id]), '_needsStart': False}
        value = {'_needsStart': True, 'turn': {'turnId': 'worker-turn', 'conversationId': conv,
            'actor': 'assistant', 'kind': 'reply', 'projection': {}, 'projectionRevision': 1},
            'attempt': {'attemptId': 'worker-attempt', 'turnId': 'worker-turn', 'conversationId': conv,
                'operation': 'create', 'status': 'pending', 'config': deepcopy(config)}}
        pairs[command_id] = deepcopy(value)
        return value
    def get_attempt(*a, include_execution_config=False, **kw):
        attempt = deepcopy(next(iter(pairs.values()))['attempt'])
        if not include_execution_config: attempt.pop('config', None)
        return attempt
    def get_turn(*a, **kw): return deepcopy(next(iter(pairs.values()))['turn'])

    r.c.repo.attempt_get = get_attempt
    monkeypatch.setattr(module, 'create_turn_pair', create_pair)
    monkeypatch.setattr(module, 'claim_attempt_start', lambda *a, **kw: True)
    monkeypatch.setattr(module, 'get_attempt', get_attempt)
    monkeypatch.setattr(module, 'get_turn', get_turn)
    monkeypatch.setattr(module, 'get_conversation_revision', lambda *a, **kw: 1)
    def bind_task(attempt_id, task_id, **kw):
        next(iter(pairs.values()))['attempt']['taskId'] = task_id
        return {'taskId': task_id}
    monkeypatch.setattr(module, 'bind_task', bind_task)
    monkeypatch.setattr(startup, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr('lib.turn_lifecycle.build_api_messages', lambda *a, **kw: [{'role': 'user', 'content': 'brief', '_initiator': 'brain'}])
    def create_task(conv, messages, cfg, *, user_id, principal, **kw):
        assert principal.kind == 'system' and principal.scopes == {'chat'}
        t = {'id': 'task-a', 'convId': conv, 'config': cfg, 'messages': messages,
             '_principalContext': principal.to_payload(), '_userId': user_id,
             'abort_event': threading.Event(), 'status': 'pending', 'usage': {},
             '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId']}
        tasks['task-a'] = t
        return t
    monkeypatch.setattr(startup, 'create_task', create_task)
    monkeypatch.setattr(startup, '_prepare_unbound_task_dispatch', lambda *a: (a[2], None, False))
    def provider(task, **kw):
        launches.append(task['id'])
        admission.validate_task(task)
        lease = _lease(task); lease.mark_dispatched(); lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        stored = next(iter(pairs.values()))
        stored['attempt'].update(status='completed')
        stored['turn'].update(projectionRevision=2, projection={'content': 'verified consult report'})
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', provider)
    commands = module.ConversationTurnCommandService(build_user_message=lambda message, *a: message,
        was_aborted_after=lambda *a: False,
        start_task=lambda conv, cfg, data, watermark, bind, **kw: start_conversation_attempt_executor(
            conv, cfg, abort_after_ts=watermark, on_task_registered=bind, **kw))
    service = ProjectDispatchService(r.c.repo, commands=commands, turn_get=get_turn, attempt_get=get_attempt)
    first = service.dispatch(r.assignment['id'], principal=r.c.principal)
    assert first['execution']['status'] == 'completed'
    assert first['execution']['result']['content'] == 'verified consult report'
    accepted = service.review(r.assignment['id'], principal=r.c.principal, decision='accept',
        note='checked', result_revision=2, expected_revision=1)
    assert accepted['review']['decision'] == 'accept' and accepted['canonicalDelivery'] is False
    assert service.dispatch(r.assignment['id'], principal=r.c.principal)['execution']['taskId'] == 'task-a'
    assert launches == ['task-a'] and len(pairs) == 1


@pytest.mark.parametrize('state', ['active', 'revoked', 'unknown', 'exhausted'])
def test_consultation_cannot_buy_auxiliary_summary(runtime, monkeypatch, state):
    from lib.tasks_pkg.compaction._layer2._summary import _generate_query_aware_summary
    r = runtime; task = r.task(); admission.bind_account(task)
    if state == 'revoked': r.c.repo.revoke(r.assignment['id'], 1)
    elif state == 'unknown': _lease(task).mark_dispatched()
    elif state == 'exhausted': task['_executionActualsComplete'] = False
    def no_provider(*a, **kw): pytest.fail('Auxiliary summary bypassed the assigned account')
    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', no_provider)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', no_provider)
    with pytest.raises(AbortedError, match='auxiliary'):
        _generate_query_aware_summary([], 'brief', task=task)


def test_auto_translation_cannot_be_enabled_by_copied_config(runtime):
    r = runtime
    config = r.trusted.capture({'autoTranslate': True}, 7, r.trusted.principal, 'recipient')
    assert config['autoTranslate'] is False
    task = r.task(); task['config']['autoTranslate'] = True
    with pytest.raises((PermissionError, AbortedError)):
        admission.validate_task(task, require_account=False)


def test_corrupted_model_occurrence_is_an_integrity_error(runtime):
    from lib.project_dispatch.repository import NAMESPACE
    from lib.storage.errors import StorageError
    r = runtime
    value = r.c.client.rows[(NAMESPACE, r.c.repo.key)]['value']
    value['assignments'][0]['modelAttempts'].append({'phase': 'settled'})
    with pytest.raises(StorageError) as error:
        r.c.repo.read(r.assignment['id'])
    assert error.value.code == 'database_integrity'


def test_exact_command_receipt_lookup_is_read_only_and_never_spends(runtime):
    r = runtime
    commands = SimpleNamespace(create_turn=lambda *a, **kw: pytest.fail('Receipt lookup started paid work'))
    service = ProjectDispatchService(r.c.repo, commands=commands,
        turn_get=lambda *a, **kw: None, attempt_get=lambda *a, **kw: None)
    before = deepcopy(r.c.client.rows)
    assert service.list(principal=r.c.principal, command_id='grant')[0]['assignmentId'] == r.assignment['id']
    assert service.list(principal=r.c.principal, command_id='absent') == []
    assert r.c.client.rows == before
    r.c.repo.revoke(r.assignment['id'], 1)
    assert service.list(principal=r.c.principal, command_id='grant')[0]['revoked'] is True
    assert service.list(principal=r.c.principal, command_id='absent') == []
    other_tenant = PrincipalContext.user(subject_id='user:7', owner_user_id=7,
                                        tenant_id='tenant-b', scopes={'chat'})
    assert service.list(principal=other_tenant, command_id='grant') == []
    foreign = PrincipalContext.user(subject_id='user:8', owner_user_id=8, scopes={'chat'})
    with pytest.raises(PermissionError): service.list(principal=foreign, command_id='grant')
    with pytest.raises(ValueError): service.list(principal=r.c.principal, command_id='')


@pytest.mark.parametrize('reason', ['lane_busy', 'superseded_by_human'])
def test_busy_or_queued_human_defers_same_grant_without_birth(runtime, reason):
    from lib.turn_lifecycle import LifecycleConflict
    r = runtime
    calls = []
    def blocked(*a, **kw):
        calls.append(kw)
        raise LifecycleConflict(reason, 'Human lane has priority')
    service = ProjectDispatchService(r.c.repo, commands=SimpleNamespace(create_turn=blocked),
        turn_get=lambda *a, **kw: None, attempt_get=lambda *a, **kw: None)
    for _ in range(2):
        value = service.dispatch(r.assignment['id'], principal=r.c.principal)
        assert value['dispatch'] == 'deferred' and value['execution'] is None
    assert len(calls) == 2
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == []
    assert len(r.c.repo.list()) == 1


def test_post_intent_denial_keeps_uncertainty_even_for_same_account(runtime, monkeypatch):
    r = runtime; task = r.task(); admission.bind_account(task)
    original_mutate = r.c.repo.mutate
    def persist_then_stop(*a, **kw):
        result = original_mutate(*a, **kw)
        if result['modelAttempts'][-1]['phase'] == 'dispatch_intent':
            task['abort_event'].set()
        return result
    monkeypatch.setattr(r.c.repo, 'mutate', persist_then_stop)
    lease = _lease(task)
    with pytest.raises(AbortedError): lease.mark_dispatched()
    original = deepcopy(r.c.repo.read(r.assignment['id'])['modelAttempts'])
    assert original[0]['phase'] == 'dispatch_intent'
    lease.settle(unsent=True)
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == original
    task['abort_event'].clear()
    with pytest.raises(AbortedError): _lease(task)


def test_ordinary_workspace_preference_never_reads_dispatch_authority(monkeypatch):
    monkeypatch.setattr(admission, 'repository_for',
                        lambda _: pytest.fail('ordinary task read dispatch storage'))
    task = {'config': {'profile': {'mode': 'files'}, 'teamRole': 'worker'}}
    assert admission.requires_isolated_workspace(task) is False


def test_consult_workspace_policy_validates_without_requiring_a_clone(runtime):
    task = runtime.task()
    assert admission.requires_isolated_workspace(task) is False
    runtime.c.repo.revoke(runtime.assignment['id'], 1)
    with pytest.raises((AbortedError, ValueError)):
        admission.requires_isolated_workspace(task)


@pytest.mark.parametrize('writable', [False, True])
def test_file_workspace_isolation_comes_from_live_profile(context, monkeypatch, writable):
    c = context
    request = deepcopy(c.request)
    request['profile'] = {
        'mode': 'files', 'tools': ['read_files', 'write_file'] if writable else ['read_files'],
        'readPaths': ['allowed'], 'writePaths': ['allowed'] if writable else [],
        'maxToolCalls': 2,
    }
    row = c.repo.authorize(request, c.principal, 'isolated-files')
    binding = c.repo.admission(row['id'])
    monkeypatch.setattr(admission, 'repository_for', lambda _: c.repo)
    task = {'config': {admission.KEY: binding}}
    assert admission.requires_isolated_workspace(task) is True
    # Selecting storage is not permission for an unbound task to execute.
    with pytest.raises((AbortedError, ValueError, PermissionError)):
        admission.validate_task(task)
    c.repo.revoke(row['id'], 1)
    with pytest.raises((AbortedError, ValueError)):
        admission.requires_isolated_workspace(task)


def test_lost_dispatch_grant_cannot_fall_back_to_direct_workspace():
    task = {'config': {}, '_projectDispatchBinding': {'assignmentId': 'lost'}}
    with pytest.raises(AbortedError, match='grant carrier'):
        admission.requires_isolated_workspace(task)
