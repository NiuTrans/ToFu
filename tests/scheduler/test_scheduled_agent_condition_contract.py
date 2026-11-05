"""Scheduled agents: ``command`` is the action, a predicate decides.

For ``task_type='agent'`` the ``command`` is always the instruction executed
when the agent is ready, so creation and update refuse an empty one. A
``condition_command`` makes the agent ``code`` from creation: the predicate
decides every poll with zero LLM calls, a match dispatches the command, and a
non-matching or ambiguous predicate never does. ``hybrid`` is reached only by
the code-tier ambiguity demotion. Agents without a predicate stay ``llm`` and
timers keep their own derivation.
"""

from __future__ import annotations

import shlex

import pytest

import lib.scheduler.manager as manager_module
import lib.scheduler.proactive as proactive
from lib.identity import PrincipalContext
from lib.scheduler._shared import fallback_streak_threshold
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.scheduler.executor import execute_scheduler_tool
from lib.scheduler.manager import ScheduledTaskManager

pytestmark = pytest.mark.unit

OWNER = 7
INSTRUCTION = 'Triage every newly released model and file an intake note.'


class _SchedulerStore:
    """Semantic scheduler storage: task rows by id, poll ledger newest first."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.polls: list[dict] = []
        self.commands: list[str] = []

    def query(self, operation, payload, **_kwargs):
        if operation == 'scheduler.task.get':
            row = self.rows.get(payload['task_id'])
            return dict(row) if row and row['user_id'] == payload['user_id'] else None
        if operation == 'scheduler.poll.log':
            return [dict(row) for row in self.polls[:payload['limit']]]
        raise AssertionError(operation)

    def command(self, operation, payload, key=None, **_kwargs):
        self.commands.append(operation)
        if operation == 'scheduler.task.create':
            row = {'poll_count': 0, 'execution_count': 0, 'promotion_streak': 0,
                   'last_execution_task_id': '', **payload, 'id': payload['task_id']}
            self.rows[row['id']] = row
            return {'applied': True, 'task': dict(row)}
        if operation == 'scheduler.task.update':
            row = self.rows.get(payload['task_id'])
            if row is None or row['user_id'] != payload['user_id']:
                return {'changed': False}
            row.update({k: v for k, v in payload.items()
                        if k not in ('task_id', 'user_id')})
            return {'changed': True}
        if operation == 'scheduler.poll.append':
            self.polls.insert(0, dict(payload))
            return {'inserted': True}
        raise AssertionError(operation)


@pytest.fixture
def store(monkeypatch):
    store = _SchedulerStore()

    def client(*, write=False):
        return store

    monkeypatch.setattr(manager_module, '_scheduler_client', client)
    monkeypatch.setattr(proactive, '_scheduler_client', client)
    return store


@pytest.fixture
def eager_poll_llm(monkeypatch):
    """A poll LLM that votes to act whenever unsure (its prompt prefers act)."""
    import lib.llm_dispatch as llm_dispatch

    calls = []

    def smart_chat(messages, **_kwargs):
        calls.append(messages)
        return '{"act": true, "reason": "conditions seem close"}', {'total_tokens': 11}

    monkeypatch.setattr(llm_dispatch, 'smart_chat', smart_chat)
    return calls


@pytest.fixture
def dispatched(monkeypatch):
    turns = []

    def dispatch(**kwargs):
        turns.append(kwargs)
        return ScheduledTurnDispatch(disposition='started', task_id=f'exec-{len(turns)}')

    monkeypatch.setattr(
        'lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', dispatch)
    monkeypatch.setattr(proactive, 'gather_system_status', lambda _task: 'STATUS')
    return turns


def _principal():
    return PrincipalContext.user(
        subject_id='scheduled-agent-contract', owner_user_id=OWNER,
        scopes={'agents:scheduler'})


def _create(command=INSTRUCTION, *, task_type='agent', condition_command='', **extra):
    return ScheduledTaskManager().create_task(
        'Model release intake', '*/30 * * * *', command, principal=_principal(),
        task_type=task_type, target_conv_id='conv-intake',
        condition_command=condition_command, **extra)


def _poll(task_id):
    manager = ScheduledTaskManager()
    manager._run_proactive_poll(manager.get_task(task_id, user_id=OWNER))


def _tool(tool_name, /, **arguments):
    return execute_scheduler_tool(tool_name, {**arguments, '_user_id': OWNER})


# ── Kind derivation ─────────────────────────────────────────────────────────

def test_an_agent_with_a_predicate_is_code_from_creation(store):
    task = _create(condition_command='release-watch --new-only')

    assert task['condition_kind'] == 'code'
    assert store.rows[task['id']]['condition_kind'] == 'code'
    assert store.rows[task['id']]['command'] == INSTRUCTION


def test_an_agent_without_a_predicate_stays_llm(store):
    assert _create()['condition_kind'] == 'llm'


def test_non_agent_tasks_never_take_a_predicate_tier(store):
    task = _create('Summarize the inbox', task_type='prompt',
                   condition_command='test -f ready')
    assert task['condition_kind'] == 'llm'


@pytest.mark.parametrize(('check_instruction', 'condition_command', 'kind'), [
    ('Has CI finished?', 'test -f ready', 'hybrid'),
    ('', 'test -f ready', 'code'),
    ('Has CI finished?', '', 'llm'),
])
def test_timer_derivation_is_unchanged(monkeypatch, check_instruction,
                                       condition_command, kind):
    from lib.scheduler.timer import _crud

    created = []

    class _TimerStore:
        def command(self, operation, payload, key=None, **_kwargs):
            assert operation == 'timer.create'
            created.append(payload)
            return {'timer': dict(payload)}

    monkeypatch.setattr(_crud, '_timer_client', lambda *, write=False: _TimerStore())
    monkeypatch.setattr('lib.scheduler.timer._notify.notify_timer_changed',
                        lambda *_args, **_kwargs: None)

    _crud.create_timer(
        user_id=OWNER, conv_id='conv-ci', check_instruction=check_instruction,
        continuation_message='Report the build result.',
        condition_command=condition_command)

    assert created[0]['condition_kind'] == kind


# ── The command is required at every boundary ───────────────────────────────

@pytest.mark.parametrize('command', ['', '   \n\t'])
@pytest.mark.parametrize('condition_command', ['', 'release-watch --new-only'])
def test_creation_refuses_an_agent_without_a_command(store, command, condition_command):
    with pytest.raises(ValueError, match=r"command") as refused:
        _create(command, condition_command=condition_command)

    assert 'executes when ready' in str(refused.value)
    assert store.commands == []


def test_schedule_create_reports_the_required_command(store):
    result = _tool('schedule_create', name='Model release intake',
                   schedule='*/30 * * * *', command='', task_type='agent',
                   target_conv_id='conv-intake',
                   condition_command='release-watch --new-only')

    assert result.startswith('Error:') and 'command' in result
    assert store.commands == []


@pytest.mark.parametrize('updates', [
    {'command': ''},
    {'command': '  '},
    {'task_type': 'agent', 'command': ''},
])
def test_update_refuses_to_leave_an_agent_without_a_command(store, updates):
    task = _create(condition_command='release-watch --new-only')

    with pytest.raises(ValueError, match=r"command"):
        ScheduledTaskManager().update_task(task['id'], user_id=OWNER, **updates)

    result = _tool('schedule_manage', action='update', task_id=task['id'],
                   updates=updates)
    assert result.startswith('Error:') and 'command' in result
    assert store.rows[task['id']]['command'] == INSTRUCTION


def test_update_accepts_a_new_agent_instruction_and_keeps_the_stored_kind(store):
    task = _create(condition_command='release-watch --new-only')
    legacy = dict(store.rows[task['id']], id='legacy-hybrid', condition_kind='hybrid')
    store.rows['legacy-hybrid'] = legacy

    for task_id in (task['id'], 'legacy-hybrid'):
        result = _tool('schedule_manage', action='update', task_id=task_id,
                       updates={'command': 'File one intake note per release.'})
        assert result.startswith(f'Task {task_id} updated')

    assert store.rows[task['id']]['command'] == 'File one intake note per release.'
    assert store.rows[task['id']]['condition_kind'] == 'code'
    assert store.rows['legacy-hybrid']['condition_kind'] == 'hybrid'


def test_a_task_type_change_derives_the_kind_as_creation_would(store):
    task = _create('Summarize the inbox', task_type='prompt',
                   condition_command='test -f ready')
    manager = ScheduledTaskManager()

    assert manager.update_task(task['id'], user_id=OWNER, task_type='agent')
    assert store.rows[task['id']]['condition_kind'] == 'code'
    assert manager.update_task(task['id'], user_id=OWNER, task_type='prompt')
    assert store.rows[task['id']]['condition_kind'] == 'llm'


def test_update_cannot_change_the_predicate_or_its_kind(store):
    task = _create(condition_command='release-watch --new-only')

    ScheduledTaskManager().update_task(
        task['id'], user_id=OWNER, name='Renamed intake',
        condition_command='', condition_kind='hybrid')

    row = store.rows[task['id']]
    assert row['name'] == 'Renamed intake'
    assert row['condition_command'] == 'release-watch --new-only'
    assert row['condition_kind'] == 'code'


# ── Code-tier polls are decisive ────────────────────────────────────────────

def test_a_matching_predicate_runs_the_command_without_the_poll_llm(
        store, eager_poll_llm, dispatched, tmp_path):
    ready = tmp_path / 'new-release'
    ready.write_text('google/example-2\n')
    task = _create(condition_command=f'test -f {shlex.quote(str(ready))}')

    _poll(task['id'])

    assert eager_poll_llm == []
    (turn,) = dispatched
    assert turn['conversation_id'] == 'conv-intake'
    assert INSTRUCTION in turn['user_message']['content']
    (ledger,) = store.polls
    assert (ledger['decision'], ledger['tier'], ledger['predicate_matched'],
            ledger['tokens_used']) == ('act', 'code', 1, 0)
    assert store.rows[task['id']]['last_execution_task_id'] == 'exec-1'
    assert store.rows[task['id']]['execution_count'] == 1


@pytest.mark.parametrize(('predicate', 'matched'), [
    ('test -f {missing}', 0),        # exit 1: nothing new
    ("sh -c 'exit 2'", -1),          # ambiguous: never a trigger
])
def test_a_non_matching_predicate_never_dispatches(
        store, eager_poll_llm, dispatched, tmp_path, predicate, matched):
    task = _create(condition_command=predicate.format(
        missing=shlex.quote(str(tmp_path / 'absent'))))

    _poll(task['id'])

    assert dispatched == []
    assert eager_poll_llm == []
    (ledger,) = store.polls
    assert (ledger['decision'], ledger['tier'], ledger['predicate_matched'],
            ledger['tokens_used']) == ('skip', 'code', matched, 0)
    assert store.rows[task['id']]['execution_count'] == 0


def test_hybrid_is_reached_only_by_demotion_and_polls_with_the_command(
        store, eager_poll_llm, dispatched):
    task = _create(condition_command="sh -c 'exit 2'")

    for _ in range(fallback_streak_threshold()):
        _poll(task['id'])

    assert eager_poll_llm == [] and dispatched == []
    assert store.rows[task['id']]['condition_kind'] == 'hybrid'

    _poll(task['id'])

    (messages,) = eager_poll_llm
    assert f'YOUR STANDING INSTRUCTION:\n{INSTRUCTION}' in messages[1]['content']
    (turn,) = dispatched
    assert INSTRUCTION in turn['user_message']['content']
