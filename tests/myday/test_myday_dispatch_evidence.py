"""request_rows reads exactly one Coordinator request's Workers from dispatch.

Real delegation through the Coordinator pipeline (the coordinator worker
fixture) feeds the My Day evidence projection, so the goal view maps real
dispatch rows, not hand-built ones.
"""
from __future__ import annotations

import pytest

from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_coordinator_workers import runtime as runtime
from tests.project_mod.test_project_dispatch_repository import context as context

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('base_runtime', 'context', 'runtime')

pytestmark = pytest.mark.unit


def test_request_rows_select_one_request_and_feed_worker_evidence(runtime):
    from lib.myday.evidence import worker_evidence
    from lib.project_dispatch.repository import ProjectDispatchError
    r = runtime
    first = r.call('delegate_project_work', **r.args)
    second = r.call('delegate_project_work', **{**r.args, 'title': 'Implement part two',
                                                  'idempotency_key': 'part-two'})
    cohort = r.c.repo.request_rows('coordinator-attempt')
    assert [row['id'] for row in cohort['rows']] == [first['assignment_id'], second['assignment_id']]
    assert cohort['settled'] is False
    assert all(row['request']['delegation']['attemptId'] == 'coordinator-attempt' for row in cohort['rows'])
    assert r.c.repo.request_rows('another-attempt') == {'rows': [], 'settled': False}
    with pytest.raises(ProjectDispatchError):
        r.c.repo.request_rows('')

    workers = worker_evidence(cohort['rows'], busy={first['conversation_id']})
    assert [(worker['assignmentId'], worker['state'], worker['reason']) for worker in workers] == [
        (first['assignment_id'], 'in_progress', 'running'),
        (second['assignment_id'], 'in_progress', 'awaiting_review')]
    assert workers[0]['title'] == 'Implement and verify the requested feature.'
