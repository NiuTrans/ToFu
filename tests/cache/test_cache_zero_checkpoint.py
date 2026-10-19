"""Observed zero is a completed-turn fact; absent usage is not a write."""
from __future__ import annotations

import time

import pytest

from lib.cost import canonicalize_usage_cache_keys, observed_cache_read
from lib.task_replay.task_result_checkpoint_contract import (
    TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD,
    TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD,
    TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
)
from lib.tasks_pkg.cache_tracking import _persist as persist
from lib.tasks_pkg.cache_tracking import _state as state
from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
from lib.tasks_pkg.invocation import InvocationPolicy
from lib.tasks_pkg.manager._persist import _upsert_task_row
from lib.tasks_pkg.orchestrator._cache_round_accounting import stamp_round_cache_accounting
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


@pytest.fixture(autouse=True)
def isolated_memory():
    stores = (state._cache_states, state._cache_invocations,
              persist._hwm_read_cache, persist._last_turn_read_cache)
    saved = [dict(store) for store in stores]
    for store in stores:
        store.clear()
    yield
    for store, original in zip(stores, saved):
        store.clear()
        store.update(original)


def cold_memory():
    state._cache_states.clear()
    state._cache_invocations.clear()
    persist._hwm_read_cache.clear()
    persist._last_turn_read_cache.clear()


def task_for(conv_id, user_id=7, **extra):
    return {'id': f'zero-task-{time.time_ns()}', 'convId': conv_id,
            '_userId': user_id, 'created_at': time.time(), **extra}


def conversation():
    conv_id = f'zero-predecessor-{time.time_ns()}'
    seed_conversation(conv_id, user_id=7,
                      settings={'lastTurnCacheRead': 34_304, 'unrelated': 'kept'})
    return conv_id


def settings(conv_id, user_id=7):
    from lib.storage import get_storage_client
    doc = get_storage_client().query('conversation.get', {
        'conv_id': conv_id, 'user_id': user_id, 'derive_messages': False,
    })
    return doc['metadata']['settings']


def checkpoint(task, status='done'):
    return _upsert_task_row(task, task['convId'], content='answer', thinking='',
                            status=status, error_json=None, tr_json=None,
                            meta_json=None)


def stamp(task, usage):
    # Exercise the same presence-preserving seam as streaming/chat adapters.
    usage = canonicalize_usage_cache_keys(usage)
    stamp_round_cache_accounting(
        task, messages=[{'role': 'user', 'content': 'hello'}], tools=[],
        model='claude-test', usage=usage, api_rounds=[{'round': 1}],
        round_num=0, tid=task['id'][:8], assistant_msg={'content': 'answer'})


@pytest.mark.parametrize('usage, expected', [
    ({'cache_read_input_tokens': 0}, 0),
    ({'cache_read_tokens': 0}, 0),
    ({'cached_tokens': 0}, 0),
    ({'prompt_tokens_details': {'cached_tokens': 0}}, 0),
    ({'input_tokens_details': {'cache_read': 0}}, None),
    ({'cache_read_tokens': 0, 'cached_tokens': 12}, 12),
    ({'cached_tokens': '0'}, 0),
    (None, None), ({}, None), ({'input_tokens': 60}, None),
    ({'cache_read_tokens': None}, None), ({'cache_read_tokens': False}, None),
    ({'cache_read_tokens': -1}, None), ({'cache_read_tokens': 'invalid'}, None),
])
def test_cache_read_observation_presence(usage, expected):
    assert observed_cache_read(usage) == expected


@pytest.mark.parametrize('read', [0, 1, 1_024])
def test_completed_observed_read_replaces_warm_and_survives_cold_memory(read):
    from lib.storage import get_storage_client
    conv_id = conversation()
    task = task_for(conv_id)
    # Prime a warm owner cache before the completed zero overwrites it.
    assert persist.read_last_turn_cache_read(conv_id, user_id=7) == 34_304
    with state.cache_tracking_scope(task):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == 34_304
        stamp(task, {'input_tokens': 60, 'cache_read_input_tokens': read})
        assert task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] == read
        assert checkpoint(task)
    assert TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD not in task
    row = get_storage_client().query('record.get', {
        'namespace': 'task_results', 'key': task['id']})
    assert row['value'][TASK_RESULT_LAST_TURN_CACHE_READ_FIELD] == read
    assert settings(conv_id)['lastTurnCacheRead'] == read
    assert settings(conv_id)['unrelated'] == 'kept'
    assert persist.read_last_turn_cache_read(conv_id, user_id=7) == read
    with state.cache_tracking_scope(task_for(conv_id)):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == read
    cold_memory()
    with state.cache_tracking_scope(task_for(conv_id)):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == read
    assert persist._last_turn_read_cache[(7, conv_id)][0] == read


@pytest.mark.parametrize('usage', [None, {}, {'input_tokens': 60},
                                   {'cache_read_input_tokens': None},
                                   {'cache_read_input_tokens': False}])
