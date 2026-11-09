"""Conversations a restart interrupted resume from their checkpoint by themselves.

docs/RUN_CONTINUITY.md, "Restart-paused conversations resume by themselves".
A stopping server records which conversations it quiesced; the next server
generation continues every main-lane tail Turn the restart settled
``interrupted`` (cause ``server_restart``) through the ordinary resume command,
exactly as the owner's Continue click would, once per interrupted Attempt.
Turns other owners resume (dispatched Workers and Coordinator wakes, Goal/Flow
runs), Turns waiting for the owner's answer, and anything the owner stopped are
never touched. Real Turn transactions on SQL and native authorities; only the
executor start is a seam (no model runs).
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage
from lib import boot_identity
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.conversation_sync.execution_identity import EXECUTION_IDENTITY_CONFIG_KEY, capture_execution_identity
from lib.conversations import restart_resume
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit

__all__ = ('storage',)

OWNER = 1
RESTART = {'verdict': 'clean', 'reason': 'restart', 'restart_storm': False}


@pytest.fixture
def chats(storage, monkeypatch, tmp_path):
    """Running conversations of one owner, a stopping generation and the next one."""
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import runtime

    from lib import lifecycle_approval as gate
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)
    monkeypatch.setattr(boot_identity, 'BOOT_ID', 'stopping-generation')

    def running(conversation, *, content='', thinking='', model='chat-model', **extra):
        config = capture_execution_identity({'model': model, 'projectPath': str(tmp_path),
                                              'thinkingEnabled': True, **extra}, OWNER, principal)
        pair = turns.create_turn_pair(conversation, command_id=conversation + ':input',
            input_projection={'content': 'Keep working on ' + conversation}, config=deepcopy(config),
            user_id=OWNER, conversation_defaults={'allowCreate': True, 'title': conversation,
                                                  'settings': {'projectPath': str(tmp_path)}})
        task_id = conversation + '-task'
        turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=OWNER)
        task = {'id': task_id, 'convId': conversation, '_turnId': pair['turn']['turnId'],
                '_attemptId': pair['attempt']['attemptId'], '_userId': OWNER, '_attended': True,
                '_principalContext': principal.to_payload(), 'config': deepcopy(config),
                'status': 'running', 'model': model, 'content': content, 'thinking': thinking,
                'toolRounds': []}
        turns.record_task_event(task, {'type': 'thinking', 'content': thinking} if thinking
                                else {'type': 'delta', 'content': content})
        return task

    def settle(task, abort_reason):
        task.update(status='aborted', aborted=True, _abort_reason=abort_reason)
        turns.record_task_event(task, {'type': 'aborted'})

    started = []

    def start(conv, cfg, request, abort_after, registered, **kwargs):
        task_id = f'resumed-{len(started)}'
        registered(task_id)
        started.append((conv, deepcopy(cfg)))
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False, start_task=start)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    # The scheduled-turn command (restart notices) binds the service at import.
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.conversation_turn_commands', commands)

    generations = []

    def next_generation():
        generations.append(len(generations) + 1)
        monkeypatch.setattr(boot_identity, 'BOOT_ID', f'generation-{generations[-1]}')
        monkeypatch.setattr(restart_resume, '_PASS_FINISHED', False)
        monkeypatch.setattr(restart_resume, '_LAST_RESULTS', [])
    return SimpleNamespace(turns=turns, running=running, settle=settle, started=started,
                           commands=commands, next_generation=next_generation)


def _tail(c, conversation):
    page = c.turns.list_turns(conversation, user_id=OWNER, lane_id='main')
    return page['turns'][-1]


def test_interrupted_chats_resume_from_their_checkpoint_once(chats):
    """2026-11-08 16:55: two interrupted chats, one of them a swarm master, were never
    resumed; the Router's restart turn had ended and must not be continued."""
    c = chats
    prose = c.running('chat-prose', content='Half of the answer')
    reasoning = c.running('swarm-master', thinking='Waiting for the swarm')
    finished = c.running('finished-before-stop', content='All done')
    stopped = c.running('owner-stopped', content='Stopped by the owner')
    restart_resume.record_quiesced_conversations([prose, reasoning, finished, stopped])
    # The stopping generation settles its quiesced work; one turn had already ended
    # and the owner had stopped another just before.
    c.settle(prose, 'server_shutdown')
    c.settle(reasoning, 'server_shutdown')
    finished.update(status='done', finishReason='stop')
    c.turns.record_task_event(finished, {'type': 'done', 'finishReason': 'stop'})
    c.settle(stopped, None)
    before = {name: _tail(c, name) for name in ('chat-prose', 'swarm-master', 'owner-stopped')}
    assert before['chat-prose']['settlement']['cause'] == 'server_restart'
    assert before['owner-stopped']['settlement']['cause'] == 'user_abort'
    c.next_generation()
    results = {item['conversationId']: item for item in
               restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)}
    assert restart_resume.pass_finished() is True
    assert {name for name, item in results.items() if item['outcome'] == 'resumed'} == {
        'chat-prose', 'swarm-master'}
    assert results['owner-stopped']['outcome'] == 'not_resumed'
    assert 'user_abort' in results['owner-stopped']['reason']
    assert results['finished-before-stop']['outcome'] == 'not_resumed'
    assert sorted(conversation for conversation, _ in c.started) == ['chat-prose', 'swarm-master']
    for name in ('chat-prose', 'swarm-master'):
        interrupted, item = before[name], results[name]
        after = c.turns.get_turn(name, interrupted['turnId'], user_id=OWNER)
        assert after['currentAttemptId'] == item['attemptId'] != interrupted['currentAttemptId']
        offered = [option['operation'] for option in interrupted['settlement']['resumeOptions']
                   if option['operation'] in ('continue', 'checkpoint_resume')]
        assert item['operation'] == offered[0]
        accepted = c.turns.get_attempt(item['attemptId'], user_id=OWNER, include_execution_config=True)
        assert accepted['commandId'] == 'restart-resume:' + interrupted['currentAttemptId']
        assert accepted['operation'] == item['operation']
        assert accepted['config']['model'] == 'chat-model'
        # Its own accepted identity (never a synthetic one) and the Continue
        # receipt a detached swarm needs to re-attach to its parent.
        assert accepted['config'][EXECUTION_IDENTITY_CONFIG_KEY]['principal']['subject_id'] == 'owner'
        assert accepted['config'].get('_resumeSource')
    # What the restart notice reports for each conversation (durable Turn state).
    since = min(item['createdAt'] for item in (before['chat-prose'], before['swarm-master'])) / 1000
    assert restart_resume.describe_conversation(OWNER, 'chat-prose', since).startswith(
        'resumed from its checkpoint (')
    assert restart_resume.describe_conversation(OWNER, 'owner-stopped', since) == 'interrupted (user_abort)'
    # Exactly once: a second pass, then a later generation reading a duplicate record.
    assert restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART) == []
    restart_resume.record_quiesced_conversations([prose])
    c.next_generation()
    again = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert [item['outcome'] for item in again] == ['not_resumed'] and len(c.started) == 2


