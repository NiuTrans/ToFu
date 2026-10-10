"""Collect command receipts before one automatic completion turn."""

from __future__ import annotations

import threading
import json

import pytest

from lib import agent_inbox, message_queue
from lib.tasks_pkg import background_commands
from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude
from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


@pytest.fixture(autouse=True)
def _reset_registry():
    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()
        background_commands._recovery_in_progress = False
        background_commands._recovery_scan_failed = False
        background_commands._recovery_incomplete_conversations.clear()
    yield
    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()
        background_commands._recovery_in_progress = False
        background_commands._recovery_scan_failed = False
        background_commands._recovery_incomplete_conversations.clear()


def _receipt(conv_id: str, command_id: str) -> str:
    return message_queue.enqueue_message(
        conv_id, {
            'text': f'<background-command id="{command_id}">done</background-command>',
            '_backgroundCommand': command_id,
        }, {'model': 'test'}, kind=message_queue.KIND_BACKGROUND_RECEIPT,
        user_id=1,
    )['queueId']


def _command(conv_id: str, command_id: str, *, published: bool) -> None:
    done = threading.Event()
    if published:
        done.set()
    with background_commands._lock:
        background_commands._registry[command_id] = {
            'conv_id': conv_id, 'user_id': 1, 'done': done,
            'state': {'delivery_status': 'queued' if published else 'pending',
                      'command': command_id, 'result': 'done'},
        }


def test_last_published_result_creates_one_collective_turn(monkeypatch):
    conv_id = 'batch-wake-after-all'
    seed_conversation(conv_id, user_id=1, title='Batch')
    ids = [_receipt(conv_id, 'bg_first'), _receipt(conv_id, 'bg_last')]
    _command(conv_id, 'bg_first', published=True)
    _command(conv_id, 'bg_last', published=False)
    dispatched = []
    monkeypatch.setattr(message_queue, 'dispatch_next_queued',
                        lambda conv_id, *, user_id, _wait=None: dispatched.append(conv_id) or 'attempt')

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert dispatched == []
    assert background_commands.conversation_results_complete(
        conv_id=conv_id, user_id=2)
    with background_commands._lock:
        last = background_commands._registry['bg_last']
        last['done'].set()  # Process exited, but its queue write is not done.
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    with background_commands._lock:
        last['state']['delivery_status'] = 'queued'
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) == 'attempt'

    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    wake = [row for row in rows if row['kind'] == message_queue.KIND_WORKFLOW]
    assert len(wake) == 1
    assert wake[0]['payload']['_backgroundBatchWake'] == sorted(ids)
    assert wake[0]['payload']['_user_msg']['_initiator'] == 'proactive'
    assert [row['queueId'] for row in rows if row['kind'] ==
            message_queue.KIND_BACKGROUND_RECEIPT] == ids
    assert dispatched == [conv_id]

    leased = message_queue.dequeue_next(conv_id, user_id=1)
    assert leased['queueId'] == wake[0]['queueId']
    task = {'id': 'batch-model-task', 'convId': conv_id, '_userId': 1,
            'aborted': False, 'toolRounds': []}
    run_turn_prelude(task, {}, 'batch')
    messages = [{'role': 'user', 'content': 'Review the completed commands'}]
    drain_and_inject_inbox(task=task, messages=messages, round_num=0,
                           tid='batch')
    assert len(messages) == 2
    assert all(command_id in messages[-1]['content']
               for command_id in ('bg_first', 'bg_last'))
    assert [item['queueId'] for item in task['_bgcmd_inject_pending']] == ids


def test_existing_queued_request_consumes_results_without_extra_wake(monkeypatch):
    conv_id = 'batch-natural-turn'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_first')
    _command(conv_id, 'bg_first', published=True)
    human_id = message_queue.enqueue_message(
        conv_id, {'text': 'Please review results'}, {}, user_id=1)['queueId']
    dispatched = []
    monkeypatch.setattr(message_queue, 'dispatch_next_queued',
                        lambda conv_id, *, user_id, _wait=None: dispatched.append(conv_id) or 'attempt')

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) == 'attempt'
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert [row['queueId'] for row in rows if row['kind'] ==
            message_queue.KIND_REAL] == [human_id]
    assert all(row['kind'] != message_queue.KIND_WORKFLOW for row in rows)
    assert dispatched == [conv_id]


