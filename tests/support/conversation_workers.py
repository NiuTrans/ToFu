"""Controlled worker publications for HTTP/Turn contract tests.

Admission, worker claims and fenced storage writes stay real. Tests supply only
the external execution outcome. No API request constructs a local executor.
"""
import time
import uuid

import pytest

from lib.durable_worker import (
    StorageWorkerJobStore, WorkerExecutionContext, WorkerJobClaim, WorkerJobOutcome,
)


def observe_admissions(monkeypatch, on_admitted=None):
    """Observe actual new bindings and retain the dispatch owner's receipt."""
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import worker_dispatch
    from lib.conversation_sync.command_service import restore_accepted_attempt_config
    original = worker_dispatch.dispatch_accepted_attempt
    admissions = []
    def dispatch(attempt_id, *, user_id):
        before = turns.get_attempt(attempt_id, user_id=user_id)
        result = original(attempt_id, user_id=user_id)
        after = turns.get_attempt(attempt_id, user_id=user_id)
        if (before and not before.get('taskId') and after
                and after.get('taskId') == 'conversation-attempt:' + attempt_id):
            turn = turns.get_turn(after['conversationId'], after['turnId'], user_id=user_id)
            row = {'conversationId': after['conversationId'], 'taskId': after['taskId'],
                   'config': restore_accepted_attempt_config(turn, after, user_id=user_id)}
            admissions.append(row)
            if on_admitted is not None:
                on_admitted(row)
        return result
    monkeypatch.setattr(worker_dispatch, 'dispatch_accepted_attempt', dispatch)
    return admissions


class ConversationWorkers:
    def __init__(self, owner):
        self.owner = owner
        self.store = StorageWorkerJobStore()
        self.worker_id = 'contract-worker:' + uuid.uuid4().hex
        self.contexts = {}
        self.started = set()
        self.completed = set()

    def claim(self, attempt_id):
        while attempt_id not in self.contexts:
            document = self.store.claim_next(worker_id=self.worker_id,
                task_kinds=('conversation-attempt',), now_ms=time.time_ns() // 1_000_000,
                lease_ms=300_000)
            assert document is not None, f'No queued worker for {attempt_id}'
            claim = WorkerJobClaim.from_document(document, expected_worker_id=self.worker_id)
            self.contexts[claim.payload['attemptId']] = WorkerExecutionContext(claim)
        return self.contexts[attempt_id]

    def config(self, attempt_id):
        from lib import turn_lifecycle as turns
        from lib.conversation_sync.command_service import restore_accepted_attempt_config
        attempt = turns.get_attempt(attempt_id, user_id=self.owner)
        turn = turns.get_turn(attempt['conversationId'], attempt['turnId'], user_id=self.owner)
        return restore_accepted_attempt_config(turn, attempt, user_id=self.owner)

    def enter(self, attempt_id):
        from lib import turn_lifecycle as turns
        context = self.claim(attempt_id)
        if attempt_id not in self.started:
            turns.mark_task_started(attempt_id, context.claim.task_id, user_id=self.owner,
                                     worker_claim=context.publication_claim())
            self.started.add(attempt_id)
        return context

    def publish(self, fields, event):
        from lib import turn_lifecycle as turns
        context = self.enter(fields['_attemptId'])
        attempt_id = context.claim.payload['attemptId']
        task = dict(fields, id=context.claim.task_id, _userId=self.owner,
            convId=context.claim.payload['conversationId'], _turnId=context.claim.payload['turnId'],
            _principalContext=context.principal.to_payload(), _conversationWorkerContext=context)
        result = turns.record_task_event(task, event)
        attempt = turns.get_attempt(attempt_id, user_id=self.owner)
        if attempt and attempt['status'] in {'completed', 'truncated', 'failed', 'interrupted'}:
            self.complete(context, 'succeeded' if attempt['status'] in {'completed', 'truncated'} else 'failed')
        return result

    def complete(self, context, status):
        if context.claim.task_id not in self.completed:
            result = self.store.complete(context.claim, now_ms=time.time_ns() // 1_000_000,
                outcome=WorkerJobOutcome(status=status, result_ref=context.claim.task_id))
            assert result['ok']
            self.completed.add(context.claim.task_id)

    def cancel(self, attempt_id):
        from lib.conversation_sync.worker_execution import cancel_claimed_attempt
        context = self.claim(attempt_id)
        context.observe_claim_state(self.store.claim_state(
            context.claim, now_ms=time.time_ns() // 1_000_000))
        outcome = cancel_claimed_attempt(context, context.claim.payload)
        self.complete(context, outcome.status)

    def fail(self, attempt_id, error):
        from lib.conversation_sync.worker_execution import settle_claimed_failure
        context = self.claim(attempt_id)
        outcome = settle_claimed_failure(context, context.claim.payload, error)
        self.complete(context, outcome.status)

    def run(self, attempt_id):
        from lib import turn_lifecycle as turns
        from lib.conversation_sync.worker_execution import execute_claimed_attempt, settle_claimed_failure
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        context = self.claim(attempt_id)
        try:
            try:
                outcome = execute_claimed_attempt(context, context.claim.payload)
            except Exception as error:
                outcome = settle_claimed_failure(context, context.claim.payload, error)
            self.complete(context, outcome.status)
            return turns.get_turn(context.claim.payload['conversationId'],
                                  context.claim.payload['turnId'], user_id=self.owner)
        finally:
            chat_task_runtime.discard(context.claim.task_id)

    def close(self):
        # This module's disposable authority contains only test-created jobs.
        # Retire each outstanding synthetic worker before the next HTTP case.
        while True:
            document = self.store.claim_next(worker_id=self.worker_id,
                task_kinds=('conversation-attempt',), now_ms=time.time_ns() // 1_000_000,
                lease_ms=300_000)
            if document is None:
                break
            claim = WorkerJobClaim.from_document(document, expected_worker_id=self.worker_id)
            self.contexts[claim.payload['attemptId']] = WorkerExecutionContext(claim)
        for context in self.contexts.values():
            self.complete(context, 'cancelled')


@pytest.fixture
def worker_attempts(chat_sidecar, monkeypatch):
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *args, **kwargs: pytest.fail('HTTP entered physical execution'))
    workers = ConversationWorkers(owner=1)
    try:
        yield workers
    finally:
        workers.close()
