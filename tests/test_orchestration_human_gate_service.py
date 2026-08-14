"""Contract tests for the orchestration human-gate application boundary."""

from __future__ import annotations

import pytest

from lib.orchestration.human_gate_service import (
    HumanGateServiceError,
    OrchestrationHumanGateService,
)
from lib.orchestration_mutation import (
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_INPUT_GATE,
    MUTATION_NOT_FOUND,
)


pytestmark = pytest.mark.unit


def test_gate_service_preserves_inputs_and_projects_canonical_mutations():
    calls = []
    service = OrchestrationHumanGateService(
        approval_resolver=lambda request_id, approved: (
            calls.append(('approve', request_id, approved)) or approved
        ),
        guidance_resolver=lambda request_id, response: (
            calls.append(('input', request_id, response)) or False
        ),
    )

    approval = service.approve('opaque/approval', True)
    guidance = service.input('opaque/input', 'continue verbatim')

    assert calls == [
        ('approve', 'opaque/approval', True),
        ('input', 'opaque/input', 'continue verbatim'),
    ]
    assert approval.ok is True
    assert approval.action == MUTATION_ACTION_APPROVE_GATE
    assert approval.target_id == 'opaque/approval'
    assert approval.target_exists is False
    assert guidance.ok is False
    assert guidance.reason == MUTATION_NOT_FOUND
    assert guidance.action == MUTATION_ACTION_INPUT_GATE
    assert guidance.target_id == 'opaque/input'
    assert guidance.target_exists is False


@pytest.mark.parametrize(('method', 'resolver_name', 'expected_message'), [
    ('approve', 'approval_resolver',
     'failed to resolve orchestration approval request'),
    ('input', 'guidance_resolver',
     'failed to resolve orchestration input request'),
])
def test_gate_dependency_failures_use_shared_application_error_boundary(
    method, resolver_name, expected_message,
):
    failure = OSError('shared gate registry unavailable')

    def fail(*_args):
        raise failure

    service = OrchestrationHumanGateService(**{resolver_name: fail})

    with pytest.raises(HumanGateServiceError, match=expected_message) as caught:
        if method == 'approve':
            service.approve('gate-1', True)
        else:
            service.input('gate-1', 'continue')

    assert caught.value.__cause__ is failure
