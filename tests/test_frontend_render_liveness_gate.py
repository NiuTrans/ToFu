"""Regression (pt_ae8777b4eee04ef1): the bottom action bar, the Continue
affordance and Delete must NOT appear on a turn the backend is STILL GENERATING.

WHY THIS EXISTS
---------------
`renderMessage` (static/js/ui/chat_render.js) decides "has this turn settled?"
FOUR separate times, and they disagree:

  :1718  _isLiveTail    = activeStreams.has(id) || conv.activeTaskId   (認 pin)
  :1780  isLastAssistant= !activeStreams.has(id)                       (NOT 認 pin)
  :1810  canDelete      = !activeStreams.has(id) && !conv.activeTaskId (認 pin)
  :1951  _turnFailed    = error || finishReason === 'interrupted'
         + styles.css `.message.turn-failed .message-actions{opacity:1}`

So when the SSE is down but the conversation is still pinned to a running task
(cold-attach to an autopilot VU carrier, a socket-down window, the poll-only
lane), `_isLiveTail` says "in flight" (and correctly suppresses the finish bar)
while `isLastAssistant` says "settled" — IN THE SAME RENDER PASS. The Continue
button and the whole action bar then render under a message that is still being
generated.

The second half is the settlement verdict: the backend deliberately WITHHOLDS
`finishReason` mid-stream, and `_tsClassifyOutcome` maps a MISSING reason to
`interrupted` (fail-open, correct for a legacy/historical turn). Combined with a
position-only `isLastAssistant`, "no finishReason yet" reads as "interrupted and
resumable" — the exact inversion this guard pins.

This is a DIFFERENT LAYER from pt_d97f9098776c48e9 (the stale-pin sweep whose
liveness source cannot see a VU carrier and therefore stamps `interrupted` on
live work). The two ADD UP:
  * cause A (that ticket): the sweep stamps `interrupted` → via `_turnFailed`
    the action bar is promoted from hover-reveal to ALWAYS VISIBLE;
  * cause B (this ticket): the render predicate lacks the liveness dimension →
    the buttons appear with NO sweep involvement at all.
Fixing A does not remove B.

WHAT IS ASSERTED
----------------
The tests drive the SHIPPED `computeTurnSettlement` /
`continueButtonForSettlement` out of core/turn_settlement.js (never a hand-typed
copy — the project has a standing rule that a guard must not re-implement the
predicate it guards) plus the gate expressions extracted from the shipped
chat_render.js source.

FAILING-FIRST: `test_live_tail_hides_continue`, `test_live_tail_hides_delete`,
`test_two_predicates_never_disagree` and `test_source_gate_reads_liveness` are
RED against today's source and go green only once the gate grows the liveness
dimension (a shared `isTurnInFlight` predicate). The COMPLEMENT tests
(`test_settled_tail_shows_continue`, `test_settled_tail_shows_delete`) are GREEN
today and must STAY green — without them a "fix" that simply never shows the
buttons would pass, which is the reverse defect.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CHAT_RENDER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'chat_render.js')
SETTLEMENT_JS = os.path.join(ROOT, 'static', 'js', 'core', 'turn_settlement.js')
REDUCER_JS = os.path.join(ROOT, 'static', 'js', 'core', 'conv_state_reducer.js')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')


def _node_available() -> bool:
    return bool(shutil.which('node'))


requires_node = pytest.mark.skipif(
    not _node_available(), reason='node not installed')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _scan_source(path: str) -> str:
    """Read a source file with comments STRIPPED.

    Project rule (charter #24): a guard that scans source text must strip
    comments first and must not carry a second hand-written implementation of
    the stripper — prefer the shared tests/_source_scan.py helper, falling back
    to a local regex only where it is unavailable.
    """
    src = _read(path)
    try:
        from _source_scan import strip_comments  # type: ignore
        return strip_comments(src)
    except Exception:
        scan = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', scan, flags=re.M)


# ── The harness ───────────────────────────────────────────────────────────
# Loads the REAL turn_settlement.js + the REAL conv_state_reducer.js, then
# extracts the SHIPPED liveness predicates out of chat_render.js and evaluates
# the gate exactly as renderMessage does.
#
# Nothing here is a hand-typed copy of a predicate: `_convTurnInFlight` /
# `_isTurnInFlight` are sliced out of the shipped source, and the gate
# expressions are rebuilt from those same functions. A guard that retypes the
# logic it guards can only ever prove the copy is self-consistent — and this
# whole defect family came from predicates drifting apart.
#
# `isLastAssistant` is deliberately NOT hardcoded — that is the flaw in
# tests/test_frontend_continue_button_gating.py, which pinned it to a constant
# `true` and was therefore structurally blind to the liveness half of the gate.
_HARNESS = r"""
const fs = require('fs');
const settlementSrc = fs.readFileSync(process.argv[2], 'utf8');
const reducerSrc = fs.readFileSync(process.argv[3], 'utf8');
const renderSrc = fs.readFileSync(process.argv[4], 'utf8');
const scenario = JSON.parse(process.argv[5]);

// ── Reconstruct the render-time world FIRST (the shipped predicates close
//    over `activeStreams` / `computeConvBusy` at call time). ──
const conv = {
  id: 'convA',
  activeTaskId: scenario.activeTaskId,          // null | '<taskId>'
  messages: scenario.messages,
};
const idx = conv.messages.length - 1;
const msg = conv.messages[idx];
const isUser = (msg.role === 'user' || msg.role === 'optimizer');

// activeStreams: EMPTY in the live-carrier scenario (SSE down / poll-only lane).
global.activeStreams = new Map();
for (const k of (scenario.activeStreamKeys || [])) global.activeStreams.set(k, {});
if (scenario.authoritativeTaskIds) {
  conv._authoritativeActiveTaskIds = new Set(scenario.authoritativeTaskIds);
}
global.window = {};
global.getActiveConv = () => conv;

// The REAL settlement verdict + the REAL busy union.
(0, eval)(settlementSrc);
const computeTurnSettlement = window.computeTurnSettlement;
const continueButtonForSettlement = window.continueButtonForSettlement;
(0, eval)(reducerSrc);
if (typeof computeConvBusy === 'undefined' && window.computeConvBusy) {
  global.computeConvBusy = window.computeConvBusy;
}

/* Slice the SHIPPED liveness predicates out of chat_render.js by brace
 * matching — never a retyped copy. */
function sliceFn(src, name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('predicate not found in chat_render.js: ' + name);
  let depth = 0;
  const open = src.indexOf('{', i);
  for (let k = open; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  throw new Error('unbalanced braces for ' + name);
}
(0, eval)(sliceFn(renderSrc, '_convBusyAnyLane'));
(0, eval)(sliceFn(renderSrc, '_convMainTurnInFlight'));
(0, eval)(sliceFn(renderSrc, '_isTurnInFlight'));

/* ── The gate under test ────────────────────────────────────────────────
 * `GATE_SRC` is spliced in by the python side: either the shipped gate (built
 * from the predicates above) or a NEUTER variant. It must define
 * `isLastAssistant`, `canDelete` and `_isLiveTail`. */
// >>>GATE<<<

const _tsVerdict = computeTurnSettlement(msg, msg.model || null);
const _tsBtn = continueButtonForSettlement(_tsVerdict);

const showsContinue = !!(isLastAssistant && _tsBtn.show);
const showsDelete = !!canDelete;
/* `_turnFailed` promotes the bar from an opacity:0 hover-reveal to always
 * visible (styles.css `.message.turn-failed .message-actions{opacity:1}`).
 * Shipped form excludes an in-flight turn — see chat_render.js. */
const _turnFailed = !isUser
  && (!!msg.error || msg.finishReason === 'interrupted')
  && !_isTurnInFlight(conv, idx);

console.log(JSON.stringify({
  outcome: _tsVerdict ? _tsVerdict.outcome : null,
  resumeMode: _tsVerdict && _tsVerdict.resume ? _tsVerdict.resume.mode : null,
  btnShow: !!_tsBtn.show,
  btnKind: _tsBtn.kind || null,
  isLastAssistant: !!isLastAssistant,
  isLiveTail: !!_isLiveTail,
  showsContinue: showsContinue,
  showsDelete: showsDelete,
  turnFailed: _turnFailed,
  barAlwaysVisible: _turnFailed,
}));
"""

# The gate as SHIPPED — expressed through the sliced predicates, so this stays
# byte-faithful to chat_render.js without retyping the liveness logic.
_GATE_SHIPPED = """
const _isLiveTail = _isTurnInFlight(conv, idx);
const isLastAssistant =
  !isUser && conv && idx === conv.messages.length - 1
  && !_isTurnInFlight(conv, idx);
const canDelete = conv && !_convBusyAnyLane(conv);
"""

# The PRE-FIX gate: position + this tab's activeStreams only. Used as the NEUTER
# to prove these tests actually measure the liveness dimension.
_GATE_POSITION_ONLY = """
const _isLiveTail = !!(conv
  && idx === conv.messages.length - 1
  && (activeStreams.has(conv.id) || conv.activeTaskId));
const isLastAssistant =
  !isUser && conv && idx === conv.messages.length - 1
  && !activeStreams.has(conv.id);
const canDelete = conv && !activeStreams.has(conv.id) && !conv.activeTaskId;
"""


def _run(scenario: dict, gate: str = _GATE_SHIPPED) -> dict:
    harness = _HARNESS.replace('// >>>GATE<<<', gate)
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        proc = subprocess.run(
            ['node', path, SETTLEMENT_JS, REDUCER_JS, CHAT_RENDER_JS,
             json.dumps(scenario)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


# ── Scenarios ─────────────────────────────────────────────────────────────
# A turn that is STILL BEING GENERATED: real payload, but the backend has not
# sent finishReason yet. activeStreams is EMPTY (SSE down) while the conv is
# still pinned to the running task — the cold-attach / poll-only-lane shape.
_CARRIER = 'task-carrier-1'


def _live_tail_scenario() -> dict:
    return {
        'activeTaskId': _CARRIER,
        'activeStreamKeys': [],
        'messages': [
            {'role': 'user', 'content': 'redesign the game loop'},
            {
                'role': 'assistant',
                'model': 'claude-opus-5',
                'content': 'I have read the existing design…',
                'thinking': 'x' * 10000,
                'toolRounds': [{'toolCallId': 'tc1', 'status': 'done',
                                'toolContent': 'bootstrap.ts', 'roundNum': 0}],
                # finishReason DELIBERATELY absent — withheld mid-stream.
            },
        ],
    }


def _settled_tail_scenario() -> dict:
    """The COMPLEMENT: the turn really ended. Buttons MUST appear."""
    return {
        'activeTaskId': None,
        'activeStreamKeys': [],
        'messages': [
            {'role': 'user', 'content': 'redesign the game loop'},
            {
                'role': 'assistant',
                'model': 'claude-opus-5',
                'content': 'I have read the existing design…',
                'finishReason': 'interrupted',
                'toolRounds': [{'toolCallId': 'tc1', 'status': 'done',
                                'toolContent': 'bootstrap.ts', 'roundNum': 0}],
            },
        ],
    }


def _settled_main_with_live_branch_scenario() -> dict:
    """The MAIN turn finished cleanly, but a BRANCH stream is running.

    branch_stream.js:430 writes `convId:msgIdx:branchIdx` straight into
    `activeStreams`, and computeConvBusy scans for that `conv.id + ':'` prefix.
    Borrowing that conv-level union for the TURN-level gates made a finished
    main turn read as in-flight — it lost its finish bar and its Delete button
    while an unrelated branch streamed. A branch does not write the main tail.
    """
    return {
        'activeTaskId': None,
        'activeStreamKeys': ['convA:3:0'],   # a live BRANCH, not the main stream
        'messages': [
            {'role': 'user', 'content': 'redesign the game loop'},
            {
                'role': 'assistant',
                'model': 'claude-opus-5',
                'content': 'the complete answer',
                'finishReason': 'stop',
                'usage': {'input_tokens': 10, 'output_tokens': 20},
                'toolRounds': [{'toolCallId': 'tc1', 'status': 'done',
                                'toolContent': 'bootstrap.ts', 'roundNum': 0}],
            },
        ],
    }


# ── Premise anchoring ─────────────────────────────────────────────────────
# If these ever stop holding, every assertion below is vacuous.

@requires_node
def test_premise_missing_finish_reason_reads_resumable():
    """PREMISE: the shipped verdict maps a MISSING finishReason to a resumable
    outcome. This is why a position-only gate leaks the button mid-stream."""
    r = _run(_live_tail_scenario())
    assert r['outcome'] == 'interrupted', (
        'Premise broken: a turn with no finishReason no longer reads as '
        f'interrupted (got {r["outcome"]!r}) — re-derive this guard.')
    assert r['btnShow'] is True, (
        'Premise broken: continueButtonForSettlement no longer offers a resume '
        'for a finishReason-less turn; this guard would be vacuous.')


def test_premise_shipped_gate_matches_the_literal_under_test():
    """PREMISE: all four gates in chat_render.js derive from the ONE shared
    liveness predicate, and the harness can slice that predicate out.

    Comments are stripped first (project rule: a source-scanning guard must not
    be satisfiable by a comment) via the shared helper when available.
    """
    scan = _scan_source(CHAT_RENDER_JS)
    assert 'function _convMainTurnInFlight(' in scan, (
        'the TURN-level liveness predicate _convMainTurnInFlight is gone — the '
        'tail gates have no single source to derive from.')
    assert 'function _convBusyAnyLane(' in scan, (
        'the CONV-level predicate _convBusyAnyLane is gone.')
    assert 'function _isTurnInFlight(' in scan, (
        'the shared tail-liveness predicate _isTurnInFlight is gone.')
    # The CONV-level one must delegate to the reducer's union (Stop-button parity).
    m = re.search(r'function _convBusyAnyLane\(conv\)\s*\{(.*?)\n\}', scan,
                  flags=re.S)
    assert m and 'computeConvBusy' in m.group(1), (
        '_convBusyAnyLane no longer delegates to computeConvBusy — the delete '
        'gate and the composer Stop button can drift apart again.')
    # The TURN-level one must NOT scan branch keys (that was the regression).
    mt = re.search(r'function _convMainTurnInFlight\(conv\)\s*\{(.*?)\n\}', scan,
                   flags=re.S)
    assert mt, '_convMainTurnInFlight body not found'
    body = mt.group(1)
    assert 'computeConvBusy' not in body, (
        '_convMainTurnInFlight delegates to computeConvBusy, which scans '
        'branch-stream keys — a branch stream would then suppress the MAIN '
        "turn's finish bar. Keep the turn-level predicate branch-blind.")
    assert '.keys()' not in body and "+ ':'" not in body, (
        '_convMainTurnInFlight appears to scan stream keys by prefix; the '
        'turn-level predicate must only read exact-id / pin / authoritative.')
    # Tail gates read the turn-level predicate; delete reads the conv-level one.
    for decl in ('const _isLiveTail', 'const isLastAssistant',
                 'const _turnFailed'):
        mm = re.search(re.escape(decl) + r'\s*=(.*?);', scan, flags=re.S)
        assert mm, f'{decl} not found in chat_render.js'
        assert '_isTurnInFlight' in mm.group(1), (
            f'{decl} does not derive from the TURN-level predicate '
            f'— got: {mm.group(1).strip()!r}')
    md = re.search(r'const canDelete\s*=(.*?);', scan, flags=re.S)
    assert md, 'canDelete not found in chat_render.js'
    assert '_convBusyAnyLane' in md.group(1), (
        'canDelete must use the CONV-level predicate (a delete shifts indices '
        'and can corrupt a live branch stream keyed on msgIdx) — got: '
        f'{md.group(1).strip()!r}')


# ── FAILING-FIRST: the defect ─────────────────────────────────────────────

@requires_node
def test_two_predicates_never_disagree():
    """A single render pass must not answer "has this turn settled?" both ways.

    RED today: _isLiveTail=True (in flight) AND isLastAssistant=True (settled).
    """
    r = _run(_live_tail_scenario())
    assert not (r['isLiveTail'] and r['isLastAssistant']), (
        'renderMessage contradicts itself: _isLiveTail says the turn is IN '
        'FLIGHT (finish bar suppressed) while isLastAssistant says it has '
        'SETTLED (action bar + Continue rendered) — in the same pass. '
        f'got {r!r}')


@requires_node
def test_live_tail_hides_continue():
    """RED today: Continue must not render on a turn still being generated."""
    r = _run(_live_tail_scenario())
    assert r['showsContinue'] is False, (
        'The Continue affordance rendered on a turn the backend is still '
        'generating (activeStreams empty, conv.activeTaskId pinned to a live '
        f'carrier, no finishReason yet). got {r!r}')


@requires_node
def test_live_tail_hides_delete():
    """Delete must not render mid-turn either (canDelete DOES read the pin, so
    this one is expected GREEN — it pins the correct sibling behaviour so a
    future edit cannot regress it to match the broken predicate)."""
    r = _run(_live_tail_scenario())
    assert r['showsDelete'] is False, (
        f'Delete rendered on an in-flight turn. got {r!r}')


def test_source_gate_reads_liveness():
    """RED today: the Continue/action-bar gate must consult the liveness of the
    turn, not only this tab's activeStreams.

    The fix is a SHARED predicate (the ticket proposes `isTurnInFlight`), so the
    assertion is deliberately about the gate reading a liveness source at all —
    it accepts either the shared helper or a direct activeTaskId read, but NOT
    the current activeStreams-only form.
    """
    scan = _scan_source(CHAT_RENDER_JS)
    m = re.search(r'const isLastAssistant\s*=(.*?);', scan, flags=re.S)
    assert m, 'isLastAssistant declaration not found in chat_render.js'
    expr = m.group(1)
    reads_liveness = ('isTurnInFlight' in expr
                      or 'activeTaskId' in expr
                      or '_convActionGate' in expr)
    assert reads_liveness, (
        'isLastAssistant gates the action bar / Continue on POSITION + this '
        "tab's activeStreams only. It never consults whether the turn is still "
        'in flight (conv.activeTaskId / the carrier-aware busy set), so a '
        'cold-attached or socket-down live turn renders as settled. Derive it '
        'from ONE shared predicate together with _isLiveTail and canDelete.\n'
        f'  got: {expr.strip()!r}')


def test_turn_failed_force_reveal_excludes_in_flight():
    """RED today: `.turn-failed` promotes the action bar from hover-reveal to
    ALWAYS VISIBLE. A turn that is still in flight but already carries a stamped
    `interrupted` (what the stale-pin sweep does) must NOT get that promotion —
    otherwise the bar is permanently on screen under live work."""
    scan = _scan_source(CHAT_RENDER_JS)
    m = re.search(r'const _turnFailed\s*=(.*?);', scan, flags=re.S)
    assert m, '_turnFailed declaration not found in chat_render.js'
    expr = m.group(1)
    # The CSS promotion must exist (premise) …
    assert '.message.turn-failed .message-actions{opacity:1' in _read(STYLES_CSS), (
        'Premise broken: .turn-failed no longer force-reveals the action bar.')
    # … and the stamp must be gated on the turn NOT being in flight.
    guarded = ('isTurnInFlight' in expr or 'activeTaskId' in expr
               or '_convActionGate' in expr)
    assert guarded, (
        '_turnFailed force-reveals the action bar purely from a stamped '
        "finishReason==='interrupted', with no in-flight exclusion. A live turn "
        'that the stale-pin sweep stamped then shows a permanently-visible '
        f'action bar. got: {expr.strip()!r}')


# ── COMPLEMENT: must stay GREEN (guards against the reverse defect) ───────

@requires_node
def test_settled_tail_shows_continue():
    """A genuinely finished, resumable turn MUST still offer Continue."""
    r = _run(_settled_tail_scenario())
    assert r['isLiveTail'] is False, f'settled scenario read as live: {r!r}'
    assert r['showsContinue'] is True, (
        'Continue vanished from a turn that really ended — the liveness gate '
        f'was made too strict (reverse defect). got {r!r}')


@requires_node
def test_settled_tail_shows_delete():
    """A genuinely finished turn MUST still be deletable."""
    r = _run(_settled_tail_scenario())
    assert r['showsDelete'] is True, (
        f'Delete vanished from a settled turn (reverse defect). got {r!r}')


# ── NEUTER: prove the guard discriminates ─────────────────────────────────

@requires_node
def test_live_branch_does_not_suppress_settled_main_turn():
    """A live BRANCH stream must not make a finished MAIN turn read as live.

    Regression cell for the first fix, which routed the turn-level gates
    through computeConvBusy (branch-key prefix scan included).
    """
    r = _run(_settled_main_with_live_branch_scenario())
    assert r['isLiveTail'] is False, (
        'A branch stream made the settled MAIN turn read as in-flight, so its '
        f'finish bar (cost / duration / model) is suppressed. got {r!r}')
    assert r['outcome'] == 'completed', (
        f'premise broken: the main turn should verdict as completed. got {r!r}')


@requires_node
def test_live_branch_still_blocks_delete():
    """…but Delete STAYS blocked while a branch streams.

    Deleting shifts message indices and a branch stream is keyed on
    `convId:msgIdx:branchIdx`, so this is the deliberate conv-level semantic —
    asserted explicitly so the split cannot be "simplified" into one predicate.
    """
    r = _run(_settled_main_with_live_branch_scenario())
    assert r['showsDelete'] is False, (
        'Delete was offered while a branch stream is live in this conversation '
        f'— an index shift can corrupt the branch stream key. got {r!r}')


@requires_node
def test_neuter_turn_level_predicate_scanning_branch_keys_bites():
    """NEUTER: put the branch-key prefix scan back into the TURN-level gate and
    the settled-main-turn cell must go RED — proving that cell measures the
    branch dimension rather than passing for an unrelated reason."""
    branch_scanning_gate = """
function _turnLevelWithBranchScan(conv) {
  if (activeStreams.has(conv.id)) return true;
  if (conv.activeTaskId) return true;
  if (conv._authoritativeActiveTaskIds && conv._authoritativeActiveTaskIds.size > 0) return true;
  const prefix = conv.id + ':';
  for (const k of activeStreams.keys()) {
    if (typeof k === 'string' && k.startsWith(prefix)) return true;
  }
  return false;
}
const _inFlight = !!(conv && idx === conv.messages.length - 1
  && _turnLevelWithBranchScan(conv));
const _isLiveTail = _inFlight;
const isLastAssistant =
  !isUser && conv && idx === conv.messages.length - 1 && !_inFlight;
const canDelete = conv && !_convBusyAnyLane(conv);
"""
    r = _run(_settled_main_with_live_branch_scenario(), gate=branch_scanning_gate)
    assert r['isLiveTail'] is True, (
        'NEUTER did not bite: re-adding the branch-key scan to the turn-level '
        'predicate did NOT make the settled main turn read as live, so '
        f'test_live_branch_does_not_suppress_settled_main_turn is vacuous. got {r!r}')


@requires_node
def test_neuter_position_only_gate_leaks_continue():
    """NEUTER: with the gate reduced to today's activeStreams-only form, the
    in-flight scenario MUST leak Continue. If it does not, this guard is not
    actually measuring the liveness dimension."""
    r = _run(_live_tail_scenario(), gate=_GATE_POSITION_ONLY)
    assert r['showsContinue'] is True, (
        'NEUTER did not bite: the activeStreams-only gate did NOT leak the '
        'Continue button on an in-flight turn, so these tests do not '
        f'discriminate liveness. got {r!r}')
    assert r['isLiveTail'] and r['isLastAssistant'], (
        'NEUTER did not reproduce the self-contradiction the fix removed. '
        f'got {r!r}')


@requires_node
def test_neuter_liveness_aware_gate_closes_the_hole():
    """The INVERSE of the NEUTER above: a gate derived from one shared liveness
    predicate hides Continue in-flight AND still shows it once settled. This
    pins the TARGET behaviour so the fix has a concrete, executable definition
    rather than a prose description."""
    fixed_gate = """
const isTurnInFlight = !!(conv
  && idx === conv.messages.length - 1
  && (activeStreams.has(conv.id)
      || conv.activeTaskId
      || (conv._authoritativeActiveTaskIds
          && conv._authoritativeActiveTaskIds.size > 0)));
const _isLiveTail = isTurnInFlight;
const isLastAssistant =
  !isUser && conv && idx === conv.messages.length - 1 && !isTurnInFlight;
const canDelete = conv && !isTurnInFlight;
"""
    live = _run(_live_tail_scenario(), gate=fixed_gate)
    assert live['showsContinue'] is False, f'target gate still leaks: {live!r}'
    assert live['showsDelete'] is False, f'target gate still leaks delete: {live!r}'
    assert not (live['isLiveTail'] and live['isLastAssistant']), (
        f'target gate still self-contradicts: {live!r}')

    settled = _run(_settled_tail_scenario(), gate=fixed_gate)
    assert settled['showsContinue'] is True, (
        f'target gate over-suppresses on a settled turn: {settled!r}')
    assert settled['showsDelete'] is True, (
        f'target gate over-suppresses delete on a settled turn: {settled!r}')
