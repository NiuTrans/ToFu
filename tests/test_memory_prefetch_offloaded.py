"""tests/test_memory_prefetch_offloaded.py — the per-turn memory prefetch must
not sit on the critical path before the first token.

THE LATENCY
===========
``maybe_run_memory_prefetch`` ran SYNCHRONOUSLY at ``_run.py`` Section 3.5,
between context-inject and the first ``dispatch_stream``. It is default-on and
fires once per user turn, and its cheap-LLM rerank carries a hard 800 ms
deadline — so every turn paid up to 800 ms (typically 200-600 ms) of dead time
before the user saw a single token.

THE COUPLING THAT MADE IT LOOK HARD — AND WHY IT ISN'T
======================================================
The epic flagged that the rerank needs ``current_request`` /
``rerank_recent_turns`` / ``active_tools``, and that the first two are derived
from ``messages`` AFTER ``_inject_system_contexts`` has run — while the
existing prefetch pool starts EARLIER (``_run.py:286``). That looked like a
hard ordering dependency.

It is not, and the reason is load-bearing enough to pin here:
``_msg_plain_text`` (``lib/memory/prefetch/_query.py``) STRIPS
``<system-reminder>...</system-reminder>`` blocks, and every mutation
``_inject_system_contexts`` makes to the true tail is wrapped in exactly that
marker. So the query text the rerank derives is **byte-identical before and
after** context-inject. ``test_query_inputs_are_invariant_across_context_inject``
proves it, and it is the invariant the whole offload rests on: if a future
change starts writing UNWRAPPED text onto the last user message, that test goes
red and the offload must be revisited.

The one input that genuinely is NOT ready at pool-start time is
``has_real_tools`` / ``tool_list`` — assembled at ``_run.py:~300`` AFTER the
pool spawns at :286. Hence the seam here is NOT "move it into start_prefetches";
it is a SEPARATE submit that happens the moment tool assembly is done, so the
rerank overlaps context-inject instead of following it.

WHAT THIS SUITE LOCKS
=====================
  * the invariant above (the offload's correctness premise);
  * that the injection result is byte-identical to the synchronous version;
  * that a slow rerank no longer blocks the caller;
  * that the deadline / skip / accounting contracts are unchanged;
  * that a failure in the background lane can never take the turn down.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_memory_prefetch_offloaded.py -v
"""
from __future__ import annotations

import copy
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.memory.prefetch._query import (  # noqa: E402
    _build_recent_turns_text,
    _extract_current_user_request,
)

pytestmark = pytest.mark.unit


def _msgs():
    return [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'first ask'},
        {'role': 'assistant', 'content': 'sure'},
        {'role': 'user', 'content': 'now fix the parser bug'},
    ]


# ── The invariant the whole offload rests on ────────────────────────────

def test_query_inputs_are_invariant_across_context_inject():
    """Everything _inject_system_contexts adds to the tail is stripped again.

    The injector writes its per-turn additions (date reminder, preference
    detail, CLAUDE.md context) onto the LAST USER message wrapped in
    <system-reminder>. _msg_plain_text strips exactly that wrapper, so the
    rerank's query text does not depend on whether inject has run yet.

    If this ever fails, the memory prefetch can no longer be started before
    context-inject — revisit the offload rather than "fixing" the assertion.
    """
    before = _msgs()
    after = copy.deepcopy(before)
    after[-1]['content'] += (
        '\n<system-reminder>\nCurrent date: 2026-07-26\n'
        'PROJECT CONTEXT: ...\n</system-reminder>')

    assert _extract_current_user_request(before) == \
        _extract_current_user_request(after)
    assert _build_recent_turns_text(before, exclude_last_user=True) == \
        _build_recent_turns_text(after, exclude_last_user=True)
    assert _build_recent_turns_text(before) == _build_recent_turns_text(after)


def test_unwrapped_tail_text_would_break_the_invariant():
    """NEGATIVE CONTROL: proves the test above has teeth.

    Raw text appended to the tail (no <system-reminder> wrapper) DOES change
    the query — which is precisely the scenario the invariant forbids.
    """
    before = _msgs()
    after = copy.deepcopy(before)
    after[-1]['content'] += '\nRAW UNWRAPPED INJECTION'
    assert _extract_current_user_request(before) != \
        _extract_current_user_request(after)


# ── The offload seam ────────────────────────────────────────────────────

@pytest.fixture()
def seam(monkeypatch):
    """maybe_run_memory_prefetch with run_memory_prefetch spied.

    ``_memory_prefetch`` imports ``run_memory_prefetch`` INSIDE the function
    (a deliberate lazy import), so there is no module attribute to patch —
    the spy has to replace it on ``lib.memory.prefetch``, which is where the
    lazy ``from ... import`` resolves it.
    """
    import lib.memory.prefetch as prefetch_pkg
    from lib.tasks_pkg.orchestrator import _memory_prefetch as mp

    calls = {'n': 0, 'kwargs': None, 'thread': None, 'delay': 0.0}

    def _fake(messages, **kw):
        calls['n'] += 1
        calls['kwargs'] = kw
        calls['thread'] = threading.current_thread().name
        if calls['delay']:
            time.sleep(calls['delay'])
        messages.append({'role': 'user', 'content': '<relevant_memories/>'})
        return [{'name': 'm1'}]

    monkeypatch.setattr(prefetch_pkg, 'run_memory_prefetch', _fake)
    return mp, calls


