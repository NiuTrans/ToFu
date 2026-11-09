"""Original human, Worker and resumed accounts record usage without a spending ceiling."""
from copy import deepcopy
import threading
import time

import pytest

from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context, _worker_task
from lib.project_dispatch.request_budget import bind_source_account, usage as request_usage

pytestmark = pytest.mark.unit


def bind(r, **options):
    r.c.now[0] = time.time()
    r.attempts['coordinator-attempt']['createdAt'] = int(r.c.now[0] * 1000)
    r.task.update(status='running', usage={}, abort_event=threading.Event())
    return bind_source_account(r.task, repository=r.c.repo, **options)


def spend(task, tokens=10, *, complete=True):
    lease = task['_executionBudget'].reserve_model(task, input_tokens=1, max_output_tokens=tokens)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 1, 'completion_tokens': tokens - 1} if complete else None)


def usage(r, row):
    return request_usage(r.c.repo, r.c.repo._load()[0], row['id'])


def test_request_account_has_no_policy_and_coordinator_plus_workers_are_summed(runtime):
    r = runtime; row = bind(r)
    assert 'policy' not in row
    spend(r.task, 100)
    assert r.task['_executionBudget'].reserve_tool(r.task) is None
    worker, assignment = _worker_task(r)
    assert assignment['request']['budget'] == {}
    assert 'maxToolCalls' not in assignment['request']['profile']
    assert worker['config']['executionLimits'] == {'max_tokens': None, 'max_tool_calls': None, 'deadline': None}
    spend(worker, 50)
    assert worker['_executionBudget'].reserve_tool(worker) is None
    state = usage(r, row)
    assert (state['tokens'], state['modelRequests'], state['toolCalls']) == (150, 2, 2)
    assert state['usageComplete'] and state['activeWorkers'] == 1
    r.attempts[assignment['execution']['attemptId']]['status'] = 'completed'
    assert usage(r, row)['activeWorkers'] == 0


def test_spend_far_beyond_the_old_ceiling_is_never_refused(runtime):
    r = runtime; row = bind(r)
    for _ in range(140):  # Old default allowed 128 model requests per request.
        spend(r.task, 20_000)
    for _ in range(600):  # Old per-Worker ceiling was 512 tool calls.
        assert r.task['_executionBudget'].reserve_tool(r.task) is None
    state = usage(r, row)
    assert state['modelRequests'] == 140 and state['tokens'] == 2_800_000 and state['toolCalls'] == 600


def test_worker_missing_usage_receipt_never_blocks_its_next_call(runtime):
    """Regression for coordinator-1a390889: workers died on 'usage unconfirmed'."""
    r = runtime; row = bind(r)
    worker, _ = _worker_task(r)
    spend(worker, 50, complete=False)
    spend(worker, 50)
    for _ in range(20):  # Old per-Worker ceiling was 16 model requests.
        spend(worker, 5)
    state = usage(r, row)
    assert state['modelRequests'] == 22 and not state['usageComplete']


def test_coordinator_missing_usage_receipt_never_blocks_its_next_call(runtime):
    r = runtime; row = bind(r)
    spend(r.task, 10, complete=False)
    spend(r.task, 10)
    assert r.task['_executionBudget'].reserve_tool(r.task) is None
    assert usage(r, row)['modelRequests'] == 2 and not usage(r, row)['usageComplete']


def test_request_account_records_workers_without_limiting_concurrency(runtime, monkeypatch):
    r = runtime; account = bind(r)
    monkeypatch.setattr('runtime_guards.task_concurrency_hard_ceiling', lambda: 2)
    results = [r.call('delegate_project_work', **{**r.args, 'idempotency_key': str(i)}) for i in range(12)]
    assert all(item.get('dispatch') == 'accepted' for item in results), results
    assert len(r.created) == len(r.calls) == usage(r, account)['activeWorkers'] == 12


