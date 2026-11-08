"""Collect command receipts before one automatic completion turn."""

from __future__ import annotations

import threading
import json
import shutil

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


def _without_model_execution(monkeypatch) -> None:
    """Never run a model task, whichever executor an accepted attempt reaches.

    Whether admission starts execution in this process or enqueues a durable
    worker job is the dispatch owner's contract; these specs observe the turn
    authority instead.
    """
    import lib.conversation_sync.task_start as task_start

    def start(conversation_id, config, **kwargs):
        kwargs['on_task_registered']('wake-task')
        return 'wake-task', None

    monkeypatch.setattr(task_start, 'start_conversation_attempt_executor', start)


def _wake_inputs(conv_id: str) -> list[dict]:
    from lib.turn_lifecycle import list_turns
    return [turn for turn in list_turns(conv_id, user_id=1)['turns']
            if turn['actor'] == 'human'
            and turn['projection'].get('origin', {}).get('initiator') == 'proactive']


def _wake_attempts(conv_id: str) -> list[dict]:
    """Current attempts of the collective wake replies, from the turn authority."""
    from lib.turn_lifecycle import get_attempt, list_turns
    inputs = {turn['turnId'] for turn in _wake_inputs(conv_id)}
    return [get_attempt(turn['currentAttemptId'], user_id=1)
            for turn in list_turns(conv_id, user_id=1)['turns']
            if turn.get('parentTurnId') in inputs]


def _queue_kinds(conv_id: str) -> list[str]:
    return [row['kind'] for row in message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})]


def test_last_published_result_creates_one_collective_turn(monkeypatch):
    conv_id = 'batch-wake-after-all'
    seed_conversation(conv_id, user_id=1, title='Batch')
    ids = [_receipt(conv_id, 'bg_first'), _receipt(conv_id, 'bg_last')]
    _command(conv_id, 'bg_first', published=True)
    _command(conv_id, 'bg_last', published=False)
    _without_model_execution(monkeypatch)

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert _wake_inputs(conv_id) == []
    assert background_commands.conversation_results_complete(
        conv_id=conv_id, user_id=2)
    with background_commands._lock:
        last = background_commands._registry['bg_last']
        last['done'].set()  # Process exited, but its queue write is not done.
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    with background_commands._lock:
        last['state']['delivery_status'] = 'queued'
    attempt_id = message_queue.dispatch_completed_background_batch(conv_id, user_id=1)

    # The wake is the turn itself; the receipts stay until it consumes them.
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert [row['queueId'] for row in rows] == ids
    assert _queue_kinds(conv_id) == [message_queue.KIND_BACKGROUND_RECEIPT] * 2
    wake = _wake_inputs(conv_id)
    assert len(wake) == 1
    assert wake[0]['projection']['content'] == message_queue._BACKGROUND_BATCH_PROMPT
    [attempt] = _wake_attempts(conv_id)
    assert attempt['attemptId'] == attempt_id
    assert attempt['status'] in ('pending', 'running')
    # The live wake owns these results; evaluating again starts nothing.
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert len(_wake_inputs(conv_id)) == 1
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


def test_simultaneous_completion_checks_start_one_wake_turn(monkeypatch):
    conv_id = 'batch-concurrent-wake'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_concurrent')
    _command(conv_id, 'bg_concurrent', published=True)
    _without_model_execution(monkeypatch)
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
    assert len(_wake_inputs(conv_id)) == 1 and len(_wake_attempts(conv_id)) == 1
    assert message_queue.KIND_WORKFLOW not in _queue_kinds(conv_id)


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


def test_startup_scan_settles_records_without_starting_a_wake_turn(monkeypatch):
    """The record scan gates wakes and must finish before readiness, but the
    collective wake starts a model turn. It runs after readiness from the
    post-ready pass and the maintenance tick (2026-10-23: a wake turn started
    inside the recovery phase held boot readiness for ~17 s)."""
    called = []
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: False)
    monkeypatch.setattr(message_queue, 'drain_idle_background_batches',
                        lambda: called.append(True) or [])
    assert background_commands.recover_background_commands_on_startup() == 0
    assert called == []
    assert background_commands.conversation_results_complete(
        conv_id='no-live-command', user_id=1)


def test_recovery_scan_wakes_a_receipt_only_conversation(monkeypatch):
    conv_id = 'batch-recovery-scan'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_recovered')
    _without_model_execution(monkeypatch)

    background_commands._recovery_in_progress = True
    try:
        assert message_queue.dispatch_completed_background_batch(
            conv_id, user_id=1) is None
    finally:
        background_commands._recovery_in_progress = False
    assert message_queue.drain_idle_background_batches()
    assert len(_wake_inputs(conv_id)) == 1
    assert message_queue.KIND_WORKFLOW not in _queue_kinds(conv_id)


