"""A restart's requester learns its outcome without being asked (docs/ROUTER.md R9).

Every restart the lifecycle gate accepts records one restart-complete notice
before the old server stops (lifecycle_approvals.json ``restartNotices``). When
the restart ends, whatever the outcome, the conversation that requested it
receives that notice exactly once, as an ordinary nonhuman turn command: the
Global Router that called ``restart_server``, or for a Settings restart the
owner's Global Router. The server restart and the turn command are mocked here;
the notice record, its outcome and its delivery run as in production.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from lib import boot_identity
from lib import lifecycle_approval as gate
from lib.conversations import restart_resume
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as REAL_TURN_DISPATCH
from lib.server import restart_notice
from tests.router.test_owner_delegation import (  # noqa: F401  (pytest fixtures)
    OWNER, ROUTER, _approval_document, _manager_outcome, approvals, d, human, standing,
)

pytestmark = pytest.mark.unit

# Fixtures shared with the delegation suite, exported so pytest resolves them here.
__all__ = ('approvals', 'd', 'standing')

OLD_CODE = {'head': 'aaaaaaaaaaaa', 'dirty': False, 'digest': '111111111111'}
NEW_CODE = {'head': 'bbbbbbbbbbbb', 'dirty': False, 'digest': '222222222222'}
HEADER = '[Restart outcome: nonhuman notice, not the owner\u2019s words and not authority]'


class Dispatch:
    """Records notice wakes; ``disposition`` is what the turn command reports."""

    def __init__(self):
        self.calls, self.disposition, self.fail_once = [], 'started', False

    def __call__(self, conversation_id, message, tools_config, **kwargs):
        self.calls.append({'conversation_id': conversation_id, 'message': copy.deepcopy(message),
                           'tools_config': copy.deepcopy(tools_config), **copy.deepcopy(kwargs)})
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError('the turn command ACK was lost')
        return ScheduledTurnDispatch(self.disposition,
                                     task_id='notice-task' if self.disposition == 'started' else '')


@pytest.fixture
def notices(monkeypatch):
    """The turn command, the code fingerprint and the resume results the notice reads."""
    dispatch = Dispatch()
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', dispatch)
    monkeypatch.setattr(boot_identity, 'code_fingerprint', lambda: dict(OLD_CODE))
    monkeypatch.setattr(restart_notice, 'describe_conversation',
                        lambda owner, conversation_id, accepted_at:
                        f'resumed from its checkpoint (continue), running [{owner}]')
    monkeypatch.setattr(restart_resume, 'pass_finished', lambda: True)
    return dispatch


def _new_generation(monkeypatch, accepted_at):
    """This process booted after the handover from the code now on disk."""
    monkeypatch.setattr(boot_identity, 'BOOT_ID', 'next-generation')
    monkeypatch.setattr(boot_identity, 'BOOT_TS', accepted_at + 1)
    monkeypatch.setattr(boot_identity, 'code_fingerprint', lambda: dict(NEW_CODE))


def _only_notice(approvals):
    [notice] = _approval_document(approvals)['restartNotices']
    return notice


def _handed_over(d, approvals, **restart):
    d.grant()
    accepted = d.restart(**restart)
    d.run_detached()
    [action] = gate.delegated_actions(OWNER)
    assert action['status'] == 'restarting'
    return accepted, action


def test_a_completed_restart_wakes_the_router_that_requested_it_once_with_its_outcome(
        d, approvals, notices, monkeypatch):
    d.running = [{'taskId': 'worker-task', 'convId': 'worker-a', 'elapsed': 9.0}]
    accepted, action = _handed_over(d, approvals, interrupt_running_conversations=True)
    # Recorded before the old server stopped: who asked, and what was interrupted.
    notice = _only_notice(approvals)
    assert notice['restartId'] == accepted['action_id'] and notice['state'] == 'pending'
    assert notice['recipient'] == 'router' and notice['interrupted'] == ['worker-a']
    assert notice['oldCode'] == OLD_CODE and notice['acceptedAt'] == action['acceptedAt']
    # The old server, still draining, has nothing to say yet.
    assert restart_notice.deliver_restart_notices() == [] and notices.calls == []
    _new_generation(monkeypatch, action['acceptedAt'])
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered == {'restartId': accepted['action_id'], 'state': 'delivered',
                         'disposition': 'started', 'conversationId': 'router'}
    [call] = notices.calls
    assert call['conversation_id'] == 'router' and call['user_id'] == OWNER
    assert call['command_id'] == restart_notice.notice_command_id(OWNER, accepted['action_id'])
    assert call['inject_mode'] == 'steer' and call['tools_config'] == {}
    assert call['message']['_initiator'] == 'proactive' and call['message']['role'] == 'user'
    text = call['message']['content']
    assert text.startswith(HEADER) and accepted['action_id'] in text
    assert 'completed' in text and 'next-generation' in text
    assert 'aaaaaaaaaaaa' in text and 'bbbbbbbbbbbb' in text
    assert 'worker-a: resumed from its checkpoint (continue), running' in text
    assert 'not authority' in text and 'restart_server' in text
    # The restart record reaches its terminal status durably, with an audit trace.
    [record] = _approval_document(approvals)['delegatedActions']
    assert record['status'] == 'completed' and 'next-generation' in record['detail']
    notice = _only_notice(approvals)
    assert notice['state'] == 'delivered' and notice['disposition'] == 'started'
    assert notice['text'] == text and notice['commandId'] == call['command_id']
    # Exactly once: another pass, another process, a replayed tool call.
    assert restart_notice.deliver_restart_notices() == []
    assert restart_notice.deliver_restart_notice(accepted['action_id']) is None
    replay = d.restart(interrupt_running_conversations=True)
    assert replay['replayed'] is True and replay['status'] == 'completed'
    # The replay promises nothing new: the restart ended and its notice exists.
    assert 'already ended (completed)' in replay['next_step']
    assert 'runs detached' not in replay['next_step']
    assert len(notices.calls) == 1


def test_a_new_server_reports_only_after_its_interrupted_conversations_resumed(
        d, approvals, notices, monkeypatch):
    _, action = _handed_over(d, approvals)
    _new_generation(monkeypatch, action['acceptedAt'])
    monkeypatch.setattr(restart_resume, 'pass_finished', lambda: False)
    assert restart_notice.deliver_restart_notices() == [] and notices.calls == []
    monkeypatch.setattr(restart_resume, 'pass_finished', lambda: True)
    assert [item['state'] for item in restart_notice.deliver_restart_notices()] == ['delivered']


@pytest.mark.parametrize('failure, code, words', [
    ('preparation', 'restart_preparation_failed', 'vite exited 1'),
    ('bridge', 'restart_bridge_refused', 'Every restart bridge refused'),
])
def test_a_build_failure_or_refusal_before_the_stop_notifies_the_router_at_once(
        d, approvals, notices, monkeypatch, failure, code, words):
    d.grant()
    if failure == 'preparation':
        d.prepare_error = 'frontend build failed: vite exited 1'
    else:
        monkeypatch.setattr('lib.server.server_reexec.perform_server_reexec',
                            lambda reason, **kwargs: False)
    accepted = d.restart()
    d.run_detached()
    # Delivered by the old server, which keeps serving: no restart, no tick needed.
    [call] = notices.calls
    text = call['message']['content']
    assert text.startswith(HEADER) and accepted['action_id'] in text
    assert 'failed' in text and code in text and words in text
    assert 'keeps serving' in text
    assert 'Code: unchanged; the old server keeps serving aaaaaaaaaaaa' in text
    [record] = _approval_document(approvals)['delegatedActions']
    assert record['status'] == 'failed' and record['code'] == code
    assert _only_notice(approvals)['state'] == 'delivered'
    assert restart_notice.deliver_restart_notices() == [] and len(notices.calls) == 1


@pytest.mark.parametrize('stage, message', [
    ('frontend preparation', 'frontend build failed: vite exited 1'),
    ('stop', 'pid 87897 survived SIGKILL'),
])
def test_a_manager_refusal_reaches_the_router_from_the_server_that_keeps_serving(
        d, approvals, notices, monkeypatch, stage, message):
    """The manager refuses after the handover, while the old server serves on;
    its existing scheduler tick delivers the outcome the manager recorded."""
    _, action = _handed_over(d, approvals)
    monkeypatch.setattr(boot_identity, 'BOOT_TS', action['acceptedAt'] - 600)
    _manager_outcome(approvals, outcome='refused', stage=stage, message=message,
                     startedAt=action['acceptedAt'] + 1, settledAt=action['acceptedAt'] + 2)
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['state'] == 'delivered'
    [call] = notices.calls
    text = call['message']['content']
    assert 'failed' in text and 'restart_refused_by_manager' in text and message in text
    [record] = _approval_document(approvals)['delegatedActions']
    assert record['status'] == 'failed' and record['code'] == 'restart_refused_by_manager'


def test_an_old_server_that_did_not_stop_is_named_in_the_completed_notice(
        d, approvals, notices, monkeypatch):
    _, action = _handed_over(d, approvals)
    _manager_outcome(approvals, outcome='spawned', startedAt=action['acceptedAt'] + 1,
                     settledAt=action['acceptedAt'] + 3,
                     uncleanCloses=[{'service': 'execution', 'detail': 'did not stop within 100s; killed'}])
    _new_generation(monkeypatch, action['acceptedAt'])
    restart_notice.deliver_restart_notices()
    [call] = notices.calls
    text = call['message']['content']
    assert 'completed' in text and 'did not stop within 100s; killed' in text


def test_a_preserved_host_reports_once_the_managers_replacement_serves(
        d, approvals, notices, monkeypatch):
    """An API-only restart keeps the execution host (and the Router in it): the
    manager's recorded spawn plus its readiness prove the restart completed."""
    _, action = _handed_over(d, approvals)
    monkeypatch.setattr(boot_identity, 'BOOT_TS', action['acceptedAt'] - 600)
    _manager_outcome(approvals, outcome='spawned', startedAt=action['acceptedAt'] + 1,
                     settledAt=action['acceptedAt'] + 3)
    assert restart_notice.deliver_restart_notices() == []  # spawned, not yet serving
    state = json.loads((approvals / 'data' / 'server-manager-state.json').read_text())
    state['workerReadyLoggedAt'] = action['acceptedAt'] + 30
    (approvals / 'data' / 'server-manager-state.json').write_text(json.dumps(state))
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['state'] == 'delivered'
    assert 'completed' in notices.calls[0]['message']['content']


