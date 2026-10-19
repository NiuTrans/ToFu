"""Worker outcomes resume the original finite request through ordinary admission."""
from copy import deepcopy
from types import SimpleNamespace
import json
import threading

import pytest

from tests.project_mod.test_coordinator_workers import runtime, base_runtime, context
from lib.project_dispatch import admission, continuation
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.repository import CONTRACT, ProjectDispatchError
from lib.project_dispatch.service import ProjectDispatchService
from lib.conversations.coordinator_tools import execute_coordinator_tool

pytestmark = pytest.mark.unit


@pytest.fixture
def run(runtime, monkeypatch):
    r = runtime
    r.human = r.task
    r.queued_human = False
    r.workspaces = {}
    r.reports = []
    r.c.repo.workspace_get = lambda project, task, **kw: deepcopy(r.workspaces.get(task))
    r.command_lock = threading.Lock()

    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']
            trusted.checked()
            assert kwargs['principal'].kind == 'system' and request['inputActor'] == 'virtual_user'
            assert request['inputTurn']['_initiator'] == 'brain'
            with r.command_lock:
                row = r.c.repo.read(trusted.binding['assignmentId'])
                if row['execution']:
                    return SimpleNamespace(value={})
                from lib.turn_lifecycle import LifecycleConflict
                if conv == 'coordinator' and r.queued_human:
                    raise LifecycleConflict('superseded_by_human', 'human first')
                if any(a['conversationId'] == conv and a['status'] in ('pending', 'running') for a in r.attempts.values()):
                    raise LifecycleConflict('lane_busy', 'busy')
                key = row['id']; turn, attempt, task = 'turn-' + key, 'attempt-' + key, 'task-' + key
                trusted.bind_execution(turn, attempt)
                r.turns[turn] = {'turnId': turn, 'conversationId': conv, 'actor': 'assistant',
                    'projectionRevision': 1, 'projection': {'content': 'verified evidence'}, 'createdAt': 1_001_000}
                r.attempts[attempt] = {'attemptId': attempt, 'conversationId': conv, 'turnId': turn,
                    'taskId': task, 'status': 'running', 'createdAt': 1_001_000}
                r.calls.append(conv)
            return SimpleNamespace(value={})

        def abort_project_attempt(self, attempt, owner):
            assert owner == 7
            if r.attempts[attempt]['status'] in ('pending', 'running'):
                r.attempts[attempt]['status'] = 'interrupted'

    r.service = ProjectDispatchService(r.c.repo, commands=Commands(),
        turn_get=r.c.repo.turn_get, attempt_get=r.c.repo.attempt_get)
    monkeypatch.setattr('lib.conversations.coordinator_workers.ProjectDispatchService', lambda repo: r.service)
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', lambda repo: r.service)
    names = CONTRACT['continuationTools'] + ['run_command', 'read_files', 'write_file']
    capture_worker_policy(r.task, r.task['config'], [{'function': {'name': name}} for name in names], 'model-a')

    class Reports:
        def publish(self, scope, args):
            if args not in r.reports:
                r.reports.append(args)
            return {'turnId': 'report-limit'}

    r.wake = lambda: continuation.recover_coordinator(r.c.repo, service=r.service, reports=Reports())
    r.call = lambda name, **args: json.loads(execute_coordinator_tool(r.task, name, args))
    return r


def delegate(r, key='one'):
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': key})
    assert result.get('dispatch') == 'accepted', result
    return r.c.repo.read(result['assignment_id'])


def controls(r):
    return [row for row in r.c.repo.list() if row['request']['profile']['mode'] == 'coordinator']


def end(r, row, status='completed', delivered=True):
    attempt = r.attempts[row['execution']['attemptId']]
    attempt['status'] = status
    if delivered:
        r.workspaces[attempt['taskId']] = {'task_id': attempt['taskId'], 'user_id': 7, 'project_root': '/project',
            'origin': {admission.KEY: r.c.repo.admission(row['id']), 'delivery': {'state': 'delivered'}}}


def activate(r):
    row = controls(r)[-1]
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']))
    config = trusted.capture({}, 7, trusted.principal, 'coordinator')
    config['_turnOwnerUserId'] = 7
    r.task = {'id': r.attempts[row['execution']['attemptId']]['taskId'], 'convId': 'coordinator',
        '_userId': 7, 'config': config, '_principalContext': trusted.principal.to_payload(), '_attended': False,
        '_turnId': row['execution']['turnId'], '_attemptId': row['execution']['attemptId'],
        'status': 'running', 'abort_event': threading.Event(), 'usage': {}}
    admission.bind_account(r.task)
    capture_worker_policy(r.task, config, [{'function': {'name': 'get_project_workers'}}], 'forged-model')
    return row


