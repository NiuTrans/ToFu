"""tests/test_tool_lifecycle_timestamps.py — pt_67ffc2b700094ce9 face ④.

THE REQUIREMENT BEHIND THE REQUIREMENT
--------------------------------------
    "otherwise it's hard for me to troubleshoot where the lag is happening!"

Faces ①-③ remove three barriers. None of them makes the REMAINING latency
attributable. A tool row that takes 40 seconds can be slow in three completely
different places:

  * EXECUTION   — the upstream HTTP call / MCP server / subprocess was slow;
  * TRANSPORT   — the tool finished but the event sat in a queue, or SSE was
                  buffered by a proxy, or the round waited on a barrier;
  * RENDER      — the frame arrived and the browser did not paint its updated
                  authoritative Turn projection promptly.

The original defect made all three indistinguishable because tool lifecycle
events carried no clocks. The executable contracts below now keep that defect
from returning.

So "I fixed the latency" is unfalsifiable — which is the real reason this face
exists. It is the measurement instrument for the other three.

THE CONTRACT
------------
Backend, monotonic-derived but wall-clock-comparable epoch-ms:
  * ``tStart``    — when the tool actually began executing;
  * ``tEnd``      — when it returned (on terminal events);
  * ``emittedAt`` — when the frame was handed to the event chokepoint.
Frontend:
  * the canonical turn projection must preserve every diagnostic clock already
    present on a tool round, including an optional ingress ``receivedAt``.

From those four the three segments are derivable, per tool row:
  execution = tEnd - tStart   |   transport = receivedAt - emittedAt
  render    = painted - receivedAt

WHAT THIS SUITE PINS
--------------------
  1. The contract is DECLARED in the event registry (so a third-party frontend
     discovers it via /api/v1/capabilities, not by reading our JS).
  2. ``tool_start`` carries ``tStart``; the terminal events carry both
     ``tStart`` and ``tEnd``, so a row is self-describing without cross-event
     arithmetic the client would have to get right.
  3. Every emitted frame carries ``emittedAt``, stamped at ONE chokepoint —
     not per call site, which would drift.
  4. The clocks are sane: epoch MILLISECONDS (not seconds — the project has
     been bitten by a seconds/ms mixup before, see ``_isPlausibleEpochMs``),
     and tEnd >= tStart.
  5. A slow tool's measured execution really reflects its duration (the number
     is honest, not a constant).
  6. The production TypeScript projection preserves all four clocks through
     both its direct-message and full-state public projections.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_tool_lifecycle_timestamps.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CONVERSATION_VIEW_MODEL_TS = os.path.join(
    ROOT, 'frontend', 'src', 'conversation', 'presentation',
    'conversation-view-model.ts')

TOOL_EVENT_TYPES = ('tool_start', 'tool_progress', 'tool_result', 'tool_complete')


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the contract is declared, not folklore
# ═══════════════════════════════════════════════════════════════════

def test_tool_events_declare_their_clock_fields():
    """The registry is the machine-discoverable contract.

    ``lib/agent_core/events.py`` exists precisely so the wire vocabulary is not
    reverse-engineered from our JS. A timing field that is emitted but
    undeclared is invisible to every headless client and to
    ``/api/v1/capabilities``.
    """
    from lib.agent_core.events import get_event_spec

    for etype in TOOL_EVENT_TYPES:
        spec = get_event_spec(etype)
        assert spec is not None, '%s must be registered' % etype
        assert 'tStart' in spec.fields, (
            '%s must DECLARE tStart — without a backend clock on the tool '
            'lifecycle, execution time cannot be separated from transport or '
            'render time, and any latency fix is unfalsifiable. Declared '
            'fields: %r' % (etype, sorted(spec.fields)))
        assert 'emittedAt' in spec.fields, (
            '%s must DECLARE emittedAt (when the backend handed the frame to '
            'the stream) — the transport segment is receivedAt - emittedAt'
            % etype)

    for etype in ('tool_result', 'tool_complete'):
        spec = get_event_spec(etype)
        assert 'tEnd' in spec.fields, (
            '%s is terminal for its tool, so it must declare tEnd; execution '
            'time = tEnd - tStart' % etype)


def test_capabilities_exposes_the_timing_contract():
    """A third-party frontend must be able to discover the timing fields."""
    from lib.agent_core.events import to_capabilities_dict

    caps = to_capabilities_dict()
    tool_specs = {e['type']: e for e in caps['categories'].get('tool', [])}
    assert tool_specs, 'the tool category must be present in capabilities'
    for etype in TOOL_EVENT_TYPES:
        assert etype in tool_specs, '%s missing from capabilities' % etype
        assert 'tStart' in tool_specs[etype]['fields'], (
            '%s must expose tStart through /api/v1/capabilities' % etype)


# ═══════════════════════════════════════════════════════════════════
#  Faces 2-5 — the clocks are actually emitted, and honest
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'ts-task-1',
        'convId': 'cv-ts-1',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, rn: int):
    """Build the parsed_tcs 7-tuple through the REAL round constructor.

    Hand-rolling the round dict here would omit ``tStart`` and make every
    measured duration read 0ms — a fixture defect that looks exactly like a
    product defect. Using the production constructor keeps the shape honest.
    """
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, {}, tc_id, '{}', rn - 1, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, {}, round_entry['roundNum'], round_entry, None)


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))

    def of_type(self, etype):
        return [e for e in self.events if e.get('type') == etype]


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()
    import lib.tasks_pkg.tool_dispatch._heartbeat as td_facade
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    monkeypatch.setattr(_pipeline, 'append_event', r, raising=False)
    monkeypatch.setattr(td_facade, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


@pytest.fixture()
def fake_tools(monkeypatch):
    script: dict[str, tuple[float, str]] = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        sleep_s, text = script.get(fn_name, (0.0, 'ok'))
        if sleep_s:
            time.sleep(sleep_s)
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': text[:50],
              'source': 'Test', 'fetched': True, 'fetchedChars': len(text)}])
        return tc_id, text, False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)
    return script


def _run(task, tcs):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    execute_tool_pipeline(
        task, tcs, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=[],
        all_search_results_text=[], round_num=0, model='test-model')


def test_terminal_events_carry_both_clocks(rec, fake_tools):
    """Face 2 — a tool row must be self-describing.

    Carrying only ``tEnd`` would force the client to pair it with a
    ``tool_start`` it may never have seen (cold reconnect / replay from a
    cursor), so the duration would silently render blank exactly on the paths
    where a user is investigating a slow turn.
    """
    fake_tools['web_search'] = (0.35, 'BODY')

    task = _mk_task()
    _run(task, [_mk_tc('tc-1', 'web_search', 1)])

    for etype in ('tool_result', 'tool_complete'):
        evs = rec.of_type(etype)
        assert evs, 'no %s emitted' % etype
        ev = evs[0]
        for fld in ('tStart', 'tEnd', 'emittedAt'):
            assert fld in ev, (
                '%s must carry %s; got keys=%r' % (etype, fld, sorted(ev)))


def test_clocks_are_epoch_milliseconds_and_ordered(rec, fake_tools):
    """Face 4 — a seconds/ms mixup silently produces 1970 timestamps.

    This project already carries a defensive ``_isPlausibleEpochMs`` in the
    paper media tabs because that exact confusion shipped once. Pin the unit at
    the source instead of defending against it downstream.
    """
    fake_tools['web_search'] = (0.3, 'BODY')

    now_ms = time.time() * 1000.0
    task = _mk_task()
    _run(task, [_mk_tc('tc-1', 'web_search', 1)])

    ev = rec.of_type('tool_result')[0]
    for fld in ('tStart', 'tEnd', 'emittedAt'):
        v = ev[fld]
        assert isinstance(v, (int, float)), '%s must be numeric; got %r' % (fld, v)
        assert v > 1e12, (
            '%s=%r looks like epoch SECONDS — the wire contract is epoch '
            'MILLISECONDS' % (fld, v))
        assert v <= now_ms + 60_000, (
            '%s=%r is implausibly far in the future' % (fld, v))
    assert ev['tEnd'] >= ev['tStart'], 'tEnd must not precede tStart'
    assert ev['emittedAt'] >= ev['tEnd'] - 1, (
        'emittedAt must be at or after the tool finished')


def test_measured_execution_reflects_real_duration(rec, fake_tools):
    """Face 5 — the number has to be honest.

    A constant, or a value computed at emission time for both ends, would make
    every tool look instant and the instrument worthless. A 600ms tool must
    measure ~600ms.
    """
    fake_tools['web_search'] = (0.6, 'BODY')

    task = _mk_task()
    _run(task, [_mk_tc('tc-1', 'web_search', 1)])

    ev = rec.of_type('tool_result')[0]
    measured_ms = ev['tEnd'] - ev['tStart']
    assert 450 <= measured_ms <= 3000, (
        'a ~600ms tool measured %.0fms — the clocks are not bracketing real '
        'execution' % measured_ms)


def test_tool_start_carries_its_own_start_clock(rec, fake_tools):
    """Face 2b — ``tool_start`` anchors the row before any result exists.

    While a tool is still running, ``tStart`` is the ONLY thing that lets the
    UI show a truthful "running for 38s" instead of a client-side stopwatch
    that re-mints on every render (the bug pattern ``_pmAdoptServerClocks``
    exists to prevent in the media tabs).
    """
    from lib.tasks_pkg.tool_display import _build_tool_round_entry

    _n, round_entry, event = _build_tool_round_entry(
        'web_search', {'query': 'q'}, 'tc-1', '{}', 0, False)

    assert round_entry.get('tStart') is not None, (
        'the round scaffold built at dispatch time must carry a start clock so '
        'a still-running tool can render a truthful elapsed time; got keys=%r'
        % sorted(round_entry))
    assert event.get('tStart') == round_entry['tStart'], (
        'the tool_start EVENT must carry the same clock as the round, else the '
        'client and the server disagree about when the tool began')
    assert event.get('emittedAt') is not None, (
        'tool_start must also carry emittedAt — a tool that is slow to even '
        'ANNOUNCE is a transport problem, and without this the user cannot '
        'tell that apart from a slow tool')


def test_emitted_at_is_stamped_at_one_chokepoint():
    """Face 3 — one stamper, not N call sites.

    ``emittedAt`` means "when the backend handed this frame to the stream". If
    each call site stamped its own, the field would drift (some stamping before
    a slow serialization, some after) and the transport segment would be noise.
    ``build_event`` is the single typed constructor every emission goes
    through.
    """
    from lib.agent_core.events import EventType, build_event

    ev = build_event(EventType.TOOL_RESULT, roundNum=1, toolCallId='tc-1')
    assert 'emittedAt' in ev, (
        'build_event must stamp emittedAt for tool events at the ONE typed '
        'construction chokepoint; got %r' % sorted(ev))
    assert ev['emittedAt'] > 1e12, 'emittedAt must be epoch ms'

    # A non-tool event must NOT be forced to carry it — the delta stream is the
    # hottest path in the product and a per-frame clock there is pure overhead.
    d = build_event(EventType.DELTA, content='hi')
    assert 'emittedAt' not in d, (
        'delta frames must stay lean — stamping every token frame adds bytes '
        'to the hottest path for no diagnostic value; got %r' % sorted(d))


# ═══════════════════════════════════════════════════════════════════
#  Face 6 — the clocks survive the production frontend projection
# ═══════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
global.window = global;
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const T0 = Date.now() - 5000;
const round = {
  roundNum: 1, toolCallId: 'tc-1', status: 'done', results: [],
  tStart: T0, tEnd: T0 + 2500, emittedAt: T0 + 2505,
  receivedAt: T0 + 2507,
};
const turn = {
  turnId: 'turn-1', actor: 'assistant', kind: 'reply', laneId: 'main',
  conversationId: 'conv-a', ordinal: 1,
  parentTurnId: null, status: 'completed', currentAttemptId: null,
  projectionRevision: 1, projection: { content: '', toolRounds: [round],
    segments: [{type:'tool_use', blockId:'tool:tc-1', id:'tc-1',
      name:'search', input:{}, result:{status:'done'}}] },
  settlement: {}, createdAt: T0,
};

const direct = selectTurnBlocks(turn).find(block => block.kind === 'tool').round;
const projected = selectConversationViewModel({
    turnsById: { 'turn-1': turn }, laneOrder: { main: ['turn-1'] },
    attemptsById:{}, queueItems:[], pendingEventsByTurn:{},
    commandPending: {}, liveRoundUsageByTurn: {}, transport: 'connected',
    conversationId:'conv-a', conversationRevision:1,
  }).mainLane.turns[0].blocks.find(block => block.kind === 'tool').round;

console.log(JSON.stringify({ direct, projected, original: round }));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_frontend_projection_preserves_tool_clocks():
    """Both direct and full-state projections preserve diagnostic clocks."""
    built = native_module_path(
        'tool-lifecycle-conversation-view-model.js',
        CONVERSATION_VIEW_MODEL_TS)
    proc = subprocess.run(
        ['node', '-e', _HARNESS, built], capture_output=True, text=True,
        timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    expected = got['original']
    assert got['direct'] == expected
    assert got['projected'] == expected
    assert expected['tEnd'] - expected['tStart'] == 2500
    assert expected['receivedAt'] - expected['emittedAt'] == 2