def test_the_next_generation_resumes_the_chat_then_wakes_the_requester_with_the_result(
        chats, monkeypatch, tmp_path):
    """The whole restart path on real Turn storage: a Settings restart is accepted
    with a running chat, the stopping generation records and settles it, the next
    generation continues it and then wakes the owner's Global Router with one
    notice Turn naming that resume."""
    from lib import lifecycle_approval as gate
    from lib.server import restart_notice
    c = chats
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create=False, client=None: {
        'conversationId': 'router', 'availability': 'available', 'revision': 1})
    router = c.running('router', content='Restarting now as you asked.')
    router.update(status='done', finishReason='stop')
    c.turns.record_task_event(router, {'type': 'done', 'finishReason': 'stop'})
    chat = c.running('chat', content='Half of the answer')
    approval = gate.create_request('restart', origin={'conv_id': ''})
    gate.decide(approval['id'], True, provenance={
        'kind': 'human-ui', 'ownerUserId': OWNER, 'subject': 'session-key', 'credential': 'session',
        'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'})
    assert gate.consume_restart(approval['id'], notice={'owner': OWNER, 'interrupted': ['chat'],
                                                        'oldCode': None})[0]
    restart_resume.record_quiesced_conversations([chat])
    c.settle(chat, 'server_shutdown')
    # The old generation, still stopping, reports nothing.
    assert restart_notice.deliver_restart_notices() == []
    c.next_generation()
    monkeypatch.setattr(boot_identity, 'BOOT_TS', boot_identity.BOOT_TS + 3600)
    [resumed] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert resumed['conversationId'] == 'chat' and resumed['outcome'] == 'resumed'
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['conversationId'] == 'router' and delivered['state'] == 'delivered'
    turns = c.turns.list_turns('router', user_id=OWNER, lane_id='main')['turns']
    notice_input = next(turn for turn in turns if turn.get('actor') == 'human'
                        and str((turn.get('projection') or {}).get('content', '')).startswith(
                            restart_notice.NOTICE_HEADER))
    text = notice_input['projection']['content']
    assert 'completed' in text and 'chat: resumed from its checkpoint (' in text
    from lib.turn_initiation import INITIATOR_PROACTIVE, resolve_turn_initiator
    assert resolve_turn_initiator(notice_input) == INITIATOR_PROACTIVE
    assert [conversation for conversation, _ in c.started] == ['chat', 'router']
    # Exactly one notice Turn, however often delivery runs.
    assert restart_notice.deliver_restart_notices() == []
    assert len([turn for turn in c.turns.list_turns('router', user_id=OWNER, lane_id='main')['turns']
                if str((turn.get('projection') or {}).get('content', '')).startswith(
                    restart_notice.NOTICE_HEADER)]) == 1