def report(r, kind='delivery'):
    r.turns['human-report'] = {'turnId': 'human-report', 'conversationId': 'coordinator', 'actor': 'assistant',
        'kind': 'coordinator_report.' + kind, 'createdAt': 1_002_000,
        'projection': {'content': 'Verified results', 'origin': {'initiator': 'brain'}}}
    return {'report_turn_id': 'human-report', 'outcome': 'complete' if kind == 'delivery' else 'needs_user'}


def ready(r, status='completed'):
    worker = delegate(r)
    end(r, worker, status)
    assert r.wake() == []  # A live original human task owns follow-through.
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    return worker


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted', 'truncated'])
def test_one_outcome_wakes_exactly_one_ordinary_system_turn(run, status):
    r = run; worker = ready(r, status)
    assert r.wake() == [] and len(controls(r)) == 1
    row = controls(r)[0]
    assert row['request']['delegation'] == worker['request']['delegation']
    assert row['grant']['subject'] == 'user:7'
    assert row['request']['coordinatorId'] == row['request']['recipientId'] == 'coordinator'
    assert row['request']['profile']['tools'] == CONTRACT['continuationTools']


def test_continuation_can_delegate_and_review_without_becoming_human(run):
    r = run; first = ready(r); control = activate(r)
    assert not admission.requires_isolated_workspace(r.task)
    assert r.task['_principalContext']['kind'] == 'system' and not r.task['_attended']
    assert r.task['_coordinatorWorkerPolicy'].model == 'model-a'
    assert 'write_file' in r.task['_coordinatorWorkerPolicy'].tools
    replay = delegate(r)
    assert replay['id'] == first['id'] and len(r.created) == 1
    second = delegate(r, 'second')
    assert second['request']['delegation'] == first['request']['delegation']
    review = r.call('review_project_work', assignment_id=first['id'], result_revision=1, decision='accept', note='Verified')
    assert review['review']['actor'] == 'coordinator', review
    page = r.call('get_project_workers', limit=1)
    assert len(page['workers']) == 1 and page['next_cursor'] == first['id']
    assert r.call('get_project_workers', after_id=page['next_cursor'])['workers'][0]['assignment_id'] == second['id']
    from lib.project_dispatch.scope import admit_tool
    from lib.llm.llm_errors import AbortedError
    assert admit_tool(r.task, 'get_project_workers', {}, '/project') is None
    with pytest.raises(AbortedError):
        admit_tool(r.task, 'write_file', {'path': 'escape', 'content': 'x'}, '/project')
    assert r.task['_principalContext']['kind'] == 'system'


def test_pending_birth_reuses_grant_and_respects_human_lane(run):
    r = run; worker = delegate(r); end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'; r.queued_human = True
    assert r.wake() == ['deferred']
    first = controls(r)[0]
    assert first['execution'] is None and r.wake() == ['deferred'] and len(controls(r)) == 1
    r.queued_human = False
    assert r.wake() == ['accepted'] and controls(r)[0]['id'] == first['id']


@pytest.mark.parametrize('violation', ['stop', 'rebind', 'source', 'owner'])
def test_original_authority_withdrawal_cannot_wake(run, violation):
    r = run; worker = delegate(r); end(r, worker); r.attempts['coordinator-attempt']['status'] = 'completed'
    if violation == 'stop':
        from lib.project_dispatch.repository import record_stop
        record_stop(7, 'coordinator', 1001, client=r.c.client)
    if violation == 'rebind': r.c.binding.value['revision'] = 2
    if violation == 'source': r.turns['human-turn']['actor'] = 'virtual_user'
    if violation == 'owner': r.c.conversations.rows['coordinator']['user_id'] = 8
    assert r.wake() == ['deferred'] and not controls(r)


def test_deleted_first_worker_does_not_disable_request_continuation(run):
    r = run; worker = delegate(r); end(r, worker)
    r.c.conversations.rows.pop(worker['request']['recipientId'])
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']


def test_repeated_empty_turns_stop_once_with_unread_report_and_no_more_model_calls(run):
    r = run; ready(r)
    for _ in range(2):
        end(r, controls(r)[-1], delivered=False)
        assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    assert r.wake() == ['stopped'] and len(controls(r)) == 3 and len(r.reports) == 1
    assert r.reports[0]['kind'] == 'blocked' and r.wake() == []
    value, _ = r.c.repo._load()
    assert value['coordinatorSettlements'][0]['reason'] == 'no_progress'


def test_waiting_for_active_worker_is_not_no_progress(run):
    r = run; first = delegate(r); second = delegate(r, 'second')
    end(r, first); r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']; end(r, controls(r)[-1], delivered=False)
    assert r.wake() == [] and not r.reports
    end(r, second)
    assert r.wake() == ['accepted'] and len(controls(r)) == 2