def test_missing_usage_does_not_erase_durable_or_memory_predecessor(usage):
    conv_id = conversation()
    with state.cache_tracking_scope(task_for(conv_id)):
        stamp(task_for(conv_id), {'cache_read_input_tokens': 34_304})
    task = task_for(conv_id)
    with state.cache_tracking_scope(task):
        stamp(task, usage)
        assert TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD not in task
        assert checkpoint(task)
    with state.cache_tracking_scope(task_for(conv_id)):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == 34_304
    assert settings(conv_id)['lastTurnCacheRead'] == 34_304
    cold_memory()
    with state.cache_tracking_scope(task_for(conv_id)):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == 34_304


def test_running_checkpoint_keeps_read_staged_until_terminal():
    conv_id = conversation()
    task = task_for(conv_id)
    with state.cache_tracking_scope(task):
        stamp(task, {'cache_read_input_tokens': 0})
        assert checkpoint(task, 'running')
        assert task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] == 0
        assert settings(conv_id)['lastTurnCacheRead'] == 34_304
        assert checkpoint(task)
    assert settings(conv_id)['lastTurnCacheRead'] == 0


def test_legacy_zero_write_after_absent_read_persists_presence():
    conv_id = f'absent-predecessor-{time.time_ns()}'
    seed_conversation(conv_id, user_id=7)
    assert persist.read_last_turn_cache_read(conv_id, user_id=7) == 0
    assert persist._last_turn_read_cache[(7, conv_id)][0] is None
    assert persist.write_last_turn_cache_read(conv_id, 0, user_id=7)
    assert settings(conv_id)['lastTurnCacheRead'] == 0
    cold_memory()
    assert persist.read_last_turn_cache_read(conv_id, user_id=7) == 0
    assert persist._last_turn_read_cache[(7, conv_id)][0] == 0


def test_zero_checkpoint_is_owner_isolated():
    conv_id = conversation()
    foreign = task_for(conv_id, user_id=8)
    foreign[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 0
    assert not checkpoint(foreign)
    assert settings(conv_id)['lastTurnCacheRead'] == 34_304
    persist.remember_persisted_cache_facts(
        conv_id, user_id=8, last_turn_cache_read=12_345)
    owned = task_for(conv_id)
    owned[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 0
    assert checkpoint(owned)
    assert persist.read_last_turn_cache_read(conv_id, user_id=7) == 0
    assert persist.read_last_turn_cache_read(conv_id, user_id=8) == 12_345
    assert not persist.write_last_turn_cache_read(conv_id, 0, user_id=8)
    assert settings(conv_id)['lastTurnCacheRead'] == 0


@pytest.mark.parametrize('extra', [
    {'_transientRuntime': True}, {'_inline_messages': True},
    {'_invocationPolicy': InvocationPolicy.child('parent')},
])
def test_isolated_invocations_never_stage_or_publish_cache_facts(extra):
    conv_id = conversation()
    task = task_for(conv_id, **extra)
    with state.cache_tracking_scope(task):
        stamp(task, {'cache_read_input_tokens': 0})
        assert TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD not in task
        # Defend again at the manager boundary against stale/inherited fields.
        task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 0
        task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] = 20
        assert checkpoint(task)
    assert settings(conv_id) == {'lastTurnCacheRead': 34_304, 'unrelated': 'kept'}


@pytest.mark.parametrize('sink_fails', [False, True])
def test_scoped_missing_or_failed_sink_never_falls_back_to_teardown_write(monkeypatch, sink_fails):
    conv_id = conversation()
    writes = []
    monkeypatch.setattr(persist, 'write_last_turn_cache_read',
                        lambda *a, **k: writes.append((a, k)))
    monkeypatch.setattr(persist, 'advance_persisted_boundary',
                        lambda *a, **k: writes.append((a, k)))

    def fail(_value):
        raise RuntimeError('injected staging failure')

    with state.cache_tracking_scope(task_for(conv_id)):
        detect_cache_break(
            conv_id, [{'role': 'user', 'content': str(i)} for i in range(12)],
            [], 'claude-test', {'cache_read_input_tokens': 3_000}, user_id=7,
            cache_prefix_hwm_sink=fail if sink_fails else None,
            last_turn_cache_read_sink=fail if sink_fails else None)
    assert writes == []
    assert settings(conv_id)['lastTurnCacheRead'] == 34_304


@pytest.mark.parametrize('last_read', [0, 12_345])
def test_missing_final_round_keeps_latest_observation(last_read):
    conv_id = conversation()
    task = task_for(conv_id)
    with state.cache_tracking_scope(task):
        stamp(task, {'cache_read_input_tokens': last_read})
        stamp(task, {'input_tokens': 60})
        assert task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] == last_read
        assert checkpoint(task)
    with state.cache_tracking_scope(task_for(conv_id)):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == last_read
    cold_memory()
    with state.cache_tracking_scope(task_for(conv_id)):
        assert state.get_prev_turn_cache_read(conv_id, user_id=7) == last_read