def test_legacy_issued_worker_policy_does_not_limit_active_workers(runtime, monkeypatch):
    from lib.project_dispatch import delegation
    from lib.conversations import coordinator_workers
    r = runtime; account = bind(r)
    original = delegation.policy_for_task
    with monkeypatch.context() as issued:
        def legacy(*args):
            return {**original(*args), 'maxConcurrent': 1}
        issued.setattr(delegation, 'policy_for_task', legacy)
        issued.setattr(coordinator_workers, 'policy_for_task', legacy)
        first = r.call('delegate_project_work', **r.args)
    assert first.get('dispatch') == 'accepted', first
    results = [r.call('delegate_project_work', **{**r.args, 'idempotency_key': str(i)}) for i in range(4)]
    assert all(item.get('dispatch') == 'accepted' for item in results), results
    assert all(r.c.repo.read(item['assignment_id'])['request']['delegation']['policy']['maxConcurrent'] == 1
               for item in results)
    assert len(r.created) == len(r.calls) == usage(r, account)['activeWorkers'] == 5


def _dispatch(task):
    lease = task['_executionBudget'].reserve_model(task, input_tokens=1, max_output_tokens=10)
    try:
        lease.mark_dispatched()
    finally:
        lease.settle(unsent=not lease.dispatched)


def test_changed_source_refuses_dispatch_as_authority_not_model_failure(runtime):
    """Regression for coordinator-1a390889: a builtin PermissionError was retried as a
    provider failure on every key and fallback model."""
    from lib.llm.llm_errors import ModelAdmissionRefused
    r = runtime; bind(r)
    r.turns['human-turn']['projectionRevision'] += 1
    with pytest.raises(ModelAdmissionRefused, match='authority ended: Original request source'):
        _dispatch(r.task)
    assert not r.task.get('aborted')


def test_deferred_translation_survives_restart_of_its_persisted_request(runtime):
    r = runtime
    translated = deepcopy(r.turns['human-turn'])
    translated['projectionRevision'] += 1
    translated['projection'].update(content='Translated request', originalContent='Original request')
    r.turns['human-turn'] = translated
    account = bind(r, expected_input=('human-turn', translated['projectionRevision']))

    # Boot recovery settles the orphaned attempt. A resume is a new physical
    # task/attempt over the same accepted human source, not a nonce reset.
    r.attempts['coordinator-attempt']['status'] = 'interrupted'
    r.turns['next-turn'] = {**r.turns['coordinator-turn'], 'turnId': 'next-turn'}
    r.attempts['next-attempt'] = {**r.attempts['coordinator-attempt'], 'attemptId': 'next-attempt',
        'turnId': 'next-turn', 'taskId': 'next-task', 'status': 'running'}
    resumed = deepcopy({key: value for key, value in r.task.items()
                        if key in ('id', 'convId', 'config', '_userId', '_principalContext',
                                   '_attended', '_turnId', '_attemptId', 'status', 'usage')})
    resumed.update(id='next-task', _turnId='next-turn', _attemptId='next-attempt', usage={},
                   abort_event=threading.Event())
    repository = type(r.c.repo)(
        7, '/project', client=r.c.client, storage_version=1,
        conversations=r.c.conversations, turn_get=r.c.repo.turn_get,
        binding_repository=r.c.binding, clock=lambda: r.c.now[0],
        attempt_get=r.c.repo.attempt_get)
    assert bind_source_account(resumed, repository=repository)['id'] == account['id']
    _dispatch(resumed)

    stored = repository._load()[0]['requestBudgets']
    assert len(stored) == 1 and len(stored[0]['accounts']) == 2
    assert usage(r, account)['modelRequests'] == 1


@pytest.mark.parametrize('field, replacement', [
    ('tenant_id', 'tenant-b'),
    ('subject_id', 'user:8'),
])
def test_changed_principal_still_refuses_dispatch(runtime, field, replacement):
    from lib.llm.llm_errors import ModelAdmissionRefused
    r = runtime; account = bind(r)
    r.task['_principalContext'][field] = replacement
    with pytest.raises(ModelAdmissionRefused, match='Original request source or principal changed'):
        _dispatch(r.task)
    assert usage(r, account)['modelRequests'] == 0


def test_replaced_source_turn_still_refuses_dispatch(runtime):
    from lib.llm.llm_errors import ModelAdmissionRefused
    r = runtime; account = bind(r)
    r.turns['human-turn'] = {**r.turns['human-turn'], 'turnId': 'replacement-turn'}
    with pytest.raises(ModelAdmissionRefused, match='Authorization source changed'):
        _dispatch(r.task)
    assert usage(r, account)['modelRequests'] == 0


