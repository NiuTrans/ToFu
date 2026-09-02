"""Regression tests for the senior-review fix batch (2026-06).

Each test class pins ONE confirmed bug found during the architecture
review so it cannot silently regress:

  1. TestSlotInflightRelease       — lib/llm_dispatch/slot.py Slot.release()
                                      + the dispatch payload-reject paths that
                                      must release instead of leaking inflight.
  2. TestContentRefSlice           — executor._resolve_content_ref coerces /
                                      bounds-checks model-supplied slice indices.
  3. TestRateLimiterOrdering       — swarm RateLimiter waits out backoff BEFORE
                                      taking a permit.
  4. TestToolAdjacencyDedup        — llm_sanitize adjacency scan is bounded by
                                      tool_call count, not the dedup'd id set.
  5. TestReasoningPreservedOnStrip — llm_sanitize keeps reasoning_content /
                                      thinking_signature / reasoning_details when
                                      stripping orphaned/non-adjacent tool_calls.
  6. TestAbortFinishRace           — TaskRuntime.abort() sets abort_event under
                                      _lock so a finish() race can't mislabel an
                                      aborted task 'done'.

NOTE: the backtest win_rate / carry-forward regression tests moved to the
standalone tofu-trading package along with lib/trading_backtest_engine.

Run:  pytest tests/test_review_fixes.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════
#  1. Slot.release() — inflight must not leak on payload rejects
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSlotInflightRelease:
    def _slot(self):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name='k0', api_key='', model='qwen-plus',
                    capabilities={'text'})

    def test_release_decrements_inflight(self):
        slot = self._slot()
        slot.record_request()
        slot.record_request()
        assert slot.inflight == 2
        slot.release()
        assert slot.inflight == 1

    def test_release_floors_at_zero(self):
        slot = self._slot()
        slot.release()  # nothing reserved
        assert slot.inflight == 0

    def test_release_does_not_touch_health_counters(self):
        """release() is for payload-level rejects — it must NOT bump the
        error counters (that would wrongly cool the slot) nor mark success."""
        slot = self._slot()
        slot.record_request()
        slot.release()
        assert slot.consecutive_errors == 0
        assert slot.total_errors == 0
        assert slot.last_success_time == 0.0

    def test_record_request_release_roundtrip_returns_to_zero(self):
        slot = self._slot()
        for _ in range(50):
            slot.record_request()
            slot.release()
        assert slot.inflight == 0

    def test_dispatch_handlers_call_release(self):
        """Guard against the original leak: the payload-reject except blocks
        in api.py must call slot.release() before re-raising."""
        import inspect
        from lib.llm_dispatch import api
        src = inspect.getsource(api)
        # Every payload-level reject handler raises; each must release first.
        assert src.count('slot.release()') >= 6, (
            'expected slot.release() in both dispatch_chat and dispatch_stream '
            'payload-reject handlers (content filter / prompt-too-long / '
            'invalid-image / abort)')


# ═══════════════════════════════════════════════════════════
#  2. content_ref slice safety
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestContentRefSlice:
    def _task(self, content):
        return {'id': 'abcdef0123456', 'toolRounds': [
            {'roundNum': 1, 'toolContent': content}]}

    def test_full_content_when_no_slice(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        out = _resolve_content_ref(self._task('hello world'), {'tool_round': 1})
        assert out == 'hello world'

    def test_valid_slice(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        out = _resolve_content_ref(self._task('hello world'),
                                   {'tool_round': 1, 'start': 0, 'end': 5})
        assert out == 'hello'

    def test_string_indices_are_coerced(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        # Model emitted JSON strings — must coerce, not silently mis-slice.
        out = _resolve_content_ref(self._task('hello world'),
                                   {'tool_round': 1, 'start': '0', 'end': '5'})
        assert out == 'hello'

    def test_out_of_range_end_is_clamped(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        out = _resolve_content_ref(self._task('hello'),
                                   {'tool_round': 1, 'start': 0, 'end': 9999})
        assert out == 'hello'

    def test_negative_start_clamped_to_zero(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        # No Python-style wraparound — negative clamps to 0.
        out = _resolve_content_ref(self._task('hello'),
                                   {'tool_round': 1, 'start': -3})
        assert out == 'hello'

    def test_end_before_start_returns_empty(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        out = _resolve_content_ref(self._task('hello world'),
                                   {'tool_round': 1, 'start': 8, 'end': 2})
        assert out == ''

    def test_garbage_index_falls_back_to_default(self):
        from lib.tasks_pkg.executor import _resolve_content_ref
        out = _resolve_content_ref(self._task('hello world'),
                                   {'tool_round': 1, 'start': 'abc'})
        assert out == 'hello world'  # bad start → default 0, no end → full


# ═══════════════════════════════════════════════════════════
#  5. RateLimiter — backoff before permit
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRateLimiterOrdering:
    def test_backoff_waited_before_acquiring_permit(self, monkeypatch):
        import lib.swarm.rate_limiter as rl_mod
        from lib.swarm.rate_limiter import RateLimiter

        order = []
        rl = RateLimiter(max_concurrent=1)

        real_acquire = rl._semaphore.acquire

        def _tracked_acquire(*a, **k):
            order.append('permit')
            return real_acquire(*a, **k)

        monkeypatch.setattr(rl._semaphore, 'acquire', _tracked_acquire)
        monkeypatch.setattr(rl_mod.time, 'sleep', lambda s: order.append('sleep'))

        # Force an active backoff window.
        rl._rate_limit_until = rl_mod.time.monotonic() + 100
        rl.acquire()

        # The backoff sleep must happen BEFORE the semaphore permit is taken,
        # otherwise a permit is held hostage for the whole backoff.
        assert order == ['sleep', 'permit'], order

    def test_no_backoff_means_no_sleep(self, monkeypatch):
        import lib.swarm.rate_limiter as rl_mod
        from lib.swarm.rate_limiter import RateLimiter

        slept = []
        rl = RateLimiter(max_concurrent=2)
        monkeypatch.setattr(rl_mod.time, 'sleep', lambda s: slept.append(s))
        rl.acquire()
        assert slept == []
        assert rl.active == 1


# ═══════════════════════════════════════════════════════════
#  6. Tool-call adjacency dedup bound
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolAdjacencyDedup:
    def test_adjacent_results_preserved(self):
        from lib.llm_sanitize import _fix_tool_call_adjacency
        msgs = [
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'tool_calls': [
                {'id': 'a', 'function': {'name': 'f', 'arguments': '{}'}},
                {'id': 'b', 'function': {'name': 'g', 'arguments': '{}'}},
            ]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'ra'},
            {'role': 'tool', 'tool_call_id': 'b', 'content': 'rb'},
        ]
        out = _fix_tool_call_adjacency(msgs)
        # All results already adjacent → unchanged order, both kept.
        assert [m.get('role') for m in out] == ['user', 'assistant', 'tool', 'tool']
        assert [m.get('tool_call_id') for m in out if m.get('role') == 'tool'] == ['a', 'b']

    def test_three_calls_all_results_kept(self):
        """The scan bound is the tool_call COUNT — three calls, three adjacent
        results must all be recognised as adjacent (the dedup-set bound bug
        would stop scanning early)."""
        from lib.llm_sanitize import _fix_tool_call_adjacency
        msgs = [
            {'role': 'assistant', 'tool_calls': [
                {'id': 'a', 'function': {'name': 'f', 'arguments': '{}'}},
                {'id': 'b', 'function': {'name': 'g', 'arguments': '{}'}},
                {'id': 'c', 'function': {'name': 'h', 'arguments': '{}'}},
            ]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'ra'},
            {'role': 'tool', 'tool_call_id': 'b', 'content': 'rb'},
            {'role': 'tool', 'tool_call_id': 'c', 'content': 'rc'},
            {'role': 'user', 'content': 'next'},
        ]
        out = _fix_tool_call_adjacency(msgs)
        kept = [m.get('tool_call_id') for m in out if m.get('role') == 'tool']
        assert kept == ['a', 'b', 'c']
        # The trailing user message survives intact.
        assert out[-1] == {'role': 'user', 'content': 'next'}


# ═══════════════════════════════════════════════════════════
#  7. Reasoning fields survive tool_call stripping
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReasoningPreservedOnStrip:
    """When an assistant turn's tool_calls are stripped (orphaned or
    non-adjacent), its reasoning_content / thinking_signature /
    reasoning_details MUST be preserved. Rebuilding as a bare
    {'role':'assistant','content':...} dropped them and triggered Anthropic
    HTTP 400 on the next extended-thinking replay turn."""

    def test_orphaned_tool_call_keeps_reasoning(self):
        from lib.llm_sanitize import _fix_orphaned_tool_calls
        msgs = [
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'thinking done',
             'reasoning_content': 'R' * 20,
             'thinking_signature': 'sig-abc',
             'reasoning_details': [{'type': 'reasoning', 'text': 'r'}],
             'tool_calls': [
                 {'id': 'orphan', 'function': {'name': 'f', 'arguments': '{}'}},
             ]},
            # No matching tool result → orphaned → tool_calls stripped.
        ]
        out = _fix_orphaned_tool_calls(msgs)
        asst = [m for m in out if m.get('role') == 'assistant'][0]
        assert 'tool_calls' not in asst
        assert asst['content'] == 'thinking done'
        assert asst['reasoning_content'] == 'R' * 20
        assert asst['thinking_signature'] == 'sig-abc'
        assert asst['reasoning_details'] == [{'type': 'reasoning', 'text': 'r'}]

    def test_non_adjacent_tool_call_keeps_reasoning(self):
        from lib.llm_sanitize import _fix_tool_call_adjacency
        msgs = [
            {'role': 'assistant', 'content': 'c',
             'reasoning_content': 'keep-me',
             'tool_calls': [
                 {'id': 'x', 'function': {'name': 'f', 'arguments': '{}'}},
             ]},
            # tool result missing entirely → tool_calls stripped.
            {'role': 'user', 'content': 'next'},
        ]
        out = _fix_tool_call_adjacency(msgs)
        asst = [m for m in out if m.get('role') == 'assistant'][0]
        assert 'tool_calls' not in asst
        assert asst['reasoning_content'] == 'keep-me'


# ═══════════════════════════════════════════════════════════
#  8. TaskRuntime abort/finish race
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAbortFinishRace:
    def test_abort_sets_event_under_lock(self):
        from lib.agent_core.task_runtime import TaskRuntime
        rt = TaskRuntime('test-abort')
        task = rt.create(user_id=1)
        assert rt.abort(task['id']) is True
        assert task['abort_event'].is_set()

    def test_abort_after_finish_is_noop(self):
        from lib.agent_core.task_runtime import TaskRuntime
        rt = TaskRuntime('test-abort')
        task = rt.create(user_id=1)
        rt.finish(task['id'], result='done')
        assert rt.abort(task['id']) is False
        assert task['status'] == 'done'

    def test_finish_after_abort_marks_aborted(self):
        from lib.agent_core.task_runtime import TaskRuntime
        rt = TaskRuntime('test-abort')
        task = rt.create(user_id=1)
        rt.abort(task['id'])
        rt.finish(task['id'])  # no error → abort_event decides 'aborted'
        assert task['status'] == 'aborted'
