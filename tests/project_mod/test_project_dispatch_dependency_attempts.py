"""Real ExecutionBudget with pinned prerequisite withdrawal; no provider ingress."""
from copy import deepcopy
import threading

import time

import pytest

from lib.agent_core.execution_budget import model_budget_admission
from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission

from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_project_dispatch_dependencies import (
    dependency_context as dependency_context, completed, dependent, reference,
)

pytestmark = pytest.mark.unit


def bound_task(c):

    c.request['budget']['deadline'] = time.time() + 300
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    c.repo.bind_execution(row['id'], 'dependent-turn', 'dependent-attempt')
    c.attempts['dependent-attempt'] = {'attemptId': 'dependent-attempt', 'turnId': 'dependent-turn',
        'conversationId': 'recipient', 'taskId': 'dependent-task', 'status': 'running'}
    trusted = admission.TrustedDispatch(c.repo.admission(row['id']))
    cfg = trusted.capture({}, c.repo.owner, trusted.principal, 'recipient')
    cfg.update(_turnOwnerUserId=c.repo.owner, _turnId='dependent-turn', _attemptId='dependent-attempt')
    task = {'id': 'dependent-task', 'convId': 'recipient', 'config': cfg, 'status': 'running',
        '_userId': c.repo.owner, '_principalContext': trusted.principal.to_payload(),
        '_turnId': 'dependent-turn', '_attemptId': 'dependent-attempt', '_attended': False,
        'usage': {}, 'abort_event': threading.Event()}
    admission.bind_account(task)
    assert admission.validate_task(task) == c.repo.admission(row['id'])
    return prior, row, task


def reserve(task):
    return model_budget_admission(task)(input_tokens=4, max_output_tokens=6)


def test_prerequisite_revoke_after_reservation_prevents_first_physical_intent(dependency_context):
    c = dependency_context
    prior, row, task = bound_task(c)
    lease = reserve(task)
    c.repo.revoke(prior['id'], 1)
    with pytest.raises((AbortedError, ValueError)):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    assert c.repo.read(row['id'])['modelAttempts'] == []
    assert c.repo.read(row['id'])['execution'] == {'turnId': 'dependent-turn', 'attemptId': 'dependent-attempt'}


def test_withdrawal_blocks_new_spending_but_does_not_strand_paid_settlement(dependency_context):
    c = dependency_context
    prior, row, task = bound_task(c)
    lease = reserve(task)
    lease.mark_dispatched()
    intent = deepcopy(c.repo.read(row['id'])['modelAttempts'][0])
    c.repo.revoke(prior['id'], 1)
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    settled = c.repo.read(row['id'])['modelAttempts'][0]
    assert settled['phase'] == 'settled' and settled['usage_complete'] is True
    assert settled['reported_tokens'] == 10
    assert settled['account_nonce'] == intent['account_nonce']
    assert settled['root_task_id'] == intent['root_task_id'] == 'dependent-task'
    with pytest.raises((AbortedError, ValueError)):
        later = reserve(task)
        try:
            later.mark_dispatched()
        finally:
            later.settle(unsent=True)
    assert c.repo.read(row['id'])['modelAttempts'] == [settled]
    assert c.repo.dependency_states(c.repo.read(row['id']))[0]['reason'] == 'revoked'