def test_stop_at_dispatch_stays_a_plain_abort(runtime):
    from lib.llm.llm_errors import AbortedError, AdmissionRefused
    r = runtime; bind(r)
    r.task['abort_event'].set()
    with pytest.raises(AbortedError) as stopped:
        _dispatch(r.task)
    assert not isinstance(stopped.value, AdmissionRefused)


@pytest.mark.parametrize('settled, binds', [
    (('human-turn', 3), True),   # the verdict's revision is still current
    ((None, None), True),        # no verdict settled: the current source binds
    (('human-turn', 2), False),  # the input changed after its verdict
])
def test_deferred_input_account_pins_the_revision_its_verdict_settled(runtime, settled, binds):
    r = runtime
    if binds:
        assert bind(r, expected_input=settled)['authorizationSource']['projectionRevision'] == 3
        return
    with pytest.raises(PermissionError, match='changed before its account was bound'):
        bind(r, expected_input=settled)
    assert r.task.get('_coordinatorRequestBudget') is None
    assert not r.c.repo._load()[0].get('requestBudgets')


def test_stopped_request_opens_no_account(runtime):
    r = runtime; r.task['aborted'] = True
    assert bind(r) is None
    assert r.task.get('_coordinatorRequestBudget') is None
    assert not r.c.repo._load()[0].get('requestBudgets')


def test_same_human_input_new_attempt_keeps_one_account(runtime):
    r = runtime; row = bind(r)
    spend(r.task, 500)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.turns['next-turn'] = {**r.turns['coordinator-turn'], 'turnId': 'next-turn'}
    r.attempts['next-attempt'] = {**r.attempts['coordinator-attempt'], 'attemptId': 'next-attempt',
        'turnId': 'next-turn', 'taskId': 'next-task', 'status': 'running'}
    resumed = {key: value for key, value in r.task.items() if key not in ('_coordinatorRequestBudget', '_executionBudget')}
    resumed.update(id='next-task', _turnId='next-turn', _attemptId='next-attempt', usage={})
    current = bind_source_account(resumed, repository=r.c.repo)
    assert current['id'] == row['id'] and len(current['accounts']) == 2
    spend(resumed, 9000)
    assert usage(r, row)['tokens'] == 9500


def test_plan_counts_own_usage_but_never_delegates(runtime):
    r = runtime; r.task['config']['planMode'] = True
    row = bind(r); spend(r.task)
    assert usage(r, row)['tokens'] == 10
    assert 'error' in r.call('delegate_project_work', **r.args)
    assert not r.created


def test_unknown_source_write_ack_never_rebinds(runtime, monkeypatch):
    r = runtime; row = bind(r)
    change = r.c.repo._change
    def lost_ack(callback, **options):
        change(callback, **options)
        raise OSError('ack lost')
    monkeypatch.setattr(r.c.repo, '_change', lost_ack)
    with pytest.raises(OSError):
        r.task['_executionBudget'].reserve_tool(r.task)
    monkeypatch.setattr(r.c.repo, '_change', change)
    assert usage(r, row)['toolCalls'] == 1
    replacement = {key: value for key, value in r.task.items() if key not in ('_coordinatorRequestBudget', '_executionBudget')}
    with pytest.raises(PermissionError, match='replenished'):
        bind_source_account(replacement, repository=r.c.repo)


@pytest.mark.parametrize('withdraw', ['stop', 'binding', 'source'])
def test_source_spending_revalidates_live_authority(runtime, withdraw):
    r = runtime; row = bind(r)
    if withdraw == 'stop': r.task['aborted'] = True
    if withdraw == 'binding': r.c.binding.value['revision'] += 1
    if withdraw == 'source': r.c.turn['projectionRevision'] += 1; r.turns['human-turn'] = r.c.turn
    with pytest.raises(Exception):
        spend(r.task)
    assert usage(r, row)['tokens'] == 0
    if withdraw == 'stop':
        assert r.task['_executionBudget'].reserve_tool(r.task) == 'execution_owner_cancelled'
    else:
        with pytest.raises(Exception):
            r.task['_executionBudget'].reserve_tool(r.task)
    assert usage(r, row)['toolCalls'] == 0