def test_failed_batch_drain_identifies_exact_conversation_and_traceback(monkeypatch, caplog):
    conv_id = 'worker-2-full-conversation-identity'
    seed_conversation(conv_id, user_id=1, title='Batch failure')
    _receipt(conv_id, 'bg_failed_drain')

    def fail_dispatch(*args, **kwargs):
        raise RuntimeError('database_conflict')

    monkeypatch.setattr(message_queue, 'dispatch_completed_background_batch', fail_dispatch)
    assert message_queue.drain_idle_background_batches() == []
    records = [record for record in caplog.records
               if 'background batch drain failed' in record.getMessage()]
    assert any(conv_id in record.getMessage() for record in records)
    record = next(record for record in records if conv_id in record.getMessage())
    assert record.exc_info and record.exc_info[0] is RuntimeError


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
    outcomes = iter(('failed', 'queued'))
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
                        lambda record, result: 'queued')
    assert background_commands.recover_background_commands_on_startup() == 1
    assert not background_commands.recovery_needs_retry()
    assert not record_dir.exists()


def test_batch_wake_never_blocks_on_a_held_dispatch_lock(monkeypatch):
    """A nested batch drain never waits for the queue's dispatch lock.

    The 2026-09-28 boot wedge: startup background-command recovery held the
    non-reentrant ``_dispatch_lock`` through the queued turn's create_turn,
    whose throttled task cleanup re-entered this drain; the unbounded
    acquire deadlocked the boot thread behind the readiness gate for 15+
    minutes. Draining queued work that owns the receipts skips a contended
    lock, and the collective wake turn takes no queue lock at all.
    """
    conv_id = 'batch-wake-under-held-lock'
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_nested')
    _command(conv_id, 'bg_nested', published=True)
    queued = message_queue.enqueue_message(
        conv_id, {'text': 'Please review results'}, {}, user_id=1)['queueId']
    _without_model_execution(monkeypatch)

    def nested_drain():
        outcome: dict = {}
        worker = threading.Thread(
            target=lambda: outcome.setdefault(
                'result', message_queue.dispatch_completed_background_batch(
                    conv_id, user_id=1)),
            name='nested-batch-drain', daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), (
            'batch drain wedged behind the held dispatch lock')
        return outcome.get('result')

    message_queue._dispatch_lock.acquire()
    try:
        assert nested_drain() is None
        assert _wake_inputs(conv_id) == []
        assert message_queue.remove_from_queue(conv_id, queued, user_id=1)
        assert nested_drain()
        assert len(_wake_inputs(conv_id)) == 1
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
    _without_model_execution(monkeypatch)
    attempt_id = message_queue.dispatch_completed_background_batch(conv_id, user_id=1)
    assert attempt_id
    from lib.turn_lifecycle import get_attempt
    config = get_attempt(attempt_id, user_id=1, include_execution_config=True)['config']
    assert config['model'] == 'test'
    assert config['projectPath'] == '/project'
    assert config['readOnlyPaths'] == ['/project/docs']
    rows = message_queue._queue_client().query('queue.list', {
        'conv_id': conv_id, 'user_id': 1, 'include_full': True})
    original = next(row for row in rows if row['queueId'] == receipt['queueId'])
    assert original['config'] == old_config


def test_batch_wake_turn_records_the_context_it_runs_with(monkeypatch, tmp_path):
    """The proactive wake round carries its own context rail (mvk7np4uiwkpv2)."""
    import lib.conversation_sync.task_start as task_start
    from lib.turn_lifecycle import list_turns

    conv_id = 'batch-wake-context'
    seed_conversation(conv_id, user_id=1, title='Batch')
    project = str(tmp_path / 'workspace' / 'project')
    message_queue.enqueue_message(
        conv_id, {'text': 'done', '_backgroundCommand': 'bg_ctx'},
        {'model': 'claude-opus-5.5', 'projectPath': project,
         'searchMode': 'multi', 'fetchEnabled': True, 'codeExecEnabled': True},
        kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=1)
    _command(conv_id, 'bg_ctx', published=True)

    def start(conversation_id, config, **kwargs):
        kwargs['on_task_registered']('wake-task')
        return 'wake-task', None

    monkeypatch.setattr(task_start, 'start_conversation_attempt_executor', start)
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1)

    wake = next(turn for turn in list_turns(conv_id, user_id=1)['turns']
                if turn['actor'] == 'human')
    assert wake['projection']['origin']['initiator'] == 'proactive'
    assert wake['projection']['contextSnapshot'] == {
        'blockId': 'turn-context',
        'snapshot': {
            'roots': [{'short': 'workspace/project', 'path': project, 'ro': False}],
            'tools': [{'label': 'Search ×N', 'tone': 'search'},
                      {'label': 'Fetch', 'tone': 'net'},
                      {'label': 'Code Exec', 'tone': 'code'}],
            'modes': [], 'model': 'claude-opus-5.5', 'depth': '',
        },
    }