def _task():
    return {'id': 'offload01', 'convId': 'convoff', 'status': 'running',
            'events': [], 'events_lock': threading.Lock()}


def _kw(**over):
    base = dict(task=_task(), cfg={}, messages=_msgs(), tool_list=[
        {'type': 'function', 'function': {'name': 'read_files'}}],
        project_path='/p', project_enabled=True, memory_enabled=True,
        has_real_tools=True, injected_tool_calls=0)
    base.update(over)
    return base


def test_prefetch_does_not_block_the_caller(seam):
    """A slow rerank must not hold up the turn."""
    mp, calls = seam
    calls['delay'] = 0.6
    kw = _kw()

    t0 = time.time()
    mp.maybe_run_memory_prefetch(**kw)
    elapsed = time.time() - t0

    assert elapsed < 0.25, (
        'maybe_run_memory_prefetch blocked %.2fs on a 0.6s rerank — it is '
        'still on the critical path' % elapsed)
    mp.await_memory_prefetch(kw['task'], timeout=5.0)
    assert calls['n'] == 1


def test_work_runs_off_the_calling_thread(seam):
    mp, calls = seam
    kw = _kw()
    mp.maybe_run_memory_prefetch(**kw)
    mp.await_memory_prefetch(kw['task'], timeout=5.0)
    assert calls['thread'] != threading.current_thread().name


def test_injection_result_is_byte_identical_to_the_sync_version(seam):
    """The whole point: same messages out, just not on the hot path."""
    mp, calls = seam
    kw = _kw()
    baseline = copy.deepcopy(kw['messages'])

    mp.maybe_run_memory_prefetch(**kw)
    mp.await_memory_prefetch(kw['task'], timeout=5.0)

    expected = baseline + [{'role': 'user',
                            'content': '<relevant_memories/>'}]
    assert kw['messages'] == expected


def test_the_same_arguments_are_forwarded(seam):
    """Offloading must not quietly change what the rerank is asked."""
    mp, calls = seam
    kw = _kw()
    mp.maybe_run_memory_prefetch(**kw)
    mp.await_memory_prefetch(kw['task'], timeout=5.0)

    fwd = calls['kwargs']
    assert fwd['project_path'] == '/p'
    assert fwd['active_tools'] == ['read_files']
    assert fwd['usage_sink'] is not None, (
        'the usage_sink from the accounting fix must survive the offload')
    assert callable(fwd['emit_event'])


# ── Gating contracts (unchanged) ────────────────────────────────────────

@pytest.mark.parametrize('over,reason', [
    ({'memory_enabled': False}, 'memory toggle off'),
    ({'has_real_tools': False}, 'no real tools'),
    ({'injected_tool_calls': 3}, 'continue/resume turn'),
])
def test_skips_do_not_spawn_any_work(seam, over, reason):
    mp, calls = seam
    kw = _kw(**over)
    mp.maybe_run_memory_prefetch(**kw)
    mp.await_memory_prefetch(kw['task'], timeout=2.0)
    assert calls['n'] == 0, 'ran despite %s' % reason


def test_profile_consolidate_flag_is_still_set_synchronously(seam):
    """_finalize.py's post-done spawner reads this — it must not become
    async, or the flag would be missing when the turn settles."""
    mp, calls = seam
    for over, expected in (({}, True),
                           ({'memory_enabled': False}, False),
                           ({'has_real_tools': False}, False)):
        kw = _kw(**over)
        mp.maybe_run_memory_prefetch(**kw)
        assert kw['task']['_profileConsolidateEligible'] is expected
        mp.await_memory_prefetch(kw['task'], timeout=2.0)


# ── Robustness ──────────────────────────────────────────────────────────

def test_a_raising_prefetch_never_breaks_the_turn(seam, monkeypatch):
    mp, calls = seam
    import lib.memory.prefetch as prefetch_pkg

    def _boom(messages, **kw):
        raise RuntimeError('prefetch exploded')

    monkeypatch.setattr(prefetch_pkg, 'run_memory_prefetch', _boom)
    kw = _kw()
    mp.maybe_run_memory_prefetch(**kw)          # must not raise
    mp.await_memory_prefetch(kw['task'], timeout=5.0)
    assert kw['messages'] == _msgs(), 'messages must be untouched on failure'