def test_coordinator_status_exposes_usage_not_allowance(runtime):
    from lib.conversations.coordinator_budget import read_state
    r = runtime; row = bind(r); spend(r.task)
    before = r.c.client.puts
    status = r.call('get_project_workers')
    human = read_state(7, '/project', repository=r.c.repo, bindings=r.c.binding)
    assert status['request_usage'] == human['request']
    assert status['request_usage']['usage']['tokens'] == 10 and 'request_allowance' not in status
    assert r.c.client.puts == before and row['id'] == human['request']['id']


def test_fifty_physical_calls_and_passive_reads_have_bounded_evidence(runtime):
    import json
    r = runtime; row = bind(r)
    start = time.perf_counter()
    for _ in range(50): spend(r.task)
    elapsed = time.perf_counter() - start
    value, _ = r.c.repo._load()
    byte_count = len(json.dumps(value, ensure_ascii=False).encode())
    before = r.c.client.puts
    threads = threading.active_count()
    for _ in range(50): assert usage(r, row)['tokens'] == 500
    assert r.c.client.puts == before and threading.active_count() == threads
    # Settled receipts fold into a counter, so the record stays small.
    assert len(value['requestBudgets'][0]['modelAttempts']) == 16 and byte_count < 32000
    print(f'usage evidence: calls=50 seconds={elapsed:.4f} bytes={byte_count} passive_writes=0 added_threads=0')


def test_long_running_records_stay_bounded_and_replays_are_not_double_counted(runtime):
    import json
    from lib.project_dispatch.repository import CONTRACT
    r = runtime; row = bind(r)
    worker, assignment = _worker_task(r)
    for _ in range(80):
        spend(r.task, 5)
        spend(worker, 5)
    value, _ = r.c.repo._load()
    retained = CONTRACT['delegationDefaults']['retainedSettledReceipts']
    source = value['requestBudgets'][0]
    worker_row = next(item for item in value['assignments'] if item['id'] == assignment['id'])
    assert len(source['modelAttempts']) <= retained and len(worker_row['modelAttempts']) <= retained
    state = usage(r, row)
    assert state['modelRequests'] == 160 and state['tokens'] == 800 and state['usageComplete']
    folded = source['settledModelUsage']['watermarks'][r.task['id']]
    replay = {'root_task_id': r.task['id'], 'attempt_ordinal': folded, 'attempt_task_id': r.task['id'],
              'reserved_tokens': 5, 'phase': 'settled', 'reported_tokens': 5, 'usage_complete': True,
              'account_observed_tokens': 0, 'account_nonce': r.task['_coordinatorRequestBudget'].nonce}
    from lib.project_dispatch.accounting import folded_model_event
    assert folded_model_event(source, replay)
    assert len(json.dumps(value).encode()) < 64_000


def test_worker_validation_reads_its_request_budget_from_its_own_snapshot(runtime, monkeypatch):
    """One live Worker validation loads and validates its request budget once.

    The budget's authority fields are immutable and its accounts append-only, so
    the validation snapshot decides exactly as a reload; a missing account still refuses.
    """
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import NAMESPACE
    r = runtime; bind(r)
    task, row = _worker_task(r)
    assert row['request']['delegation']['policy'].get('requestBudgetId')
    binding = task['config'][admission.KEY]
    namespaces, query = [], r.c.client.query

    def counted(operation, args):
        namespaces.append(args['namespace'])
        return query(operation, args)
    monkeypatch.setattr(r.c.client, 'query', counted)
    validated = r.c.repo.validate(binding)
    assert namespaces.count(NAMESPACE) == 1
    assert validated == r.c.repo.read(row['id'])
    delegation = row['request']['delegation']
    stored = r.c.client.rows[(NAMESPACE, r.c.repo.key)]['value']
    budget = next(item for item in stored['requestBudgets'] if item['id'] == delegation['policy']['requestBudgetId'])
    for account in budget['accounts']:
        if account['attemptId'] == delegation['attemptId']:
            account['turnId'] = 'another-turn'
    with pytest.raises(PermissionError, match='original request account'):
        r.c.repo.validate(binding)