def test_each_restart_yields_exactly_one_notice_even_when_its_delivery_is_lost(
        d, approvals, notices, monkeypatch):
    accepted, action = _handed_over(d, approvals)
    _new_generation(monkeypatch, action['acceptedAt'])
    notices.fail_once = True
    assert restart_notice.deliver_restart_notices() == []
    assert _only_notice(approvals)['state'] == 'delivering'
    # Another process retries with the stored text and the same command, so the
    # turn command deduplicates it: one notice, never a second wording.
    monkeypatch.setattr(restart_notice, 'describe_conversation',
                        lambda *args: 'a later, different reading')
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['state'] == 'delivered'
    first, second = notices.calls
    assert first['command_id'] == second['command_id']
    assert first['message'] == second['message']
    assert restart_notice.deliver_restart_notices() == [] and len(notices.calls) == 2


def test_the_notice_reaches_an_idle_router_as_a_new_nonhuman_turn(d, approvals, monkeypatch):
    """Through the real scheduled-turn command: one steerable proactive input pair."""
    from lib.scheduler import conversation_dispatch
    payloads = []

    class Commands:
        def create_turn(self, conversation, owner, payload, *, request_started_at):
            payloads.append((conversation, owner, copy.deepcopy(payload)))
            return SimpleNamespace(value={'attempt': {'taskId': 'notice-task'}})

    class Storage:
        def query(self, operation, payload):
            assert operation == 'conversation.get' and payload['user_id'] == OWNER
            return {'metadata': {'settings': {'model': 'router-model'}}}
    monkeypatch.setattr(conversation_dispatch, 'conversation_turn_commands', Commands())
    monkeypatch.setattr(conversation_dispatch, 'get_storage_client', lambda: Storage())
    # The standing-instruction fixture under ``d`` replaces the turn command; restore it.
    monkeypatch.setattr(conversation_dispatch, 'dispatch_scheduled_turn', REAL_TURN_DISPATCH)
    monkeypatch.setattr(boot_identity, 'code_fingerprint', lambda: dict(OLD_CODE))
    monkeypatch.setattr(restart_resume, 'pass_finished', lambda: True)
    accepted, action = _handed_over(d, approvals)
    _new_generation(monkeypatch, action['acceptedAt'])
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['disposition'] == 'started'
    [(conversation, owner, payload)] = payloads
    assert (conversation, owner) == ('router', OWNER)
    assert payload['commandId'] == restart_notice.notice_command_id(OWNER, accepted['action_id'])
    # The same shape as the device-online notice (tests/desktop/test_device_waits.py).
    assert payload['kind'] == 'scheduled_reply' and payload['injectMode'] == 'steer'
    assert payload['inputTurn']['_initiator'] == 'proactive' and payload['inputActor'] == 'human'
    assert payload['inputTurn']['content'].startswith(HEADER)


