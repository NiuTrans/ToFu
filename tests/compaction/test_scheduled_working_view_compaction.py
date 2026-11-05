"""Owner-scheduled working-view compaction: apply at the next model call, retire on adoption.

Protected (coordinator/project) conversations never rewrite canonical Turns.
Their compaction request is a durable marker; the next publishing root turn
runs the forced manual L2 path, and settlement retires the marker only when
the stored checkpoint carries the summarized view (docs/CONTEXT_COMPACTION.md,
coordinator context lifecycle).
"""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction import _pipeline as pipeline
from lib.tasks_pkg.compaction._handoff import (
    HANDOFF_FIELD, handoff_scheduled_request_ids, is_compaction_handoff,
    make_compaction_handoff,
)
from tests.compaction.test_compaction_history_wire import compact, tool_round, user  # noqa: F401

pytestmark = pytest.mark.unit
MARKER = {'requestId': 'request-1', 'requestedAt': 1_000}


class Store:
    def __init__(self, marker=MARKER, *, fail=False):
        self.marker, self.fail = marker, fail
        self.reads, self.clears = [], []

    def scheduled_working_view_compaction(self, conv_id, *, user_id):
        self.reads.append((conv_id, user_id))
        if self.fail:
            raise RuntimeError('storage unavailable')
        return deepcopy(self.marker)

    def clear_scheduled_working_view_compaction(self, conv_id, *, user_id, request_id):
        self.clears.append((conv_id, user_id, request_id))
        if self.fail:
            raise RuntimeError('storage unavailable')
        if self.marker and self.marker['requestId'] == request_id:
            self.marker = None
            return True
        return False


def _task(**extra):
    return {'id': 'scheduled-task', 'convId': 'conv-1', '_turnId': 'turn-9',
            '_userId': 7, 'aborted': False, 'config': {}, **extra}


