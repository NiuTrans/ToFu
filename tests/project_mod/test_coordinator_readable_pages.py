"""Coordinator list reads return a first page the model reads inline, plus an exact cursor.

A page above the single tool-result budget reaches the model only as an
artifact pointer. get_project_workers keeps the leading rows that fit and
resumes after the last one shown, in both request and project scope.
"""
import json

import pytest

from tests.project_mod.test_coordinator_continuation import activate, delegate, end, ready, run as run
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context

__all__ = ('runtime', 'base_runtime', 'context')

pytestmark = pytest.mark.unit

_NOTE = ('Verified the migration evidence, the rollback rehearsal, the command output and the '
         'canonical delivery receipt against the brief. ' * 16)[:2048]


def reaches_model_inline(tool, value):
    from lib.tasks_pkg.compaction.api import budget_tool_result_v2
    view = json.loads(budget_tool_result_v2(tool, json.dumps(value, ensure_ascii=False), user_id=0, model=''))
    return view.get('status') == 'ok' and not view.get('truncated')


@pytest.mark.parametrize('scope', ['request', 'project'])
def test_worker_pages_fit_the_model_and_the_cursor_reaches_every_worker(run, scope):
    r = run
    workers = [ready(r)]
    activate(r)
    workers += [delegate(r, f'part-{index}') for index in range(15)]
    for row in workers[1:]:
        end(r, row)
    for row in workers:
        reviewed = r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                          decision='accept', note=_NOTE)
        assert 'error' not in reviewed, reviewed
    seen, cursor, pages = [], None, 0
    while True:
        page = r.call('get_project_workers', scope=scope, **({'after_id': cursor} if cursor else {}))
        assert 'error' not in page, page
        assert reaches_model_inline('get_project_workers', page), len(json.dumps(page))
        seen += [worker['assignment_id'] for worker in page['workers']]
        pages += 1
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert pages > 1
    assert len(seen) == len(set(seen)) and set(seen) == {row['id'] for row in workers}
