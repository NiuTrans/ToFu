"""Guard: the on-open narration backfill must NEVER run its SYNCHRONOUS
(requests-based) translate core on the event-loop thread.

WHY THIS EXISTS (regression lock)
---------------------------------
The serving webpage went unresponsive (infinite spinner, connections not
accepted) while the process stayed alive. faulthandler caught the MAIN event
loop wedged in ``ssl.read`` under this exact chain (JOURNAL 2026-07-15):

    server <module> → run_forever → asyncio events._run
      → segment_backfill.backfill_conv_narration_segments   (create_task'd)
        → runtime._translate_segments_to_map → engine._translate_one_chunk
          → http_client.http_post → requests → ssl.read   ← BLOCKED, on-loop

Root cause: ``routes/conversations.py`` did ``loop.create_task(...)`` on a
coroutine whose body ran the SYNCHRONOUS translate engine INLINE on the only
event loop. A slow/hung upstream (402 quota / 503 throttle) then froze the
whole server. The fix offloads the blocking core via ``asyncio.to_thread``
under a bounded semaphore.

These tests lock that contract with TEETH:
  • the translate work executes on a thread that is NOT the loop thread;
  • a slow (blocking) translate does NOT freeze the loop (heartbeat keeps
    ticking) — with a NEGATIVE CONTROL proving that if the offload is removed
    (blocking runs inline) the loop DOES freeze;
  • a burst of opens cannot spawn unbounded blocking workers (semaphore cap);
  • the server-side watchdog wiring (structured stall audit + on-loop
    blocking guard) is present (source-locked, mirrors the project's
    test_queue_redispatch_after_restart.py convention).
"""

import asyncio
import json
import threading
import time

import pytest

import lib.translate.runtime as rt
import lib.translate.segment_backfill as sb

pytestmark = pytest.mark.unit


# ── Fixtures: an already-translated turn with UNSTAMPED narration ────────────

def _msg():
    return {
        'role': 'assistant', 'content': 'The answer.',
        'translatedContent': 'ZH:The answer.', '_translateDone': True,
        '_msgId': 'm1',
        'segments': [
            {'type': 'thinking', 'text': 'reasoning', 'llmRound': 0},
            {'type': 'text', 'text': 'Let me read the files.',
             'deliverable': False, 'llmRound': 0},
            {'type': 'text', 'text': 'Now let me check the tests.',
             'deliverable': False, 'llmRound': 1},
            {'type': 'text', 'text': 'The answer.', 'deliverable': True,
             'terminal': True},
        ],
    }


class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeTxnConn:
    def __init__(self, current_rev=3):
        self.current_rev = current_rev

    async def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        if s.startswith('UPDATE conversations SET messages='):
            return _Cursor(1 if params[2] == self.current_rev else 0)
        return _Cursor(1)


class _FakeTxnCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _patch_db(monkeypatch, *, fresh_per_call=True, rev=3):
    """Patch the async DB seams the backfill imports at call time.

    ``fresh_per_call``: return a NEW copy of the messages each fetch so
    concurrent convs don't share mutable segment dicts.
    """
    import lib.database as dbmod
    import lib.database.aio as aiomod

    async def _fetchone(sql, params=(), **kw):
        msgs = [_msg()] if fresh_per_call else _shared
        return {'messages': json.dumps(msgs), 'rev': rev}

    _shared = [_msg()]
    monkeypatch.setattr(dbmod, 'async_fetchone', _fetchone, raising=False)
    monkeypatch.setattr(aiomod, 'async_transaction',
                        lambda: _FakeTxnCtx(_FakeTxnConn(rev)), raising=False)


def _reset_semaphore():
    """Force a fresh per-loop semaphore (each asyncio.run = new loop)."""
    sb._sem = None
    sb._sem_loop = None


# ── 1. The blocking translate executes OFF the event-loop thread ─────────────

def test_backfill_translate_runs_off_the_event_loop_thread(monkeypatch):
    _reset_semaphore()
    loop_tid = {}
    exec_tid = {}

    def _capture_tf(text, system_prompt, source='', target='', **kw):
        exec_tid['t'] = threading.get_ident()
        return 'ZH:' + text, {'_dispatch': {'model': 'fake'}}

    monkeypatch.setattr(rt, '_translate_freetext', _capture_tf)
    _patch_db(monkeypatch)

    async def _drive():
        loop_tid['t'] = threading.get_ident()
        return await sb.backfill_conv_narration_segments('c1')

    summary = asyncio.run(_drive())

    assert summary['wrote'] is True and summary['segmentsStamped'] == 2, \
        'backfill must actually run + stamp (else the thread assert is vacuous)'
    assert 't' in exec_tid, 'the translate core never ran'
    assert exec_tid['t'] != loop_tid['t'], \
        'translate ran ON the event-loop thread — the wedge bug is back'


# ── 2. A slow (blocking) translate does NOT freeze the loop ──────────────────