@pytest.fixture
def staged(monkeypatch):
    """Run the real pipeline with every stage but marker policy stubbed."""
    from lib.tasks_pkg import tool_hooks
    import lib.agent_core.store as store_module

    store = Store()
    l2_calls = []

    def force(messages, task=None, **kwargs):
        l2_calls.append(kwargs)
        return False

    monkeypatch.setattr(store_module, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(tool_hooks, 'run_pre_compact_hooks', lambda *a, **k: None)
    monkeypatch.setattr(pipeline, 'micro_compact', lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, 'force_compact_if_needed', force)
    monkeypatch.setattr(pipeline, '_maybe_record_context_budget', lambda *a, **k: None)
    return store, l2_calls


def test_first_model_call_runs_forced_manual_l2_once_per_task(staged):
    store, l2_calls = staged
    task = _task()
    messages = [user('Coordinate the release')]
    pipeline.run_compaction_pipeline(messages, 1, task)
    pipeline.run_compaction_pipeline(messages, 2, task)
    first, second = l2_calls
    assert first['force'] is True and first['_compaction_trigger'] == 'manual'
    assert first['_scheduled_request_id'] == 'request-1'
    # A declined or failed attempt keeps the durable marker for the next task
    # instead of re-purchasing a summary every round.
    assert 'force' not in second and '_scheduled_request_id' not in second
    assert store.reads == [('conv-1', 7)]


def test_view_that_already_carries_the_request_is_not_compacted_again(staged):
    store, l2_calls = staged
    carried = make_compaction_handoff('## Summary', phase='pre_turn',
                                      scheduled_request_id='request-1')
    pipeline.run_compaction_pipeline([user('Old'), carried, user('Next')], 1, _task())
    assert [call.get('_scheduled_request_id') for call in l2_calls] == [None]
    assert store.reads == [('conv-1', 7)]


@pytest.mark.parametrize('extra', [
    {'_transientRuntime': True},
    {'_turnId': ''},
    {'config': {'autopilot': True}},
    {'config': {'flowId': 'flow-1'}},
    {'config': {'compaction': {'disableForceCompact': True}}},
], ids=['transient', 'no-turn', 'goal-run', 'flow', 'force-disabled'])
def test_turns_that_cannot_publish_the_view_never_consume_the_marker(staged, extra):
    store, l2_calls = staged
    pipeline.run_compaction_pipeline([user('Work')], 1, _task(**extra))
    assert store.reads == []
    assert all(call.get('_scheduled_request_id') is None for call in l2_calls)


def test_child_invocations_never_consume_the_parent_conversation_marker(staged):
    from lib.tasks_pkg.invocation import InvocationPolicy

    store, l2_calls = staged
    task = _task(_invocationPolicy=InvocationPolicy.child('parent-task'))
    pipeline.run_compaction_pipeline([user('Work')], 1, task)
    assert store.reads == [] and l2_calls[0].get('_scheduled_request_id') is None


def test_unreadable_marker_keeps_the_ordinary_path(staged):
    store, l2_calls = staged
    store.fail = True
    pipeline.run_compaction_pipeline([user('Work')], 1, _task())
    assert len(store.reads) == 1 and l2_calls[0].get('_scheduled_request_id') is None


def _history():
    messages = [user('Original coordination request'),
                {'role': 'assistant', 'content': 'old result ' * 7000},
                user('Continue the plan.')]
    for index in range(8):
        messages.extend(tool_round(index, size=10, count=2))
    messages.append(user('Report status.'))
    return messages


def test_summarized_view_carries_the_request_and_later_folds_inherit_it(monkeypatch, compact):  # noqa: F811
    from lib.tasks_pkg.compaction._layer2 import _compact as l2

    messages = _history()
    meta = {}
    assert l2.force_compact_if_needed(
        messages, task={'id': 'scheduled', '_userId': 1, 'config': {}},
        force=True, preserve_budget_tokens=1, _compaction_trigger='manual',
        _scheduled_request_id='request-1', _compaction_skip_archive=True,
        _result_meta=meta)
    assert handoff_scheduled_request_ids(messages) == {'request-1'}
    # A later automatic summary folds the carrier: the proof survives so
    # settlement can still adopt the request from the stored checkpoint.
    messages.extend([{'role': 'assistant', 'content': 'status ' * 7000},
                     user('Continue.')])
    meta = {}
    assert l2.force_compact_if_needed(
        messages, task={'id': 'scheduled', '_userId': 1, 'config': {}},
        force=True, preserve_budget_tokens=1, _compaction_skip_archive=True,
        _result_meta=meta)
    handoffs = [m for m in messages if is_compaction_handoff(m)]
    assert len(handoffs) == 1
    assert handoffs[0][HANDOFF_FIELD]['scheduledRequestId'] == 'request-1'


def test_completed_publication_stores_the_carrier_unchanged():
    from lib.tasks_pkg.model_replay import prepare_completed_task

    carrier = make_compaction_handoff('## Summary', phase='pre_turn',
                                      scheduled_request_id='request-1')
    task = {'_userId': 7, 'convId': 'conv-1', '_turnId': 'turn-1',
            'config': {'_modelReplayBase': []},
            'messages': [carrier, user('Report status.'),
                         {'role': 'assistant', 'content': 'All green.'}]}
    candidate = prepare_completed_task(task)
    stored = candidate['projection']['messages']
    assert handoff_scheduled_request_ids(stored) == {'request-1'}


def _settled(status='stored', *, carried=True):
    messages = [user('Report status.'), {'role': 'assistant', 'content': 'Done.'}]
    if carried:
        messages.insert(0, make_compaction_handoff(
            '## Summary', phase='pre_turn', scheduled_request_id='request-1'))
    return _task(_modelReplayPublishStatus=status, messages=messages,
                 _scheduledWorkingViewCompaction=dict(MARKER))


def test_marker_retires_exactly_when_a_stored_checkpoint_carries_the_view(monkeypatch):
    import lib.agent_core.store as store_module

    store = Store()
    monkeypatch.setattr(store_module, 'get_conversation_store', lambda: store)
    task = _settled()
    assert pipeline.settle_scheduled_compaction(task) is True
    assert store.clears == [('conv-1', 7, 'request-1')] and store.marker is None
    assert task['_scheduledWorkingViewCompaction'] is None
    # Settling again is a no-op: nothing left to retire.
    assert pipeline.settle_scheduled_compaction(task) is False
    assert len(store.clears) == 1


@pytest.mark.parametrize(('status', 'carried'), [
    ('unverified_inbox_delivery', True), ('not_completed', True),
    ('unsupported_mode', True), ('stored', False),
])
def test_marker_survives_every_outcome_that_did_not_store_the_view(monkeypatch, status, carried):
    import lib.agent_core.store as store_module

    store = Store()
    monkeypatch.setattr(store_module, 'get_conversation_store', lambda: store)
    assert pipeline.settle_scheduled_compaction(_settled(status, carried=carried)) is False
    assert store.clears == [] and store.marker == MARKER


def test_failed_retirement_is_retried_by_a_later_turn(monkeypatch):
    import lib.agent_core.store as store_module

    store = Store(fail=True)
    monkeypatch.setattr(store_module, 'get_conversation_store', lambda: store)
    task = _settled()
    assert pipeline.settle_scheduled_compaction(task) is False
    assert task['_scheduledWorkingViewCompaction'] == MARKER


# ── Scheduling while a reply runs: wake hints, atomic claim, cancellation ──

@pytest.fixture
def live_runtime(monkeypatch):
    """An isolated task registry standing in for the process chat runtime."""
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.tasks_pkg.manager import _registry

    runtime = TaskRuntime(kind='scheduled-compaction-test')
    monkeypatch.setattr(_registry, 'chat_task_runtime', runtime)

    def start(**fields):
        task = runtime.create(user_id=fields.pop('owner', 7))
        task.update({'convId': 'conv-1', '_turnId': 'turn-9', 'aborted': False,
                     'config': {}, 'status': 'running', **fields})
        return task
    return start


class SchedulingStore(Store):
    def __init__(self, marker=None):
        super().__init__(marker)
        self.scheduled = []

    def schedule_working_view_compaction(self, conv_id, *, user_id, reason):
        self.scheduled.append((conv_id, user_id, reason))
        if self.marker:
            return {**self.marker, 'created': False}
        self.marker = {'requestId': f'request-{len(self.scheduled)}',
                       'requestedAt': 2_000, 'reason': reason}
        return {**self.marker, 'created': True}


def test_schedule_during_a_reply_applies_at_its_next_model_call(staged, live_runtime, monkeypatch):
    from lib.tasks_pkg.compaction import _manual

    store, l2_calls = staged
    store.marker = None
    task = live_runtime()
    messages = [user('Coordinate the release')]
    pipeline.run_compaction_pipeline(messages, 1, task)
    assert store.reads == [('conv-1', 7)] and 'force' not in l2_calls[-1]

    scheduling = SchedulingStore()
    scheduling.reads = store.reads
    import lib.agent_core.store as store_module
    monkeypatch.setattr(store_module, 'get_conversation_store', lambda: scheduling)
    reply = _manual.schedule_busy_compaction('conv-1', user_id=7, store=scheduling)
    assert reply == {'ok': True, 'scheduled': True, 'alreadyScheduled': False,
                     'requestId': 'request-1', 'requestedAt': 2_000,
                     'protection': None, 'appliesAt': 'next_model_call'}
    assert scheduling.scheduled == [('conv-1', 7, 'task_active')]

    pipeline.run_compaction_pipeline(messages, 2, task)
    assert l2_calls[-1]['force'] is True
    assert l2_calls[-1]['_scheduled_request_id'] == 'request-1'
    assert task['_scheduledCompactionAttempted'] == 'request-1'
    # Applied once: later calls neither re-read nor re-summarize.
    pipeline.run_compaction_pipeline(messages, 3, task)
    assert 'force' not in l2_calls[-1] and len(store.reads) == 2


def test_cancel_before_the_claim_is_never_applied(staged, live_runtime, monkeypatch):
    from lib.tasks_pkg.compaction import _manual

    store, l2_calls = staged
    task = live_runtime()
    outcomes = []
    real_pending = pipeline.pending_scheduled_compaction

    def racing(task, messages):
        request = real_pending(task, messages)
        if request and not outcomes:
            # The owner cancels after the marker read, before the claim.
            outcomes.append(_manual.cancel_scheduled_compaction(
                'conv-1', user_id=7, request_id=request, store=store))
        return request

    monkeypatch.setattr(pipeline, 'pending_scheduled_compaction', racing)
    pipeline.run_compaction_pipeline([user('Work')], 1, task)
    assert outcomes == [{'ok': True, 'cancelled': True, 'applying': False}]
    assert all(call.get('_scheduled_request_id') is None for call in l2_calls)
    assert store.marker is None and '_scheduledCompactionAttempted' not in task


def test_cancel_after_the_claim_reports_the_reply_already_applying(staged, live_runtime):
    from lib.tasks_pkg.compaction import _manual

    store, l2_calls = staged
    task = live_runtime()
    pipeline.run_compaction_pipeline([user('Work')], 1, task)
    assert l2_calls[-1]['_scheduled_request_id'] == 'request-1'
    assert _manual.cancel_scheduled_compaction(
        'conv-1', user_id=7, request_id='request-1', store=store) == {
            'ok': True, 'cancelled': True, 'applying': True}
    # Withdrawn for future calls: the hint re-read finds no marker.
    pipeline.run_compaction_pipeline([user('Work')], 2, task)
    assert task['_scheduledWorkingViewCompaction'] is None
    assert 'force' not in l2_calls[-1]


@pytest.mark.parametrize('fields', [
    {'owner': 8}, {'convId': 'conv-2'}, {'aborted': True}, {'status': 'done'},
    {'config': {'flowId': 'flow-1'}}, {'config': {'autopilot': True}},
    {'_transientRuntime': True}, {'config': {'compaction': {'disableForceCompact': True}}},
], ids=['foreign-owner', 'other-conversation', 'aborted', 'finished', 'flow',
        'goal-run', 'transient', 'force-disabled'])
def test_hints_reach_only_live_tasks_that_can_apply_the_request(live_runtime, fields):
    from lib.tasks_pkg.manager import hint_scheduled_compaction

    task = live_runtime(**fields)
    woken, claimed = hint_scheduled_compaction(
        'conv-1', user_id=7, eligible=pipeline.applies_scheduled_compaction)
    assert (woken, claimed) == (0, False)
    assert '_scheduledCompactionRefresh' not in task


def test_idle_or_unpublishing_conversations_apply_at_the_next_reply(live_runtime):
    from lib.tasks_pkg.compaction import _manual

    live_runtime(config={'flowId': 'flow-1'})
    store = SchedulingStore()
    reply = _manual.schedule_busy_compaction('conv-1', user_id=7, store=store)
    assert reply['appliesAt'] == 'next_reply' and reply['alreadyScheduled'] is False
    again = _manual.schedule_busy_compaction('conv-1', user_id=7, store=store)
    assert again['alreadyScheduled'] is True and again['requestId'] == reply['requestId']
