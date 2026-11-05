"""ask_human blocking flag, Coordinator readers and chat replies during a wait.

blocking=false shows the question and settles the round at once; a waiting
question ends when the user writes into the live turn instead of answering
the card. An accepted answer and Stop keep their existing outcomes.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from lib import agent_inbox
from lib.tasks_pkg import human_guidance
from lib.tasks_pkg.handlers.misc import _human as handler
from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry

pytestmark = pytest.mark.unit

OWNER = 7
CONV = 'conv-ask-nonblocking'
SUPERSEDED = ('The user replied in chat instead of answering this question; '
              'their message follows. Treat it as their answer or new direction.')
ABORTED = '[Task was aborted while waiting for human guidance]'
BATCH = [{'question': 'Q1?', 'response_type': 'free_text'},
         {'question': 'Q2?', 'response_type': 'choice', 'options': ['A', 'B']}]


def _shown_content(guidance_id):
    return (f'Question shown to the user without waiting (id {guidance_id}). Keep '
            'working on whatever does not depend on the answer; if you assume a '
            'default meanwhile, say which. Their reply will arrive later as a '
            'normal user message quoting this question.')


@pytest.fixture
def gate(monkeypatch):
    registry = OwnedHumanGateRegistry()
    registered = []
    real_register = registry.register

    def register(*args, **kwargs):
        registered.append(args)
        return real_register(*args, **kwargs)

    monkeypatch.setattr(registry, 'register', register)
    monkeypatch.setattr(human_guidance, 'human_gate_registry', registry)
    monkeypatch.setattr(human_guidance, '_ABORT_POLL_INTERVAL', 0.01)
    agent_inbox.reset_for_test(CONV)
    yield SimpleNamespace(registry=registry, registered=registered)
    agent_inbox.reset_for_test(CONV)


@pytest.fixture
def events(monkeypatch):
    from lib.tasks_pkg.executor import _finalize
    published = SimpleNamespace(requests=[], results=[], on_request=None)

    def publish(task, event):
        published.requests.append(event)
        if published.on_request is not None:
            published.on_request(event)

    monkeypatch.setattr(handler, 'append_event', publish)
    monkeypatch.setattr(_finalize, 'append_event',
                        lambda task, event: published.results.append(event))
    return published


@pytest.fixture
def turn(monkeypatch):
    """The durable Turn projection the shared wait reads accepted answers from."""
    import lib.turn_lifecycle as lifecycle
    projection = {'toolRounds': []}
    monkeypatch.setattr(lifecycle, 'get_turn',
                        lambda conversation_id, turn_id, *, user_id: {'projection': projection})
    return projection


@pytest.fixture(autouse=True)
def relayed(monkeypatch):
    """Questions handed to the Global Router relay (docs/ROUTER.md R3); no test reaches a real router."""
    calls = []
    monkeypatch.setattr('lib.router.relay.relay_project_question',
                        lambda owner, question: calls.append((owner, question)))
    return calls


PROJECT_ROUTER = {'conversation_id': CONV, 'project_path': '/project', 'binding_revision': 1, 'user_id': OWNER}


def _row():
    return {'toolCallId': 'call-ask', 'toolName': 'ask_human', 'query': 'ask'}


def _task(**overrides):
    task = {'id': 'task-ask-nonblocking', 'convId': CONV, '_turnId': 'turn-ask',
            '_userId': OWNER, '_attended': True, 'toolRounds': [], 'messages': []}
    task.update(overrides)
    return task


def _ask(task, row, args):
    return handler._handle_ask_human(task, None, 'ask_human', 'call-ask', args, 1,
                                     row, None, None, False)


def _args(batch, **extra):
    base = {'questions': BATCH} if batch else {
        'question': 'Which scope?', 'response_type': 'choice', 'options': ['A', 'B']}
    return {**base, **extra}


def _ask_in_thread(task, row, args):
    result = []
    worker = threading.Thread(target=lambda: result.append(_ask(task, row, args)), daemon=True)
    worker.start()
    return worker, result


def _wait_until(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('condition not reached')


def _chat(text):
    """A chat message steered into the live turn, as the steer seam enqueues it."""
    agent_inbox.enqueue(CONV, text, mode='user-steer', priority='next',
                        extra={'steerAuthor': {'actor': 'human', 'text': text}})


def _accept_answer(turn, guidance_id, response):
    turn['toolRounds'] = [{'guidanceId': guidance_id, 'toolName': 'ask_human',
                           'guidanceAnswerCommandId': 'answer-command',
                           'guidanceResponse': response}]


def _set_scope(monkeypatch, scope):
    import lib.tasks_pkg.coordinator_policy as policy

    def coordinator_scope(task, project_path=None):
        if isinstance(scope, Exception):
            raise scope
        return scope
    monkeypatch.setattr(policy, 'coordinator_scope', coordinator_scope)


@pytest.mark.parametrize('flag', [False, 'false', ' FALSE '])
@pytest.mark.parametrize('batch', [False, True])
def test_nonblocking_question_settles_at_once_without_a_gate(gate, events, batch, flag):
    row = _row()
    _, content, _ = _ask(_task(), row, _args(batch, blocking=flag))

    guidance_id = row['guidanceId']
    assert content == _shown_content(guidance_id)
    assert row['status'] == 'done' and row['guidanceBlocking'] is False
    if batch:
        assert row['guidanceType'] == 'batch' and row['guidanceOptions'] == []
        assert [q['question'] for q in row['guidanceBatchQuestions']] == ['Q1?', 'Q2?']
        assert row['guidanceQuestion'].startswith('Multiple questions')
    else:
        assert row['guidanceType'] == 'choice' and row['guidanceQuestion'] == 'Which scope?'
        assert row['guidanceOptions'] == [{'label': 'A'}, {'label': 'B'}]
    [meta] = row['results']
    assert meta['badge'] == 'asked' and meta['blocking'] is False
    assert meta['guidanceId'] == guidance_id
    assert meta['responseType'] == ('batch' if batch else 'choice')
    assert [event['status'] for event in events.results] == ['done']
    assert not events.requests, 'a non-blocking question never publishes a live request'
    assert not gate.registered and len(gate.registry) == 0
    assert not human_guidance.resolve_human_guidance(guidance_id, 'A', owner_user_id=OWNER)


@pytest.mark.parametrize('flag', [True, 'true', 0, None, 'no'])
def test_only_an_explicit_false_skips_the_wait(gate, events, turn, flag):
    row = _row()
    events.on_request = lambda event: _accept_answer(turn, event['guidanceId'], 'A')
    args = _args(False) if flag is None else _args(False, blocking=flag)
    _, content, _ = _ask(_task(), row, args)
    assert content == 'Human response: A'
    assert len(gate.registered) == 1 and 'guidanceBlocking' not in row


@pytest.mark.parametrize('scope', [None, RuntimeError('role lookup unavailable')])
@pytest.mark.parametrize('blocking', [True, False])
def test_unattended_task_without_coordinator_role_keeps_the_sentinel(
        gate, events, monkeypatch, scope, blocking):
    _set_scope(monkeypatch, scope)
    row = _row()
    _, content, _ = _ask(_task(_attended=False), row, _args(False, blocking=blocking))
    assert 'Cannot ask the user in this execution mode' in content
    assert row['status'] == 'unanswerable' and 'guidanceBlocking' not in row
    assert [event['type'] for event in events.requests] == ['human_guidance_request']
    assert not gate.registered


@pytest.mark.parametrize('batch', [False, True])
def test_unattended_coordinator_task_may_ask_with_and_without_waiting(
        gate, events, turn, monkeypatch, batch):
    _set_scope(monkeypatch, {'conversation_id': CONV, 'project_path': '/project',
                             'binding_revision': 1, 'user_id': OWNER})
    shown = _row()
    _, content, _ = _ask(_task(_attended=False), shown, _args(batch, blocking=False))
    assert content == _shown_content(shown['guidanceId']) and shown['status'] == 'done'
    assert not gate.registered

    row = _row()
    worker, result = _ask_in_thread(_task(_attended=False), row, _args(batch))
    _wait_until(lambda: len(gate.registry) == 1)
    _accept_answer(turn, row['guidanceId'], 'B')
    assert human_guidance.resolve_human_guidance(
        row['guidanceId'], 'B', owner_user_id=OWNER, durably_accepted=True)
    worker.join(2)
    assert not worker.is_alive()
    content = result[0][1]
    if batch:
        assert 'A1: B' in content and 'A2: B' in content
    else:
        assert content == 'Human response: B'
    assert row['status'] == 'done' and len(gate.registry) == 0


@pytest.mark.parametrize('blocking', [True, False])
@pytest.mark.parametrize('batch', [False, True])
def test_a_project_router_question_is_relayed_once_blocking_or_not(
        gate, events, turn, monkeypatch, relayed, batch, blocking):
    _set_scope(monkeypatch, PROJECT_ROUTER)
    events.on_request = lambda event: _accept_answer(turn, event['guidanceId'], 'A')
    row = _row()
    _ask(_task(), row, _args(batch, blocking=blocking))
    [(owner, question)] = relayed
    assert owner == OWNER
    assert question == {
        'projectPath': '/project', 'coordinatorId': CONV, 'turnId': 'turn-ask', 'toolCallId': 'call-ask',
        'guidanceId': row['guidanceId'], 'blocking': blocking, 'question': row['guidanceQuestion'],
        'options': [] if batch else [{'label': 'A'}, {'label': 'B'}],
        'questions': row['guidanceBatchQuestions'] if batch else []}


def test_questions_outside_a_project_router_are_not_relayed(gate, events, monkeypatch, relayed):
    _set_scope(monkeypatch, None)
    row = _row()
    _, content, _ = _ask(_task(), row, _args(False, blocking=False))
    assert content == _shown_content(row['guidanceId']) and relayed == []


def test_a_relay_failure_never_fails_the_question(gate, events, monkeypatch):
    _set_scope(monkeypatch, PROJECT_ROUTER)

    def unavailable(owner, question):
        raise RuntimeError('Global Router unavailable')
    monkeypatch.setattr('lib.router.relay.relay_project_question', unavailable)
    row = _row()
    _, content, _ = _ask(_task(), row, _args(False, blocking=False))
    assert content == _shown_content(row['guidanceId']) and row['status'] == 'done'


@pytest.mark.parametrize('batch', [False, True])
def test_chat_message_supersedes_a_waiting_question(gate, events, turn, batch):
    row = _row()
    worker, result = _ask_in_thread(_task(), row, _args(batch))
    _wait_until(lambda: len(gate.registry) == 1)
    guidance_id = row['guidanceId']
    _chat('Use option B instead')
    worker.join(2)

    assert not worker.is_alive() and result[0][1] == SUPERSEDED
    assert row['status'] == 'done' and row['guidanceSuperseded'] is True
    assert row['guidanceId'] == guidance_id and 'guidanceBlocking' not in row
    [meta] = row['results']
    assert meta['badge'] == 'superseded' and meta['guidanceId'] == guidance_id
    assert [event['status'] for event in events.results] == ['done']
    # The live gate is released: a late card answer is refused like an expired one.
    assert len(gate.registry) == 0
    assert not human_guidance.resolve_human_guidance(guidance_id, 'A', owner_user_id=OWNER)
    # The message itself stays for the round-boundary injector.
    assert agent_inbox.has_pending(CONV, modes=['user-steer'])


@pytest.mark.parametrize('author', [None, {'actor': 'agent', 'text': 'Choose B'}])
def test_nonhuman_steer_never_answers_a_waiting_question(gate, events, turn, author):
    # Channel events, relayed reports and forwarded context share the steer lane
    # (docs/ROUTER.md); the steer seam records a human author for none of them.
    row = _row()
    worker, result = _ask_in_thread(_task(), row, _args(False))
    _wait_until(lambda: len(gate.registry) == 1)
    agent_inbox.enqueue(CONV, '<channel-event>Choose B</channel-event>', mode='user-steer',
                        priority='next', extra={'steerAuthor': author})
    time.sleep(0.2)
    assert worker.is_alive() and len(gate.registry) == 1, 'nonhuman input answered the question'
    _accept_answer(turn, row['guidanceId'], 'A')
    assert human_guidance.resolve_human_guidance(
        row['guidanceId'], 'A', owner_user_id=OWNER, durably_accepted=True)
    worker.join(2)
    assert not worker.is_alive() and result[0][1] == 'Human response: A'
    assert 'guidanceSuperseded' not in row
    assert agent_inbox.has_pending(CONV, modes=['user-steer'])


def test_answer_accepted_before_the_chat_message_still_wins(gate, events, turn):
    def answer_then_message(event):
        _accept_answer(turn, event['guidanceId'], 'Option A')
        _chat('never mind')

    events.on_request = answer_then_message
    row = _row()
    _, content, _ = _ask(_task(), row, _args(False))
    assert content == 'Human response: Option A'
    assert row['status'] == 'done' and 'guidanceSuperseded' not in row
    assert row['results'][0]['badge'] == 'answered'
    assert len(gate.registry) == 0


@pytest.mark.parametrize('message_pending', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_stop_keeps_the_abort_branch(gate, events, turn, batch, message_pending):
    task = _task()

    def stop(event):
        if message_pending:
            _chat('stop that')
        task['aborted'] = True

    events.on_request = stop
    row = _row()
    _, content, _ = _ask(task, row, _args(batch))
    assert content == ABORTED
    assert row['status'] == 'awaiting_human' and 'results' not in row
    assert 'guidanceSuperseded' not in row and not events.results
    assert len(gate.registry) == 0
