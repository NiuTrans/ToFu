"""Transient runtime mutation application-boundary coverage."""

from __future__ import annotations

import pytest

from lib.orchestration.runtime_mutation_service import (
    OrchestrationRuntimeMutationService,
    RuntimeMutationError,
)
from lib.orchestration.mutation_result import (
    MUTATION_NOT_FOUND,
    MUTATION_TERMINAL,
)


pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self):
        self.tasks = {
            'done': {'status': 'done'},
            'active': {'status': 'running'},
        }

    def abort_owned(self, task_id, *, user_id):
        return user_id == 41 and task_id == 'live'

    def get_owned(self, task_id, *, user_id):
        return self.tasks.get(task_id) if user_id == 41 else None


def test_runtime_abort_uses_canonical_mutation_classification():
    service = OrchestrationRuntimeMutationService(_Runtime(), 41)

    accepted = service.abort('live')
    terminal = service.abort('done')
    missing = service.abort('missing')

    assert accepted.ok and accepted.run_status == 'aborting'
    assert terminal.reason == MUTATION_TERMINAL
    assert missing.reason == MUTATION_NOT_FOUND
    assert {result.target_id for result in (accepted, terminal, missing)} == {
        'live', 'done', 'missing',
    }
    assert OrchestrationRuntimeMutationService(
        _Runtime(), 42).abort('done').reason == MUTATION_NOT_FOUND


def test_runtime_abort_dependency_failure_uses_service_error_contract():
    class BrokenRuntime:
        def abort_owned(self, _task_id, *, user_id):
            assert user_id == 41
            raise OSError('runtime registry unavailable')

        def get_owned(self, _task_id, *, user_id):
            raise AssertionError('abort failure must remain primary')

    with pytest.raises(RuntimeMutationError) as caught:
        OrchestrationRuntimeMutationService(BrokenRuntime(), 41).abort('live')

    assert isinstance(caught.value.__cause__, OSError)
    assert 'transient orchestration run' in str(caught.value)