def test_busy_lane_in_another_process_persists_no_wake(monkeypatch):
    """Lane liveness is the turn authority's fact, not this process's registry.

    The API host and the resident execution host each register only their own
    tasks. The execution host judged a conversation idle while the API host ran
    its turn and queued a wake row that the authority refused for hours; the row
    outlived the receipts the live turn consumed, so an empty "review the
    results" turn followed. A busy lane must leave nothing behind.
    """
    from lib.turn_lifecycle import create_turn_pair, list_turns

    conv_id = 'batch-wake-busy-elsewhere'
    seed_conversation(conv_id, user_id=1, title='Batch')
    create_turn_pair(conv_id, command_id='turn-run-by-another-process',
                     input_projection={'content': 'long task'},
                     config={'model': 'test'}, user_id=1)
    receipt = _receipt(conv_id, 'bg_finished_elsewhere')
    _without_model_execution(monkeypatch)

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None

    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert [row['queueId'] for row in rows] == [receipt]
    assert len(list_turns(conv_id, user_id=1)['turns']) == 2


def test_unsettled_record_of_another_process_holds_the_wake(monkeypatch, tmp_path):
    """Each server process registers only its own commands; records are host-wide."""
    conv_id = 'batch-foreign-record'
    root = tmp_path / 'background_commands'
    record_dir = root / 'bg_running_elsewhere'
    record_dir.mkdir(parents=True)
    (record_dir / 'record.json').write_text(json.dumps({
        'command_id': 'bg_running_elsewhere', 'command': 'sleep 600',
        'conv_id': conv_id, 'user_id': 1,
    }))
    (root / 'bg_record_not_yet_written').mkdir()
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(root))
    monkeypatch.setattr(background_commands, '_posix_durable', lambda: True)

    assert not background_commands.conversation_results_complete(conv_id=conv_id, user_id=1)
    assert background_commands.conversation_results_complete(conv_id=conv_id, user_id=2)
    assert background_commands.conversation_results_complete(
        conv_id='another-conversation', user_id=1)
    # This process's own settled command is judged by its registry entry, not
    # by the record its worker removes after publication.
    _command(conv_id, 'bg_running_elsewhere', published=True)
    assert background_commands.conversation_results_complete(conv_id=conv_id, user_id=1)

    with background_commands._lock:
        background_commands._registry.clear()
    seed_conversation(conv_id, user_id=1, title='Batch')
    _receipt(conv_id, 'bg_published_here')
    _without_model_execution(monkeypatch)
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert _wake_inputs(conv_id) == []
    shutil.rmtree(record_dir)
    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1)
    assert len(_wake_inputs(conv_id)) == 1


def test_queued_wake_row_from_an_older_release_is_retired_not_run(monkeypatch):
    """Older releases persisted the collective wake as a dispatchable row.

    Such a row can outlive the receipts it named; running it asks the model to
    review results another turn already consumed. The drain retires it, and the
    wake is derived again from the receipts that remain.
    """
    conv_id = 'batch-wake-legacy-row'
    seed_conversation(conv_id, user_id=1, title='Batch')
    receipt = _receipt(conv_id, 'bg_legacy')
    message_queue.enqueue_message(conv_id, {
        'text': message_queue._BACKGROUND_BATCH_PROMPT,
        '_backgroundBatchWake': [receipt],
    }, {'model': 'test'}, kind=message_queue.KIND_WORKFLOW, user_id=1)
    _without_model_execution(monkeypatch)
    from lib.turn_lifecycle import list_turns

    assert message_queue.dispatch_next_queued(conv_id, user_id=1) is None
    assert list_turns(conv_id, user_id=1)['turns'] == []
    assert _queue_kinds(conv_id) == [message_queue.KIND_BACKGROUND_RECEIPT]

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1)
    assert len(_wake_inputs(conv_id)) == 1


def test_settled_wake_for_the_same_results_starts_nothing(monkeypatch):
    """A wake stopped before its first model round consumed its results is final.

    Its command identity derives from the receipt set, so every later
    evaluation replays that settled pair. Nothing starts and nothing is
    reported as started; the results wait for the next turn instead of being
    re-announced on every maintenance tick.
    """
    from lib.turn_lifecycle import abort_attempt
    from tests.support.conversation_workers import ConversationWorkers

    conv_id = 'batch-wake-settled-replay'
    seed_conversation(conv_id, user_id=1, title='Batch')
    receipt = _receipt(conv_id, 'bg_unconsumed')
    _without_model_execution(monkeypatch)
    attempt_id = message_queue.dispatch_completed_background_batch(conv_id, user_id=1)
    assert attempt_id

    abort_attempt(attempt_id, user_id=1)
    workers = ConversationWorkers(owner=1)
    try:
        workers.cancel(attempt_id)
    finally:
        workers.close()
    [attempt] = _wake_attempts(conv_id)
    assert attempt['status'] not in ('pending', 'running')

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert len(_wake_inputs(conv_id)) == 1
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert [row['queueId'] for row in rows] == [receipt]
