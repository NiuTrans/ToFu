"""Contract tests for orchestration state-changing operations."""

from __future__ import annotations

import pytest

from lib.orchestration.mutation_contract import mutation_contract
from lib.orchestration.mutation_operations import (
    resolved_mutation,
    runtime_abort_mutation,
)
from lib.orchestration.mutation_response import mutation_response
from lib.orchestration.mutation_result import (
    MUTATION_ACCEPTED,
    MUTATION_ACTIVE,
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_INPUT_GATE,
    MUTATION_ACTION_TRANSITION_RUN,
    MUTATION_CONFLICT,
    MUTATION_FORMAT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TRANSPORT_FAILED,
    OrchestrationMutationResult,
)


pytestmark = pytest.mark.unit


def test_versioned_payload_keeps_machine_state_and_retryability():
    result = OrchestrationMutationResult(
        False,
        MUTATION_PERSISTENCE_FAILED,
        run_status='running',
        action=MUTATION_ACTION_ABORT_RUN,
        target_id='run-1',
    )

    assert result.payload() == {
        'format': MUTATION_FORMAT,
        'ok': False,
        'action': MUTATION_ACTION_ABORT_RUN,
        'reason': MUTATION_PERSISTENCE_FAILED,
        'target_id': 'run-1',
        'resource_status': 'running',
        'resource_terminal': False,
        'target_exists': True,
        'retryable': True,
        'reconcile_required': True,
    }


def test_success_has_one_explicit_reason_and_one_canonical_payload():
    result = OrchestrationMutationResult(
        True,
        run_status='aborted',
        action=MUTATION_ACTION_ABORT_RUN,
        target_id='run-1',
    )
    payload, status = mutation_response(result)

    assert status == 200
    assert payload['ok'] is True
    assert set(payload) == {'ok', 'mutation'}
    assert payload['mutation']['reason'] == MUTATION_ACCEPTED
    assert payload['mutation']['target_exists'] is True
    assert payload['mutation']['reconcile_required'] is False
    assert 'error' not in payload


@pytest.mark.parametrize(
    ('reason', 'status'),
    [
        (MUTATION_NOT_FOUND, 404),
        (MUTATION_CONFLICT, 409),
        (MUTATION_PERSISTENCE_FAILED, 500),
    ],
)
def test_failure_reason_owns_http_classification(reason, status):
    result = OrchestrationMutationResult(
        False,
        reason,
        action=MUTATION_ACTION_DELETE_RUN,
        target_id='run-1',
    )
    payload, actual_status = mutation_response(result)

    assert actual_status == status
    assert payload['ok'] is False
    assert payload['mutation']['reason'] == reason
    assert payload['error']


def test_gate_resolver_uses_the_same_contract_without_exposing_response_text():
    calls = []
    accepted = resolved_mutation(
        MUTATION_ACTION_APPROVE_GATE,
        'gate-1',
        lambda: calls.append('resolved') or True,
    )
    missing = resolved_mutation(
        MUTATION_ACTION_APPROVE_GATE,
        'gate-2',
        lambda: False,
    )

    assert calls == ['resolved']
    assert accepted.ok and accepted.payload()['reason'] == MUTATION_ACCEPTED
    assert accepted.payload()['target_exists'] is False
    assert accepted.target_id == 'gate-1'
    assert not missing.ok and missing.reason == MUTATION_NOT_FOUND
    assert missing.payload()['target_exists'] is False


@pytest.mark.parametrize(
    ('action', 'ok', 'reason', 'expected'),
    [
        (MUTATION_ACTION_APPROVE_GATE, True, '', False),
        (MUTATION_ACTION_INPUT_GATE, False, MUTATION_NOT_FOUND, False),
        (MUTATION_ACTION_INPUT_GATE, False, MUTATION_CONFLICT, None),
        (MUTATION_ACTION_DELETE_RUN, True, '', False),
        (MUTATION_ACTION_DELETE_RUN, False, MUTATION_NOT_FOUND, False),
        (MUTATION_ACTION_DELETE_RUN, False, MUTATION_ACTIVE, True),
        (MUTATION_ACTION_DELETE_RUN, False,
         MUTATION_PERSISTENCE_FAILED, None),
        (MUTATION_ACTION_ABORT_RUN, False, MUTATION_NOT_FOUND, False),
        (MUTATION_ACTION_ABORT_RUN, False, MUTATION_CONFLICT, True),
        (MUTATION_ACTION_TRANSITION_RUN, True, '', True),
        ('future_action', False, MUTATION_NOT_FOUND, None),
    ],
)
def test_target_presence_is_scoped_to_the_mutation_action(
    action, ok, reason, expected,
):
    result = OrchestrationMutationResult(
        ok,
        reason,
        action=action,
        target_id='target-1',
    )

    assert result.target_exists is expected
    assert result.payload()['target_exists'] is expected