def _run_tool(task, call_id):
    budget = task['_executionBudget']
    assert budget.reserve_tool(task) is None
    event = budget.begin_tool_attempt(task, call_id, 'read_files', 1)
    budget.finish_tool_attempt(event, phase='executor_returned')


def _restart_continuation(r, worker, assignment, *, resumed=True, predecessor='interrupted',
                          cause='server_restart', operation='continue'):
    """The accepted successor of the Worker's settled attempt: same root, next attempt."""
    from lib.project_dispatch import admission
    old = r.c.repo.read(assignment['id'])['execution']
    r.attempts[old['attemptId']].update(status='interrupted', taskId=worker['id'])
    r.attempts['restart-continuation'] = {**r.attempts[old['attemptId']],
                                          'attemptId': 'restart-continuation', 'status': 'running'}
    r.c.repo.continue_execution(assignment['id'], old, old['turnId'], 'restart-continuation')
    r.attempts[old['attemptId']]['status'] = predecessor
    trusted = admission.TrustedDispatch(r.c.repo.admission(assignment['id']))
    config = trusted.capture({}, 7, trusted.principal, worker['convId'])
    config.update(_turnOwnerUserId=7, _turnId=old['turnId'], _attemptId='restart-continuation',
                  _turnOperation=operation)
    if resumed:
        config['_resumeSource'] = {'attemptId': old['attemptId'], 'taskId': worker['id'],
                                   'status': predecessor, 'cause': cause}
    return {**{key: worker[key] for key in ('id', 'convId', 'status', '_userId', '_principalContext', '_attended')},
            '_turnId': old['turnId'], '_attemptId': 'restart-continuation', 'config': config,
            'abort_event': threading.Event(), 'usage': {}}


@pytest.mark.parametrize(('operation', 'status', 'cause'), [
    ('continue', 'interrupted', 'server_restart'),
    ('continue', 'interrupted', 'user_abort'),
    ('continue', 'failed', 'generation_error'),
    ('checkpoint_resume', 'failed', 'generation_error'),
    ('continue', 'truncated', 'max_tokens'),
    ('answer_guidance', 'interrupted', 'human_guidance'),
    ('regenerate', 'failed', 'generation_error'),
    ('regenerate', 'completed', 'model_finished'),
])
def test_accepted_resume_continues_the_worker_tool_reservation_counter(
        runtime, operation, status, cause):
    """A settled predecessor's work remains charged across accepted resumes."""
    from lib.project_dispatch import admission
    r = runtime; row = bind(r)
    worker, assignment = _worker_task(r)
    spend(worker)
    for number in range(3):
        _run_tool(worker, f'before-restart-{number}')
    counter = r.c.repo.read(assignment['id'])['toolReservations']
    # The ledger's root is the assignment's work id, shared by every physical attempt.
    assert counter['rootTaskId'] == assignment['execution']['taskId'] != worker['id']
    assert counter['count'] == 3

    resumed = _restart_continuation(r, worker, assignment, predecessor=status,
                                    cause=cause, operation=operation)
    admission.bind_account(resumed)
    assert r.c.repo.read(assignment['id'])['toolReservations'] == counter
    _run_tool(resumed, 'after-restart')

    stored = r.c.repo.read(assignment['id'])
    assert stored['toolReservations'] == {**counter, 'count': 4}
    assert [(item['tool_ordinal'], item['tool_call_id'], item['phase']) for item in stored['toolAttempts']] == [
        (1, 'before-restart-0', 'executor_returned'), (2, 'before-restart-1', 'executor_returned'),
        (3, 'before-restart-2', 'executor_returned'), (4, 'after-restart', 'executor_returned')]
    assert all(item['account_nonce'] == counter['accountNonce'] for item in stored['toolAttempts'])
    lease = resumed['_executionBudget'].reserve_model(resumed, input_tokens=1, max_output_tokens=10)
    assert lease.ordinal == 2
    lease.settle(unsent=True)
    assert usage(r, row)['toolCalls'] == 4 and not resumed.get('aborted')


