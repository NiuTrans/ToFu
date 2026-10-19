"""Real owned event projections reconcile transient/lost Goal acknowledgements."""
from copy import deepcopy
from threading import Event

import pytest

from tests.support.semantic_authorities import semantic_authority
from tests.goals.test_goal_conversation_recovery import recovery as recovery
from lib.goal_runs.execution_journal import create_goal_execution_journal, read_goal_execution
from lib.goal_runs.journal_codec import CHUNK_EVENT
from lib.orchestration.execution_journal import RecoveryRefused, fingerprint
from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    from lib.tasks_pkg import persistence_wait
    monkeypatch.setattr(persistence_wait, '_BASE_DELAY_SECONDS', 0.0)


class FaultingRuns:
    """Inject on the real semantic boundary; no alternative receipt authority."""
    def __init__(self, runs, *, phase, code, after_commit=False, persistent=False, rejected=False):
        self.runs, self.phase, self.code = runs, phase, code
        self.after_commit, self.persistent, self.rejected = after_commit, persistent, rejected
        self.target = None
        self.attempts = []
        self.header_committed = False
        self.on_attempt = None
    def __getattr__(self, name):
        return getattr(self.runs, name)
    def project_event(self, run_id, cursor, packet):
        kind = packet.get('type')
        matches = (
            (self.phase == 'header_chunk' and not self.header_committed
             and kind == CHUNK_EVENT and packet.get('index') == 1)
            or (self.phase == 'completion_chunk' and self.header_committed
                and kind == CHUNK_EVENT and packet.get('index') == 1)
            or kind == self.phase)
        identity = (run_id, cursor, fingerprint(packet))
        if self.target is None and matches:
            self.target = identity
        if self.target is not None and (run_id, cursor) == self.target[:2]:
            self.attempts.append(identity)
            assert identity == self.target
            if self.on_attempt is not None:
                self.on_attempt(len(self.attempts))
            if self.persistent or len(self.attempts) == 1:
                if self.after_commit:
                    assert self.runs.project_event(run_id, cursor, packet)
                if self.rejected:
                    return False
                raise StorageError(self.code, 'Injected authority outcome', retryable=False)
        accepted = self.runs.project_event(run_id, cursor, packet)
        if accepted and kind == 'execution_journal_start':
            self.header_committed = True
        return accepted


def packets(recovery):
    result, cursor = [], 0
    while True:
        page = recovery.runs.replay('goal_source-task', cursor)
        result.extend(page.events)
        if page.caught_up:
            return result
        assert page.next_cursor > cursor
        cursor = page.next_cursor


def run_effect(recovery, runs):
    journal = create_goal_execution_journal(
        recovery.task, recovery.definition, recovery.launch, runs=runs)
    calls = []
    snapshot = {'messages': [{'role': 'user', 'content': 'working evidence' * 100_000}],
                'task_state': {'_todos': [{'id': 'full-scope', 'status': 'completed'}]}}
    runner = journal.runner(
        lambda *args: calls.append('model/tool') or {'output': 'settled'},
        context_snapshot=lambda: deepcopy(snapshot), context_restore=lambda value: None)
    return journal, runner, calls, snapshot


