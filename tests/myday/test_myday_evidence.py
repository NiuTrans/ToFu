"""Goals mapped to a Coordinator request show its Workers as evidence.

Evidence is read from the request's dispatch rows alone (one bounded cohort read
per linked goal, no attempt or transcript reads, per the DB load measurement of
2026-11-08) and the owner's live-task set. The owner's manual status always wins.
"""
from __future__ import annotations

import pytest

from tests.support.myday_records import OWNER

pytestmark = pytest.mark.unit
SOURCE = 'coordinator-attempt-1'
LINK = {'projectPath': '/work/tofu', 'coordinatorId': 'coordinator-1', 'sourceAttemptId': SOURCE,
        'requestTurnId': 'human-turn-1', 'label': '看一下 tofu 的稳定性'}


def row(ident, *, review=None, execution=True, revoked=False, rework_of=None, mode='project',
        source=SOURCE, brief=None):
    request = {'recipientId': 'worker-' + ident, 'coordinatorId': 'coordinator-1',
               'brief': brief or f'[{ident.upper()}] Fix part {ident}\nDetails for the worker.',
               'profile': {'mode': mode},
               'authorizationSource': {'conversationId': 'coordinator-1', 'turnId': 'human-turn-1',
                                       'projectionRevision': 1},
               'delegation': {'attemptId': source, 'turnId': 'coordinator-turn', 'taskId': 't'}}
    if rework_of:
        request['reworkOf'] = {'assignmentId': rework_of, 'grantRevision': 1, 'resultRevision': 2}
    return {'id': ident, 'request': request,
            'grant': {'revoked': revoked, 'owner': OWNER, 'createdAt': 1_794_000_000},
            'execution': {'turnId': 't-' + ident, 'attemptId': 'a-' + ident} if execution else None,
            'review': {'decision': review, 'note': '', 'resultRevision': 2} if review else None}


def test_worker_rows_project_to_explainable_states():
    from lib.myday.contract import validate
    from lib.myday.evidence import worker_evidence
    rows = [row('a1', review='accept'), row('a2'), row('a3'), row('a4', execution=False),
            row('a5', revoked=True), row('a6', review='rework'), row('a7', rework_of='a6'),
            row('a8', review='reject'), row('c0', mode='coordinator')]
    workers = worker_evidence(rows, busy={'worker-a2', 'worker-a7'})
    projected = {worker['assignmentId']: (worker['state'], worker['reason']) for worker in workers}
    assert projected == {
        'a1': ('completed', 'accepted'), 'a2': ('in_progress', 'running'),
        'a3': ('in_progress', 'awaiting_review'), 'a4': ('pending', 'queued'),
        'a5': ('cancelled', 'cancelled'), 'a6': ('cancelled', 'superseded'),
        'a7': ('in_progress', 'running'), 'a8': ('cancelled', 'rejected'),
    }, 'a Coordinator\'s own row is not a Worker'
    assert workers[0]['title'] == '[A1] Fix part a1' and workers[0]['conversationId'] == 'worker-a1'
    for worker in workers:
        validate({key: worker[key] for key in ('assignmentId', 'conversationId', 'review', 'state', 'reason')},
                 'workerEvidence')


def linked(records, clock, rows, *, settled=False, busy=frozenset(), failure=None):
    """``failure`` is a mutable box: {'error': exception or None} read on every view."""
    from lib.myday.evidence import request_evidence
    from lib.myday.goals import GoalStore, goal_view
    goals = GoalStore(OWNER, client=records, clock=clock)
    goal = goals.create('让 tofu 稳定下来')
    goals.link(goal['id'], LINK)

    def read_rows(owner, link):
        assert owner == OWNER and link['sourceAttemptId'] == SOURCE
        if failure and failure.get('error'):
            raise failure['error']
        return rows, settled

    def show():
        stored = goals.read(goal['id'])
        return goal_view(stored, request=request_evidence(OWNER, stored['link'], read_rows=read_rows,
                                                          busy=lambda owner: set(busy)))
    return goals, goal, show


