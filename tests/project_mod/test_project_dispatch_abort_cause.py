"""Dispatch authority stops settle with a typed non-user cause; a Stop stays user_abort.

A project-dispatch task halts through the abort lane when its grant is revoked
or when its admission carrier or authority can no longer be re-established.
The durable terminal record must name that cause (failed, ``external_authority``
evidence, a non-retryable typed envelope) instead of an unexplained user Stop,
and the task must not reach another provider/tool call or retry.
"""
import pytest

from lib.llm.llm_errors import AbortedError, ToolAdmissionRefused
from lib.project_dispatch import admission
from lib.tasks_pkg.turn_retry import should_auto_retry_turn
from lib.turn_verdict import TurnStatus, TurnTerminationEvidence, derive_task_verdict
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_project_dispatch_runtime import _lease, runtime as runtime

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('context', 'runtime')

pytestmark = pytest.mark.unit


def _settle_fatal(monkeypatch, task, error):
    import lib.tasks_pkg.orchestrator._post_loop as post
    events = []
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.stamp_chat_task_terminal', lambda *a, **kw: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage', lambda task: None)
    monkeypatch.setattr(post, '_maybe_auto_retry_turn', lambda *a: pytest.fail('authority stop retried'))
    monkeypatch.setattr(post, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(post, 'persist_task_result', lambda task: None)
    assert post.handle_task_fatal(task, error) is False
    return events[-1]


def _durable(task, event):
    from lib.turn_lifecycle import _settlement
    return _settlement(task, event, {'content': task.get('content') or '', 'toolRounds': []})


def _assert_settled(task, event, cause):
    verdict = derive_task_verdict(task, event)
    assert verdict.status is TurnStatus.FAILED and verdict.cause == cause
    assert verdict.evidence is TurnTerminationEvidence.EXTERNAL_AUTHORITY
    status, settlement = _durable(task, event)
    assert status == 'failed'
    assert (settlement['outcome'], settlement['cause'], settlement['evidence']) == (
        'failed', cause, 'external_authority')
    error = settlement['error']
    assert error['kind'] == cause and error['retryable'] is False
    assert should_auto_retry_turn(error, 0, {})[0] is False
    return settlement


def test_revocation_seen_at_model_dispatch_settles_grant_revoked(runtime):
    from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
    r = runtime; t = r.task(); admission.bind_account(t)
    lease = _lease(t)
    r.c.repo.revoke(r.assignment['id'], 1)
    with pytest.raises(AbortedError):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == []
    # The cooperative halt is raised where the authority failed, before any send.
    assert t['aborted'] is True and t['abort_event'].is_set()
    assert t['_abort_reason'] == 'dispatch_grant_revoked'
    # The round loop adopts the abort and exits; finalization names the cause.
    finish = _settle_post_loop_finish_reason(
        t, 'stop', loop_exit_reason='user_abort', abort_detected_phase='llm_round_0',
        model='worker-model', tid='worker')
    assert finish == 'error'
    _assert_settled(t, {'type': 'done', 'finishReason': finish, 'error': t['error']},
                    'dispatch_grant_revoked')


def test_lost_carrier_at_tool_admission_settles_authority_lost(runtime, monkeypatch):
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    t = runtime.task(); admission.bind_account(t)
    t['config'].pop(admission.KEY)
    with pytest.raises(ToolAdmissionRefused, match='carrier') as refused:
        _execute_tool_one(t, {}, 'run_command', 'tc', {'command': 'pwd'}, 0, {}, {}, None, False)
    assert t['aborted'] is True and t['_abort_reason'] == 'dispatch_authority_lost'
    done = _settle_fatal(monkeypatch, t, refused.value)
    assert t['status'] == 'error' and done['finishReason'] == 'error'
    assert done['error']['kind'] == 'dispatch_authority_lost'
    assert '`run_command`' in done['error']['detail'] and 'carrier' in done['error']['detail']
    _assert_settled(t, done, 'dispatch_authority_lost')


def test_revoked_grant_at_tool_admission_settles_grant_revoked(runtime, monkeypatch):
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    t = runtime.task(); admission.bind_account(t)
    runtime.c.repo.revoke(runtime.assignment['id'], 1)
    with pytest.raises(ToolAdmissionRefused, match='Grant revoked') as refused:
        _execute_tool_one(t, {}, 'run_command', 'tc', {'command': 'pwd'}, 0, {}, {}, None, False)
    assert t['aborted'] is True and t['_abort_reason'] == 'dispatch_grant_revoked'
    done = _settle_fatal(monkeypatch, t, refused.value)
    assert '`run_command`' in done['error']['detail']
    settlement = _assert_settled(t, done, 'dispatch_grant_revoked')
    # The refusal names the assignment and its Coordinator conversation.
    coordinator = runtime.assignment['request']['coordinatorId']
    for detail in (t['_abortReasonDetail'], settlement['error']['detail']):
        assert runtime.assignment['id'] in detail and coordinator in detail


@pytest.mark.parametrize('failure, cause, message', [
    ('revoked', 'dispatch_grant_revoked', 'revocation refuses'),
    ('integrity', 'dispatch_authority_lost', 'durably acknowledged'),
])
def test_intent_record_failure_after_the_presend_check_names_its_cause(
        runtime, monkeypatch, failure, cause, message):
    from lib.storage.errors import StorageError
    r = runtime; t = r.task(); admission.bind_account(t)
    lease = _lease(t)
    mutate, calls = r.c.repo.mutate, []

    def intent_record(assignment_id, callback, **kwargs):
        # The pre-send check passed; the grant changes or the record fails in the CAS.
        calls.append(assignment_id)
        if len(calls) == 1 and failure == 'integrity':
            raise StorageError('database_integrity', 'dispatch record corrupt')
        if len(calls) == 1:
            r.c.repo.revoke(r.assignment['id'], 1)
        return mutate(assignment_id, callback, **kwargs)
    monkeypatch.setattr(r.c.repo, 'mutate', intent_record)
    with pytest.raises(AbortedError, match=message):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    assert r.c.repo.read(r.assignment['id'])['modelAttempts'] == []
    assert t['aborted'] is True and t['_abort_reason'] == cause


def test_unknown_authority_for_a_summary_settles_authority_lost(runtime, monkeypatch):
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    t = runtime.task(); admission.bind_account(t)
    monkeypatch.setattr(runtime.c.repo, 'validate_own', lambda *a, **kw: (_ for _ in ()).throw(
        OSError('dispatch storage unavailable')))
    with pytest.raises(AbortedError, match='auxiliary'):
        summary_model_admission(t)
    assert t['_abort_reason'] == 'dispatch_authority_lost'
    done = _settle_fatal(monkeypatch, t, AbortedError('summary admission refused'))
    _assert_settled(t, done, 'dispatch_authority_lost')


def test_revoked_grant_at_summary_admission_settles_grant_revoked(runtime):
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    t = runtime.task(); admission.bind_account(t)
    runtime.c.repo.revoke(runtime.assignment['id'], 1)
    with pytest.raises(AbortedError, match='auxiliary'):
        summary_model_admission(t)
    assert t['_abort_reason'] == 'dispatch_grant_revoked'


@pytest.mark.parametrize('revoke_after_stop', [False, True])
def test_user_stop_still_settles_user_abort(runtime, monkeypatch, revoke_after_stop):
    r = runtime; t = r.task(); admission.bind_account(t)
    # A person's Stop is the first cause; a later dead grant cannot rename it.
    t['aborted'] = True
    t['abort_event'].set()
    if revoke_after_stop:
        r.c.repo.revoke(r.assignment['id'], 1)
    with pytest.raises(AbortedError):
        admission.validate_task(t)
    assert '_abort_reason' not in t
    done = _settle_fatal(monkeypatch, t, AbortedError('Stopped by the user'))
    assert t['status'] == 'aborted' and t['error'] is None and done['finishReason'] == 'aborted'
    verdict = derive_task_verdict(t, done)
    assert verdict.status is TurnStatus.INTERRUPTED and verdict.cause == 'user_abort'
    status, settlement = _durable(t, done)
    assert status == 'interrupted' and settlement['cause'] == 'user_abort'
    assert settlement['error'] is None


def test_failed_authority_probe_ends_the_probed_task(runtime):
    t = runtime.task()
    with pytest.raises(AbortedError, match='bound conserved'):
        admission.validate_task(t)
    assert t['aborted'] is True and t['_abort_reason'] == 'dispatch_authority_lost'
    with pytest.raises(AbortedError):
        admission.bind_account(t)


def test_request_activity_row_reports_the_authority_stop():
    from lib.tasks_pkg.manager._stream import _abort_request_attribution
    for reason in ('dispatch_grant_revoked', 'dispatch_authority_lost'):
        status, kind, detail = _abort_request_attribution(
            {'id': 't', '_abort_reason': reason}, AbortedError('User aborted'))
        assert (status, kind) == ('failed', reason)
        assert reason in detail and 'not a manual stop' in detail
    _, _, detail = _abort_request_attribution(
        {'id': 't', '_abort_reason': 'dispatch_grant_revoked',
         '_abortReasonDetail': 'Revoked by Coordinator conversation coordinator-x.'},
        AbortedError('User aborted'))
    assert detail.endswith('Revoked by Coordinator conversation coordinator-x.')
    assert _abort_request_attribution({'id': 't'}, AbortedError('User aborted')) == (
        'aborted', None, None)


def test_revocation_cancel_port_records_the_revoked_cause(monkeypatch):
    import lib.conversation_sync.command_service as module
    calls = []
    monkeypatch.setattr(module, 'abort_attempt',
                        lambda attempt, **kw: calls.append((attempt, kw)) or {'status': 'abort_signaled'})
    commands = module.ConversationTurnCommandService(
        build_user_message=lambda message, *a: message, was_aborted_after=lambda *a: False,
        start_task=lambda *a, **kw: None)
    commands.abort_project_attempt('worker-attempt', 7, provenance='Revoked by Coordinator c.')
    commands.abort_attempt('human-attempt', 7)
    assert calls == [
        ('worker-attempt', {'user_id': 7, 'withdraw_dispatch': False,
                            'abort_reason': 'dispatch_grant_revoked',
                            'abort_detail': 'Revoked by Coordinator c.'}),
        ('human-attempt', {'user_id': 7}),
    ]


def test_revocation_provenance_reaches_the_runtime_stamp():
    import threading
    from lib.agent_core.task_runtime import TaskRuntime
    runtime_ = TaskRuntime(kind='test')
    task = {'id': 'w', '_userId': 7, 'status': 'running', 'abort_event': threading.Event()}
    runtime_._tasks['w'] = task
    assert runtime_.abort_owned('w', user_id=7, source='turn_attempt_abort',
                                reason='dispatch_grant_revoked',
                                reason_detail='Revoked by Coordinator conversation c.')
    assert task['_abort_reason'] == 'dispatch_grant_revoked'
    assert task['_abortReasonDetail'] == 'Revoked by Coordinator conversation c.'
    # A later stop never restamps the first cause or its provenance.
    assert runtime_.abort_owned('w', user_id=7, source='other', reason='dispatch_authority_lost',
                                reason_detail='later')
    assert task['_abortReasonDetail'] == 'Revoked by Coordinator conversation c.'