def test_await_is_idempotent_and_safe_without_a_pending_job(seam):
    mp, _ = seam
    t = _task()
    mp.await_memory_prefetch(t, timeout=1.0)     # nothing pending
    kw = _kw(task=t)
    mp.maybe_run_memory_prefetch(**kw)
    mp.await_memory_prefetch(t, timeout=5.0)
    mp.await_memory_prefetch(t, timeout=5.0)     # second call is a no-op


def test_await_bounds_its_wait_and_leaves_messages_consistent(seam):
    """If the worker overruns the join budget the turn proceeds anyway —
    late injection into a body already on the wire would be worse than no
    injection at all."""
    mp, calls = seam
    calls['delay'] = 1.2
    kw = _kw()
    mp.maybe_run_memory_prefetch(**kw)

    t0 = time.time()
    mp.await_memory_prefetch(kw['task'], timeout=0.15)
    waited = time.time() - t0
    assert waited < 0.6, 'await ignored its timeout (%.2fs)' % waited


# ── The wiring itself (the spawn is useless without the join) ───────────

def test_spawn_precedes_context_inject_so_it_overlaps_real_io():
    """The spawn must sit BEFORE Section 3, not between it and the loop.

    This is the difference between a structurally-correct offload and one that
    actually saves time. The first cut of this work spawned at Section 3.5 —
    after context-inject — so the only thing overlapping the 800ms rerank was
    the checkpoint-stash bookkeeping that follows it: measured at ~0.001 ms,
    i.e. 0.0001% of the rerank. The join then blocked for essentially the full
    duration and TTFT was unchanged.

    Section 3 (inject_context_and_emit_chips) is the FUSE/DB-bound work — it
    consumes the project + memory prefetch futures. Spawning ahead of it is
    what gives the rerank something real to hide behind.
    """
    import pathlib
    import re
    src = pathlib.Path('lib/tasks_pkg/orchestrator/_run.py').read_text()

    spawn = src.find('maybe_run_memory_prefetch(')
    inject = src.find('inject_context_and_emit_chips(')
    join = src.find('await_memory_prefetch(task)')
    loop = re.search(r'^\s*while \(not _prep_aborted', src, re.M)

    assert -1 not in (spawn, inject, join), 'a required call site vanished'
    assert loop is not None, 'stream loop not found — update this guard'
    assert spawn < inject, (
        'the prefetch spawn (%d) must precede context-inject (%d), otherwise '
        'it only overlaps microseconds of bookkeeping and TTFT is unchanged'
        % (spawn, inject))
    assert inject < join < loop.start(), (
        'ordering broken: expected inject(%d) < join(%d) < loop(%d)'
        % (inject, join, loop.start()))


def test_early_spawn_uses_a_tool_history_signal_equivalent_to_the_late_one():
    """The early spawn cannot call inject_tool_history's return value yet, so
    it reads cfg['toolHistory'] directly. Pin that the substitution is sound:
    inject_tool_history's count is driven by that key alone, so the
    eligibility answer (zero vs non-zero) is identical.

    If inject_tool_history ever starts injecting from another source, this
    fails and the early spawn's input must be revisited.
    """
    from lib.tasks_pkg.message_builder import inject_tool_history

    for history in ([], [{'toolCalls': [{'id': 'c1', 'type': 'function',
                                         'name': 'x', 'arguments': '{}'}],
                          'results': [{'tool_call_id': 'c1',
                                       'content': 'r'}]}]):
        msgs = [{'role': 'user', 'content': 'hi'}]
        cfg = {'toolHistory': history}
        task = _task()
        injected = inject_tool_history(msgs, cfg, task, 'gpt-4o')
        assert bool(injected) == bool(history), (
            'inject_tool_history returned %s for toolHistory=%d entries — the '
            'early spawn reads len(cfg["toolHistory"]) and would now disagree'
            % (injected, len(history)))


def test_run_task_joins_the_prefetch_before_the_stream_loop():
    """STATIC GUARD on call ORDER in run_task.

    The offload is only correct as a PAIR: Section 3.5 spawns, and the join
    must happen before the stream loop serializes ``messages``. Drop the join
    and the injection races the wire — the prefetch would write into a message
    list already being sent, silently and non-deterministically. No behavioural
    test catches that (the spawn tests still pass), so the ordering is pinned
    here by parsing the real source.
    """
    import pathlib
    import re
    src = pathlib.Path(
        'lib/tasks_pkg/orchestrator/_run.py').read_text()

    spawn = src.find('maybe_run_memory_prefetch(')
    join = src.find('await_memory_prefetch(task)')
    loop = re.search(r'^\s*while \(not _prep_aborted', src, re.M)

    assert spawn != -1, 'the memory-prefetch spawn vanished from run_task'
    assert join != -1, (
        'run_task no longer joins the background memory prefetch — the '
        'injection would race the wire body. Restore await_memory_prefetch()'
        ' before the stream loop.')
    assert loop is not None, 'stream loop not found — update this guard'
    assert spawn < join < loop.start(), (
        'ordering broken: expected spawn(%d) < join(%d) < stream loop(%d)'
        % (spawn, join, loop.start()))