def test_simultaneous_completion_checks_enqueue_one_wake(monkeypatch):
    conv_id = 'batch-concurrent-wake'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_concurrent')
    _command(conv_id, 'bg_concurrent', published=True)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued',
                        lambda conv_id, *, user_id, _wait=None: None)
    barrier = threading.Barrier(3)
    errors = []

    def check():
        try:
            barrier.wait(timeout=10)
            message_queue.dispatch_completed_background_batch(conv_id, user_id=1)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=check) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert sum(row['kind'] == message_queue.KIND_WORKFLOW for row in rows) == 1


def test_pending_real_task_owns_delivery_before_batch_wake(monkeypatch):
    conv_id = 'batch-pending-task'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_finished')
    _command(conv_id, 'bg_finished', published=True)
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda *, user_id: [{
        'convId': conv_id, 'status': 'pending', 'aborted': False,
    }])
    assert message_queue.dispatch_completed_background_batch(
        conv_id, user_id=1) is None
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert all(row['kind'] != message_queue.KIND_WORKFLOW for row in rows)


def test_startup_scans_receipts_even_without_process_records(monkeypatch):
    called = []
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: False)
    monkeypatch.setattr(message_queue, 'drain_idle_background_batches',
                        lambda: called.append(True) or [])
    assert background_commands.recover_background_commands_on_startup() == 0
    assert called == [True]
    assert background_commands.conversation_results_complete(
        conv_id='no-live-command', user_id=1)


def test_recovery_scan_wakes_a_receipt_only_conversation(monkeypatch):
    conv_id = 'batch-recovery-scan'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_recovered')
    dispatched = []
    monkeypatch.setattr(message_queue, 'dispatch_next_queued',
                        lambda candidate, *, user_id, _wait=None:
                        dispatched.append(candidate) or 'attempt')

    background_commands._recovery_in_progress = True
    try:
        assert message_queue.dispatch_completed_background_batch(
            conv_id, user_id=1) is None
    finally:
        background_commands._recovery_in_progress = False
    assert 'attempt' in message_queue.drain_idle_background_batches()
    assert conv_id in dispatched
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert sum(row['kind'] == message_queue.KIND_WORKFLOW for row in rows) == 1


def test_failed_delivery_is_retried_before_collective_wake(monkeypatch):
    conv_id = 'batch-delivery-retry'
    _command(conv_id, 'bg_retry', published=True)
    with background_commands._lock:
        background_commands._registry['bg_retry']['state']['delivery_status'] = 'failed'
    published = []
    woken = []
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: False)
    monkeypatch.setattr('lib.tasks_pkg.handlers._background_command._queue_completion',
                        lambda **kwargs: published.append(kwargs['command_id']) or True)
    monkeypatch.setattr(background_commands, 'wake_completed_background_batch',
                        lambda conv_id, user_id: woken.append((conv_id, user_id)))

    assert not background_commands.conversation_results_complete(
        conv_id=conv_id, user_id=1)
    assert background_commands.retry_failed_background_deliveries() == 1
    assert published == ['bg_retry']
    assert woken == [(conv_id, 1)]
    assert background_commands.conversation_results_complete(
        conv_id=conv_id, user_id=1)


def test_failed_restart_delivery_blocks_batch_until_recovered(monkeypatch, tmp_path):
    root = tmp_path / 'background_commands'
    record_dir = root / 'bg_durable'
    record_dir.mkdir(parents=True)
    (record_dir / 'record.json').write_text(json.dumps({
        'command_id': 'bg_durable', 'command': 'echo done',
        'conv_id': 'batch-restart-failure', 'user_id': 1,
    }))
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(root))
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: True)
    monkeypatch.setattr(background_commands, '_read_exit_code', lambda paths: 0)
    monkeypatch.setattr(background_commands, '_result_from_record',
                        lambda record, paths: 'done')
    outcomes = iter((False, True))
    monkeypatch.setattr(background_commands, '_deliver_record',
                        lambda record, result: next(outcomes))
    monkeypatch.setattr(message_queue, 'drain_idle_background_batches',
                        lambda: [])

    assert background_commands.recover_background_commands_on_startup() == 1
    assert background_commands.recovery_needs_retry()
    assert not background_commands.conversation_results_complete(
        conv_id='batch-restart-failure', user_id=1)
    assert background_commands.conversation_results_complete(
        conv_id='batch-restart-failure', user_id=2)
    assert record_dir.exists()
    assert background_commands.recover_background_commands_on_startup() == 1
    assert not background_commands.recovery_needs_retry()
    assert background_commands.conversation_results_complete(
        conv_id='batch-restart-failure', user_id=1)
    assert not record_dir.exists()


