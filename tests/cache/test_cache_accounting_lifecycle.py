"""Task-identity cache accounting regressions; all state and I/O are isolated."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from copy import deepcopy
import json

import pytest

from lib.tasks_pkg.cache_tracking import _state as state
from lib.tasks_pkg.cache_tracking import _persist as persist
from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
from lib.tasks_pkg.orchestrator._cache_round_accounting import stamp_round_cache_accounting

from lib.tasks_pkg.invocation import InvocationPolicy
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    # Preserve singleton identity for consumers lazily imported by this test.
    saved_states = dict(state._cache_states)
    saved_invocations = dict(state._cache_invocations)
    state._cache_states.clear()
    state._cache_invocations.clear()
    monkeypatch.setattr(persist, 'read_last_turn_cache_read', lambda *a, **k: 0)
    monkeypatch.setattr(persist, 'read_persisted_boundary', lambda *a, **k: 0)
    monkeypatch.setattr(persist, 'write_last_turn_cache_read', lambda *a, **k: None)
    monkeypatch.setattr(persist, 'advance_persisted_boundary', lambda *a, **k: None)

    yield
    state._cache_states.clear()
    state._cache_states.update(saved_states)
    state._cache_invocations.clear()
    state._cache_invocations.update(saved_invocations)

def task(task_id, **fields):
    return {'id': task_id, 'convId': 'isolated-cache-conv', '_userId': 1, **fields}


def messages():
    return [{'role': 'system', 'content': 'system'}] + [
        {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'message {i}'}
        for i in range(12)]


def usage(read, write=0):
    return {'input_tokens': 60, 'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write, 'output_tokens': 10}


def detect(read, *, body=None, tools=None):
    return detect_cache_break('isolated-cache-conv', body or messages(), tools,
                              'claude-test', usage(read, 27392 if read == 8448 else 0),
                              user_id=1, durable=False)


def complete(task_id, read):
    with state.cache_tracking_scope(task(task_id)):
        detect(read)


def test_same_worker_uses_immediate_turn_not_older_sibling():
    with ThreadPoolExecutor(max_workers=1) as other_worker:
        other_worker.submit(complete, 'older-sibling', 26624).result()
    complete('immediate-predecessor', 34304)
    current = task('current')
    with state.cache_tracking_scope(current):
        assert state.get_prev_turn_cache_read(current['convId'], user_id=1) == 34304
        rounds = [{'round': 1, 'usage': usage(8448, 27392)}]
        stamp_round_cache_accounting(
            current, round_num=0, tid='current', model='claude-test', tools=None,
            usage=rounds[0]['usage'], assistant_msg={}, api_rounds=rounds,
            messages=messages())
        verdict = rounds[0]['cacheBreak']
        breakdown = rounds[0]['writeBreakdown']
        assert verdict['prev_read'] == breakdown['prevRead'] == 34304
        assert breakdown['readDrop'] == 25856
        assert breakdown['recacheBody'] == 26592
        assert breakdown['envelope'] == breakdown['envelopeMaxTokens'] == 800
        assert breakdown['allocation'] == 'estimated'
        assert breakdown['totalsSource'] == 'provider_usage'
        assert not breakdown['capped']
        # The frozen predecessor survives detection updating THIS turn's read.
        assert state.get_prev_turn_cache_read(current['convId'], user_id=1) == 34304
    assert len(state._cache_states) == len(state._cache_invocations) == 1


def test_concurrent_contexts_on_same_worker_do_not_borrow_active_lane():
    complete('completed', 34304)
    lane_a, lane_b = Context(), Context()
    scope_a = state.cache_tracking_scope(task('a'))
    scope_b = state.cache_tracking_scope(task('b'))
    lane_a.run(scope_a.__enter__)
    try:
        lane_a.run(detect, 90000)
        lane_b.run(scope_b.__enter__)
        try:
            assert lane_b.run(state.get_prev_turn_cache_read,
                              'isolated-cache-conv', user_id=1) == 34304
            assert lane_a.run(state._state_key, 'isolated-cache-conv', user_id=1) != (
                lane_b.run(state._state_key, 'isolated-cache-conv', user_id=1))
            lane_b.run(detect, 12000)
            lane_a.run(scope_a.__exit__, None, None, None)
            # Finishing A must not alter B's frozen baseline or active state.
            assert lane_b.run(state.get_prev_turn_cache_read,
                              'isolated-cache-conv', user_id=1) == 34304
            b_key = lane_b.run(state._state_key, 'isolated-cache-conv', user_id=1)
            assert state._cache_states[b_key].last_cache_read_tokens == 12000
        finally:
            lane_b.run(scope_b.__exit__, None, None, None)
    finally:
        if scope_a.gen.gi_frame is not None:
            lane_a.run(scope_a.__exit__, None, None, None)
    with state.cache_tracking_scope(task('next')):
        assert state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1) == 12000


@pytest.mark.parametrize('fields', [
    {'_transientRuntime': True}, {'_inline_messages': [{'role': 'user'}]},
    {'_invocationPolicy': InvocationPolicy.child('parent')},
])
def test_child_lanes_never_borrow_or_publish_root_baselines(fields, monkeypatch):
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    complete('root', 34304)
    monkeypatch.setattr(persist, 'read_last_turn_cache_read', lambda *a, **k: 99000)
    monkeypatch.setattr(persist, 'read_persisted_boundary', lambda *a, **k: 500)
    with state.cache_tracking_scope(task('child', **fields)):
        assert state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1) == 0
        assert state.get_warm_cache_read('isolated-cache-conv', user_id=1) == 0
        assert get_cache_prefix_count('isolated-cache-conv', user_id=1) == 0
        detect(80000)
    with state.cache_tracking_scope(task('next-root')):
        assert state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1) == 34304


def test_zero_read_completed_predecessor_is_not_replaced_by_older_warm_read():
    complete('warm', 34304)
    complete('cold', 0)
    with state.cache_tracking_scope(task('next')):
        assert state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1) == 0


def test_exception_releases_context_and_cleanup_drops_metadata():
    with pytest.raises(RuntimeError), state.cache_tracking_scope(task('throws')):
        detect(34304)
        raise RuntimeError('injected failure')
    assert state._cache_invocation.get() is None
    state.cleanup_cache_state('isolated-cache-conv', user_id=1)
    assert not state._cache_states and not state._cache_invocations


def test_tools_and_prefix_mutation_both_serialize_with_strength():
    old = messages()
    tools = [{'type': 'function', 'function': {'name': 'a', 'description': 'old'}}]
    changed_tools = deepcopy(tools)
    changed_tools[0]['function']['description'] = 'new'
    changed = deepcopy(old)
    changed[2]['content'] = 'mutated already-sent message'
    with state.cache_tracking_scope(task('both')):
        detect(34304, body=old, tools=tools)
        verdict = detect(8448, body=changed, tools=changed_tools)
    saved = json.loads(json.dumps(verdict))
    assert 'tools' in saved and 'prefix_mutation' in saved
    reasons = {reason['code']: reason for reason in saved['reasons']}
    assert reasons['tools']['evidence'] == 'source_only'
    assert reasons['prefix_mutation']['evidence'] == 'source_only'
    assert reasons['cache_read_drop']['evidence'] == 'provider_usage'
    assert reasons['cache_read_drop']['readDrop'] == 25856


@pytest.mark.parametrize('raw,expected', [
    ({'prompt_tokens': 35840, 'prompt_tokens_details': {'cached_tokens': 35840}}, 35840),
    ({'prompt_tokens': 35840, 'input_tokens': 0, 'cache_read_input_tokens': 8448,
      'cache_creation_input_tokens': 27392}, 35840),
    ({'prompt_tokens': 35840, 'cache_read_tokens': 8448,
      'cache_write_tokens': 27392}, 35840),
    ({'input_tokens': 35840, 'cache_read_input_tokens': 35840}, 71680),
    ({'input_tokens': 50000, 'cache_read_input_tokens': 10000}, 60000),
    ({'input_tokens': 0, 'cache_read_input_tokens': 8448,
      'cache_creation_input_tokens': 27392}, 35840),
])
def test_round_usage_respects_canonical_conventions(monkeypatch, raw, expected):
    from lib.cost import split_input_tokens
    from lib.tasks_pkg.llm_fallback import _usage
    events = []
    monkeypatch.setattr(_usage, 'append_event', lambda task, ev: events.append(ev))
    current = task('usage')
    _usage._emit_round_usage(current, 1, 'test-model', raw)
    assert split_input_tokens(raw)[1] == expected
    assert current['_lastRoundUsage']['tokensIn'] == expected
    assert events[0]['tokensIn'] == expected


def test_wire_tools_and_message_changes_preserve_both_verified_reasons():
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
        wire_byte_region,
    )
    old = messages()
    changed = deepcopy(old)
    changed[2]['content'] = 'wire mutation inside cached prefix'
    old_tools = [{'name': 'read', 'description': 'before'}]
    new_tools = [{'name': 'read', 'description': 'after'}]

    def wire_usage(body, tools, read, write):
        return {**usage(read, write), '_wire_fp': canonical_messages(body),
                '_wire_static': static_prefix_hash(body),
                '_wire_bytes': wire_byte_prefix(body),
                '_wire_region': wire_byte_region('system', tools)}

    with state.cache_tracking_scope(task('verified-both')):
        detect_cache_break('isolated-cache-conv', old, old_tools, 'claude-test',
                           wire_usage(old, old_tools, 34304, 0),
                           user_id=1, durable=False)
        verdict = detect_cache_break(
            'isolated-cache-conv', changed, new_tools, 'claude-test',
            wire_usage(changed, new_tools, 8448, 27392), user_id=1, durable=False)
    reasons = {item['code']: item for item in json.loads(json.dumps(verdict))['reasons']}
    assert reasons['tools']['evidence'] == 'wire_verified'
    assert reasons['prefix_mutation']['evidence'] == 'wire_verified'
    assert reasons['cache_read_drop']['readDrop'] == 25856


def test_empty_invocation_does_not_replace_last_completed_turn():
    complete('completed', 34304)
    with state.cache_tracking_scope(task('no-provider-round')):
        pass
    with state.cache_tracking_scope(task('next')):
        assert state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1) == 34304
    assert len(state._cache_states) == len(state._cache_invocations) == 1


def test_cold_concurrent_roots_freeze_durable_baseline_once(monkeypatch):
    reads = []
    durable = {'read': 34304}
    monkeypatch.setattr(persist, 'read_last_turn_cache_read',
                        lambda *a, **k: reads.append(True) or durable['read'])
    with state.cache_tracking_scope(task('cold-root')):
        durable['read'] = 99999  # a sibling must not change this turn's baseline
        assert state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1) == 34304
        assert state.get_warm_cache_read('isolated-cache-conv', user_id=1) == 34304
    assert len(reads) == 1


def test_active_sibling_durable_checkpoint_is_not_a_predecessor(monkeypatch):
    durable = {'read': 34304}
    monkeypatch.setattr(persist, 'read_last_turn_cache_read',
                        lambda *a, **k: durable['read'])
    a, b = Context(), Context()
    scope_a = state.cache_tracking_scope(task('active-a'))
    scope_b = state.cache_tracking_scope(task('active-b'))
    a.run(scope_a.__enter__)
    try:
        a.run(detect, 99999)
        durable['read'] = 99999  # simulated current-round atomic checkpoint
        b.run(scope_b.__enter__)
        try:
            assert b.run(state.get_prev_turn_cache_read,
                         'isolated-cache-conv', user_id=1) == 34304
        finally:
            b.run(scope_b.__exit__, None, None, None)
    finally:
        a.run(scope_a.__exit__, None, None, None)


def test_completed_turn_retention_is_constant_not_one_entry_per_task():
    for index in range(100):
        complete(f'sequential-{index}', 1000 + index)
        assert len(state._cache_states) == len(state._cache_invocations) == 1
    assert next(iter(state._cache_states.values())).last_cache_read_tokens == 1099


def test_cold_initialization_waits_for_shared_baseline(monkeypatch):
    from threading import Event
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    started, release, waiting = Event(), Event(), Event()
    reads = []

    def durable_read(*a, **k):
        reads.append(True)
        started.set()
        assert release.wait(5)
        return 34304

    monkeypatch.setattr(persist, 'read_last_turn_cache_read', durable_read)
    monkeypatch.setattr(persist, 'read_persisted_boundary', lambda *a, **k: 96)

    def enter(name):
        with state.cache_tracking_scope(task(name)):
            return (state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1),
                    get_cache_prefix_count('isolated-cache-conv', user_id=1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(enter, 'initializing-a')
        try:
            assert started.wait(5)
            with state._cache_lock:
                ancestor = next(iter(state._cache_invocations.values()))
            real_wait = ancestor.baseline_ready.wait

            def observed_wait(*a, **k):
                waiting.set()
                return real_wait(*a, **k)

            monkeypatch.setattr(ancestor.baseline_ready, 'wait', observed_wait)
            second = pool.submit(enter, 'initializing-b')
            assert waiting.wait(5)
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=5) == (34304, 96)
        assert second.result(timeout=5) == (34304, 96)
    assert len(reads) == 1
    assert not state._cache_invocations


@pytest.mark.parametrize('outer_read', [None, 12000])
def test_same_task_nested_retry_keeps_successful_state(outer_read):
    current = task('recursive-retry')
    with state.cache_tracking_scope(current):
        original = state._state_key(current['convId'], user_id=1)
        if outer_read is not None:
            detect(outer_read)
        with state.cache_tracking_scope(current):
            assert state._state_key(current['convId'], user_id=1) == original
            detect(34304)
        assert not state._cache_invocations[original].completed
    assert len(state._cache_states) == len(state._cache_invocations) == 1
    assert state._cache_states[original].last_cache_read_tokens == 34304
    with state.cache_tracking_scope(current):
        assert state._state_key(current['convId'], user_id=1) != original
        assert state.get_prev_turn_cache_read(current['convId'], user_id=1) == 34304
        detect(40000)
    assert len(state._cache_states) == len(state._cache_invocations) == 1


def _record_pending_roi():
    from lib.tasks_pkg.cache_tracking._roi import record_l2_compaction
    record_l2_compaction('isolated-cache-conv', user_id=1, tokens_before=60000,
                         tokens_after=10000, msgs_before=100, msgs_after=20)


def _capture_unpaired_roi(monkeypatch):
    from lib.tasks_pkg.cache_tracking import _roi
    emitted = []

    def emit(conv_id, roi, **kwargs):
        assert state._cache_lock.acquire(blocking=False), 'emit must run outside lock'
        state._cache_lock.release()
        emitted.append((conv_id, dict(roi), kwargs))

    monkeypatch.setattr(_roi, '_emit_l2_roi', emit)
    return emitted


def test_abort_before_dispatch_flushes_roi_once(monkeypatch):
    emitted = _capture_unpaired_roi(monkeypatch)
    complete('warm', 34304)
    with pytest.raises(RuntimeError), state.cache_tracking_scope(task('aborts')):
        _record_pending_roi()
        raise RuntimeError('abort before dispatch')
    assert len(emitted) == 1
    assert emitted[0][2]['cache_write_rebilled'] is None
    state.cleanup_cache_state('isolated-cache-conv', user_id=1)
    assert len(emitted) == 1


def test_isolated_teardown_flushes_roi_once(monkeypatch):
    emitted = _capture_unpaired_roi(monkeypatch)
    with state.cache_tracking_scope(task('child-roi', _transientRuntime=True)):
        detect(34304)
        _record_pending_roi()
    assert len(emitted) == 1
    assert emitted[0][2]['cache_write_rebilled'] is None
    assert not state._cache_states and not state._cache_invocations


def test_predecessor_eviction_flushes_roi_once(monkeypatch):
    emitted = _capture_unpaired_roi(monkeypatch)
    with state.cache_tracking_scope(task('prior-roi')):
        detect(34304)
        _record_pending_roi()
    complete('replaces-prior', 12000)
    assert len(emitted) == 1
    assert emitted[0][2]['cache_write_rebilled'] is None
    state.cleanup_cache_state('isolated-cache-conv', user_id=1)
    assert len(emitted) == 1


def test_same_task_concurrent_contexts_use_distinct_execution_keys():

    from threading import Barrier
    barrier = Barrier(2)
    current = task('same-task-independent-executions')

    def run(read):
        with state.cache_tracking_scope(current):
            key = state._state_key(current['convId'], user_id=1)
            barrier.wait(timeout=5)
            detect(read)
            barrier.wait(timeout=5)
            assert state._cache_states[key].last_cache_read_tokens == read
            return key

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, 10000)
        second = pool.submit(run, 30000)
        assert first.result(timeout=5) != second.result(timeout=5)
    assert len(state._cache_states) == len(state._cache_invocations) == 1


def test_interrupted_baseline_initialization_releases_waiter(monkeypatch):
    from threading import Event
    started, release, waiting = Event(), Event(), Event()

    def interrupted(*a, **k):
        started.set()
        assert release.wait(5)
        raise KeyboardInterrupt('cancel initializing owner')

    monkeypatch.setattr(persist, 'read_last_turn_cache_read', interrupted)

    def enter(name):
        with state.cache_tracking_scope(task(name)):
            return state.get_prev_turn_cache_read('isolated-cache-conv', user_id=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(enter, 'cancel-a')
        try:
            assert started.wait(5)
            with state._cache_lock:
                ancestor = next(iter(state._cache_invocations.values()))
            real_wait = ancestor.baseline_ready.wait

            def observed_wait(*a, **k):
                waiting.set()
                return real_wait(*a, **k)

            monkeypatch.setattr(ancestor.baseline_ready, 'wait', observed_wait)
            second = pool.submit(enter, 'cancel-b')
            assert waiting.wait(5)
        finally:
            release.set()
        with pytest.raises(KeyboardInterrupt):
            first.result(timeout=5)
        assert second.result(timeout=5) == 0
    assert not state._cache_invocations and not state._cache_states


def test_child_compaction_namespace_uses_same_invocation_guard_without_durable_reads(monkeypatch):
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    child=task('alias-child',_invocationPolicy=InvocationPolicy.child('root'),_parentTaskId='root',_transientRuntime=True)
    monkeypatch.setattr(persist,'read_persisted_boundary',lambda *_a,**_k:pytest.fail('child alias read durable cache'))
    with state.cache_tracking_scope(child):
        original=state._current_invocation(child['convId'],1)
        assert original is not None
        assert state._current_invocation('invocation:1:alias-child',1) is original
        assert state._current_invocation('invocation:1:other-child',1) is None
        assert state._current_invocation('invocation:2:alias-child',2) is None
        assert get_cache_prefix_count('invocation:1:alias-child',user_id=1)==0
