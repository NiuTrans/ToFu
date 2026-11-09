"""Every decision not to resume an interrupted Worker lane leaves a trace (D8).

After the 2026-11-06 restart, four of five interrupted lanes got no restart
attempt, and nothing said why: ``recover_project_dispatch`` folded eight
unrelated conditions (a grant without the durable-Stop lifecycle, a revoked
grant, a passed deadline, an execution that was not restart-paused, ...) into
one ``skipped`` counter. The reason is now named per assignment, while plain
filters and settled executions stay quiet.
"""
import logging

import pytest

from lib.project_dispatch import recovery

pytestmark = pytest.mark.unit

NOW = 1_000.0


def _assignment(assignment_id, *, execution=True, revoked=False, lifecycle=1,
                deadline=None, recipient='worker-a'):
    return {
        'id': assignment_id,
        'request': {'profile': {'mode': 'project'}, 'projectPath': '/project',
                    'recipientId': recipient, 'coordinatorId': 'coordinator',
                    'budget': {'deadline': deadline}, 'dependencies': []},
        'grant': {'lifecycleVersion': lifecycle, 'owner': 1, 'revoked': revoked,
                  'createdAt': 1.0, 'revision': 1},
        'execution': {'turnId': 't', 'attemptId': 'a'} if execution else None,
        'modelAttempts': [],
    }


class _Repository:
    owner, project, storage_version = 1, '/project', 2

    def __init__(self, rows, paused):
        self.rows, self.paused = rows, paused

    def list(self):
        return self.rows

    def restart_paused(self, assignment):
        return assignment['id'] in self.paused

    def stopped(self, assignment):
        return False


def _recover(rows, paused, **kwargs):
    return recovery.recover_project_dispatch(
        1, '/project', repository=_Repository(rows, paused),
        service=object(), was_aborted_after=lambda *a: False, clock=lambda: NOW, **kwargs)


def _lines(caplog):
    return {(record.levelno, record.getMessage()) for record in caplog.records
            if 'unresumed' in record.getMessage()}


def test_an_interrupted_lane_left_unresumed_names_why(caplog):
    rows = [_assignment('revoked', revoked=True),
            _assignment('expired', deadline=NOW - 1),
            _assignment('legacy', lifecycle=0)]
    with caplog.at_level(logging.DEBUG, logger='lib.project_dispatch.recovery'):
        stats = _recover(rows, paused={'revoked', 'expired', 'legacy'})

    assert stats['skipped'] == 3
    lines = _lines(caplog)
    assert (logging.WARNING, '[ProjectDispatch] recovery left assignment=revoked owner=1 '
            'unresumed: grant_revoked') in lines
    assert (logging.WARNING, '[ProjectDispatch] recovery left assignment=expired owner=1 '
            'unresumed: deadline_passed') in lines
    assert (logging.INFO, '[ProjectDispatch] recovery left assignment=legacy owner=1 '
            'unresumed: grant_without_durable_stop_lifecycle') in lines


def test_filters_and_settled_lanes_stay_quiet(caplog):
    rows = [_assignment('settled'), _assignment('elsewhere', recipient='worker-b')]
    with caplog.at_level(logging.INFO, logger='lib.project_dispatch.recovery'):
        stats = _recover(rows, paused=set(), recipient_id='worker-a')

    assert stats['skipped'] == 2
    assert _lines(caplog) == set()
