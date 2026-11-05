"""Dispatch waits preserve exact occurrence accounting on each selected authority."""
import pytest

from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_coordinator_workers import _worker_task, base_runtime, runtime
from tests.project_mod.test_coordinator_request_budget import bind, spend, usage
from tests.storage.test_coordinator_segmented_allowance import segmented_runtime

pytest_plugins = ('tests.support.semantic_authorities',)
# The native authority admits only its own owner; this runtime's dispatch scope is owner 7.
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'postgres'], indirect=True)]
__all__ = ('base_runtime', 'context', 'runtime', 'segmented_runtime')


@pytest.mark.parametrize('boundary', ['model', 'tool'])
def test_segmented_occurrence_survives_timeout_and_lost_ack(
        segmented_runtime, monkeypatch, boundary):
    r = segmented_runtime
    source = bind(r)
    task, assignment = _worker_task(r)
    factory = r.c.repo.client
    failures = []
    read_failures = []
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)

    class UncertainAuthority:
        def query(self, *args, **kwargs):
            if len(read_failures) < 12:
                read_failures.append(True)
                raise StorageError('database_timeout', 'deadline_elapsed', retryable=True)
            return factory().query(*args, **kwargs)

        def command(self, *args, **kwargs):
            assert args[0] == 'project.dispatch.commit'
            if not failures:
                failures.append('before commit')
                raise StorageError('database_timeout', 'deadline_elapsed', retryable=False)
            result = factory(write=True).command(*args, **kwargs)
            if len(failures) == 1:
                failures.append('after commit')
                raise StorageError('database_timeout', 'reply lost', retryable=False)
            return result

    monkeypatch.setattr(r.c.repo, 'client', lambda **kwargs: UncertainAuthority())
    budget = task['_executionBudget']
    if boundary == 'model':
        spend(task, 50)
    else:
        assert budget.reserve_tool(task) is None
        event = budget.begin_tool_attempt(task, 'one-effect', 'read_files', 1)
        budget.finish_tool_attempt(event, phase='executor_returned')
    evidence = usage(r, source)
    assert failures == ['before commit', 'after commit']
    assert len(read_failures) == 12
    assert (evidence['tokens'], evidence['modelRequests'], evidence['toolCalls']) == (
        (50, 1, 0) if boundary == 'model' else (0, 0, 1))
    assert evidence['usageComplete'] and not task.get('aborted')
    if boundary == 'tool':
        occurrence, = r.c.repo.read(assignment['id'])['toolAttempts']
        assert occurrence['phase'] == 'executor_returned'