def test_a_conversation_the_owner_moved_on_from_is_not_resumed(chats):
    c = chats
    task = c.running('moved-on', content='Interrupted work')
    restart_resume.record_quiesced_conversations([task])
    c.settle(task, 'server_shutdown')
    # Before the pass ran, the owner sent a new message: the lane advanced.
    c.turns.create_turn_pair('moved-on', command_id='moved-on:second-input',
        input_projection={'content': 'Forget that; do this instead'}, config={'model': 'chat-model'},
        user_id=OWNER)
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert item['outcome'] == 'not_resumed' and c.started == []


@pytest.mark.parametrize('previous, reason', [
    ({'verdict': 'clean', 'reason': 'manual', 'restart_storm': False}, 'manual shutdown'),
    ({'verdict': 'unclean', 'restart_storm': True}, 'restart storm'),
])
def test_a_manual_shutdown_or_restart_storm_resumes_nothing(chats, previous, reason):
    c = chats
    task = c.running('after-shutdown', content='Interrupted work')
    restart_resume.record_quiesced_conversations([task])
    c.settle(task, 'server_shutdown')
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=previous)
    assert item['outcome'] == 'not_resumed' and reason in item['reason']
    assert c.started == [] and restart_resume.pass_finished() is True
    # The records were consumed: a later restart does not resurrect this stand-down.
    c.next_generation()
    assert restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART) == []


def test_boot_settled_orphans_of_a_killed_server_resume_too(chats, monkeypatch):
    """A killed server quiesced nothing; boot recovery settles its orphans."""
    c = chats
    task = c.running('killed', content='Interrupted work')
    c.turns.recover_running_attempts()
    c.next_generation()
    monkeypatch.setattr(restart_resume, '_task_owner', lambda task_id: OWNER if task_id == task['id'] else None)
    [item] = restart_resume.resume_restart_paused_turns(
        previous_shutdown={'verdict': 'unclean', 'restart_storm': False},
        boot_recovered=[{'taskId': task['id'], 'conversationId': 'killed'}])
    assert item['outcome'] == 'resumed' and [conv for conv, _ in c.started] == ['killed']


@pytest.mark.parametrize('config, settlement, reason', [
    ({'_projectDispatchAdmission': {'assignmentId': 'a-1'}},
     {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]}, 'project dispatch'),
    ({'autopilot': True}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]}, 'Goal'),
    ({'activeFlow': 'review-flow'}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]},
     'Flow'),
    ({'flowBuiltin': 'deep-research'}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]},
     'Flow'),
    ({}, {'cause': 'server_restart', 'resumeOptions': [
        {'operation': 'continue'}, {'operation': 'checkpoint_resume', 'anchor': {}},
        {'operation': 'answer_guidance', 'anchor': {'guidanceId': 'g-1'}}]}, 'answer'),
    ({}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'regenerate'}]}, 'regenerate'),
])
def test_work_other_owners_resume_or_the_owner_must_answer_is_left_alone(monkeypatch, config, settlement,
                                                                         reason):
    turn = {'turnId': 't-1', 'conversationId': 'c-1', 'laneId': 'main', 'actor': 'assistant',
            'status': 'interrupted', 'currentAttemptId': 'a-1', 'projectionRevision': 3,
            'settlement': settlement}
    monkeypatch.setattr(restart_resume, '_lane_tail', lambda owner, conversation: dict(turn))
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda attempt, **kwargs: {
        'attemptId': 'a-1', 'conversationId': 'c-1', 'turnId': 't-1', 'status': 'interrupted',
        'config': {'model': 'm', '_turnOwnerUserId': OWNER, **config}})
    monkeypatch.setattr('lib.conversation_sync.runtime.conversation_turn_commands', SimpleNamespace(
        create_attempt=lambda *args, **kwargs: pytest.fail('nothing may resume ' + reason)))
    item = restart_resume.resume_conversation(OWNER, 'c-1')
    assert item['outcome'] == 'not_resumed' and reason in item['reason']