def test_linked_goal_counts_workers_and_the_owner_override_is_authoritative(records, clock):
    rows = [row('a1', review='accept'), row('a2'), row('a3', execution=False), row('a4', revoked=True)]
    goals, goal, show = linked(records, clock, rows, busy={'worker-a2'})
    goals.add_item(goal['id'], '写一份稳定性复盘')
    shown = show()
    evidence = [item for item in shown['items'] if item['origin'] == 'evidence']
    assert [(item['status'], item['counted']) for item in evidence] == [
        ('completed', True), ('in_progress', True), ('pending', True), ('cancelled', False)]
    assert all(item['id'].startswith('w-') and not item['manual'] for item in evidence)
    assert shown['progress'] == {'counted': 4, 'completed': 1, 'inProgress': 1, 'pending': 2,
                                 'suggested': 0, 'percent': 25}
    assert shown['request'] == {'available': True, 'settled': False, 'workers': 4, 'reason': None}

    queued = evidence[2]['id']
    goals.update_item(goal['id'], queued, status='completed',
                      evidence={'assignmentId': 'a3', 'title': evidence[2]['text']})
    overridden = next(item for item in show()['items'] if item['id'] == queued)
    assert overridden['status'] == 'completed' and overridden['manual'] is True
    assert overridden['evidence']['state'] == 'pending', 'the evidence stays visible beside the override'
    assert show()['progress']['percent'] == 50

    rows[2] = row('a3')  # the Worker starts; the owner's override still wins
    assert next(item for item in show()['items'] if item['id'] == queued)['status'] == 'completed'
    goals.update_item(goal['id'], queued, follow_evidence=True)
    assert next(item for item in show()['items'] if item['id'] == queued)['status'] == 'in_progress'
    assert [item for item in goals.read(goal['id'])['items'] if item['origin'] == 'evidence'] == []


def test_unavailable_evidence_is_explicit_and_keeps_owner_overrides(records, clock):
    from lib.storage.errors import StorageError
    box = {'error': None}
    goals, goal, show = linked(records, clock, [row('a1'), row('a2')], failure=box)
    worker = next(item for item in show()['items'] if item['origin'] == 'evidence')
    goals.update_item(goal['id'], worker['id'], status='in_progress',
                      evidence={'assignmentId': 'a1', 'title': worker['text']})
    box['error'] = StorageError('database_timeout', 'slow')
    shown = show()
    assert shown['request']['available'] is False and 'database_timeout' in shown['request']['reason']
    [override] = shown['items']
    assert (override['text'], override['status'], override['manual']) == (worker['text'], 'in_progress', True)
    assert 'evidence' not in override, 'no evidence is invented while it cannot be read'
    assert shown['progress']['counted'] == 1
    assert shown['progress']['percent'] is None, 'a percentage over the visible items would overstate progress'
    box['error'] = None
    assert show()['request']['available'] is True and len(show()['items']) == 2


def test_settled_request_is_reported_without_inventing_completion(records, clock):
    goals, goal, show = linked(records, clock, [row('a1')], settled=True)
    shown = show()
    assert shown['request']['settled'] is True
    assert shown['progress']['completed'] == 0, 'settlement is shown, not converted into done items'


def test_link_rejects_an_unknown_request_and_unlink_drops_overrides(records, clock):
    from lib.myday.goals import GoalStore, evidence_item_id
    goals = GoalStore(OWNER, client=records, clock=clock)
    goal = goals.create('x')
    with pytest.raises(ValueError):
        goals.link(goal['id'], {**LINK, 'sourceAttemptId': ''})
    goals.link(goal['id'], LINK)
    with pytest.raises(ValueError, match='does not match'):
        goals.update_item(goal['id'], 'w-' + '0' * 16, status='completed',
                          evidence={'assignmentId': 'a1', 'title': 'Fix part a1'})
    goals.update_item(goal['id'], evidence_item_id('a1'), status='completed',
                      evidence={'assignmentId': 'a1', 'title': 'Fix part a1'})
    assert [item['origin'] for item in goals.read(goal['id'])['items']] == ['evidence']
    unlinked = goals.unlink(goal['id'])
    assert unlinked['link'] is None and unlinked['items'] == []


def test_linkable_requests_group_the_newest_history_by_request():
    from lib.myday.contract import validate
    from lib.myday.evidence import linkable_requests
    other = 'coordinator-attempt-2'
    rows = [row('b2', source=other), row('b1', source=other), row('a2'), row('a1'),
            row('c0', mode='coordinator')]
    rows[0]['request']['authorizationSource']['turnId'] = 'human-turn-2'
    rows[1]['request']['authorizationSource']['turnId'] = 'human-turn-2'
    texts = {'human-turn-1': '看一下 tofu 的稳定性\n第二行', 'human-turn-2': 'Ship the release'}

    requests = linkable_requests(
        OWNER, projects=lambda owner: [('/work/tofu', 'coordinator-1')],
        newest_rows=lambda owner, project: rows,
        request_text=lambda owner, coordinator, turn: texts[turn])
    assert [(item['sourceAttemptId'], item['label'], item['workers']) for item in requests] == [
        (other, 'Ship the release', 2), (SOURCE, '看一下 tofu 的稳定性', 2)]
    for item in requests:
        validate(item, 'linkableRequest')
