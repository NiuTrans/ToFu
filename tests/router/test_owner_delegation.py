"""The owner's delegation lets the Global Router act, never speak, as the owner (ROUTER.md R9).

Owner decision 2026-11-06. The grant lives in the lifecycle approval gate,
the single decision point for live-server restarts: only the owner acting in
the Tofu UI grants it, any owner request revokes it at once, and every action
it covers needs an owner trigger (the owner's message of the current request,
an owner message steered into the reply, or an active standing instruction),
a reason and one audit record. The server restart is only ever mocked here.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib import lifecycle_approval as gate
from lib.identity import PrincipalContext
from lib.lifecycle_approval import DelegationRefused
from lib.router import forwarding
from lib.router.instructions import RouterInstructions
from tests.router.test_standing_instructions import INSTRUCTION, OWNER, r as standing

__all__ = ('standing',)

pytestmark = pytest.mark.unit

OWNER_WORDS = '把 Tofu 服务器重启一下，前端要先构建成功。'
ROUTER = {'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None}


def human(owner=OWNER, credential='session', browser='fetch-metadata'):
    """The provenance only routes/api_v1/auth.human_ui_provenance mints."""
    return {'kind': 'human-ui', 'ownerUserId': owner, 'subject': 'session-key', 'credential': credential,
            'browser': browser, 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    return tmp_path


@pytest.fixture
def d(standing, approvals, monkeypatch):
    """The standing-instruction fixture plus a live router attempt and mocked restart owners."""
    s = standing
    s.turns['owner-turn']['projection'] = {'content': 'restart the server', 'originalContent': OWNER_WORDS}
    owner = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)
    s.task.update(_turnId='router-turn', _attemptId='router-attempt', _attended=True,
                  _principalContext=owner.to_payload())
    s.events, s.running, s.prepare_error = [], [], ''
    monkeypatch.setattr('lib.router.owner_delegation._live_router_attempt',
                        lambda task, scope: s.events.append(('live-router-attempt',)))

    def running(exclude_conv_id=None, *, user_id=None):
        s.events.append(('protect', exclude_conv_id, user_id))
        if isinstance(s.running, Exception):
            raise s.running
        return list(s.running.pop(0) if s.running and isinstance(s.running[0], list) else s.running)
    monkeypatch.setattr('lib.tasks_pkg.manager.list_running_tasks', running)

    def prepare():
        s.events.append(('prepare',))
        return s.prepare_error
    monkeypatch.setattr('lib.server.server_reexec.prepare_server_reexec_frontend', prepare)

    def perform(reason, **kwargs):
        s.events.append(('perform', reason, kwargs))
        return True
    monkeypatch.setattr('lib.server.server_reexec.perform_server_reexec', perform)

    def human_gate(*args, **kwargs):  # the owner's own approval prompts are never touched
        raise AssertionError('owner delegation must not create, decide or consume lifecycle approvals')
    for name in ('create_request', 'decide', 'consume', 'consume_any', 'consume_restart'):
        monkeypatch.setattr(gate, name, human_gate)
    s.detached = []
    monkeypatch.setattr('lib.router.owner_delegation._start_detached', s.detached.append)

    def run_detached():
        while s.detached:
            s.detached.pop(0)()
    s.run_detached = run_detached
    s.grant = lambda **grantee: gate.set_delegation(OWNER, True, grantee={**ROUTER, **grantee},
                                                    provenance=human())
    s.revoke = lambda: gate.set_delegation(OWNER, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': OWNER, 'credential': 'header'})
    s.restart = lambda **args: s.call('restart_server', **{key: value for key, value in {
        'reason': 'The owner asked for a restart', 'source_turn_id': 'owner-turn',
        'idempotency_key': 'restart-1', **args}.items() if value is not None})
    s.audit = lambda: gate.delegated_actions(OWNER)
    s.names = lambda: [event[0] for event in s.events]
    return s


def _approval_document(approvals):
    return json.loads((approvals / 'lifecycle_approvals.json').read_text())


def test_without_the_grant_a_restart_is_a_typed_refusal_and_nothing_is_pending(d, approvals):
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_not_granted'
    assert refused.value.execution_started is False
    assert 'Act for me' in str(refused.value) and 'No approval request was created' in str(refused.value)
    assert d.events == [] and d.detached == []
    # No pending approval for the owner to click (create_request would have failed
    # the fixture), and without a grant there is no delegated action to show.
    assert not (approvals / 'lifecycle_approvals.json').exists()
    assert d.audit() == []
    # A refusal never consumes the idempotency key: the same call works once granted.
    d.grant()
    assert d.restart()['status'] == 'accepted'


def test_a_granted_restart_passes_the_gate_runs_the_sanctioned_path_detached_and_is_audited(d, approvals):
    grant = d.grant()
    assert grant['state'] == 'granted' and grant['revision'] == 1 and grant['assurance'] == 'authenticated'
    result = d.restart()
    assert result['status'] == 'accepted' and result['execution_started'] is True
    # Accepted before anything runs: the restart outlives the calling turn.
    assert d.events == [('protect', 'router', None)] and len(d.detached) == 1
    [pending] = d.audit()
    assert pending['id'] == result['action_id'] and pending['status'] == 'accepted'
    d.run_detached()
    # Frontend preparation first; the old server stops only after it succeeded,
    # through the shared restart owner of Settings \u2192 restart.
    assert d.names() == ['protect', 'prepare', 'protect', 'perform']
    _, reason, kwargs = d.events[-1]
    assert reason == 'restart' and kwargs['frontend_prevalidated'] is True
    assert isinstance(kwargs['requested_at'], float)
    [action] = d.audit()
    assert action['status'] == 'restarting' and action['acceptedAt'] >= action['requestedAt']
    assert action['requester'] == {**ROUTER, 'turnId': 'router-turn', 'attemptId': 'router-attempt'}
    assert action['reason'] == 'The owner asked for a restart'
    assert action['trigger'] == {'kind': 'owner_message', 'turnId': 'owner-turn', 'text': OWNER_WORDS}
    assert action['grantRevision'] == grant['revision'] and action['grantedBy'] == human()
    assert action['target'] == {'kind': 'server'} and action['interrupted'] == []
    # The gate stamped the restart in the same transaction (cooldown authority).
    assert isinstance(_approval_document(approvals)['last_restart_at'], float)


def test_running_conversations_refuse_unless_the_owner_asked_to_interrupt_them(d):
    d.grant()
    d.running = [{'taskId': 'worker-task', 'convId': 'worker-a', 'elapsed': 9.0}]
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'restart_would_interrupt' and 'worker-a' in str(refused.value)
    [refusal] = d.audit()
    assert refusal['status'] == 'refused' and refusal['code'] == 'restart_would_interrupt'
    assert d.detached == []
    accepted = d.restart(interrupt_running_conversations=True, idempotency_key='restart-anyway')
    assert accepted['status'] == 'accepted'
    d.run_detached()
    assert d.names()[-1] == 'perform'
    restarted = next(item for item in d.audit() if item['id'] == accepted['action_id'])
    assert restarted['status'] == 'restarting' and restarted['interrupted'] == ['worker-a']


@pytest.mark.parametrize('case', ['protected-step', 'activity-unavailable'])
def test_a_protected_step_or_unreadable_activity_refuses_even_when_interrupting(d, case):
    d.grant()
    d.running = (RuntimeError('worker_job.active.list unavailable') if case == 'activity-unavailable'
                 else [{'taskId': 'protected-step:git_land-host-42-ab', 'convId': '', 'elapsed': 3.0,
                        'protectedStep': 'git_land', 'marker': '/data/protected-steps/git_land.json'}])
    with pytest.raises(DelegationRefused) as refused:
        d.restart(interrupt_running_conversations=True)
    expected = 'restart_protection_unavailable' if case == 'activity-unavailable' else 'restart_protected_step'
    assert refused.value.code == expected
    # The caller's own conversation never counts against itself, exactly as in Settings.
    assert d.events == [('protect', 'router', None)] and d.detached == []
    [action] = d.audit()
    assert action['status'] == 'refused' and action['code'] == expected


def test_a_protected_step_that_starts_during_preparation_stops_the_restart_before_shutdown(d):
    d.grant()
    d.running = [[], [{'taskId': 'protected-step:export-host-7-cd', 'convId': '', 'elapsed': 0.1,
                       'protectedStep': 'export', 'marker': '/data/protected-steps/export.json'}]]
    d.restart(interrupt_running_conversations=True)
    d.run_detached()
    assert d.names() == ['protect', 'prepare', 'protect']
    [action] = d.audit()
    assert action['status'] == 'failed' and action['code'] == 'restart_protected_step'


def test_failed_preparation_keeps_the_old_server_and_reports_why(d):
    d.grant()
    d.prepare_error = 'frontend build failed: vite exited 1'
    d.restart()
    d.run_detached()
    assert d.names() == ['protect', 'prepare']
    [action] = d.audit()
    assert action['status'] == 'failed' and action['code'] == 'restart_preparation_failed'
    assert 'vite exited 1' in action['detail']


def test_a_replayed_restart_never_restarts_twice(d):
    d.grant()
    first = d.restart()
    d.run_detached()
    replay = d.restart()
    assert replay['action_id'] == first['action_id'] and replay['replayed'] is True
    assert replay['status'] == 'restarting'
    assert d.detached == [] and d.names().count('perform') == 1 and len(d.audit()) == 1


def test_a_handed_over_restart_reads_completed_once_a_newer_server_answers(d, monkeypatch):
    """The server manager reports no outcome back. Every restart replaces the serving
    worker, so a server generation that booted after the handover proves it happened."""
    from lib import boot_identity
    d.grant()
    first = d.restart()
    d.run_detached()
    [handed_over] = d.audit()
    assert handed_over['status'] == 'restarting'
    monkeypatch.setattr(boot_identity, 'BOOT_ID', 'next-generation')
    monkeypatch.setattr(boot_identity, 'BOOT_TS', handed_over['acceptedAt'] + 1)
    [completed] = d.audit()
    assert completed['status'] == 'completed' and 'next-generation' in completed['detail']
    replay = d.restart()
    assert (replay['action_id'], replay['status'], replay['replayed']) == (first['action_id'], 'completed', True)
    assert replay['execution_started'] is True and d.names().count('perform') == 1


def test_delegated_session_control_is_recorded_and_acts_through_the_shared_command(d):
    """Continue and Stop under the grant: a refused trigger is recorded and runs nothing;
    an accepted one runs the shared session command as the owner's delegation, without
    the owner_delegation argument, and settles its record."""
    from lib.router.owner_delegation import delegated_conversation_control
    calls = []

    def control(task, scope, arguments, *, acting_principal):
        calls.append((dict(arguments), acting_principal))
        return {'turn': None, 'attempt': None}
    args = {'conversation_id': 'elsewhere', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1',
            'action': 'continue', 'expected_revision': 3, 'idempotency_key': 'after-restart',
            'owner_delegation': {'reason': 'The restart interrupted it', 'source_turn_id': 'event-turn'}}
    with pytest.raises(DelegationRefused) as refused:
        delegated_conversation_control(d.task, d.scope, args, control=control)
    assert refused.value.code == 'owner_delegation_not_granted' and d.audit() == []
    d.grant()
    with pytest.raises(ValueError):
        delegated_conversation_control(d.task, d.scope, {**args, 'action': 'regenerate'}, control=control)
    with pytest.raises(DelegationRefused) as refused:
        delegated_conversation_control(d.task, d.scope, args, control=control)
    assert refused.value.code == 'owner_delegation_trigger_invalid' and calls == []
    target = {'kind': 'conversation', 'conversationId': 'elsewhere', 'turnId': 'turn-1',
              'attemptId': 'attempt-1', 'action': 'continue'}
    [refusal] = d.audit()
    assert (refusal['status'], refusal['scope'], refusal['target']) == ('refused', 'conversation_control', target)
    accepted = {**args, 'owner_delegation': {'reason': 'The owner asked', 'source_turn_id': 'owner-turn'}}
    result = delegated_conversation_control(d.task, d.scope, accepted, control=control)
    [(arguments, principal)] = calls
    assert arguments == {key: value for key, value in args.items() if key != 'owner_delegation'}
    assert (principal.kind, principal.owner_user_id) == ('user', OWNER)
    assert principal.subject_id == 'owner-delegation:' + result['owner_delegation_action_id']
    action = d.audit()[0]
    assert (action['id'], action['status'], action['target']) == (
        result['owner_delegation_action_id'], 'completed', target)


def test_revocation_takes_effect_immediately_even_during_preparation(d):
    d.grant()
    d.restart()
    revoked = d.revoke()
    assert revoked['state'] == 'revoked' and revoked['revision'] == 2
    assert revoked['revokedBy']['kind'] == 'authenticated-request'
    d.run_detached()
    assert 'perform' not in d.names()
    [action] = d.audit()
    assert action['status'] == 'refused' and action['code'] == 'owner_delegation_not_granted'
    with pytest.raises(DelegationRefused) as refused:
        d.restart(idempotency_key='restart-2')
    assert refused.value.code == 'owner_delegation_not_granted'


@pytest.mark.parametrize('grantee', [{'conversationId': 'router-old'}, {'bindingRevision': 2}])
def test_only_a_grant_to_the_current_router_binding_authorizes(d, grantee):
    d.grant(**grantee)
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_router_changed'
    assert d.events == []


@pytest.mark.parametrize('provenance', [
    {'kind': 'authenticated-request', 'ownerUserId': OWNER, 'credential': 'header'},
    {'kind': 'explicit-user-instruction-via-codex-chat'},
    {**human(), 'browser': ''},
    {**human(), 'credential': 'header'},
    human(owner=OWNER + 1),
    None,
])
def test_a_grant_needs_the_owners_own_ui_provenance(approvals, provenance):
    with pytest.raises(PermissionError):
        gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=provenance)
    assert gate.delegation(OWNER)['state'] == 'revoked'


def test_open_mode_grants_are_recorded_as_advisory_and_owner_scoped(approvals):
    granted = gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=human(credential='open-mode',
                                                                                 browser='origin'))
    assert granted['assurance'] == 'advisory' and granted['grantedBy']['credential'] == 'open-mode'
    # Granting again is idempotent: the record and its provenance stay as first granted.
    assert gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=human()) == granted
    assert gate.delegation(OWNER + 1)['state'] == 'revoked'
    with pytest.raises(DelegationRefused):
        gate.check_delegation(OWNER + 1, {**ROUTER, 'turnId': None, 'attemptId': None}, 'restart')


def test_a_tampered_grant_record_authorizes_nothing(d, approvals):
    d.grant()
    document = _approval_document(approvals)
    document['delegations'][str(OWNER)]['grantedBy'] = {'kind': 'explicit-user-instruction-via-codex-chat'}
    (approvals / 'lifecycle_approvals.json').write_text(json.dumps(document))
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_not_granted'


def test_an_owner_message_trigger_must_be_the_current_request(d):
    d.grant()
    with pytest.raises(DelegationRefused) as refused:
        d.restart(source_turn_id='event-turn')
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    d.source = PermissionError('Synthetic input cannot authorize new workers')
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    with pytest.raises(ValueError):
        d.restart(source_turn_id=None)
    with pytest.raises(ValueError):
        d.restart(reason='')
    assert d.events == []
    # Under the grant a refused trigger is still a recorded use; a malformed call is not a use.
    assert [(item['status'], item['code'], item['trigger']) for item in d.audit()] == [
        ('refused', 'owner_delegation_trigger_invalid', None)] * 2


def test_a_standing_instruction_authorizes_a_nonhuman_turn_and_a_paused_one_does_not(d):
    d.turns['owner-turn']['projection'] = {'content': 'translated', 'originalContent': INSTRUCTION}
    instruction = d.create()
    d.grant()
    d.source = PermissionError('the current input is a channel event, not the owner')
    d.task['_principalContext'] = PrincipalContext.system(
        subject_id='router-notice', owner_user_id=OWNER, scopes={'chat'}).to_payload()
    arguments = {'source_turn_id': None, 'instruction_id': instruction['instructionId'],
                 'event_id': 'dx:msg:77'}
    RouterInstructions(OWNER).pause(instruction['instructionId'])
    with pytest.raises(DelegationRefused) as refused:
        d.restart(**arguments)
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    RouterInstructions(OWNER).resume(instruction['instructionId'])
    assert d.restart(**arguments)['status'] == 'accepted'
    assert ('live-router-attempt',) in d.events
    assert d.audit()[0]['trigger'] == {'kind': 'standing_instruction', 'turnId': 'owner-turn',
                                       'instructionId': instruction['instructionId'], 'eventId': 'dx:msg:77',
                                       'text': INSTRUCTION}


def test_an_owner_message_steered_into_the_reply_is_a_trigger(d, monkeypatch):
    d.grant()
    d.source = PermissionError('the reply was started by a report relay')

    def steer(task, scope, steer_id):
        assert steer_id == 'steer-1'
        return {'conversationId': 'router', 'turnId': 'router-turn', 'actor': 'human',
                'projection': {'content': OWNER_WORDS}}
    monkeypatch.setattr(forwarding, '_current_steer_source', steer)
    assert d.restart(source_turn_id=None, source_steer_id='steer-1')['status'] == 'accepted'
    assert d.audit()[0]['trigger'] == {'kind': 'owner_steer', 'turnId': 'router-turn', 'steerId': 'steer-1',
                                       'text': OWNER_WORDS}


def test_only_the_global_router_holds_the_owner_delegation(d):
    d.grant()
    from lib.router.tools import execute_router_tool
    with pytest.raises(PermissionError):
        execute_router_tool(d.task, {**d.scope, 'project_path': '/work/project'}, 'restart_server',
                            {'reason': 'x', 'source_turn_id': 'owner-turn', 'idempotency_key': 'k'})
    assert d.events == []


def test_workers_never_receive_the_owner_delegation_tools():
    from lib.project_dispatch.contract import CONTRACT
    from lib.router.policy import ROUTER_ONLY_TOOLS, ROUTER_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    assert 'restart_server' in ROUTER_ONLY_TOOLS and 'restart_server' not in COORDINATOR_TOOLS
    assert 'restart_server' in CONTRACT['workerDeniedTools']
    assert not {name for name in ROUTER_TOOLS if 'grant' in name or 'delegation' in name}


# -- The boundary: the grant lets the Router act as the owner, never speak as the owner. --

def test_with_the_grant_router_text_still_never_becomes_the_owners_words(d):
    """Answering for the owner means forwarding words the owner did not write; the
    grant lets no nonhuman turn forward, record standing authority or create workers."""
    d.grant()
    d.source = PermissionError('Synthetic input cannot authorize new workers')
    with pytest.raises(PermissionError):
        d.call('route_to_project', project_path=d.project, source_turn_id='owner-turn')
    with pytest.raises(PermissionError):
        d.call('router_instruction_create', source_turn_id='owner-turn', label='Self-made',
               idempotency_key='self')
    assert d.calls == []


def test_with_the_grant_a_synthetic_request_still_cannot_start_workers(d, monkeypatch):
    """Irreversible publications (for example pushing an edition to GitHub) run in
    Workers or behind owner approvals; only a genuine owner request starts one."""
    d.grant()
    from lib.project_dispatch.delegation import source_for_task
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope', lambda task: dict(d.scope))
    monkeypatch.setattr('lib.project_dispatch.continuation.current_continuation',
                        lambda task, **kwargs: None)
    d.task['_principalContext'] = PrincipalContext.system(
        subject_id='router-notice', owner_user_id=OWNER, scopes={'chat'}).to_payload()
    with pytest.raises(PermissionError, match='Synthetic input'):
        source_for_task(SimpleNamespace(owner=OWNER, project=d.scope['project_path']), d.task)


def test_with_the_grant_owner_prompts_still_need_the_owner(d, monkeypatch):
    """Channel drafts and thread policy stay genuine-human-turn decisions, and a
    lifecycle approval request still needs the owner's own UI decision."""
    d.grant()
    from lib.channels import tools as channel_tools
    from lib.channels.model import ChannelError
    monkeypatch.setattr('lib.channels.delivery.is_global_router', lambda owner, conversation: True)
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', lambda conversation, turn, **kwargs: {
        'conversationId': 'router', 'turnId': turn, 'actor': 'assistant', 'parentTurnId': 'event-turn'}
        if turn == 'router-turn' else {'conversationId': 'router', 'turnId': turn, 'actor': 'human',
                                       'projection': {'content': 'bug', 'origin': {'blockId': 'o',
                                                                                   'initiator': 'channel'}}})
    for name, args in (('channel_outbox_approve', {'item_id': 'draft-1', 'decision': 'approve'}),
                       ('channel_configure_thread', {'thread': 'dx:gid:1', 'mode': 'watch'})):
        with pytest.raises(ChannelError) as refused:
            channel_tools.execute(name, args, d.task)
        assert refused.value.code == 'human_turn_required'


def test_an_agent_cannot_approve_a_lifecycle_request(approvals):
    record = gate.create_request('restart', origin={'ua': 'router'})
    for provenance in ({'kind': 'explicit-user-instruction-via-codex-chat'}, None,
                       {**human(), 'browser': ''}):
        with pytest.raises(PermissionError):
            gate.decide(record['id'], True, provenance=provenance)
    assert gate.get(record['id'])['status'] == 'pending'
    decided = gate.decide(record['id'], True, provenance=human())
    assert decided['status'] == 'approved' and decided['decided_via'] == human()
    assert decided['decided_assurance'] == 'authenticated'