@pytest.mark.parametrize('phase,code', [
    ('header_chunk', 'database_timeout'),
    ('execution_journal_ready', 'database_busy'),
    ('execution_account_frontier', 'database_timeout'),
    ('execution_effect_started', 'database_unavailable'),
    ('completion_chunk', 'database_busy'),
    ('execution_effect_completed', 'database_timeout'),
])
@pytest.mark.parametrize('after_commit', [False, True])
def test_identical_packet_retry_never_repeats_effect_or_chunks(recovery, phase, code, after_commit):
    recovery.task['messages'].insert(0, {'role': 'system', 'content': 'launch policy' * 100_000})
    recovery.task['abort_event'] = Event()
    runs = FaultingRuns(recovery.runs, phase=phase, code=code, after_commit=after_commit)
    journal, runner, calls, snapshot = run_effect(recovery, runs)
    result = runner({'id': 'a'}, 'context', 0)
    assert result.output == 'settled'
    assert calls == ['model/tool']
    assert len(runs.attempts) == 2
    assert not recovery.task.get('_storageWaits')
    rows = packets(recovery)
    chunk_keys = [(row['sha256'], row['index']) for row in rows if row.get('type') == CHUNK_EVENT]
    assert len(chunk_keys) == len(set(chunk_keys))
    run = recovery.runs.get_execution_snapshot('goal_source-task')
    metadata, records = read_goal_execution(rows, run=run, owner_user_id=recovery.owner)
    assert metadata['objective'] == 'full objective'
    assert len(records) == 1
    assert next(iter(records.values()))['result']['worker_context'] == snapshot
    assert not journal.abort_requested()


@pytest.mark.parametrize('failure,expected_attempts', [
    ('shutdown', 4),
    ('conflict', 1),
    ('rejected', 1),
])
def test_unacknowledged_completion_retains_ambiguity_and_never_reexecutes(recovery, failure, expected_attempts):
    recovery.task['abort_event'] = Event()
    runs = FaultingRuns(recovery.runs, phase='execution_effect_completed',
                        code='database_conflict' if failure == 'conflict' else 'database_busy',
                        persistent=True, rejected=failure == 'rejected')
    if failure == 'shutdown':
        runs.on_attempt = lambda attempt: recovery.task.update(
            _abort_reason='server_shutdown') if attempt == 4 else None
    journal, runner, calls, _ = run_effect(recovery, runs)
    with pytest.raises(RecoveryRefused):
        runner({'id': 'a'}, 'context', 0)
    assert calls == ['model/tool']
    assert len(runs.attempts) == expected_attempts
    assert not recovery.task.get('_storageWaits')
    assert journal.abort_requested()
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        read_goal_execution(packets(recovery), run=recovery.runs.get_execution_snapshot('goal_source-task'),
                            owner_user_id=recovery.owner)


@pytest.mark.parametrize('after_commit', [False, True])
def test_recovery_source_acknowledgement_reconciles_transient_delivery(recovery, after_commit):
    from lib.goal_runs.recovery import resume_chat_goal, bind_goal_recovery_task
    from lib.orchestration.run_recovery import recovery_claim_successor
    from lib.turn_lifecycle import get_attempt
    create_goal_execution_journal(recovery.task, recovery.definition, recovery.launch, runs=recovery.runs)
    recovery.retire()
    successor = resume_chat_goal(recovery.runs, 'goal_source-task',
                                 owner_user_id=recovery.owner, ports=recovery.ports)
    task = recovery.started[0]
    seed = bind_goal_recovery_task(
        task, runs=recovery.runs,
        conversation_repository=recovery.ports.conversation_repository, get_attempt=get_attempt)
    recovery.service.start(task, seed.definition)
    task['abort_event'] = Event()
    class LostAcknowledgement:
        def __init__(self):
            self.attempts = 0
        def __getattr__(self, name):
            return getattr(recovery.runs, name)
        def append_event(self, run_id, cursor, packet):
            assert run_id == seed.source_run_id
            assert packet == {'type': 'execution_recovery_claim', 'successor_run_id': successor}
            self.attempts += 1
            if self.attempts == 1:
                if after_commit:
                    assert recovery.runs.append_event(run_id, cursor, packet)
                raise StorageError('database_timeout', 'Lost source acknowledgement')
            return recovery.runs.append_event(run_id, cursor, packet)
    runs = LostAcknowledgement()
    journal = create_goal_execution_journal(task, seed.definition, seed.launch(), runs=runs)
    assert not task.get('_storageWaits')
    assert runs.attempts == (1 if after_commit else 2)
    assert recovery_claim_successor(recovery.runs, seed.source_run_id) == successor
    assert not journal.abort_requested()
    assert recovery.runs.get_execution_snapshot(seed.source_run_id)['status'] == 'error'
