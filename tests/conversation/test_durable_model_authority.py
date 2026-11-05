"""Physical model dispatch shares root authority and keeps settled usage intact."""
import uuid

import pytest

from tests.storage import test_conversation_worker_publication as root

execution = root.execution

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('loss', ['stop', 'takeover'])
def test_model_dispatch_rechecks_authority_without_local_cancel_notification(execution, monkeypatch, loss):
    from lib import turn_lifecycle as turns
    from lib.agent_core.execution_budget import _ensure_execution_budget
    from lib.llm.llm_errors import AbortedError, ModelAdmissionRefused
    from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager import create_task, discard_task
    from tests.support.worker_job_time import wall_ms, wait_until_ms
    factory, owner, pair, _ = execution
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: factory(**kw))
    job = root.claim(execution)
    context = WorkerExecutionContext(WorkerJobClaim.from_document(job, expected_worker_id='first'))
    turns.mark_task_started(pair['attempt']['attemptId'], context.claim.task_id,
        user_id=owner, worker_claim=context.publication_claim())
    parent = create_task(pair['turn']['conversationId'], [], {
        '_attemptId': pair['attempt']['attemptId'], '_turnId': pair['turn']['turnId']},
        user_id=owner, principal=context.principal, worker_context=context)
    budget = _ensure_execution_budget(parent)
    child = create_child_invocation(parent, model='controlled', tools=[], objective='nested request',
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        first = budget.reserve_model(child, input_tokens=10, max_output_tokens=10)
        first.mark_dispatched()
        first.settle({'prompt_tokens': 10, 'completion_tokens': 2})
        if loss == 'stop':
            factory(write=True).command('worker_job.request_cancel', {
                'task_id': context.claim.task_id, 'user_id': owner,
                'now_ms': wall_ms(), 'reason': 'Stop'}, uuid.uuid4().hex)
        else:
            wait_until_ms(job['leaseDeadlineMs'] + 1)
            replacement = factory(write=True).command('worker_job.claim_next', {
                'worker_id': 'replacement', 'task_kinds': ['conversation-attempt'],
                'now_ms': wall_ms(), 'lease_ms': 300_000}, uuid.uuid4().hex)
            assert replacement['fencingToken'] > context.claim.fencing_token
        assert not context.cancellation_requested
        for actor in (parent, child):
            lease = budget.reserve_model(actor, input_tokens=10, max_output_tokens=10)
            with pytest.raises(AbortedError if loss == 'stop' else ModelAdmissionRefused):
                lease.mark_dispatched()
            assert not lease.dispatched
            lease.settle(unsent=True)
        assert budget.consumption_snapshot() == (12, 0, False)
        from lib.agent_core.execution_budget import close_execution_budget
        close_execution_budget(child)
        assert budget.consumption_snapshot() == (12, 0, True)
    finally:
        discard_task(child['id'])
        from lib.tasks_pkg.event_log import flush_pending
        flush_pending(parent['id'])
        discard_task(parent['id'])
