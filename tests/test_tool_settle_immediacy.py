"""tests/test_tool_settle_immediacy.py — pt_67ffc2b700094ce9 face ①.

THE REPORTED SYMPTOM
--------------------
    "searching takes a very long time … once the backend search finishes the
     frontend must IMMEDIATELY stop the spinner and mark it completed. Don't
     wait until the first token of the next tool call."

The owner's first guess was that the settle waited for the NEXT round's first
token. Measured, it is both earlier and wider than that: a tool's
``tool_complete`` waits for EVERY SIBLING TOOL IN THE SAME ROUND.

WHY (the barrier)
-----------------
``execute_tool_pipeline`` (lib/tasks_pkg/tool_dispatch/_pipeline.py) has two
phases around one barrier:

  * MAIN phase — ``as_completed(futures)`` yields each tool the instant it
    finishes. The handler already emitted ``tool_result`` from inside the worker
    thread (``_finalize_tool_round`` → status='done'), so THAT half is prompt.
  * ``finally: pool.shutdown(wait=True)``  ← THE BARRIER
  * POST phase — a ``for … in parsed_tcs`` loop that runs the L0 budget, counts
    tokens, and emits ``tool_complete`` per tool.

So in a round with a 0.05s ``read_files`` and a 40s ``web_search``, the fast
tool's ``toolContent`` / ``toolTokens`` / preview button land 40 SECONDS after
it actually finished. The round is structurally incapable of reporting a
per-tool completion before its slowest member returns.

WHAT THIS SUITE PINS
--------------------
  1. ORDERING (the load-bearing face): a fast tool's ``tool_complete`` must be
     emitted BEFORE a slow sibling's ``tool_result`` — i.e. the fast tool
     settled fully while the slow one was still running. Under the barrier this
     is impossible, so this is the failing-first test.
  2. The fast tool's ``tool_complete`` must carry the SAME payload the
     post-phase used to attach (toolContent + toolTokens), so moving the
     emission earlier is not a downgrade to a bare ping.
  3. Per-tool L0 budgeting must still happen before the event (the preview must
     show what the model actually sees) — an oversized result must still be
     stamped ``compactionLayer='L0'`` on the EARLY event.
  4. The round-aggregate budget stays AFTER the barrier (it is inherently
     cross-tool), and when it rewrites an already-announced result it must
     issue a ``tool_compacted`` correction rather than delaying the first
     ``tool_complete``.
  5. Exactly one ``tool_complete`` per tool call — moving the emission must not
     double-emit (once early, once in the post-phase).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_tool_settle_immediacy.py -v
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  Harness — drive the REAL execute_tool_pipeline with fake tools
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'settle-task-1',
        '_userId': 1,
        'convId': 'cv-settle-1',
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, rn: int, args: str = '{}'):
    """One parsed_tcs 7-tuple: (tc, fn_name, tc_id, fn_args, rn, round_entry, err)."""
    round_entry = {
        'roundNum': rn,
        'toolCallId': tc_id,
        'toolName': fn_name,
        'query': fn_name,
        'status': 'searching',
        'results': None,
    }
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': args}}
    return (tc, fn_name, tc_id, {}, rn, round_entry, None)


class _Recorder:
    """Capture the ordered event stream, timestamped on arrival."""

    def __init__(self):
        self.events: list[tuple[float, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append((time.time(), dict(event)))

    def types_for(self, tc_id: str) -> list[str]:
        return [e['type'] for _, e in self.events
                if e.get('toolCallId') == tc_id]

    def first_index(self, tc_id: str, etype: str) -> int:
        for i, (_, e) in enumerate(self.events):
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return i
        return -1

    def find(self, tc_id: str, etype: str) -> dict | None:
        for _, e in self.events:
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return e
        return None

    def count(self, tc_id: str, etype: str) -> int:
        return sum(1 for _, e in self.events
                   if e.get('toolCallId') == tc_id and e.get('type') == etype)


@pytest.fixture()
def rec(monkeypatch):
    """Route EVERY append_event the pipeline reaches into one recorder.

    The pipeline emits through three different import sites (its own module,
    the executor's _finalize, and the heartbeat module), all of which must land
    in one ordered log or the ordering assertions would be blind to half the
    stream.
    """
    r = _Recorder()
    import lib.tasks_pkg.tool_dispatch._heartbeat as td_facade
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    from lib.tasks_pkg.executor import _finalize as exec_finalize

    monkeypatch.setattr(_pipeline, 'append_event', r, raising=False)
    monkeypatch.setattr(td_facade, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


@pytest.fixture()
def fake_tools(monkeypatch):
    """Replace tool execution with scripted sleepers.

    Returns a dict the test fills: ``{fn_name: (sleep_sec, result_text)}``.
    Each fake finalizes its round exactly the way a real handler does (via the
    shared ``_finalize_tool_round`` seam), so ``tool_result`` timing is the
    production timing.
    """
    script: dict[str, tuple[float, str]] = {}

    def _fake_execute_one(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                          cfg, project_path, project_enabled, all_tools=None):
        sleep_s, text = script.get(fn_name, (0.0, 'ok'))
        if sleep_s:
            time.sleep(sleep_s)
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': text[:80],
              'source': 'Test', 'fetched': True, 'fetchedChars': len(text)}],
        )
        return tc_id, text, False

    # Patch at BOTH the pooled wrapper's resolution site and the executor, so
    # whichever seam the pipeline reaches lands on the fake.
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake_execute_one,
                        raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake_execute_one,
                        raising=False)
    return script


def _run_pipeline(task, parsed_tcs, messages=None):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    messages = messages if messages is not None else []
    execute_tool_pipeline(
        task, parsed_tcs,
        cfg={'autoApply': True},
        project_path=None,
        project_enabled=False,
        tool_list=[],
        messages=messages,
        all_search_results_text=[],
        round_num=0,
        model='test-model',
    )
    return messages


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — THE BARRIER (failing-first)
# ═══════════════════════════════════════════════════════════════════

def test_fast_tool_completes_before_slow_sibling_finishes(rec, fake_tools):
    """★ THE LOAD-BEARING FACE.

    A round with a fast ``read_files`` and a slow ``web_search``. The fast
    tool's ``tool_complete`` MUST be emitted while the slow one is still
    running — i.e. strictly before the slow tool's ``tool_result``.

    Under the ``pool.shutdown(wait=True)`` barrier this is structurally
    impossible: every ``tool_complete`` is emitted in the post-phase, after the
    slowest tool returned. That is the defect — a search that finished in 2s
    keeps its spinner for as long as its slowest sibling runs, and the user
    cannot tell which of the two is actually slow.

    Note this asserts EVENT ORDER, not wall-clock: ordering is what the
    frontend reducer folds, and it is immune to CI scheduling jitter.
    """
    fake_tools['read_files'] = (0.0, 'FAST BODY')
    fake_tools['web_search'] = (1.5, 'SLOW BODY')

    task = _mk_task()
    tcs = [_mk_tc('tc-fast', 'read_files', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run_pipeline(task, tcs)

    fast_complete = rec.first_index('tc-fast', 'tool_complete')
    slow_result = rec.first_index('tc-slow', 'tool_result')

    assert fast_complete >= 0, (
        'the fast tool must emit a tool_complete at all; got types=%r'
        % rec.types_for('tc-fast'))
    assert slow_result >= 0, (
        'the slow tool must emit a tool_result; got types=%r'
        % rec.types_for('tc-slow'))
    assert fast_complete < slow_result, (
        "the fast tool's tool_complete (idx=%d) must be emitted BEFORE the "
        'slow sibling settles (tool_result idx=%d). It is not: every '
        'tool_complete is deferred past `pool.shutdown(wait=True)` into the '
        'post-phase, so a 0.05s read_files keeps its spinner for the full '
        'duration of a 40s web_search in the same round. Emit the per-tool '
        'budget + tool_complete from inside the as_completed loop.'
        % (fast_complete, slow_result))


def test_every_tool_completes_before_the_last_result(rec, fake_tools):
    """Generalisation of face 1 to a 3-tool round.

    With three tools of increasing duration, each tool's own settle must be
    ordered by ITS OWN completion, not batched at the end. Concretely: the two
    faster tools must both be fully settled before the slowest one's result
    arrives.
    """
    fake_tools['read_files'] = (0.0, 'A')
    fake_tools['grep_search'] = (0.4, 'B')
    fake_tools['web_search'] = (1.6, 'C')

    task = _mk_task()
    tcs = [_mk_tc('tc-a', 'read_files', 1),
           _mk_tc('tc-b', 'grep_search', 2),
           _mk_tc('tc-c', 'web_search', 3)]
    _run_pipeline(task, tcs)

    slowest_result = rec.first_index('tc-c', 'tool_result')
    for tc_id in ('tc-a', 'tc-b'):
        idx = rec.first_index(tc_id, 'tool_complete')
        assert idx >= 0, 'no tool_complete for %s' % tc_id
        assert idx < slowest_result, (
            '%s settled at idx=%d but the slowest tool only produced its '
            'result at idx=%d — the whole round is still gated on one barrier'
            % (tc_id, idx, slowest_result))


# ═══════════════════════════════════════════════════════════════════
#  Face 2 — the early event is not a downgrade
# ═══════════════════════════════════════════════════════════════════

def test_early_complete_still_carries_content_and_tokens(rec, fake_tools):
    """Moving the emission earlier must not strip its payload.

    ``tool_complete`` is what gives the round its preview button and per-tool
    token count. If the early event carried only an id, the spinner would stop
    but the content chip would never arrive — trading one invisible state for
    another.
    """
    fake_tools['read_files'] = (0.0, 'FILE BODY CONTENT')
    fake_tools['web_search'] = (1.0, 'SLOW')

    task = _mk_task()
    tcs = [_mk_tc('tc-fast', 'read_files', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run_pipeline(task, tcs)

    ev = rec.find('tc-fast', 'tool_complete')
    assert ev is not None, 'no tool_complete for the fast tool'
    assert ev.get('toolContent') == 'FILE BODY CONTENT', (
        'the early tool_complete must carry the budgeted toolContent the model '
        'actually received; got %r' % (ev.get('toolContent'),))
    assert ev.get('toolName') == 'read_files'
    assert ev.get('roundNum') == 1
    # toolTokens is best-effort (tokenizer may be unavailable) but when present
    # it must be a positive int, never a placeholder.
    if 'toolTokens' in ev:
        assert isinstance(ev['toolTokens'], int) and ev['toolTokens'] > 0


def test_round_entry_is_stamped_at_early_settle(rec, fake_tools):
    """The persisted round must carry toolContent as soon as it settles.

    ``round_entry['toolContent']`` is what the checkpoint writes to the DB; if
    it were only stamped in the post-phase, a crash between a fast tool
    finishing and its slow sibling returning would lose that tool's context and
    Continue would roll the round back.
    """
    fake_tools['read_files'] = (0.0, 'PERSISTED BODY')
    fake_tools['web_search'] = (1.0, 'SLOW')

    task = _mk_task()
    fast = _mk_tc('tc-fast', 'read_files', 1)
    tcs = [fast, _mk_tc('tc-slow', 'web_search', 2)]
    _run_pipeline(task, tcs)

    assert fast[5].get('toolContent') == 'PERSISTED BODY', (
        'round_entry["toolContent"] must be stamped when the tool settles, not '
        'in the post-phase; got %r' % (fast[5].get('toolContent'),))


# ═══════════════════════════════════════════════════════════════════
#  Face 3 — per-tool L0 budget stays UPSTREAM of the event
# ═══════════════════════════════════════════════════════════════════

def test_l0_budget_is_applied_before_the_early_complete(rec, fake_tools,
                                                        monkeypatch):
    """The preview must show what the model sees, even when settled early.

    Per-tool L0 budgeting (``budget_tool_result``) is what spills an oversized
    result to disk. It is per-tool, so it belongs on the early path; if the
    early event were emitted BEFORE budgeting, the UI would show the full blob
    while the model got a truncated one.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    def _shrink(fn_name, content, tool_use_id='', conv_id=''):
        if fn_name == 'grep_search' and len(content) > 10:
            return content[:10] + '…[spilled]'
        return content

    monkeypatch.setattr(_pipeline, 'budget_tool_result', _shrink, raising=False)

    fake_tools['grep_search'] = (0.0, 'X' * 500)
    fake_tools['web_search'] = (1.0, 'SLOW')

    task = _mk_task()
    tcs = [_mk_tc('tc-fast', 'grep_search', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run_pipeline(task, tcs)

    ev = rec.find('tc-fast', 'tool_complete')
    assert ev is not None, 'no tool_complete for the budgeted tool'
    assert ev.get('toolContent') == 'X' * 10 + '…[spilled]', (
        'the early tool_complete must carry the POST-budget content; got %r'
        % (ev.get('toolContent'),))
    assert ev.get('compactionLayer') == 'L0', (
        'a shrunk result must be stamped compactionLayer=L0 on the EARLY '
        'event, else the COMPACTED pill never renders for a tool that settled '
        'before its siblings; got %r' % (ev.get('compactionLayer'),))


# ═══════════════════════════════════════════════════════════════════
#  Face 4 — the aggregate budget corrects, it does not delay
# ═══════════════════════════════════════════════════════════════════

def test_aggregate_budget_corrects_via_tool_compacted(rec, fake_tools,
                                                      monkeypatch):
    """The round-aggregate budget is inherently cross-tool, so it MUST stay
    after the barrier — but it must not be a reason to delay the first
    ``tool_complete``.

    When it rewrites a result that was already announced, the correction has to
    travel as a ``tool_compacted`` event (the type the frontend already applies
    to already-settled rounds). Delaying the first announcement instead would
    reintroduce exactly the barrier this epic removes.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    def _aggregate(agg_dict, conv_id=''):
        # Shrink the fast tool's entry, mimicking a real spill.
        out = {}
        for tc_id, (content, tool_name, _tid) in agg_dict.items():
            if tool_name == 'grep_search':
                out[tc_id] = ('SHRUNK', tool_name, _tid)
        return out

    monkeypatch.setattr(_pipeline, 'enforce_round_aggregate_budget', _aggregate,
                        raising=False)

    fake_tools['grep_search'] = (0.0, 'Y' * 400)
    fake_tools['web_search'] = (0.8, 'SLOW')

    task = _mk_task()
    tcs = [_mk_tc('tc-fast', 'grep_search', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run_pipeline(task, tcs)

    complete_idx = rec.first_index('tc-fast', 'tool_complete')
    slow_result = rec.first_index('tc-slow', 'tool_result')
    assert complete_idx >= 0 and complete_idx < slow_result, (
        'the aggregate budget must not push the first tool_complete back '
        'behind the barrier (complete idx=%d, slow result idx=%d)'
        % (complete_idx, slow_result))

    compacted = rec.find('tc-fast', 'tool_compacted')
    assert compacted is not None, (
        'when the aggregate pass rewrites an ALREADY-ANNOUNCED result it must '
        'emit a tool_compacted correction so the UI converges on what the '
        'model really received; events for tc-fast: %r'
        % rec.types_for('tc-fast'))


# ═══════════════════════════════════════════════════════════════════
#  Face 5 — exactly once
# ═══════════════════════════════════════════════════════════════════

def test_tool_complete_is_emitted_exactly_once_per_call(rec, fake_tools):
    """Moving the emission must not leave a duplicate behind.

    A stale post-phase emit alongside the new early one would double-count
    tokens in the round accounting and re-render the chip — the classic
    half-finished-migration shape.
    """
    fake_tools['read_files'] = (0.0, 'A')
    fake_tools['grep_search'] = (0.3, 'B')
    fake_tools['web_search'] = (0.9, 'C')

    task = _mk_task()
    tcs = [_mk_tc('tc-a', 'read_files', 1),
           _mk_tc('tc-b', 'grep_search', 2),
           _mk_tc('tc-c', 'web_search', 3)]
    _run_pipeline(task, tcs)

    for tc_id in ('tc-a', 'tc-b', 'tc-c'):
        n = rec.count(tc_id, 'tool_complete')
        assert n == 1, (
            '%s emitted %d tool_complete events (expected exactly 1) — an '
            'early emit that does not REPLACE the post-phase one double-counts '
            'tokens; types=%r' % (tc_id, n, rec.types_for(tc_id)))


def test_tool_messages_still_appended_in_original_order(rec, fake_tools):
    """REGRESSION GUARD on the reason the post-phase exists.

    The post-phase loop walks ``parsed_tcs`` so the ``role:'tool'`` messages
    enter the message list in the model's ORIGINAL tool-call order, regardless
    of completion order. Emitting events early must not reorder the messages —
    an out-of-order tool_call/tool_result pairing is an API-level 400 on
    Anthropic and silent confusion on OpenAI.
    """
    fake_tools['read_files'] = (0.0, 'FIRST')
    fake_tools['web_search'] = (0.8, 'SECOND')

    task = _mk_task()
    # Declaration order: slow FIRST, fast second — completion order is inverted.
    tcs = [_mk_tc('tc-slow', 'web_search', 1),
           _mk_tc('tc-fast', 'read_files', 2)]
    messages = _run_pipeline(task, tcs)

    tool_msgs = [m for m in messages if m.get('role') == 'tool']
    assert [m['tool_call_id'] for m in tool_msgs] == ['tc-slow', 'tc-fast'], (
        'tool messages must follow the ORIGINAL tool-call order even though '
        'the fast tool completed first; got %r'
        % ([m['tool_call_id'] for m in tool_msgs],))
