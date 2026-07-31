"""Regression (pt_a1b803793eb84925): the POLL lane must be able to seed the
stream phase, and a late event after the turn ended must still be dropped.

WHY THIS EXISTS
---------------
`setStreamPhase` (static/js/ui/stream_session.js) opened with

    if (!streamSessions.has(convId) && !activeStreams.has(convId)) return;

`activeStreams` answers "does THIS TAB hold an open SSE?". The question the
guard actually needs to answer is "is this TURN still in progress?". Those come
apart in exactly the situation the whole VU-carrier family is about: the SSE is
down (cold attach to an autopilot carrier, a socket-down window, the poll-only
lane) while the backend keeps working. `sse_poll_fallback.js` is the poll lane's
ONLY phase writer, so every poll delivered a phase and the client dropped it —
the stage text was STRUCTURALLY impossible, which is the third of the three
symptoms in the original report.

Measured before the fix, driving the REAL shipped setStreamPhase:
    activeStreams EMPTY  → write silently dropped (no session at all)
    activeStreams HAS it → {"phase":"tool_exec","detail":"read_files"}

WHY NOT THE TICKET'S ORIGINAL PRESCRIPTION
------------------------------------------
The ticket said to reuse `_convTurnInFlight`, "which delegates to
computeConvBusy". That is now wrong twice over and MUST NOT be copied:
  * `_convTurnInFlight` no longer exists — 94347aa7 split it into
    `_convMainTurnInFlight` (turn level) and `_convBusyAnyLane` (conv level);
  * `computeConvBusy` ALSO scans branch-stream keys (`conv.id + ':'`). Routing
    phase through it would mean a live BRANCH makes the MAIN turn show stage
    text — the very defect 94347aa7 removed from the render gates.
So the correct source is the TURN-level, branch-blind predicate.

The presence semantics in the module docstring stay intact: a phase arriving
after the turn really ended must NOT resurrect a session, or `streamSessions`
would never be reclaimed. That is the complement asserted below.

WHAT IS ASSERTED
----------------
Everything is driven through the REAL shipped functions — `setStreamPhase` /
`getStreamSession` / `clearStreamSession` are sliced out of stream_session.js
and the liveness predicates out of chat_render.js by brace matching, never
retyped (charter #24: a guard must not carry a second implementation of the
logic it guards).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SESSION_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_session.js')
CHAT_RENDER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'chat_render.js')
REDUCER_JS = os.path.join(ROOT, 'static', 'js', 'core', 'conv_state_reducer.js')
POLL_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_poll_fallback.js')
BUNDLER_PY = os.path.join(ROOT, 'lib', 'js_bundler.py')


def _node_available() -> bool:
    return bool(shutil.which('node'))


requires_node = pytest.mark.skipif(
    not _node_available(), reason='node not installed')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _scan_source(path: str) -> str:
    """Read a JS source file with comments STRIPPED (charter #24).

    NOTE the explicit ``lang='js'``: the shared helper defaults to
    ``lang='shell'``, which does NOT strip ``/* … */`` blocks. Omitting it
    silently leaves every JSDoc block in the scanned text, so an assertion like
    "the gate must not mention computeConvBusy" would be satisfiable — or
    violable — by a COMMENT. That is exactly the failure mode charter #24 was
    written for (and which pt_852527ce031e4f93 tracks elsewhere).
    """
    src = _read(path)
    try:
        from _source_scan import strip_comments  # type: ignore
        return strip_comments(src, lang='js')
    except Exception:
        scan = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', scan, flags=re.M)


# ── The harness ───────────────────────────────────────────────────────────
# Loads the REAL stream_session.js (whole file — it is small and side-effect
# free) plus the REAL reducer, and slices the shipped liveness predicates out
# of chat_render.js. Nothing is retyped.
_HARNESS = r"""
const fs = require('fs');
const sessionSrc  = fs.readFileSync(process.argv[2], 'utf8');
const renderSrc   = fs.readFileSync(process.argv[3], 'utf8');
const reducerSrc  = fs.readFileSync(process.argv[4], 'utf8');
const scenario    = JSON.parse(process.argv[5]);

function sliceFn(src, name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('function not found: ' + name);
  let depth = 0;
  const open = src.indexOf('{', i);
  for (let k = open; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  throw new Error('unbalanced braces for ' + name);
}

// ── The world, before the module loads (setStreamPhase closes over these). ──
global.activeStreams = new Map();
for (const k of (scenario.activeStreamKeys || [])) global.activeStreams.set(k, {});

const conv = {
  id: 'convA',
  activeTaskId: scenario.activeTaskId,
  messages: [{ role: 'user' }, { role: 'assistant' }],
};
if (scenario.authoritativeTaskIds) {
  conv._authoritativeActiveTaskIds = new Set(scenario.authoritativeTaskIds);
}
global.window = {};
global.conversations = [conv];
global.activeConvId = 'convA';
global.getConvById = (id) => (id === conv.id ? conv : null);
global.getActiveConv = () => conv;

(0, eval)(reducerSrc);
if (typeof computeConvBusy === 'undefined' && window.computeConvBusy) {
  global.computeConvBusy = window.computeConvBusy;
}
// The SHIPPED liveness predicates (turn level + conv level), sliced.
(0, eval)(sliceFn(renderSrc, '_convBusyAnyLane'));
(0, eval)(sliceFn(renderSrc, '_convMainTurnInFlight'));
(0, eval)(sliceFn(renderSrc, '_isTurnInFlight'));

// The REAL module under test.
(0, eval)(sessionSrc);

// Mirror sse_poll_fallback.js:389 — the poll lane's ONLY phase writer.
setStreamPhase('convA', scenario.phase);

const sess = streamSessions.get('convA');
const out = {
  sessionExists: streamSessions.has('convA'),
  phase: sess ? sess.phase : null,
  // What the paint readers would see (health_stream_timer.js:824 / :997).
  paintReaderPhase: (streamSessions.get('convA') || {}).phase || null,
};

// Reclamation must still work: twStop's teardown drops the slice.
clearStreamSession('convA');
out.clearedAfterTeardown = !streamSessions.has('convA');

console.log(JSON.stringify(out));
"""


def _run(scenario: dict) -> dict:
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(_HARNESS)
        path = fh.name
    try:
        proc = subprocess.run(
            ['node', path, SESSION_JS, CHAT_RENDER_JS, REDUCER_JS,
             json.dumps(scenario)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


_CARRIER = 'task-carrier-1'
_PHASE = {'phase': 'tool_exec', 'detail': 'read_files'}


def _poll_lane_live_carrier() -> dict:
    """THE cell: no SSE in this tab, but the turn is demonstrably in flight —
    the conv is pinned to a live VU carrier that the server also reports in the
    authoritative set. The poll lane delivers a phase."""
    return {
        'activeStreamKeys': [],          # SSE down / poll-only lane
        'activeTaskId': _CARRIER,        # cold-attach pin
        'authoritativeTaskIds': [_CARRIER],
        'phase': _PHASE,
    }


def _turn_really_over() -> dict:
    """COMPLEMENT: the turn ended — pin cleared, carrier gone from the
    authoritative set, no stream. A late phase event must be DROPPED."""
    return {
        'activeStreamKeys': [],
        'activeTaskId': None,
        'authoritativeTaskIds': [],
        'phase': _PHASE,
    }


def _live_sse() -> dict:
    """Unchanged legacy path: this tab holds the SSE."""
    return {
        'activeStreamKeys': ['convA'],
        'activeTaskId': _CARRIER,
        'authoritativeTaskIds': [_CARRIER],
        'phase': _PHASE,
    }


def _branch_only() -> dict:
    """A BRANCH stream is live but the main turn is NOT. Phase is a MAIN-turn
    fact, so a branch must not seed it (the semantic 94347aa7 established)."""
    return {
        'activeStreamKeys': ['convA:3:0'],   # branch key, not the main stream
        'activeTaskId': None,
        'authoritativeTaskIds': [],
        'phase': _PHASE,
    }


# ── PREMISE ───────────────────────────────────────────────────────────────

@requires_node
def test_premise_live_sse_still_seeds_phase():
    """PREMISE + no-regression: the legacy path (this tab owns the SSE) must
    keep working exactly as before."""
    r = _run(_live_sse())
    assert r['phase'] == _PHASE, (
        f'the live-SSE path stopped seeding phase — regression. got {r!r}')


# ── FAILING-FIRST: the defect ─────────────────────────────────────────────

@requires_node
def test_poll_lane_can_seed_phase_for_live_carrier():
    """RED before the fix: the poll lane's phase write was silently dropped, so
    the stage text was structurally impossible while the backend was working."""
    r = _run(_poll_lane_live_carrier())
    assert r['sessionExists'] is True, (
        'the poll lane could not seed a session: activeStreams is empty (SSE '
        'down) while the conv is pinned to a LIVE carrier that the server also '
        f'reports running. got {r!r}')
    assert r['phase'] == _PHASE, (
        f'phase was dropped on the poll lane. got {r!r}')


@requires_node
def test_paint_reader_can_read_the_seeded_phase():
    """The seeded phase must be visible to the paint readers
    (health_stream_timer.js:824 / :997 read `streamSessions.get(id).phase`)."""
    r = _run(_poll_lane_live_carrier())
    assert r['paintReaderPhase'] == _PHASE, (
        f'the paint readers cannot see the seeded phase. got {r!r}')


def test_source_gate_reads_turn_liveness_not_local_sse():
    """The early-return must ask "is this TURN in flight", not "does this tab
    hold an SSE" — and must NOT route through the conv-level union.

    The check follows the gate THROUGH its delegation: setStreamPhase may call
    a local seam helper, so the assertion covers setStreamPhase plus any
    same-file helper it calls. Pinning it to setStreamPhase's literal body
    would make the guard fail the moment the call is factored out — a guard
    that breaks on refactor rather than on regression.

    Comments are stripped first (charter #24), so a comment cannot satisfy or
    violate this.
    """
    scan = _scan_source(SESSION_JS)
    m = re.search(r'function setStreamPhase\(convId, phase\)\s*\{(.*?)\n\}',
                  scan, flags=re.S)
    assert m, 'setStreamPhase not found in stream_session.js'
    body = m.group(1)

    # Follow one level of delegation into same-file helpers.
    reachable = body
    for callee in set(re.findall(r'\b(_[A-Za-z0-9_]+)\s*\(', body)):
        hm = re.search(r'function ' + re.escape(callee) + r'\([^)]*\)\s*\{(.*?)\n\}',
                       scan, flags=re.S)
        if hm:
            reachable += '\n' + hm.group(1)

    assert ('_isTurnInFlight' in reachable
            or '_convMainTurnInFlight' in reachable), (
        "setStreamPhase's early return still uses only this tab's activeStreams "
        'as a proxy for "is the turn in flight". The poll lane can then never '
        f'seed a phase. reachable gate: {reachable.strip()!r}')
    # Must NOT borrow the conv-level union (it scans branch-stream keys).
    assert '_convBusyAnyLane' not in reachable and 'computeConvBusy' not in reachable, (
        'the phase gate routes through the CONV-level busy union, which scans '
        'branch-stream keys — a live branch would then make the MAIN turn show '
        f'stage text. Use the turn-level predicate. reachable gate: {reachable.strip()!r}')


def test_predicate_is_defined_before_stream_session_in_the_bundle():
    """Load-order fact the fix depends on: the predicate's defining file must be
    concatenated BEFORE stream_session.js, so the reference resolves."""
    src = _read(BUNDLER_PY)
    i = src.index('_BUNDLE_FILES')
    seg = src[i:i + 40000]
    seen: list[str] = []
    for f in re.findall(r"['\"]([A-Za-z0-9_./-]+\.js)['\"]", seg):
        if f not in seen:
            seen.append(f)
    for name in ('ui/chat_render.js', 'ui/stream_session.js',
                 'core/conv_state_reducer.js'):
        assert name in seen, f'{name} missing from _BUNDLE_FILES'
    assert seen.index('core/conv_state_reducer.js') < seen.index('ui/chat_render.js') \
        < seen.index('ui/stream_session.js'), (
        'bundle order broke: the liveness predicate must be defined before '
        f'stream_session.js. got order: {[(n, seen.index(n)) for n in seen if n in ("core/conv_state_reducer.js", "ui/chat_render.js", "ui/stream_session.js")]}')


def test_comment_stripping_is_bidirectional(tmp_path):
    """charter #24: the source scan must be satisfiable ONLY by code.

    The shipped file legitimately MENTIONS `computeConvBusy` in a comment (to
    explain why it is deliberately NOT used). If the stripper were not applied
    with lang='js', that prose would VIOLATE the "must not route through the
    conv-level union" assertion and the guard would be red for a correct file.
    The mirror direction matters too: a comment must not SATISFY the positive
    assertion either. Both directions are checked here against the real helper.
    """
    import sys
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from _source_scan import strip_comments  # type: ignore

    # Direction A — a comment must not VIOLATE the negative assertion.
    js_a = (
        '/* explanatory: NOT the conv-level computeConvBusy / _convBusyAnyLane */\n'
        'function setStreamPhase(convId, phase) {\n'
        '  if (!streamSessions.has(convId) && !_convMainTurnInFlight(c)) return;\n'
        '}\n'
    )
    scan_a = strip_comments(js_a, lang='js')
    assert 'computeConvBusy' not in scan_a, (
        'block comments are not being stripped — a comment can VIOLATE the '
        'negative assertion and turn a correct file red.')
    assert '_convMainTurnInFlight' in scan_a, 'real code was stripped away'

    # Direction B — a comment must not SATISFY the positive assertion.
    js_b = (
        '/* we should use _convMainTurnInFlight here one day */\n'
        'function setStreamPhase(convId, phase) {\n'
        '  if (!activeStreams.has(convId)) return;\n'
        '}\n'
    )
    scan_b = strip_comments(js_b, lang='js')
    assert '_convMainTurnInFlight' not in scan_b, (
        'a COMMENT satisfied the positive assertion — the guard would pass on '
        'a file that never actually calls the predicate.')


# ── COMPLEMENT: reclamation must survive (reverse-defect guard) ───────────

@requires_node
def test_late_phase_after_turn_ended_is_still_dropped():
    """The docstring's presence semantics MUST hold: once the turn is really
    over (no stream, no pin, not in the authoritative set), a late phase event
    must NOT resurrect a session — otherwise streamSessions is never reclaimed
    and the paint readers read "a stream exists" forever."""
    r = _run(_turn_really_over())
    assert r['sessionExists'] is False, (
        'a late phase event resurrected a session after the turn ended — '
        f'streamSessions would never be reclaimed. got {r!r}')
    assert r['phase'] is None, f'phase was written for a dead turn. got {r!r}'


@requires_node
def test_branch_stream_alone_does_not_seed_main_turn_phase():
    """A live BRANCH is not the main turn. Phase is a main-turn fact, so a
    branch stream alone must not seed it (pins the 94347aa7 semantic here too)."""
    r = _run(_branch_only())
    assert r['sessionExists'] is False, (
        'a branch stream seeded the MAIN turn phase — the conv-level union '
        f'leaked in. got {r!r}')


@requires_node
def test_teardown_still_reclaims_the_session():
    """clearStreamSession (called by twStop) must still drop the slice — the fix
    must not touch the reclamation contract."""
    r = _run(_poll_lane_live_carrier())
    assert r['clearedAfterTeardown'] is True, (
        f'teardown no longer reclaims the session slice. got {r!r}')


def test_poll_lane_writer_still_present():
    """PREMISE: sse_poll_fallback.js is still the poll lane's phase writer. If
    this call disappears the behavioural cells above would pass vacuously."""
    scan = _scan_source(POLL_JS)
    assert 'setStreamPhase(convId' in scan, (
        'sse_poll_fallback.js no longer calls setStreamPhase — the poll lane '
        'has no phase writer, so this guard would be testing nothing.')