def test_scoping_retains_old_run_result_constructor_shape():
    legacy = OrchestrationMutationResult(False, MUTATION_CONFLICT, 'done')
    scoped = legacy.scoped(MUTATION_ACTION_ABORT_RUN, 'run-1')

    assert scoped.run_status == 'done'
    assert scoped.action == MUTATION_ACTION_ABORT_RUN
    assert scoped.target_id == 'run-1'


@pytest.mark.parametrize(
    ('status', 'expected'),
    [('', None), ('running', False), ('done', True), ('future', None)],
)
def test_resource_terminal_is_projected_only_for_canonical_statuses(
    status, expected,
):
    result = OrchestrationMutationResult(
        True,
        run_status=status,
        action=MUTATION_ACTION_TRANSITION_RUN,
        target_id='run-1',
    )

    assert result.resource_terminal is expected
    assert result.payload()['resource_terminal'] is expected


def test_runtime_abort_distinguishes_accepted_terminal_and_missing():
    class Runtime:
        def __init__(self):
            self.tasks = {
                'live': {'status': 'running'},
                'done': {'status': 'done'},
            }

        def abort_owned(self, task_id, *, user_id):
            return user_id == 41 and task_id == 'live'

        def get_owned(self, task_id, *, user_id):
            return self.tasks.get(task_id) if user_id == 41 else None

    runtime = Runtime()
    accepted = runtime_abort_mutation(runtime, 'live', 41)
    terminal = runtime_abort_mutation(runtime, 'done', 41)
    missing = runtime_abort_mutation(runtime, 'missing', 41)
    cross_owner = runtime_abort_mutation(runtime, 'live', 42)

    assert accepted.ok and accepted.run_status == 'aborting'
    assert terminal.reason == 'terminal' and terminal.run_status == 'done'
    assert missing.reason == MUTATION_NOT_FOUND
    assert cross_owner.reason == MUTATION_NOT_FOUND
    assert all(result.action == MUTATION_ACTION_ABORT_RUN for result in (
        accepted, terminal, missing,
    ))


def test_contract_is_serializable_and_complete():
    contract = mutation_contract()

    assert contract['format'] == MUTATION_FORMAT
    assert set(contract['actions']) >= {
        MUTATION_ACTION_ABORT_RUN,
        MUTATION_ACTION_DELETE_RUN,
        MUTATION_ACTION_APPROVE_GATE,
    }
    assert MUTATION_ACCEPTED in contract['reasons']
    assert contract['retryableReasons'] == [MUTATION_PERSISTENCE_FAILED]
    assert contract['transportFailureReason'] == MUTATION_TRANSPORT_FAILED
    assert contract['clientRetryableReasons'] == [
        MUTATION_PERSISTENCE_FAILED, MUTATION_TRANSPORT_FAILED,
    ]
    assert contract['httpStatusByReason'][MUTATION_ACCEPTED] == 200
    assert contract['httpStatusByReason'][MUTATION_NOT_FOUND] == 404
    assert contract['httpStatusByReason'][MUTATION_CONFLICT] == 409
    assert contract['httpStatusByReason'][MUTATION_PERSISTENCE_FAILED] == 500
    assert contract['reconcileField'] == 'reconcile_required'
    assert contract['targetExistsField'] == 'target_exists'
    assert contract['resourceTerminalField'] == 'resource_terminal'
    assert {
        semantic: spec['name']
        for semantic, spec in contract['payloadFields'].items()
    } == {
        'format': 'format',
        'ok': 'ok',
        'action': 'action',
        'reason': 'reason',
        'targetId': 'target_id',
        'resourceStatus': 'resource_status',
        'resourceTerminal': 'resource_terminal',
        'targetExists': 'target_exists',
        'retryable': 'retryable',
        'reconcileRequired': 'reconcile_required',
    }