def test_finish_requires_exact_review_delivery_and_published_report(run):
    r = run; first = ready(r); activate(r); args = report(r)
    assert 'Unfinished/unreviewed' in r.call('finish_project_request', **args)['error']
    reviewed = r.call('review_project_work', assignment_id=first['id'], result_revision=1, decision='accept', note='Verified')
    assert reviewed['review']['decision'] == 'accept'
    task = r.attempts[first['execution']['attemptId']]['taskId']
    r.workspaces[task]['origin']['delivery']['state'] = 'blocked'
    assert 'delivery' in r.call('finish_project_request', **args)['error']
    r.workspaces[task]['origin']['delivery']['state'] = 'delivered'
    r.turns[first['execution']['turnId']]['projectionRevision'] = 2
    assert 'exact result' in r.call('finish_project_request', **args)['error']
    r.turns[first['execution']['turnId']]['projectionRevision'] = 1
    done = r.call('finish_project_request', **args)
    assert done['reason'] == 'complete' and r.call('finish_project_request', **args) == done
    assert 'settled' in r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'late'})['error']
    end(r, controls(r)[-1]); assert r.wake() == []


@pytest.mark.parametrize('violation', ['kind', 'origin', 'age', 'id'])
def test_finish_cannot_substitute_an_unrelated_report(run, violation):
    r = run; ready(r); activate(r); args = report(r, 'decision')
    value = r.turns['human-report']
    if violation == 'kind': value['kind'] = 'message'
    if violation == 'origin': value['projection']['origin'] = {'initiator': 'human'}
    if violation == 'age': value['createdAt'] = 1
    if violation == 'id': value['turnId'] = 'wrong'
    assert 'durable report' in r.call('finish_project_request', **args)['error']


def test_cancel_failed_worker_then_finish_and_replay_is_safe(run):
    r = run; failed = ready(r, 'failed'); activate(r)
    for _ in range(2):
        cancelled = r.call('cancel_project_work', assignment_id=failed['id'])
        assert cancelled['revoked'], cancelled
    done = r.call('finish_project_request', **report(r))
    assert done['reason'] == 'complete'


def test_deadline_exhaustion_is_reported_without_new_grants(run):
    r = run; worker = delegate(r); r.attempts['coordinator-attempt']['status'] = 'completed'
    r.c.now[0] = worker['request']['budget']['deadline'] + 1
    assert r.wake() == ['stopped'] and not controls(r) and len(r.reports) == 1
    assert r.wake() == []


def test_settlement_receipts_are_immutable_and_per_source_unique(run):
    r = run; ready(r); activate(r)
    assert r.call('finish_project_request', **report(r, 'decision'))['reason'] == 'needs_user'
    with pytest.raises(ProjectDispatchError, match='immutable'):
        r.c.repo._change(lambda value: value['coordinatorSettlements'].clear())
    with pytest.raises(ProjectDispatchError):
        r.c.repo._change(lambda value: value['coordinatorSettlements'].append(deepcopy(value['coordinatorSettlements'][0])))


def test_human_api_cannot_mint_internal_control_profile(run):
    r = run; ready(r)
    with pytest.raises(ProjectDispatchError, match='issued'):
        r.c.repo.authorize(controls(r)[0]['request'], r.c.principal, 'forged-control')


def test_simultaneous_wakes_share_one_grant_and_birth(run):
    from concurrent.futures import ThreadPoolExecutor
    r = run; worker = delegate(r); end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    gate = threading.Barrier(4)
    def wake():
        gate.wait(3)
        return r.wake()
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: wake(), range(4)))
    assert len(controls(r)) == 1 and r.calls.count('coordinator') == 1, outcomes
    assert r.wake() == []


def test_finish_rechecks_new_worker_inside_same_grant_cas(run, monkeypatch):
    r = run; worker = ready(r); activate(r)
    assert 'error' not in r.call('review_project_work', assignment_id=worker['id'], result_revision=1, decision='accept', note='Verified')
    change = r.c.repo._change
    inserted = []
    def race(update, **options):
        if not inserted:
            inserted.append(True)
            delegate(r, 'parallel-work')
        return change(update, **options)
    monkeypatch.setattr(r.c.repo, '_change', race)
    assert 'Unfinished/unreviewed' in r.call('finish_project_request', **report(r))['error']
    assert not r.c.repo._load()[0].get('coordinatorSettlements')


