"""Commitments: a board task carrying ``due`` is a promise with a due wake.

Specs for docs/PROJECT_COORDINATION.md "Commitments on the task board" and
docs/ROUTER.md R10: the board is the owner's promise ledger, an open commitment
cannot vanish silently, receipts ride the same head, and a due commitment wakes
its lead exactly once per due time — as the open request's continuation when it
has one, otherwise as one nonhuman notice.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading

import pytest

from lib.conversations.coordinator_commitments import (
    DUE_TASK_TYPE, commitment_identity, due_ms, render_due_notice, run_commitment_due)
from lib.conversations.coordinator_progress import CoordinatorTaskProgress
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0
FUTURE = '2030-03-01T12:00:00+00:00'
LATER = '2030-03-02T12:00:00+00:00'
EARLIER = '2030-02-28T12:00:00+00:00'
PAST = '2020-01-01T00:00:00+00:00'


class Store:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        assert operation == 'record.get'
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            if (old['version'] if old else 0) != payload['expected_version']:
                raise StorageError('database_conflict', 'CAS conflict')
            self.rows[identity] = {'value': deepcopy(payload['value']),
                                   'version': payload['expected_version'] + 1}
            self.puts = getattr(self, 'puts', 0) + 1
            return {'version': payload['expected_version'] + 1}


@pytest.fixture(autouse=True)
def _no_scheduler(monkeypatch):
    monkeypatch.setattr('lib.conversations.coordinator_commitments.arm_due_commitments',
                        lambda *args, **kwargs: None)


@pytest.fixture
def board():
    store = Store()
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    armed = []
    clock = [NOW]

    def factory(owner=7, path='/project'):
        return CoordinatorTaskProgress(owner, path, client=store, bindings=bindings,
                                       notify=lambda: None, clock=lambda: clock[0],
                                       arm=lambda owner, scope, at: armed.append((owner, scope, at)))



    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    return SimpleNamespace(store=store, bindings=bindings, factory=factory, scope=scope, armed=armed,
                           clock=clock,
                           head=lambda: deepcopy(next(iter(store.rows.values()))['value']))


def request(tasks, summary='Two promises and one finished task.'):
    return {'summary': summary, 'tasks': tasks}


def test_due_is_stored_and_a_new_or_changed_due_must_be_in_the_future(board):
    board.factory().publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE}]))
    assert board.head()['board']['tasks'][0]['due'] == FUTURE
    with pytest.raises(ValueError, match='future'):
        board.factory().publish(board.scope, request([
            {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': PAST}]))
    with pytest.raises(ValueError):
        board.factory().publish(board.scope, request([
            {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': 'tomorrow 9am'}]))
    # An unchanged due time stays valid even after it has passed.
    board.clock[0] = due_ms(FUTURE) / 1000 + 1
    board.factory().publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'completed', 'due': FUTURE}]))
    assert board.head()['commitments'][0]['state'] == 'late'


def test_an_open_commitment_cannot_be_dropped_or_have_its_due_removed(board):
    board.factory().publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE},
        {'title': 'Unrelated work', 'status': 'pending'}]))
    with pytest.raises(ValueError, match='disappear'):
        board.factory().publish(board.scope, request([{'title': 'Unrelated work', 'status': 'pending'}]))
    with pytest.raises(ValueError, match='disappear'):
        board.factory().publish(board.scope, request([
            {'title': 'Renamed promise', 'status': 'in_progress', 'due': FUTURE},
            {'title': 'Unrelated work', 'status': 'pending'}]))
    with pytest.raises(ValueError, match='removes its due'):
        board.factory().publish(board.scope, request([
            {'title': 'Report the restart outcome', 'status': 'in_progress'},
            {'title': 'Unrelated work', 'status': 'pending'}]))
    # Completing or cancelling is allowed: that is how a commitment is closed.
    board.factory().publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'completed', 'due': FUTURE}]))
    assert board.head()['commitments'][0]['state'] == 'kept'
    board.factory().publish(board.scope, request([]))
    assert not board.head().get('commitments')


def test_two_open_commitments_cannot_share_a_title(board):
    with pytest.raises(ValueError, match='title'):
        board.factory().publish(board.scope, request([
            {'title': 'Same title', 'status': 'in_progress', 'due': FUTURE},
            {'title': 'Same title', 'status': 'pending', 'due': LATER}]))


def test_receipts_track_first_due_slips_and_closures(board):
    progress = board.factory()
    progress.publish(board.scope, request([
        {'title': 'Resume the evaluation when the quota resets', 'status': 'pending', 'due': FUTURE}]))
    receipt = board.head()['commitments'][0]
    assert receipt['firstDueAt'] == receipt['dueAt'] == due_ms(FUTURE)
    assert receipt['slips'] == 0 and receipt['state'] == 'open'
    progress.publish(board.scope, request([
        {'title': 'Resume the evaluation when the quota resets', 'status': 'in_progress', 'due': LATER}]))
    slipped = board.head()['commitments'][0]
    assert slipped['firstDueAt'] == due_ms(FUTURE)
    assert slipped['dueAt'] == due_ms(LATER)
    assert slipped['slips'] == 1
    progress.publish(board.scope, request([
        {'title': 'Resume the evaluation when the quota resets', 'status': 'cancelled', 'due': LATER}]))
    assert board.head()['commitments'][0]['state'] == 'cancelled'
    # A late completion is recorded as late, not kept.
    progress.publish(board.scope, request([
        {'title': 'Resume the evaluation when the quota resets', 'status': 'in_progress', 'due': FUTURE}]))
    board.clock[0] = due_ms(FUTURE) / 1000 + 1
    progress.publish(board.scope, request([
        {'title': 'Resume the evaluation when the quota resets', 'status': 'completed', 'due': FUTURE}]))
    assert board.head()['commitments'][0]['state'] == 'late'


def test_a_checklist_write_keeps_each_task_due_and_the_drop_guard_applies(board):
    from lib.conversations.coordinator_progress import board_from_plan
    previous = {'summary': 's', 'tasks': [
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE,
         'progress': 'Waiting for the new server.'}]}
    built = board_from_plan(previous, [{'content': 'Report the restart outcome', 'status': 'in_progress'}], 's')
    assert built['tasks'][0]['due'] == FUTURE
    assert built['tasks'][0]['progress'] == 'Waiting for the new server.'
    board.factory().publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE}]))
    with pytest.raises(ValueError, match='disappear'):
        board.factory().publish_plan(board.scope, [], '')


def test_a_board_write_arms_the_earliest_open_due_time(board):
    progress = board.factory()
    progress.publish(board.scope, request([{'title': 'No promise', 'status': 'pending'}]))
    assert board.armed == []
    progress.publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': LATER},
        {'title': 'Resume the evaluation', 'status': 'pending', 'due': FUTURE}]))
    assert len(board.armed) == 1
    owner, scope, at = board.armed[0]
    assert owner == 7 and scope['coordinatorId'] == 'coordinator'
    assert at == due_ms(FUTURE) / 1000
    # Closing the earlier commitment re-arms for the later one.
    progress.publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': LATER},
        {'title': 'Resume the evaluation', 'status': 'completed', 'due': FUTURE}]))
    assert board.armed[-1][2] == due_ms(LATER) / 1000
    # An unchanged board arms nothing new.
    progress.publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': LATER},
        {'title': 'Resume the evaluation', 'status': 'completed', 'due': FUTURE}]))
    assert len(board.armed) == 2


def scheduler_row(board, project='/project'):
    return {'id': 'commitment-due-test', 'user_id': 7,
            'command': json.dumps({'projectPath': project, 'coordinatorId': 'coordinator',
                                   'bindingRevision': 1})}


def test_due_commitments_wake_the_lead_once_with_one_notice(board, monkeypatch):
    monkeypatch.setattr('lib.conversations.coordinator_commitments.arm_due_commitments',
                        lambda *args, **kwargs: None)
    progress = board.factory()
    progress.publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE},
        {'title': 'Report the evaluation result', 'status': 'pending', 'due': FUTURE},
        {'title': 'Still waiting', 'status': 'pending', 'due': LATER}]))
    board.clock[0] = due_ms(FUTURE) / 1000 + 1
    sent = []

    def dispatch(conversation_id, message, tools_config, **kwargs):
        sent.append((conversation_id, message, kwargs))
        return SimpleNamespace(disposition='started')

    ok, result = run_commitment_due(scheduler_row(board), client=board.store, bindings=board.bindings,
                                    dispatch=dispatch, recover=lambda repository: [],
                                    clock=lambda: board.clock[0])
    assert ok, result
    assert len(sent) == 1, 'two due commitments are one wake, not two'
    conversation_id, message, kwargs = sent[0]
    assert conversation_id == 'coordinator'
    assert kwargs['inject_mode'] == 'steer'
    assert kwargs['command_id'].startswith('commitment-due:')
    assert message['_initiator'] == 'proactive'
    for title in ('Report the restart outcome', 'Report the evaluation result'):
        assert title in message['content']
    assert 'Still waiting' not in message['content']
    receipts = {row['title']: row for row in board.head()['commitments']}
    assert receipts['Report the restart outcome']['wake']['via'] == 'notice'
    assert receipts['Report the evaluation result']['wake']['disposition'] == 'started'
    assert 'wake' not in receipts['Still waiting']
    # Replaying the same due row wakes nothing: each due time wakes once.
    ok, result = run_commitment_due(scheduler_row(board), client=board.store, bindings=board.bindings,
                                    dispatch=dispatch, recover=lambda repository: [],
                                    clock=lambda: board.clock[0])
    assert ok and len(sent) == 1, result


def test_a_due_commitment_whose_request_continues_does_not_also_get_a_notice(board):
    progress = board.factory()
    progress.publish(board.scope, request([
        {'title': 'Resume the evaluation when the quota resets', 'status': 'pending', 'due': FUTURE}]))
    board.clock[0] = due_ms(FUTURE) / 1000 + 1
    sent = []

    def recover(repository):
        # The continuation path records its own wake receipts in the same head.
        progress.mark_commitments_woken(board.scope, [
            {'title': 'Resume the evaluation when the quota resets', 'dueAt': due_ms(FUTURE)}],
            via='continuation', command_id='coordinator-wake:test', disposition='started')
        return ['started']

    ok, result = run_commitment_due(scheduler_row(board), client=board.store, bindings=board.bindings,
                                    dispatch=lambda *args, **kwargs: sent.append(args),
                                    recover=recover, clock=lambda: board.clock[0])
    assert ok, result
    assert sent == [], 'the continuation and the notice must not both wake the lead'
    assert board.head()['commitments'][0]['wake']['via'] == 'continuation'


def test_a_busy_lane_keeps_the_obligation_for_the_next_due_row(board):
    progress = board.factory()
    progress.publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE}]))
    board.clock[0] = due_ms(FUTURE) / 1000 + 1
    sent = []
    ok, result = run_commitment_due(
        scheduler_row(board), client=board.store, bindings=board.bindings,
        dispatch=lambda *args, **kwargs: sent.append(kwargs) or SimpleNamespace(disposition='busy'),
        recover=lambda repository: [], clock=lambda: board.clock[0])
    assert not ok and sent, 'a busy lane keeps the obligation for the next due row'
    assert 'wake' not in board.head()['commitments'][0]
    ok, result = run_commitment_due(
        scheduler_row(board), client=board.store, bindings=board.bindings,
        dispatch=lambda *args, **kwargs: sent.append(kwargs) or SimpleNamespace(disposition='steered'),
        recover=lambda repository: [], clock=lambda: board.clock[0])
    assert ok and len(sent) == 2, result
    assert board.head()['commitments'][0]['wake']['disposition'] == 'steered'


def test_a_replaced_binding_or_a_missing_board_wakes_nobody(board):
    progress = board.factory()
    progress.publish(board.scope, request([
        {'title': 'Report the restart outcome', 'status': 'in_progress', 'due': FUTURE}]))
    board.clock[0] = due_ms(FUTURE) / 1000 + 1
    replaced = SimpleNamespace(read=lambda path: {'availability': 'available',
                                                  'conversationId': 'other', 'revision': 2})
    sent = []
    ok, result = run_commitment_due(scheduler_row(board), client=board.store, bindings=replaced,
                                    dispatch=lambda *args, **kwargs: sent.append(args),
                                    recover=lambda repository: [], clock=lambda: NOW)
    assert ok and not sent, result
    ok, result = run_commitment_due(scheduler_row(board, project='/nowhere'), client=board.store,
                                    bindings=board.bindings,
                                    dispatch=lambda *args, **kwargs: sent.append(args),
                                    recover=lambda repository: [], clock=lambda: NOW)
    assert ok and not sent, result


def test_notice_text_states_the_obligation_and_its_limits(board):
    text = render_due_notice([
        {'title': 'Report the restart outcome', 'dueAt': due_ms(FUTURE), 'slips': 1}])
    assert 'nonhuman notice' in text and 'not authority' in text
    assert 'Report the restart outcome' in text
    assert 'update_task_progress' in text and 'report_to_user' in text
    assert 'cannot delegate' in text
    assert commitment_identity(7, 'coordinator', [{'title': 't', 'dueAt': 5}]) == \
        commitment_identity(7, 'coordinator', [{'title': 't', 'dueAt': 5}])
    assert DUE_TASK_TYPE == 'coordinator_commitment_due'
