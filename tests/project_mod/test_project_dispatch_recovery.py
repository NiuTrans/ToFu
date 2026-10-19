"""Lifecycle recovery over fake metadata/CAS/executor; no providers or processes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Lock
from types import SimpleNamespace

import pytest

from tests.project_mod.test_project_dispatch_repository import context as context_fixture

context = context_fixture
from lib.project_dispatch import admission, recovery
from lib.project_dispatch.service import ProjectDispatchService
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


@pytest.fixture
def rig(context, monkeypatch):
    """The real service/command identity/birth path, with only IO ports replaced."""
    import lib.conversation_sync.command_service as commands
    from lib.conversation_sync.execution_identity import restore_execution_identity
    from lib.turn_lifecycle import LifecycleConflict
    c = context
    monkeypatch.setattr(admission, 'repository_for', lambda binding: c.repo)
    state = SimpleNamespace(busy=True, human_queued=False, pairs={}, starts=[],
                            births=[], checks=[], stop_at=None, lose_birth_ack=False,
                            lose_service_ack=False, claimed=set(), lock=Lock())

    def stopped(conv, owner, timestamp):
        assert owner == 7
        state.checks.append((conv, timestamp))
        return state.stop_at is not None and state.stop_at > timestamp

    def create_pair(conv, *, command_id, config, input_projection, **kwargs):
        assert conv == 'recipient' and kwargs['user_id'] == 7
        assert kwargs['input_actor'] == 'virtual_user'
        assert kwargs['require_lane_idle'] and kwargs['reject_if_human_queued']
        assert input_projection['_initiator'] == 'brain'
        assert '_goalObjective' not in config
        with state.lock:
            if command_id in state.pairs:
                return deepcopy(state.pairs[command_id])
            if state.human_queued:
                raise LifecycleConflict('superseded_by_human', 'A real human owns priority')
            if state.busy:
                raise LifecycleConflict('lane_busy', 'Existing lane is busy')
            pair = {'_needsStart': True,
                    'turn': {'turnId': 'worker-turn', 'conversationId': conv,
                             'actor': 'assistant', 'kind': 'reply',
                             'projectionRevision': 1, 'projection': {}},
                    'attempt': {'attemptId': 'worker-attempt', 'turnId': 'worker-turn',
                                'conversationId': conv, 'operation': 'create',
                                'status': 'pending', 'config': deepcopy(config)}}
            state.pairs[command_id] = pair
            state.births.append(command_id)
            if state.lose_birth_ack:
                state.lose_birth_ack = False
                raise StorageError('database_timeout', 'Birth committed; ACK lost', True)
            return deepcopy(pair)

    def get_attempt(attempt_id, *, user_id, include_execution_config=False):
        assert attempt_id == 'worker-attempt' and user_id == 7
        row = deepcopy(next(iter(state.pairs.values()))['attempt'])
        if not include_execution_config:
            row.pop('config', None)
        return row

    def get_turn(conv, turn_id, *, user_id):
        assert (conv, turn_id, user_id) == ('recipient', 'worker-turn', 7)
        return deepcopy(next(iter(state.pairs.values()))['turn'])

    def claim(attempt_id, *, user_id):
        with state.lock:
            if attempt_id in state.claimed:
                return False
            state.claimed.add(attempt_id)
            return True

    def bind(attempt_id, task_id, *, user_id):
        next(iter(state.pairs.values()))['attempt'].update(taskId=task_id, status='running')
        return {'taskId': task_id}

    def start(conv, config, data, watermark, registered):
        principal, attended = restore_execution_identity(config)
        assert principal.kind == 'system' and principal.scopes == {'chat'}
        assert principal.owner_user_id == 7 and attended is False
        assert config[admission.KEY]['assignmentId'] in state.births[0]
        assert watermark == 1000
        registered('fake-executor-task')
        state.starts.append((conv, principal.to_payload(), deepcopy(config)))
        return 'fake-executor-task', None

    monkeypatch.setattr(commands, 'create_turn_pair', create_pair)
    monkeypatch.setattr(commands, 'get_attempt', get_attempt)
    monkeypatch.setattr(commands, 'get_turn', get_turn)
    monkeypatch.setattr(commands, 'get_conversation_revision', lambda *a, **kw: 1)
    monkeypatch.setattr(commands, 'claim_attempt_start', claim)
    monkeypatch.setattr(commands, 'bind_task', bind)
    c.repo.attempt_get = get_attempt
    command_service = commands.ConversationTurnCommandService(
        build_user_message=lambda value, *a: value, was_aborted_after=stopped,
        start_task=start)
    real_create_turn = command_service.create_turn

    def create_turn(*args, **kwargs):
        result = real_create_turn(*args, **kwargs)
        if state.lose_service_ack:
            state.lose_service_ack = False
            raise StorageError('database_timeout', 'Executor born; response lost', True)
        return result

    monkeypatch.setattr(command_service, 'create_turn', create_turn)
    service = ProjectDispatchService(c.repo, commands=command_service,
                                     turn_get=get_turn, attempt_get=get_attempt)
    assignment = c.repo.authorize(c.request, c.principal, 'existing-human-grant')

    resolve_recovery = recovery.recover_conversation_dispatch
    def recover():
        return resolve_recovery('recipient', user_id=7,
            conversations=c.conversations, repository_factory=c.repository,
            service_factory=lambda repo: service, was_aborted_after=stopped,
            clock=lambda: c.now[0])

    return SimpleNamespace(c=c, state=state, service=service, assignment=assignment,
                           recover=recover, stopped=stopped)


def test_busy_grant_retries_same_assignment_on_idle_without_new_human_grant(rig):
    r = rig
    before = deepcopy(r.c.repo.read(r.assignment['id']))
    assert r.service.dispatch(r.assignment['id'], principal=r.c.principal)['dispatch'] == 'deferred'
    assert r.recover()['deferred'] == 1
    assert r.state.starts == []
    r.state.busy = False
    assert r.recover()['accepted'] == 1
    after = r.c.repo.read(r.assignment['id'])
    assert after['execution'] == {'turnId': 'worker-turn', 'attemptId': 'worker-attempt'}
    assert after['grant'] == before['grant'] and after['request'] == before['request']
    assert after['modelAttempts'] == []
    assert r.state.births == ['project-dispatch:' + r.assignment['id']]
    assert r.recover()['skipped'] == 1 and len(r.state.starts) == 1


def test_queued_real_human_wins_atomic_birth_even_if_callback_observed_idle(rig):
    r = rig; r.state.busy = False; r.state.human_queued = True
    assert r.recover()['deferred'] == 1
    assert r.c.repo.read(r.assignment['id'])['execution'] is None
    assert r.state.starts == []
    r.state.human_queued = False
    assert r.recover()['accepted'] == 1


@pytest.mark.parametrize('where', ['lose_birth_ack', 'lose_service_ack'])
def test_lost_ack_recovery_never_buys_a_second_birth(rig, where):
    r = rig; r.state.busy = False; setattr(r.state, where, True)
    assert r.recover()['failed'] == 1
    second = r.recover()
    assert second['accepted' if where == 'lose_birth_ack' else 'skipped'] == 1
    assert len(r.state.starts) == len(r.state.births) == 1
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == []


def test_duplicate_callbacks_share_existing_stable_command_and_start_claim(rig):
    r = rig; r.state.busy = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: r.recover(), range(2)))
    assert sum(row['accepted'] + row['skipped'] for row in outcomes) == 2
    assert len(r.state.starts) == len(r.state.births) == 1


@pytest.mark.parametrize('state', ['revoked', 'expired', 'executed'])
def test_ineligible_grants_skip_without_executor_or_ledger_replenishment(rig, state):
    r = rig; r.state.busy = False
    if state == 'revoked':
        r.c.repo.revoke(r.assignment['id'], 1)
    elif state == 'expired':
        r.c.now[0] = r.c.request['budget']['deadline']
    else:
        r.c.repo.bind_execution(r.assignment['id'], 'already-turn', 'already-attempt')
    before = deepcopy(r.c.client.rows)
    assert r.recover()['skipped'] == 1
    assert r.c.client.rows == before and r.state.starts == []


def test_newer_stop_revokes_existing_deferred_grant_even_after_watermark_disappears(rig):
    r = rig; r.state.busy = False; r.state.stop_at = 1001
    assert r.recover()['aborted'] == 1
    assert r.c.repo.read(r.assignment['id'])['grant']['revoked'] is True
    r.state.stop_at = None
    assert r.recover()['skipped'] == 1 and r.state.starts == []


def test_stop_between_recovery_probe_and_birth_revokes_same_existing_grant(rig, monkeypatch):
    r = rig; r.state.busy = False
    dispatch = r.service.dispatch_authorized
    def stopped_dispatch(binding):
        r.state.stop_at = 1001  # original grant predates Stop, callback does not
        return dispatch(binding)
    monkeypatch.setattr(r.service, 'dispatch_authorized', stopped_dispatch)
    assert r.recover()['aborted'] == 1
    assert r.c.repo.read(r.assignment['id'])['grant']['revoked'] is True
    r.state.stop_at = None
    assert r.recover()['skipped'] == 1
    assert r.state.births == r.state.starts == []


def test_older_stop_does_not_invalidate_a_later_explicit_grant(rig):
    r = rig; r.state.busy = False; r.state.stop_at = 999
    assert r.recover()['accepted'] == 1
    assert ('coordinator', 1000) in r.state.checks
    assert ('recipient', 1000) in r.state.checks


@pytest.mark.parametrize('change', ['coordinator', 'recipient', 'source', 'deadline'])
def test_trusted_service_rechecks_current_scope_and_deadline(rig, change):
    r = rig; r.state.busy = False
    if change == 'coordinator':
        r.c.binding.value['revision'] += 1
        r.c.binding.value['conversationId'] = 'rebound'
    elif change == 'recipient':
        r.c.conversations.rows['recipient']['settings']['projectPath'] = '/elsewhere'
    elif change == 'source':
        r.c.turn['projectionRevision'] += 1
    else:
        r.c.repo.clock = lambda: 2001  # expired after recovery's initial clock probe
    outcome = r.recover()
    if change == 'recipient':
        # Current metadata moved the lane to another project: do not scan the
        # former project's record from an old task/config scope.
        assert outcome['scanned'] == 0
    else:
        assert outcome['failed'] == 1
    assert r.state.starts == []


def test_one_failed_grant_does_not_starve_next_and_scan_is_bounded(context):
    c = context
    assignments = [c.repo.authorize(c.request, c.principal, f'grant-{i}')
                   for i in range(128)]
    calls = []
    def dispatch(binding):
        calls.append(binding)
        c.repo.validate(binding)
        if binding['assignmentId'] == assignments[0]['id']:
            raise RuntimeError('one unavailable grant')
        return {'dispatch': 'deferred'}
    before = deepcopy(c.client.rows)
    result = recovery.recover_project_dispatch(7, '/project', repository=c.repo,
        service=SimpleNamespace(dispatch_authorized=dispatch),
        was_aborted_after=lambda *a: False, clock=lambda: 1000)
    assert result['scanned'] == 128 and result['failed'] == 1 and result['deferred'] == 127
    assert len(calls) == 128 and c.client.rows == before


def test_only_current_owner_canonical_project_and_idle_recipient_are_scanned(context):
    c = context
    c.repo.authorize(c.request, c.principal, 'current')
    other = deepcopy(c.request); other['recipientId'] = 'other-lane'
    c.conversations.rows['other-lane'] = deepcopy(c.conversations.rows['recipient'])
    c.repo.authorize(other, c.principal, 'other-lane')
    c.conversations.rows['recipient']['settings'] = {
        'canonicalProjectPath': '/project', 'projectPath': '/old-worktree'}
    scopes, bindings = [], []
    def repository(owner, project):
        scopes.append((owner, project)); return c.repo
    def dispatch(binding):
        bindings.append(binding); return {'dispatch': 'deferred'}
    result = recovery.recover_conversation_dispatch('recipient', user_id=7,
        conversations=c.conversations, repository_factory=repository,
        service_factory=lambda repo: SimpleNamespace(dispatch_authorized=dispatch),
        was_aborted_after=lambda *a: False, clock=lambda: 1000)
    assert scopes == [(7, '/project')]
    assert result['scanned'] == 2 and result['deferred'] == result['skipped'] == 1
    assert [b['recipientId'] for b in bindings] == ['recipient']
    assert c.conversations.calls[-1] == ('recipient', 7, False)


@pytest.mark.parametrize('missing', ['conversation', 'project', 'owner'])
def test_unscoped_idle_callback_is_inert(context, missing):
    c = context
    if missing == 'conversation':
        c.conversations.rows.pop('recipient')
    elif missing == 'project':
        c.conversations.rows['recipient']['settings'] = {}
    owner = 8 if missing == 'owner' else 7
    result = recovery.recover_conversation_dispatch('recipient', user_id=owner,
        conversations=c.conversations,
        repository_factory=lambda *a: pytest.fail('Unscoped callback scanned records'))
    assert result['scanned'] == 0


def test_project_recovery_refuses_wrong_owner_scope_and_oversized_record(context):
    c = context
    with pytest.raises(PermissionError):
        recovery.recover_project_dispatch(8, '/project', repository=c.repo)
    repo = SimpleNamespace(owner=7, project='/project', storage_version=1, list=lambda: [None] * 129)
    with pytest.raises(ValueError, match='capacity'):
        recovery.recover_project_dispatch(7, '/project', repository=repo,
            service=SimpleNamespace(), was_aborted_after=lambda *a: False)


def _settle_old_turn(rig, monkeypatch, *, human_task=None, applied=True, cancelled=False):
    """Ride real record_task_event -> committed settlement -> existing drain."""
    import threading
    import lib.turn_lifecycle as lifecycle
    import lib.message_queue as queue
    r = rig
    committed, order = [], []

    class Client:
        def command(self, operation, payload, command_id, **kwargs):
            assert operation == 'turn.event.record'
            assert payload['terminal']
            committed.append(deepcopy(payload))
            order.append('durable-settlement')
            r.state.busy = not applied
            return {'applied': applied, 'projection_revision': 2}

    class ImmediateThread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()

    monkeypatch.setattr(threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(lifecycle, '_turn_client', lambda **kw: Client())
    monkeypatch.setattr(lifecycle, 'get_attempt', lambda *a, **kw: {
        'status': 'running', 'conversationId': 'recipient', 'turnId': 'old-turn'})
    monkeypatch.setattr(lifecycle, 'get_turn', lambda *a, **kw: {
        'conversationId': 'recipient', 'turnId': 'old-turn', 'actor': 'assistant',
        'projectionRevision': 1, 'projection': {}})
    monkeypatch.setattr(queue, 'get_queue_depth', lambda *a, **kw: int(bool(human_task)))
    def human(*a, **kw):
        assert committed[-1]['status'] == 'completed'
        order.append('real-human-first'); return human_task
    monkeypatch.setattr(queue, 'dispatch_next_queued', human)
    monkeypatch.setattr(queue, 'dispatch_completed_background_batch', lambda *a, **kw: None)
    def recover(conv, *, user_id):
        assert (conv, user_id) == ('recipient', 7)
        assert committed[-1]['status'] == ('interrupted' if cancelled else 'completed')
        order.append('trusted-recovery'); return r.recover()
    monkeypatch.setattr(recovery, 'recover_conversation_dispatch', recover)
    task = {'id': 'old-task', '_attemptId': 'old-attempt', '_turnId': 'old-turn',
            'convId': 'recipient', '_userId': 7, 'status': 'done',
            'content': 'ordinary completed work', 'thinking': '', 'toolRounds': [],
            'segments': [], 'model': 'model-a', 'config': {'model': 'model-a'},
            '_transientRuntime': True, 'finishReason': 'stop'}
    if cancelled:
        task.update(aborted=True, finishReason='user_abort')

        r.state.stop_at = 1001
    result = lifecycle.record_task_event(task, {'type': 'done', 'finishReason': task['finishReason']})
    return result, committed, order


def test_real_shared_settlement_triggers_retry_only_after_durable_commit(rig, monkeypatch):
    result, committed, order = _settle_old_turn(rig, monkeypatch)
    assert result is True and committed[0]['status'] == 'completed'
    assert order == ['durable-settlement', 'trusted-recovery']
    assert len(rig.state.starts) == 1


def test_real_shared_settlement_prefers_queued_human(rig, monkeypatch):
    result, _, order = _settle_old_turn(rig, monkeypatch, human_task='real-human-task')
    assert result is True and order == ['durable-settlement', 'real-human-first']
    assert rig.state.starts == []


def test_rejected_settlement_cannot_wake_deferred_grant(rig, monkeypatch):
    result, _, order = _settle_old_turn(rig, monkeypatch, applied=False)
    assert result is False and order == ['durable-settlement']
    assert rig.state.starts == []


def test_stopped_turn_settlement_does_not_revive_deferred_grant(rig, monkeypatch):
    _, committed, order = _settle_old_turn(rig, monkeypatch, cancelled=True)
    assert committed[0]['status'] == 'interrupted'
    assert order == ['durable-settlement', 'trusted-recovery']
    assert rig.state.starts == []
    assert rig.c.repo.read(rig.assignment['id'])['grant']['revoked'] is True


def test_read_view_and_import_do_not_trigger_recovery(rig, monkeypatch):
    r = rig
    monkeypatch.setattr(recovery, 'recover_conversation_dispatch',
                        lambda *a, **kw: pytest.fail('Read triggered recovery'))
    before = deepcopy(r.c.client.rows)
    assert r.service.view(r.assignment['id'], principal=r.c.principal)['execution'] is None
    assert len(r.service.list(principal=r.c.principal)) == 1
    assert r.c.client.rows == before and r.state.starts == []


def test_stop_withdrawal_survives_runtime_fence_expiry(context):
    from contextlib import nullcontext
    from lib.project_dispatch.recovery import revoke_conversation_dispatch, recover_project_dispatch
    c = context
    grant = c.repo.authorize(c.request, c.principal, 'before-stop')
    count = revoke_conversation_dispatch('recipient', user_id=7, occurred_at=1001,
        conversations=c.conversations, repository_factory=lambda owner, project: c.repo,
        lock_factory=lambda project: nullcontext(), stop_client=c.client)
    assert count == 1 and c.repo.read(grant['id'])['grant']['revoked'] is True
    stats = recover_project_dispatch(7, '/project', repository=c.repo,
        service=SimpleNamespace(dispatch_authorized=lambda binding: pytest.fail('Stopped grant revived')),
        was_aborted_after=lambda *a: False, clock=lambda: 1002)
    assert stats['skipped'] == 1


def test_boot_discovers_known_authority_only_with_bounded_record_cursor(context):
    from lib.project_dispatch.recovery import recover_persisted_dispatch
    c = context
    c.repo.authorize(c.request, c.principal, 'new-grant')
    calls=[]; scopes=[]
    value = next(iter(c.client.rows.values()))['value']
    def query(operation, payload, **kwargs):
        calls.append((operation, payload))
        assert operation == 'record.list' and payload['limit'] == 4
        return [{'key': c.repo.key, 'value': deepcopy(value)}] if payload['after_key'] == '' else []
    client = SimpleNamespace(query=query)
    assert recover_persisted_dispatch(client=client,
        repository_factory=lambda owner, project: c.repo,
        recovery=lambda owner, project, **kw: scopes.append((owner, project))) == 1
    assert scopes == [(7, '/project')] and len(calls) == 2
    assert calls[1][1]['after_key'] == c.repo.key


def test_legacy_authority_readable_but_not_automatically_promoted(context):

    from lib.project_dispatch.recovery import recover_project_dispatch
    c=context
    grant=c.repo.authorize(c.request,c.principal,'legacy')
    stored=next(iter(c.client.rows.values()))['value']
    stored['assignments'][0]['grant'].pop('lifecycleVersion')
    assert c.repo.read(grant['id'])['grant'].get('lifecycleVersion') is None
    stats=recover_project_dispatch(7,'/project',repository=c.repo,
        service=SimpleNamespace(dispatch_authorized=lambda binding: pytest.fail('Legacy grant silently activated')),
        was_aborted_after=lambda *a: False,clock=lambda:1000)
    assert stats['skipped']==1


def test_stop_before_metadata_restore_blocks_old_grant_even_without_runtime_fence(context):
    from contextlib import nullcontext
    from lib.project_dispatch.repository import record_stop
    c = context
    grant = c.repo.authorize(c.request, c.principal, 'old-project-grant')
    binding = c.repo.admission(grant['id'])
    c.conversations.rows['recipient']['settings']['projectPath'] = '/other'
    recovery.revoke_conversation_dispatch('recipient', user_id=7, occurred_at=1001,
        conversations=c.conversations, repository_factory=lambda owner, project: c.repository(7, project),
        lock_factory=lambda project: nullcontext(), stop_client=c.client)
    assert c.repo.read(grant['id'])['grant']['revoked'] is False  # No guessed former project.
    c.conversations.rows['recipient']['settings']['projectPath'] = '/project'
    with pytest.raises(ValueError, match='durable conversation Stop'):
        c.repo.validate(binding)
    stats = recovery.recover_project_dispatch(7, '/project', repository=c.repo,
        service=ProjectDispatchService(c.repo), was_aborted_after=lambda *a: False, clock=lambda: 1002)
    assert stats['accepted'] == 0 and stats['failed'] == 1
    c.now[0] = 1002
    fresh = c.repo.authorize(c.request, c.principal, 'new-human-grant')
    assert c.repo.validate(c.repo.admission(fresh['id']))['id'] == fresh['id']
    record_stop(7, 'recipient', 1000, client=c.client)  # Old ACK retry cannot move watermark backward.
    assert c.repo.validate(c.repo.admission(fresh['id']))['id'] == fresh['id']


def test_stop_watermark_uncertain_commit_conserves_timestamp_and_owner(context):
    from lib.project_dispatch.repository import record_stop, stopped_after
    c = context
    c.client.uncertain_once = True
    assert record_stop(7, 'recipient', 1001, client=c.client) == 1001
    assert stopped_after(7, 'recipient', 1000, client=c.client)
    assert not stopped_after(8, 'recipient', 1000, client=c.client)
    assert record_stop(7, 'recipient', 999, client=c.client) == 1001
    assert not stopped_after(7, 'recipient', 1002, client=c.client)


def test_native_attempt_stop_calls_durable_withdrawal_after_cancel_and_on_terminal_retry(monkeypatch):
    from lib import turn_lifecycle as turns
    row = {'attemptId': 'attempt', 'conversationId': 'recipient', 'turnId': 'turn',
           'taskId': 'task', 'status': 'running'}
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: SimpleNamespace(query=lambda *a, **kw: deepcopy(row)))
    calls = []
    monkeypatch.setattr('lib.tasks_pkg.manager.cancellation.cancel_task',
        lambda *a, **kw: calls.append('cancel') or {'found': True})
    monkeypatch.setattr(turns, '_notify_abort_busy_projection', lambda *a, **kw: None)
    monkeypatch.setattr(recovery, 'revoke_conversation_dispatch',
        lambda conv, **kw: calls.append((conv, kw['user_id'])))
    assert turns.abort_attempt('attempt', user_id=7)['status'] == 'abort_signaled'
    assert calls == ['cancel', ('recipient', 7)]

    calls.clear()
    assert turns.abort_attempt('attempt', user_id=7, withdraw_dispatch=False)['status'] == 'abort_signaled'
    assert calls == ['cancel']  # Revoking one grant must not withdraw its peers.
    row['status'] = 'interrupted'; calls.clear()
    assert turns.abort_attempt('attempt', user_id=7)['alreadyTerminal']
    assert calls == [('recipient', 7)]