@pytest.mark.parametrize('predecessor, cause, operation', [
    # 2026-11-07 08:57 (worker-b1c0c8af, 4647b89b): a restart interrupted the
    # Worker after 123 tool calls, its boot-recovery Continue failed before
    # entry, and the Coordinator's continue of that failed attempt never started.
    ('failed', 'generation_error', 'continue'),
    ('failed', 'provider_error', 'checkpoint_resume'),
    ('interrupted', 'user_abort', 'continue'),
    ('completed', 'stop', 'regenerate'),
])
def test_every_successor_of_the_bound_turn_continues_the_worker_tool_account(
        runtime, predecessor, cause, operation):
    """Not only a restart Continue: any settled predecessor's successor keeps the one account."""
    from lib.project_dispatch import admission
    r = runtime; row = bind(r)
    worker, assignment = _worker_task(r)
    spend(worker)
    for number in range(2):
        _run_tool(worker, f'before-{number}')
    counter = r.c.repo.read(assignment['id'])['toolReservations']
    assert counter['count'] == 2

    resumed = _restart_continuation(r, worker, assignment, predecessor=predecessor,
                                    cause=cause, operation=operation)
    admission.bind_account(resumed)
    assert r.c.repo.read(assignment['id'])['toolReservations'] == counter
    _run_tool(resumed, 'after')

    stored = r.c.repo.read(assignment['id'])
    assert stored['toolReservations'] == {**counter, 'count': 3}
    assert [(item['tool_ordinal'], item['tool_call_id'], item['account_nonce']) for item in stored['toolAttempts']] == [
        (1, 'before-0', counter['accountNonce']), (2, 'before-1', counter['accountNonce']),
        (3, 'after', counter['accountNonce'])]
    lease = resumed['_executionBudget'].reserve_model(resumed, input_tokens=1, max_output_tokens=10)
    assert lease.ordinal == 2
    lease.settle(unsent=True)
    assert usage(r, row)['toolCalls'] == 3 and not resumed.get('aborted')


@pytest.mark.parametrize('replenish', ['fresh_start', 'foreign_nonce', 'foreign_root', 'live_predecessor',
                                       'foreign_turn'])
def test_resumed_worker_cannot_replenish_or_adopt_another_tool_reservation_counter(runtime, replenish):
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch import admission
    r = runtime; bind(r)
    worker, assignment = _worker_task(r)
    spend(worker)
    _run_tool(worker, 'before-restart')
    if replenish in ('foreign_nonce', 'foreign_root'):
        key, value = (('accountNonce', 'f' * 32) if replenish == 'foreign_nonce'
                      else ('rootTaskId', 'project-worker:another-assignment'))
        r.c.repo.mutate(assignment['id'], lambda row: row['toolReservations'].update({key: value}))
    before = r.c.repo.read(assignment['id'])['toolReservations']
    resumed = _restart_continuation(r, worker, assignment, resumed=replenish != 'fresh_start',
                                    predecessor='running' if replenish == 'live_predecessor' else 'interrupted')
    if replenish == 'foreign_turn':
        r.attempts[resumed['config']['_resumeSource']['attemptId']]['turnId'] = 'another-turn'
    with pytest.raises(AbortedError, match='replenished|settled predecessor'):
        admission.bind_account(resumed)
    assert r.c.repo.read(assignment['id'])['toolReservations'] == before


@pytest.mark.parametrize(('field', 'value'), [
    ('attemptId', 'foreign-attempt'), ('taskId', 'foreign-task'),
    ('turnId', 'foreign-turn'), ('conversationId', 'foreign-conversation'),
    ('status', 'running'), ('status', 'failed'),
])
def test_resume_receipt_must_match_exact_predecessor_before_restoring_counts(runtime, field, value):
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch import admission

    r = runtime
    bind(r)
    worker, assignment = _worker_task(r)
    _run_tool(worker, 'before-resume')
    resumed = _restart_continuation(r, worker, assignment)
    before = r.c.repo.read(assignment['id'])['toolReservations']
    source = resumed['config']['_resumeSource']['attemptId']
    r.attempts[source][field] = value
    with pytest.raises(AbortedError, match='settled predecessor'):
        admission.bind_account(resumed)
    assert r.c.repo.read(assignment['id'])['toolReservations'] == before
