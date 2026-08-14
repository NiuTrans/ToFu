"""Transient runtime mutation application-boundary coverage."""

from __future__ import annotations

import pytest

from lib.orchestration.runtime_mutation_service import (
    OrchestrationRuntimeMutationService,
    RuntimeMutationError,
)
from lib.orchestration_mutation import MUTATION_NOT_FOUND, MUTATION_TERMINAL


pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self):
        self.tasks = {
            'done': {'status': 'done'},
            'active': {'status': 'running'},
        }

    def abort(self, task_id):
        return task_id == 'live'

    def get(self, task_id):
        return self.tasks.get(task_id)


def test_runtime_abort_uses_canonical_mutation_classification():
    service = OrchestrationRuntimeMutationService(_Runtime())

    accepted = service.abort('live')
    terminal = service.abort('done')
    missing = service.abort('missing')

    assert accepted.ok and accepted.run_status == 'aborting'
    assert terminal.reason == MUTATION_TERMINAL
    assert missing.reason == MUTATION_NOT_FOUND
    assert {result.target_id for result in (accepted, terminal, missing)} == {
        'live', 'done', 'missing',
    }


def test_runtime_abort_dependency_failure_uses_service_error_contract():
    class BrokenRuntime:
        def abort(self, _task_id):
            raise OSError('runtime registry unavailable')

        def get(self, _task_id):
            raise AssertionError('abort failure must remain primary')

    with pytest.raises(RuntimeMutationError) as caught:
        OrchestrationRuntimeMutationService(BrokenRuntime()).abort('live')

    assert isinstance(caught.value.__cause__, OSError)
    assert 'transient orchestration run' in str(caught.value)