def test_live_recovery_scan_never_deletes_unpublished_record(monkeypatch, tmp_path):
    root = tmp_path / 'background_commands'
    record_dir = root / 'bg_being_written'
    record_dir.mkdir(parents=True)
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(root))
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: True)
    monkeypatch.setattr(message_queue, 'drain_idle_background_batches',
                        lambda: [])
    assert background_commands.recover_background_commands_on_startup() == 0
    assert record_dir.exists()
    assert background_commands.recovery_needs_retry()

    (record_dir / 'record.json').write_text(json.dumps({
        'command_id': 'bg_being_written', 'command': 'echo done',
        'conv_id': 'batch-unpublished-record', 'user_id': 1,
    }))
    monkeypatch.setattr(background_commands, '_read_exit_code', lambda paths: 0)
    monkeypatch.setattr(background_commands, '_result_from_record',
                        lambda record, paths: 'done')
    monkeypatch.setattr(background_commands, '_deliver_record',
                        lambda record, result: True)
    assert background_commands.recover_background_commands_on_startup() == 1
    assert not background_commands.recovery_needs_retry()
    assert not record_dir.exists()


def test_batch_wake_skips_instead_of_blocking_on_a_held_dispatch_lock():
    """A nested batch drain reports "lock busy" instead of wedging its owner.

    The 2026-09-28 boot wedge: startup background-command recovery held the
    non-reentrant ``_dispatch_lock`` through the queued turn's create_turn,
    whose throttled task cleanup re-entered this drain; the unbounded
    acquire deadlocked the boot thread behind the readiness gate for 15+
    minutes. The drain is a best-effort wake — a contended entry must skip
    and leave delivery to the in-flight dispatch's completion hook.
    """
    conv_id = 'batch-wake-under-held-lock'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_nested')
    _command(conv_id, 'bg_nested', published=True)

    message_queue._dispatch_lock.acquire()
    outcome: dict = {}

    def _nested_drain():
        outcome['result'] = (
            message_queue.dispatch_completed_background_batch(
                conv_id, user_id=1))

    worker = threading.Thread(
        target=_nested_drain, name='nested-batch-drain', daemon=True)
    try:
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), (
            'batch drain wedged behind the held dispatch lock')
        assert outcome.get('result') is None
    finally:
        message_queue._dispatch_lock.release()


def test_legacy_receipt_starts_new_turn_from_canonical_root(monkeypatch):
    conv_id = 'batch-workspace-source'
    seed_conversation(conv_id, user_id=1, title='Batch')
    old_config = {
        'model': 'test', 'projectPath': '/old-task-worktree',
        'canonicalProjectPath': '/project',
        'readOnlyPaths': ['/old-task-worktree/docs'],
    }
    receipt = message_queue.enqueue_message(
        conv_id, {'text': 'done', '_backgroundCommand': 'bg_old_workspace'},
        old_config, kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=1)
    _command(conv_id, 'bg_old_workspace', published=True)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued', lambda *a, **kw: 'attempt')
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) == 'attempt'
    rows = message_queue._queue_client().query('queue.list', {
        'conv_id': conv_id, 'user_id': 1, 'include_full': True})
    wake = next(row for row in rows if row['kind'] == message_queue.KIND_WORKFLOW)
    assert wake['config'] == {'model': 'test', 'projectPath': '/project',
                              'readOnlyPaths': ['/project/docs']}
    original = next(row for row in rows if row['queueId'] == receipt['queueId'])
    assert original['config'] == old_config