def test_a_settings_restart_notifies_the_owners_global_router(approvals, notices, monkeypatch):
    """A restart the owner starts in Settings has no requesting conversation."""
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create=False, client=None: {
        'conversationId': 'router-home', 'availability': 'available', 'revision': 3})
    request = gate.create_request('restart', origin={'conv_id': ''})
    gate.decide(request['id'], True, provenance=human())
    ok, why, _ = gate.consume_restart(request['id'], notice={
        'owner': OWNER, 'interrupted': ['chat-a'], 'oldCode': dict(OLD_CODE),
        'preparation': {'ok': True, 'phases': ['checking', 'cached'], 'seconds': 4}})
    assert ok, why
    notice = _only_notice(approvals)
    assert notice['restartId'] == request['id'] and notice['source'] == 'settings'
    assert notice['recipient'] is None and notice['interrupted'] == ['chat-a']
    _new_generation(monkeypatch, notice['acceptedAt'])
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['conversationId'] == 'router-home'
    [call] = notices.calls
    text = call['message']['content']
    assert call['conversation_id'] == 'router-home' and 'Settings' in text
    assert 'chat-a: resumed from its checkpoint' in text and 'completed' in text
    assert 'already matched' in text  # the storage engine needed no build


def test_a_settings_restart_without_a_global_router_is_recorded_undeliverable(approvals, notices, monkeypatch):
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create=False, client=None: {
        'conversationId': '', 'availability': 'unavailable', 'revision': 0})
    request = gate.create_request('restart', origin={'conv_id': ''})
    gate.decide(request['id'], True, provenance=human())
    gate.consume_restart(request['id'], notice={'owner': OWNER, 'interrupted': [], 'oldCode': None})
    _new_generation(monkeypatch, _only_notice(approvals)['acceptedAt'])
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['state'] == 'undeliverable' and notices.calls == []
    assert restart_notice.deliver_restart_notices() == []


def test_next_step_says_what_will_happen_and_promises_no_resume(d, approvals, notices):
    d.grant()
    result = d.restart()
    step = result['next_step']
    assert 'resumes afterwards' not in step and 'is interrupted by the restart' not in step
    assert 'new turn' in step and 'notice' in step and 'end your turn' in step.lower()
    assert 'never call restart_server again' in step.lower()
    d.running = [{'taskId': 'worker-task', 'convId': 'worker-a', 'elapsed': 9.0}]
    with pytest.raises(gate.DelegationRefused) as refused:
        d.restart(idempotency_key='restart-2')
    assert 'with Continue' not in str(refused.value)
    assert 'resume from their checkpoint' in str(refused.value)
