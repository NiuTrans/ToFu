"""Six-hour cloud idle policy lifts L1's cache fence without spending a call."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.compaction._layer1 import micro_compact
from tests.compaction.test_l1_summary_preservation import continuation, tool_batch

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0


@pytest.fixture
def context(monkeypatch):
    from lib.llm_dispatch import factory
    slot = SimpleNamespace(
        model='model', logical_model='model', provider_id='cloud',
        routing_provider_id='cloud', routing_owner_user_id=7,
        protocol='openai', base_url='https://unknown-cloud.example/v1',
        route_snapshot={'credential': {'kind': 'api_key'}},
        local_inference=False)
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: SimpleNamespace(
        slots=SimpleNamespace(scope=lambda pin: [slot])))
    monkeypatch.setattr('lib.tasks_pkg.compaction._layer1.time.time', lambda: NOW)
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
                        lambda *a, **k: k['current_msg_count'])
    monkeypatch.setattr('lib.tasks_pkg.compaction._layer1._sent_step_boundary',
                        lambda messages, sent, *a, **k: (sent, None))
    events, notifications = [], []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: events.append(a))
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.notify_compaction',
                        lambda *a, **k: notifications.append(k))
    task = {'id': 'idle-task', 'convId': 'idle-conv', '_userId': 7,
            'model': 'model', 'provider_id': 'cloud',
            'config': {'_contextLastModelActivityAtMs': int((NOW - 6 * 3600) * 1000)}}
    messages = [{'role': 'user', 'content': 'Keep the approved constraints.'},
                *continuation('typed')]
    for index in range(45):
        messages.extend(tool_batch(f'idle-{index}'))
    return task, messages, slot, notifications


@pytest.mark.parametrize('seconds,expected', [(6 * 3600 - 1, False), (6 * 3600, True),
                                             (24 * 3600, True)])
def test_unknown_cloud_ttl_uses_six_hour_policy(context, seconds, expected):
    task, messages, _, notifications = context
    task['config']['_contextLastModelActivityAtMs'] = int((NOW - seconds) * 1000)
    original = deepcopy(messages)
    saved = micro_compact(messages, task=task)
    assert (saved > 0) is expected
    if expected:
        assert 'result compacted' in messages[3]['content']
        assert messages[:2] == original[:2]
        assert messages[-2:] == original[-2:]
        assert len(notifications) == 1
    else:
        assert messages == original and not notifications


def test_local_inference_never_uses_idle_trigger(context):
    task, messages, slot, notifications = context
    slot.local_inference = True
    task['config']['_contextLastModelActivityAtMs'] = int((NOW - 30 * 86400) * 1000)
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) == 0
    assert messages == original and not notifications


@pytest.mark.parametrize('mode', ['child', 'transient', 'aborted'])
def test_inherited_clock_does_not_authorize_unrelated_or_cancelled_work(context, mode):
    from lib.tasks_pkg.invocation import InvocationPolicy
    task, messages, _, notifications = context
    if mode == 'child':
        task['_invocationPolicy'] = InvocationPolicy.child('parent-task')
    elif mode == 'transient':
        task['_transientRuntime'] = True
    else:
        task['aborted'] = True
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) == 0
    assert messages == original and not notifications


def test_mixed_route_group_preserves_local_exclusion(context, monkeypatch):
    from lib.llm_dispatch import factory
    task, messages, cloud, notifications = context
    local = deepcopy(cloud)
    local.local_inference = True
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: SimpleNamespace(
        slots=SimpleNamespace(scope=lambda pin: [cloud, local])))
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) == 0
    assert messages == original and not notifications


def test_dry_run_does_not_consume_idle_boundary(context):
    task, messages, _, notifications = context
    original = deepcopy(messages)
    assert micro_compact(messages, task=task, _dry_run=True) == 0
    assert messages == original and not notifications
    assert '_lastIdleL1ActivityAtMs' not in task
    assert micro_compact(messages, task=task) > 0


def test_newer_live_activity_beats_old_restored_context(context):
    task, messages, _, notifications = context
    task['_lastModelActivityAtMs'] = int((NOW - 1) * 1000)
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) == 0
    assert messages == original and not notifications


def test_notification_failure_cannot_abort_local_compaction(context, monkeypatch):
    task, messages, _, _ = context
    task['_compactionCacheObservation'] = {'cacheReadTokens': 100_000}
    def fail(*args, **kwargs):
        raise RuntimeError('diagnostic unavailable')
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.notify_compaction', fail)
    assert micro_compact(messages, task=task) > 0
    assert '_compactionCacheObservation' not in task


def test_another_owner_or_local_candidate_cannot_authorize_cloud_trim(context):
    task, messages, slot, notifications = context
    slot.routing_owner_user_id = 8
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) == 0
    assert messages == original and not notifications


def test_idle_boundary_is_consumed_once_and_new_activity_restarts_clock(context):
    task, messages, _, notifications = context
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) > 0
    # Repeated preparation at the same boundary must not reopen the fence.
    replay = deepcopy(original)
    assert micro_compact(replay, task=task) == 0
    assert replay == original and len(notifications) == 1
    task['_lastModelActivityAtMs'] = int(NOW * 1000)
    task.pop('_lastIdleL1ActivityAtMs', None)
    assert micro_compact(replay, task=task) == 0
    assert replay == original


@pytest.mark.parametrize('stamp', [None, 0, -1, True, 'old', float('nan'), float('inf'),
                                  int((NOW + 1) * 1000)])
def test_absent_or_invalid_activity_does_not_invent_an_idle_period(context, stamp):
    task, messages, _, notifications = context
    task['config']['_contextLastModelActivityAtMs'] = stamp
    original = deepcopy(messages)
    assert micro_compact(messages, task=task) == 0
    assert messages == original and not notifications