def test_unknown_paid_effects_keep_full_reservation_across_recovery(run, monkeypatch):
    from tests.project_mod.test_project_dispatch_repository import model_attempt
    r = run
    monkeypatch.setattr('runtime_guards.task_concurrency_hard_ceiling', lambda: 10)
    workers = [delegate(r, str(i)) for i in range(8)]
    r.attempts['coordinator-attempt']['status'] = 'completed'
    for row in workers:
        r.c.repo.mutate(row['id'], lambda value: value['modelAttempts'].append(model_attempt(value['id'])))
        end(r, row, 'interrupted', delivered=False)
    assert r.wake() == ['stopped'] and not controls(r) and r.reports[0]['kind'] == 'blocked'
    assert all(row['modelAttempts'][0]['phase'] == 'dispatch_intent' for row in r.c.repo.list())


def test_cancel_active_worker_withdraws_grant_and_aborts_exact_attempt(run):
    r = run; done = delegate(r); active = delegate(r, 'active')
    end(r, done); r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']; activate(r)
    cancelled = r.call('cancel_project_work', assignment_id=active['id'])
    assert cancelled['revoked'] and r.attempts[active['execution']['attemptId']]['status'] == 'interrupted'
    assert r.attempts[controls(r)[0]['execution']['attemptId']]['status'] == 'running'
    assert 'error' in r.call('cancel_project_work', assignment_id=controls(r)[0]['id'])


def test_failed_settlement_only_wakes_coordinator_and_leaves_human_queue_alone(monkeypatch):
    from lib import turn_lifecycle, message_queue
    from lib.project_dispatch import recovery
    calls = []
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(threading, 'Thread', Thread)
    def forbidden(*args, **kwargs): pytest.fail('Failed turn must not drain ordinary queues')
    monkeypatch.setattr(message_queue, 'get_queue_depth', forbidden)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued', forbidden)
    monkeypatch.setattr(message_queue, 'dispatch_completed_background_batch', forbidden)
    monkeypatch.setattr(recovery, 'recover_conversation_dispatch', lambda *a, **kw: calls.append((a, kw)))
    turn_lifecycle._drain_queue_after_settlement({'_userId': 7}, 'failed-worker', 'failed')
    assert calls == [(('failed-worker',), {'user_id': 7, 'coordinator_only': True})]


@pytest.mark.parametrize('source', ['human', 'continuation'])
def test_superseded_input_does_not_restart_old_request(run, source):
    r = run
    if source == 'human':
        worker = delegate(r); end(r, worker)
        r.attempts['coordinator-attempt']['status'] = 'superseded'
    else:
        ready(r); end(r, controls(r)[-1], 'superseded', delivered=False)
    before = len(controls(r))
    assert r.wake() == [] and len(controls(r)) == before


def test_idle_callbacks_create_no_execution_threads_or_durable_growth(run):
    import time
    r = run; ready(r)
    before = json.dumps(r.c.repo._load()[0], ensure_ascii=False, separators=(',', ':')).encode()
    births, writes = len(r.calls), r.c.client.puts
    threads = {thread.ident for thread in threading.enumerate()}
    started = time.perf_counter()
    for _ in range(50):
        assert r.wake() == []
    elapsed = time.perf_counter() - started
    after = json.dumps(r.c.repo._load()[0], ensure_ascii=False, separators=(',', ':')).encode()
    assert before == after and len(r.calls) == births and r.c.client.puts == writes
    assert {thread.ident for thread in threading.enumerate()} <= threads
    print(f'Coordinator idle recovery: 50 callbacks, {elapsed:.3f}s, {len(after)} retained bytes, 0 births/writes/new threads')


@pytest.mark.parametrize('narrowed', [False, True])
def test_issued_continuation_core_is_direct_with_exact_grant_ceiling(run, narrowed):
    from tests.tasks_pkg.test_coordinator_tool_surface import assemble, names, wire
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS

    r = run
    if narrowed:
        # Freeze a narrower ORIGINAL request policy, not an edited continuation
        # flag: promotion must not invent a missing messaging grant later.
        controls = set(CONTRACT['continuationTools']) - {'send_project_message', 'consider_project_message'}
        capture_worker_policy(r.task, r.task['config'], [
            {'function': {'name': name}} for name in controls | {'run_command', 'read_files'}], 'model-a')
    ready(r)
    row = activate(r)
    tools = assemble(r.task)
    expected = set(row['request']['profile']['tools']) - {'search_tools', 'execute_tools'}
    assert expected <= COORDINATOR_TOOLS
    assert names(r.task['_executable_tool_catalog']) == names(tools) == expected
    assert set(r.task['_requiredDirectToolNames']) == expected
    for round_num in (1, 2):
        body = wire(r.task, tools, round_num=round_num)
        assert expected <= names(body['tools'])
        assert not names(body['tools']) - set(row['request']['profile']['tools'])
        assert body['_programmatic_tool_calling'] == 'off'
    assert r.task['_principalContext']['kind'] == 'system' and not r.task['_attended']
