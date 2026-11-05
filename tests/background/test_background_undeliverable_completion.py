"""Background completion settlement: permanent versus retryable delivery failures.

Incident: an inline API request label (``api-…``) named the completion's
conversation. queue.enqueue refused it with ``database_not_found`` and the
maintenance tick retried it, logging an ERROR every five minutes, forever.

Both directions are pinned here. A completion whose owner conversation the
authority confirms absent, and that no live flow can still create, settles
exactly once with one WARNING and no restart record. A refusal whose owner is
present, unknown, or still creatable by a running launcher (Feishu persists
its conversation after the task returns) keeps its record and retries.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from lib import agent_inbox
from lib.storage.errors import StorageError
from lib.tasks_pkg import background_commands
from lib.tasks_pkg.handlers import _background_command as handoff

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

OWNER = 7
RESULT = '$ echo done\ndone\n[exit code: 0]'
DAY = 86_400.0


class _Authority:
    """Owner-scoped stand-in for ``queue.enqueue`` and ``conversation.get``.

    Both storage backends refuse queue.enqueue with database_not_found unless
    the owner's active conversation exists; conversation.get returns null for
    an absent header.
    """

    def __init__(self, *conversations, enqueue_error=None, probe_error=None,
                 create_on_refusal=False, before_enqueue=None):
        self.conversations = {(OWNER, conv_id) for conv_id in conversations}
        self.enqueue_error = enqueue_error
        self.probe_error = probe_error
        self.create_on_refusal = create_on_refusal
        self.before_enqueue = before_enqueue
        self.enqueue_calls: list[str] = []
        self.receipts: dict[str, dict] = {}

    def heal(self, conv_id: str) -> None:
        self.enqueue_error = None
        self.probe_error = None
        self.conversations.add((OWNER, conv_id))

    def command(self, operation, payload, command_id=None, **_options):
        assert operation == 'queue.enqueue'
        self.enqueue_calls.append(payload['conv_id'])
        if self.before_enqueue is not None:
            self.before_enqueue()
        if self.enqueue_error is not None:
            raise self.enqueue_error
        owner = (payload['user_id'], payload['conv_id'])
        if owner not in self.conversations:
            if self.create_on_refusal:
                # The owner's creation commits right after this refusal.
                self.conversations.add(owner)
            raise StorageError('database_not_found', 'conversation not found')
        self.receipts.setdefault(payload['queue_id'], payload)
        return {'queueId': payload['queue_id'], 'position': len(self.receipts),
                'kind': payload['kind']}

    def query(self, operation, payload, *_args, **_options):
        assert operation == 'conversation.get'
        if self.probe_error is not None:
            raise self.probe_error
        if (payload['user_id'], payload['conv_id']) not in self.conversations:
            return None
        return {'metadata': {'id': payload['conv_id']}, 'messages': []}


class _Clock:
    def __init__(self, monkeypatch):
        self.now = time.monotonic()
        monkeypatch.setattr(background_commands.time, 'monotonic', lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def wakes(monkeypatch, tmp_path):
    agent_inbox.reset_for_test()
    monkeypatch.setattr(background_commands, '_registry', {})
    monkeypatch.setattr(background_commands, '_recovery_in_progress', False)
    monkeypatch.setattr(background_commands, '_recovery_scan_failed', False)
    monkeypatch.setattr(background_commands, '_recovery_incomplete_conversations', set())
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path))
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: True)
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', lambda _seconds: None)
    calls = []
    monkeypatch.setattr(background_commands, 'wake_completed_background_batch',
                        lambda conv_id, user_id: calls.append((conv_id, user_id)))
    yield calls
    agent_inbox.reset_for_test()


def _install(monkeypatch, authority: _Authority) -> _Authority:
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_kwargs: authority)
    return authority


def _status(command_id: str) -> str:
    return background_commands.check_background_command(
        command_id, user_id=OWNER)['delivery_status']


def _join_worker(command_id: str) -> None:
    for thread in threading.enumerate():
        if thread.name == f'bg-cmd-{command_id[3:]}':
            thread.join(5)
            assert not thread.is_alive()


def _failed_entry(tmp_path, command_id: str, conv_id: str, *, launcher: str = '') -> None:
    """A finished command whose first publication failed (restart record kept)."""
    record_dir = tmp_path / command_id
    record_dir.mkdir()
    (record_dir / 'record.json').write_text(json.dumps({
        'command_id': command_id, 'command': 'echo done', 'task_id': launcher,
        'conv_id': conv_id, 'user_id': OWNER}))
    done = threading.Event()
    done.set()
    background_commands._registry[command_id] = {
        'done': done, 'task': None, 'launch_task_id': launcher,
        'user_id': OWNER, 'conv_id': conv_id,
        'state': {'command': 'echo done', 'result': RESULT, 'error': None,
                  'finished': True, 'started_at': time.time(),
                  'finished_at': time.time(), 'delivery_status': 'failed'},
    }


def _finished_record(tmp_path, command_id: str, conv_id: str):
    record_dir = tmp_path / command_id
    record_dir.mkdir()
    (record_dir / 'record.json').write_text(json.dumps({
        'command_id': command_id, 'command': 'echo done', 'task_id': 'old-process-task',
        'conv_id': conv_id, 'user_id': OWNER, 'started_at': time.time(),
        'pid': None, 'pgid': None, 'pid_starttime': None}))
    (record_dir / 'stdout.log').write_text('done\n')
    (record_dir / 'exit_code').write_text('0')
    return record_dir


def _settlements(caplog, command_id: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records
            if command_id in record.getMessage() and record.levelno >= logging.WARNING]


def test_absent_owner_settles_exactly_once_after_its_launcher_is_gone(
        monkeypatch, tmp_path, caplog, wakes):
    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    authority = _install(monkeypatch, _Authority())
    command_id = background_commands.start_background_command(
        task={'id': 'api-request-task', 'convId': 'api-caf4bd3c6ad1', '_userId': OWNER},
        command='echo done', execute=lambda _task, _ctx: RESULT)
    _join_worker(command_id)
    # One refusal is not proof that no flow will still create the owner.
    assert _status(command_id) == 'failed'
    assert (tmp_path / command_id / 'delivery.json').exists()

    clock = _Clock(monkeypatch)
    assert background_commands.retry_failed_background_deliveries() == 0
    clock.advance(DAY)
    assert background_commands.retry_failed_background_deliveries() == 0

    settled = background_commands.check_background_command(command_id, user_id=OWNER)
    assert settled['delivery_status'] == 'undeliverable'
    assert settled['result'] == RESULT
    assert not (tmp_path / command_id).exists()
    assert background_commands.conversation_results_complete(
        conv_id='api-caf4bd3c6ad1', user_id=OWNER)
    attempts = len(authority.enqueue_calls)
    for _ in range(3):
        clock.advance(DAY)
        assert background_commands.retry_failed_background_deliveries() == 0
    assert background_commands.recover_background_commands_on_startup() == 0
    assert len(authority.enqueue_calls) == attempts
    assert authority.receipts == {} and wakes == []

    settlement = _settlements(caplog, command_id)
    assert [record.levelname for record in settlement] == ['WARNING']
    message = settlement[0].getMessage()
    assert 'api-caf4bd3c6ad1' in message and 'absent' in message


@pytest.mark.parametrize('case', [
    'owner-created-at-refusal', 'owner-probe-unanswered', 'storage-unavailable',
    'launcher-still-running',
])
def test_refusal_that_is_not_proven_permanent_keeps_retrying(
        monkeypatch, tmp_path, caplog, wakes, case):
    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    from lib.tasks_pkg.manager import chat_task_runtime
    conv_id = 'conv-' + case
    launcher = ''
    if case == 'owner-created-at-refusal':
        authority = _Authority(create_on_refusal=True)
    elif case == 'owner-probe-unanswered':
        authority = _Authority(probe_error=StorageError('database_unavailable', 'restarting'))
    elif case == 'storage-unavailable':
        authority = _Authority(enqueue_error=StorageError('database_unavailable', 'restarting'))
    else:
        # Feishu runs its task first and creates the conversation afterwards.
        authority = _Authority()
        launcher = chat_task_runtime.create(user_id=OWNER, task_id='feishu-launcher')['id']
    _install(monkeypatch, authority)
    command_id = 'bg_' + case.replace('-', '_')
    _failed_entry(tmp_path, command_id, conv_id, launcher=launcher)
    clock = _Clock(monkeypatch)
    try:
        for _ in range(3):
            background_commands.retry_failed_background_deliveries()
            assert _status(command_id) in ('failed', 'queued')
            clock.advance(DAY)
        if _status(command_id) == 'failed':
            assert (tmp_path / command_id).exists()
            assert not background_commands.conversation_results_complete(
                conv_id=conv_id, user_id=OWNER)
        authority.heal(conv_id)
        background_commands.retry_failed_background_deliveries()
    finally:
        if launcher:
            chat_task_runtime.discard(launcher)

    assert _status(command_id) == 'queued'
    assert len(authority.receipts) == 1
    assert not (tmp_path / command_id).exists()
    assert wakes == [(conv_id, OWNER)]
    # Transient refusals may log ERROR; none may settle the result.
    assert not any(record.levelno == logging.WARNING or 'undeliverable' in record.getMessage()
                   for record in _settlements(caplog, command_id))


def test_unanswered_owner_probe_is_not_absence_and_leaves_a_trace(monkeypatch, caplog):
    from lib.background_command_delivery import owner_absence

    def unanswered(*_args, **_kwargs):
        raise StorageError('database_unavailable', 'restarting')
    monkeypatch.setattr('lib.conversations.repository.get_conversation', unanswered)
    caplog.set_level(logging.DEBUG, logger='lib.background_command_delivery')
    refusal = StorageError('database_not_found', 'conversation not found')

    assert owner_absence(refusal, 'conv-unanswered', user_id=OWNER) is None
    traces = [record for record in caplog.records
              if record.name == 'lib.background_command_delivery']
    assert len(traces) == 1
    assert traces[0].levelno == logging.DEBUG
    message = traces[0].getMessage()
    assert 'conv-unanswered' in message and 'StorageError: restarting' in message


@pytest.mark.parametrize('case', ['owner-absent', 'storage-unavailable'])
def test_restart_recovery_settles_only_a_confirmed_absent_owner(
        monkeypatch, tmp_path, caplog, wakes, case):
    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    authority = _install(monkeypatch, _Authority(
        *(('conv-restart',) if case == 'storage-unavailable' else ()),
        enqueue_error=(StorageError('database_unavailable', 'restarting')
                       if case == 'storage-unavailable' else None)))
    record_dir = _finished_record(tmp_path, 'bg_restart', 'conv-restart')

    assert background_commands.recover_background_commands_on_startup() == 1
    if case == 'owner-absent':
        # The launching process and every creation flow it owned are gone.
        assert not record_dir.exists()
        assert not background_commands.recovery_needs_retry()
        assert background_commands.recover_background_commands_on_startup() == 0
        assert authority.enqueue_calls == ['conv-restart']
        assert [record.levelname for record in _settlements(caplog, 'bg_restart')] == ['WARNING']
    else:
        assert record_dir.exists()
        assert background_commands.recovery_needs_retry()
        assert not background_commands.conversation_results_complete(
            conv_id='conv-restart', user_id=OWNER)
        authority.heal('conv-restart')
        assert background_commands.recover_background_commands_on_startup() == 1
        assert not record_dir.exists()
        assert not background_commands.recovery_needs_retry()
        assert len(authority.receipts) == 1
        assert not any('undeliverable' in record.getMessage()
                       for record in _settlements(caplog, 'bg_restart'))


def test_overlapping_recovery_sweeps_never_publish_one_record_twice(
        monkeypatch, tmp_path, caplog, wakes):
    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    entered, release = threading.Event(), threading.Event()

    def hold_first_publication():
        entered.set()
        assert release.wait(5)

    authority = _install(monkeypatch, _Authority(before_enqueue=hold_first_publication))
    record_dir = _finished_record(tmp_path, 'bg_overlap', 'conv-gone')
    first = threading.Thread(target=background_commands.recover_background_commands_on_startup)
    first.start()
    try:
        assert entered.wait(5)
        authority.before_enqueue = None
        background_commands.recover_background_commands_on_startup()
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
    assert authority.enqueue_calls == ['conv-gone']
    assert not record_dir.exists()
    assert [record.levelname for record in _settlements(caplog, 'bg_overlap')] == ['WARNING']


def test_handed_off_completion_with_absent_owner_settles_once(monkeypatch, caplog, wakes):
    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    authority = _install(monkeypatch, _Authority())
    monkeypatch.setattr(handoff, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    task = {'id': 'handoff-task', 'convId': 'conv-deleted', '_userId': OWNER,
            'aborted': False, 'toolRounds': []}
    release = threading.Event()

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        release.wait(2)
        command_task.pop('_subprocess_pid', None)
        return RESULT

    provisional = handoff.run_with_steer_handoff(
        task=task, config={}, command='echo done', execute=execute)
    assert handoff.is_background_command_result(provisional)
    command_id = provisional.split(handoff.BACKGROUND_COMMAND_MARKER, 1)[1].split('.', 1)[0].strip()
    release.set()
    deadline = time.monotonic() + 5
    while _status(command_id) == 'pending' and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _status(command_id) == 'failed'

    clock = _Clock(monkeypatch)
    for _ in range(4):
        background_commands.retry_failed_background_deliveries()
        clock.advance(DAY)
    assert _status(command_id) == 'undeliverable'
    attempts = len(authority.enqueue_calls)
    clock.advance(DAY)
    assert background_commands.retry_failed_background_deliveries() == 0
    assert len(authority.enqueue_calls) == attempts
    assert [record.levelname for record in _settlements(caplog, command_id)] == ['WARNING']


@pytest.mark.usefixtures('chat_sidecar')
def test_real_authority_settles_trashed_and_never_created_owners_only(
        monkeypatch, tmp_path, caplog, wakes):
    """SQL authority refusal plus its owner probe decide; a live owner receives."""
    from lib.storage import get_storage_client
    from lib import message_queue
    from tests._seed import seed_conversation

    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    monkeypatch.setattr(background_commands, 'OWNER_CREATION_GRACE_S', 0.0)
    seed_conversation('bg-live-owner', user_id=OWNER, title='Live')
    seed_conversation('bg-trashed-owner', user_id=OWNER, title='Trashed')
    deleted = get_storage_client(write=True).command(
        'conversation.delete', {'conv_id': 'bg-trashed-owner', 'user_id': OWNER},
        'bg-undeliverable-delete-trashed-owner')
    assert deleted.get('deleted')
    targets = {'bg_live': 'bg-live-owner', 'bg_trashed': 'bg-trashed-owner',
               'bg_never': 'api-never-created'}
    for command_id, conv_id in targets.items():
        _failed_entry(tmp_path, command_id, conv_id)

    for _ in range(len(targets)):
        background_commands.retry_failed_background_deliveries()
        if all(_status(command_id) != 'failed' for command_id in targets):
            break
    assert {command_id: _status(command_id) for command_id in targets} == {
        'bg_live': 'queued', 'bg_trashed': 'undeliverable', 'bg_never': 'undeliverable'}
    receipts = message_queue.pending_background_command_injections(
        'bg-live-owner', user_id=OWNER)
    assert [item['commandId'] for item in receipts] == ['bg_live']
    assert not any((tmp_path / command_id).exists() for command_id in targets)
    for command_id in ('bg_trashed', 'bg_never'):
        assert [record.levelname for record in _settlements(caplog, command_id)] == ['WARNING']
    assert background_commands.retry_failed_background_deliveries() == 0
