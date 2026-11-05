"""A consult grant summarizes under its own finite allowance; the ceiling still refuses.

Consult grants carry no tools and may keep the finite budgets they were issued
with. Their auxiliary summaries use the same bound account as every other
dispatch profile: each physical summary is recorded on the consult assignment
and counts against its model-request ceiling, exactly like an answer request.
"""
import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_project_dispatch_runtime import _lease, runtime as runtime

__all__ = ('context', 'runtime')

pytestmark = pytest.mark.unit


def test_consult_summary_consumes_the_same_finite_allowance(runtime):
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    r = runtime
    task = r.task()
    admission.bind_account(task)
    assert admission.profile_for(r.assignment)['mode'] == 'consult'
    assert r.assignment['request']['budget']['maxModelRequests'] == 2
    admit = summary_model_admission(task)
    summary = admit(input_tokens=4, max_output_tokens=6)
    summary.mark_dispatched()
    summary.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    answer = _lease(task)
    answer.mark_dispatched()
    answer.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    rows = r.c.repo.read(r.assignment['id'])['modelAttempts']
    assert len(rows) == 2 and sum(row['reported_tokens'] for row in rows) == 20
    # The issued ceiling refuses a further summary like any other request.
    extra = admit(input_tokens=4, max_output_tokens=6)
    with pytest.raises(AbortedError):
        extra.mark_dispatched()
    extra.settle(unsent=True)
    assert len(r.c.repo.read(r.assignment['id'])['modelAttempts']) == 2


def test_unbound_consult_summary_is_refused_before_any_send(runtime):
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    task = runtime.task()  # Never bound to its grant's conserved account.
    with pytest.raises(AbortedError, match='bound conserved'):
        summary_model_admission(task)
    assert runtime.c.repo.read(runtime.assignment['id'])['modelAttempts'] == []
    assert task['aborted'] is True
