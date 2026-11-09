"""A board commitment resumes the open request under its own authority.

Specs for docs/PROJECT_COORDINATION.md "Commitments on the task board": a due
commitment is a continuation reason (the lead may resume, re-delegate and
verify under the request's grant), a future one is a wait instead of a stall,
and each due time wakes the request at most once.
"""
from types import SimpleNamespace
import threading

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, controls, delegate, end, ready)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.conversations.coordinator_progress import CoordinatorTaskProgress
from lib.project_dispatch import continuation

pytestmark = pytest.mark.unit


@pytest.fixture
def board(run):
    r = run
    r.boards = []
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.client, bindings=r.c.binding,
                                       notify=lambda: None, clock=lambda: r.c.now[0],
                                       arm=lambda owner, scope, at: r.boards.append(at))
    r.board = progress
    r.publish = lambda tasks: progress.publish(
        {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1},
        {'summary': 'What the owner follows', 'tasks': tasks})
    return r


def iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec='seconds')


def test_a_future_commitment_is_a_wait_not_a_stall(board):
    r = board
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    due = r.c.now[0] + 3600
    r.publish([{'title': 'Report the quota-reset result', 'status': 'pending', 'due': iso(due)}])
    end(r, controls(r)[-1], delivered=False)
    assert r.wake() == [], 'waiting for the committed time is not a stalled turn'
    assert r.scheduled[-1] == due
    r.c.now[0] = due + 1
    assert r.wake() == ['accepted']
    control = controls(r)[-1]['request']
    assert control['coordinatorWake']['reason'] == 'commitment_due'
    assert control['coordinatorWake']['commitments'] == [
        {'title': 'Report the quota-reset result', 'dueAt': int(due * 1000)}]
    assert 'Report the quota-reset result' in control['brief']
    assert r.boards, 'a board write arms the due time'
    # The wake is receipted in the board head, so replaying the pass does not re-wake it.
    assert r.wake() == []
    assert len([row for row in controls(r)
                if row['request']['coordinatorWake']['reason'] == 'commitment_due']) == 1


def test_a_due_commitment_wakes_while_a_worker_is_still_running(board):
    r = board
    worker = delegate(r)
    end(r, worker, status='running', delivered=False)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress',
                'due': iso(r.c.now[0] + 10)}])
    r.c.now[0] += 20  # advance past the due time
    assert r.wake() == ['accepted']
    assert controls(r)[-1]['request']['coordinatorWake']['reason'] == 'commitment_due'
    assert r.attempts[worker['execution']['attemptId']]['status'] == 'running'
    assert r.wake() == [] and len(controls(r)) == 1


def test_a_commitment_already_woken_is_not_woken_again(board):
    r = board
    ready(r)
    end(r, controls(r)[-1], delivered=False)  # settle the first continuation
    due = r.c.now[0] + 5
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress', 'due': iso(due)}])
    r.c.now[0] += 10  # advance past the due time
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    assert r.wake() == [], 'the same due time never wakes twice'
    # Re-dating it is a new due time and wakes once more.
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress',
                'due': iso(r.c.now[0] + 5)}])
    r.c.now[0] += 10
    assert r.wake() == ['accepted']
    assert len([row for row in controls(r)
                if row['request']['coordinatorWake']['reason'] == 'commitment_due']) == 2


def test_a_commitment_of_another_coordinator_or_binding_is_ignored(board):
    r = board
    ready(r)
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress',
                'due': iso(r.c.now[0] + 5)}])
    r.c.now[0] += 10
    other = CoordinatorTaskProgress(7, '/other', client=r.c.client,
                                    bindings=SimpleNamespace(read=lambda path: {
                                        'availability': 'available', 'conversationId': 'someone-else',
                                        'revision': 1}), notify=lambda: None, clock=lambda: r.c.now[0])
    other.publish({'owner': 7, 'projectPath': '/other', 'coordinatorId': 'someone-else',
                   'bindingRevision': 1},
                  {'summary': 'Another project', 'tasks': [
                      {'title': 'Somebody else promised this', 'status': 'pending',
                       'due': iso(r.c.now[0] + 5)}]})
    r.wake()
    assert all(row['request']['coordinatorWake']['reason'] != 'commitment_due'
               or row['request']['coordinatorWake']['commitments'] == [
                   {'title': 'Report the restart outcome', 'dueAt': int((r.c.now[0] - 5) * 1000)}]
               for row in controls(r))