def _run_with_heartbeat(monkeypatch, *, inline_offload=False):
    """Drive one backfill whose translate blocks 0.4s/segment while an on-loop
    heartbeat ticks every 20ms. Returns the max gap between heartbeats.

    ``inline_offload=True`` NEUTERS the fix: to_thread runs the blocking fn
    INLINE on the loop (the pre-fix world) — used as the negative control.
    """
    _reset_semaphore()

    def _slow_tf(text, system_prompt, source='', target='', **kw):
        time.sleep(0.4)  # simulate a slow/hung upstream HTTP
        return 'ZH:' + text, {'_dispatch': {'model': 'slow'}}

    monkeypatch.setattr(rt, '_translate_freetext', _slow_tf)
    _patch_db(monkeypatch)

    if inline_offload:
        async def _inline(fn, *a, **kw):
            return fn(*a, **kw)  # run the blocking core ON the loop
        monkeypatch.setattr(asyncio, 'to_thread', _inline)

    async def _drive():
        ticks = []
        stop = asyncio.Event()

        async def _hb():
            # Append-FIRST: when the loop unfreezes after a block, this task
            # resumes from its expired sleep and records the post-freeze tick
            # BEFORE it sees stop — so the gap that spans the freeze is measured
            # (a check-first loop would break on stop and never record it).
            while True:
                ticks.append(time.monotonic())
                if stop.is_set():
                    break
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(_hb())
        # Warm up: let the heartbeat tick a few times BEFORE the backfill so a
        # subsequent freeze produces a measurable gap (without this, nothing
        # yields to the loop before an inline block and the heartbeat never
        # records a first tick → a vacuous 0.0 gap).
        await asyncio.sleep(0.1)
        await sb.backfill_conv_narration_segments('c1')
        stop.set()
        await hb
        return ticks

    ticks = asyncio.run(_drive())
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    return max(gaps) if gaps else 0.0


def test_slow_backfill_does_not_freeze_loop(monkeypatch):
    max_gap = _run_with_heartbeat(monkeypatch, inline_offload=False)
    assert max_gap < 0.2, (
        f'event loop was frozen for {max_gap:.2f}s during a blocking translate '
        f'— the off-loop offload is not working')


def test_nc_inline_blocking_DOES_freeze_loop(monkeypatch):
    """NEGATIVE CONTROL: remove the offload (run the blocking core inline on
    the loop) and the heartbeat MUST stall — proving the to_thread offload in
    test_slow_backfill_does_not_freeze_loop is the load-bearing mechanism, not
    incidental timing."""
    max_gap = _run_with_heartbeat(monkeypatch, inline_offload=True)
    assert max_gap >= 0.3, (
        f'NC failed: inline blocking should have frozen the loop (>=0.3s gap) '
        f'but max gap was only {max_gap:.2f}s — the guard test has no teeth')


# ── 3. Concurrency is capped across a burst of opens ─────────────────────────

def test_backfill_concurrency_is_capped(monkeypatch):
    monkeypatch.setattr(sb, '_MAX_CONCURRENT_BACKFILLS', 2)
    _reset_semaphore()

    conc = {'cur': 0, 'max': 0}
    lock = threading.Lock()

    def _tracking_tf(text, system_prompt, source='', target='', **kw):
        with lock:
            conc['cur'] += 1
            conc['max'] = max(conc['max'], conc['cur'])
        time.sleep(0.15)
        with lock:
            conc['cur'] -= 1
        return 'ZH:' + text, {'_dispatch': {'model': 'x'}}

    monkeypatch.setattr(rt, '_translate_freetext', _tracking_tf)
    _patch_db(monkeypatch)

    async def _drive():
        tasks = [asyncio.create_task(
            sb.backfill_conv_narration_segments(f'c{i}')) for i in range(6)]
        return await asyncio.gather(*tasks)

    asyncio.run(_drive())

    assert conc['max'] <= 2, (
        f'{conc["max"]} backfills ran blocking work at once — the semaphore cap '
        f'(2) leaked; a burst of opens could exhaust the executor')
    assert conc['max'] == 2, (
        f'expected the cap (2) to be reached with 6 concurrent convs, saw '
        f'{conc["max"]} — parallelism/dedup regressed')


# ── 4. Server watchdog wiring is present (source-locked) ─────────────────────
#     Mirrors tests/test_queue_redispatch_after_restart.py: asserting on
#     server.py source avoids importing the heavy module (opens fault sinks,
#     installs the Flask→Quart shim) while still locking the wiring contract.

def _server_src():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, 'server.py'), encoding='utf-8') as f:
        return f.read()


def test_loopwatch_emits_structured_stall_audit():
    src = _server_src()
    assert "audit_log('event_loop_stall'" in src, \
        'LoopWatch must emit a structured event_loop_stall audit entry'
    assert '_extract_loop_top_frame(' in src, \
        'the stall path must name the culprit top frame'
    assert 'top_frame=' in src


def test_loop_blocking_guard_is_optin_and_rate_limited():
    src = _server_src()
    # The sub-stall detector exists...
    assert 'slow_callback_duration' in src and 'set_debug(True)' in src, \
        'the on-loop blocking guard (slow-callback detector) must be present'
    # ...but it is DEFAULT OFF (opt-in) — set_debug is unsafe as a 24/7 default
    # on this high-concurrency service (per-call_soon stack walk + log flood).
    assert 'TOFU_LOOP_DEBUG_GUARD' in src, \
        'the debug guard must be opt-in via TOFU_LOOP_DEBUG_GUARD, not always-on'
    # ...and when enabled, its warnings are rate-limited so a burst cannot
    # flood error.log (which would back-pressure the loop).
    assert 'addFilter' in src and 'suppressed' in src, \
        'the enabled guard must rate-limit the asyncio slow-callback warnings'


def test_backfill_offloads_blocking_core_under_semaphore():
    import inspect
    fn_src = inspect.getsource(sb.backfill_conv_narration_segments)
    assert 'asyncio.to_thread(' in fn_src, \
        'the blocking translate must be offloaded via asyncio.to_thread'
    assert '_get_backfill_semaphore()' in fn_src, \
        'the offload must be bounded by the concurrency semaphore'
    assert "log_context('narration_backfill'" in fn_src, \
        'the backfill must record start/end/duration via log_context'
