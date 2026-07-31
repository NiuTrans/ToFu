"""tests/test_frontend_trimmed_tool_activity_affordance.py — the windowed-open
"Load tool activity (N)" affordance must survive a turn that ALSO received
async-swarm / peer / user-steer inbox injects.

Real-world defect (conv ms34q20atwnf35, 2026-07-27): the first assistant turn
holds 73 real tool rounds in the DB, but on reopen the bubble showed only three
rows — all of them SYNTHETIC "received 1 async swarm update" chips — and no
affordance to pull the real history back.

Mechanism (both halves are per-design, the BUG is their intersection):
  • routes/conversations.py::_trim_heavy_for_window strips ``toolRounds`` for
    transport and stamps ``_trimmed`` + ``_trimmedToolRoundCount``.
  • core.js::getToolRoundsFromMsg falls through to ``_rehydrateInjectRows``
    when ``toolRounds`` is empty, REBUILDING the display-only inject rows.
So for a trimmed turn that had injects, ``rounds.length`` is 3 rather than 0 —
and chat_render.js gated the affordance on ``rounds.length === 0``. The user is
left with no path back to the 73 rounds: history looks lost.

Discipline (charter: never hand-copy a production predicate into a harness).
Both the round-assembly functions and the affordance predicate are SPLICED out
of the shipped sources at run time and located by SYMBOL, not by line number,
so a later refactor either keeps working or reports which half went missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_JS = os.path.join(ROOT, 'static', 'js', 'core.js')
RENDER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'chat_render.js')


def _node():
    return bool(shutil.which('node'))


def _fn_span(src, name):
    """Return the source text of a top-level ``function <name>(...) {...}``.

    Braces are balanced rather than regex-matched so nested blocks survive.
    Raises with a THREE-STATE diagnosis (charter: source-anchored guards must
    distinguish "implementation deleted" from "single source duplicated").
    """
    hits = [m.start() for m in re.finditer(r'^function\s+' + name + r'\s*\(', src, re.M)]
    if not hits:
        raise AssertionError(
            f'production function {name}() not found — implementation deleted '
            f'or renamed; this guard must be re-pointed, not deleted')
    if len(hits) > 1:
        raise AssertionError(
            f'{name}() defined {len(hits)}x — single source of truth was '
            f'copied; collapse it before re-pointing this guard')
    start = hits[0]
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'unbalanced braces while slicing {name}()')


def _affordance_predicate(render_src):
    """Splice the SHIPPED affordance decision out of chat_render.js.

    Returns ``(derivation, condition)`` where *derivation* is any statement the
    guard depends on (currently the ``_realRounds`` filter) and *condition* is
    the ``if`` expression itself. Both are lifted VERBATIM so the harness can
    never drift from the shipped decision — splicing only the ``if`` would let
    the harness keep passing while the real filter changed underneath it.
    """
    m = re.search(
        r'(?P<deriv>const\s+_realRounds\s*=.*?;\s*)?'
        r'if\s*\((?P<cond>[^{]*?_trimmedToolRoundCount[^{]*?)\)\s*\{',
        render_src, re.S)
    if not m:
        raise AssertionError(
            'the trimmed-tool-activity affordance guard is gone from '
            'chat_render.js — either the affordance was removed (a real '
            'regression) or this guard needs re-pointing')
    return (m.group('deriv') or ''), ' '.join(m.group('cond').split())


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Spliced from the shipped sources by the Python side (symbol-anchored).
%(CORE_FNS)s

// The affordance decision, spliced VERBATIM from chat_render.js (both the
// _realRounds derivation and the `if` condition). Evaluated against the same
// locals the renderer holds at that point.
function affordanceShown(msg, rounds, segTimelineRendered) {
  const _segTimelineRendered = segTimelineRendered;
  %(DERIVATION)s
  return !!(%(PREDICATE)s);
}

// ── The real DB shape of conv ms34q20atwnf35 msg[1], AFTER the server's
//    windowed trim: toolRounds/segments stripped, 73 recorded, and three
//    display-only swarm inject sidecars retained.
function trimmedTurnWithInjects() {
  return {
    role: 'assistant',
    content: '## 测试套件体检报告',
    _msgId: 'a1',
    _trimmed: true,
    _trimmedToolRoundCount: 73,
    _inboxInjects: [
      { round: 17, count: 1, agentIds: ['cov'] },
      { round: 20, count: 1, agentIds: ['a'] },
      { round: 24, count: 1, agentIds: ['e'] },
    ],
  };
}

// Control: a trimmed turn with NO injects (the shape the affordance was
// originally written against).
function trimmedTurnNoInjects() {
  return {
    role: 'assistant', content: 'plain answer', _msgId: 'a2',
    _trimmed: true, _trimmedToolRoundCount: 12,
  };
}

function run() {
  // ── 1. The reported defect: injects masquerade as tool rounds ──────────
  const m1 = trimmedTurnWithInjects();
  const r1 = getToolRoundsFromMsg(m1);
  check('trimmed+injects: rounds are ONLY synthetic inject rows',
        r1.length === 3 && r1.every(r => r._inboxInject === true));
  check('trimmed+injects: zero REAL tool rounds present',
        r1.filter(r => !r._inboxInject && !r._peerInject && !r._userSteerInject).length === 0);
  // The user must still be offered a way back to the 73 trimmed rounds.
  check('trimmed+injects: "Load tool activity" affordance IS offered',
        affordanceShown(m1, r1, false) === true);

  // ── 2. Control: the no-inject shape must keep working ──────────────────
  const m2 = trimmedTurnNoInjects();
  const r2 = getToolRoundsFromMsg(m2);
  check('trimmed, no injects: affordance offered',
        r2.length === 0 && affordanceShown(m2, r2, false) === true);

  // ── 3. COMPLEMENT (charter: a ban without a complement degrades into
  //    "always show it"). A turn whose REAL rounds are present, or which was
  //    never trimmed, must NOT get the affordance.
  const m3 = trimmedTurnWithInjects();
  const r3 = getToolRoundsFromMsg(m3).concat([
    { roundNum: 1, toolName: 'run_command', llmRound: 0, status: 'done', toolCallId: 'tc1' },
  ]);
  check('COMPLEMENT: real rounds hydrated → no affordance',
        affordanceShown(m3, r3, false) === false);

  const m4 = { role: 'assistant', content: 'x', _msgId: 'a4' };
  check('COMPLEMENT: never-trimmed turn → no affordance',
        affordanceShown(m4, getToolRoundsFromMsg(m4), false) === false);

  const m5 = trimmedTurnWithInjects();
  check('COMPLEMENT: segment timeline rendered → no affordance',
        affordanceShown(m5, getToolRoundsFromMsg(m5), true) === false);

  console.log(out.join('\n'));
}
run();
"""


def _run():
    core = open(CORE_JS, encoding='utf-8').read()
    render = open(RENDER_JS, encoding='utf-8').read()
    core_fns = '\n'.join(
        _fn_span(core, n)
        for n in ('getToolRoundsFromMsg', '_rehydrateInjectRows', '_spliceInjectRow')
    )
    deriv, pred = _affordance_predicate(render)
    js = _HARNESS % {
        'CORE_FNS': core_fns,
        'DERIVATION': deriv,
        'PREDICATE': pred,
    }
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(js)
        h = f.name
    try:
        r = subprocess.run(['node', h], capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr
    finally:
        os.unlink(h)


@pytest.mark.skipif(not _node(), reason='node not available')
def test_scan_surface_report():
    """Print what was actually spliced BEFORE asserting anything.

    charter: a source-anchored guard must show its scan surface, or it can pass
    while pointed at nothing.
    """
    core = open(CORE_JS, encoding='utf-8').read()
    render = open(RENDER_JS, encoding='utf-8').read()
    for name in ('getToolRoundsFromMsg', '_rehydrateInjectRows', '_spliceInjectRow'):
        span = _fn_span(core, name)
        print(f'spliced {name}(): {len(span)} chars, {span.count(chr(10)) + 1} lines')
    deriv, pred = _affordance_predicate(render)
    print(f'affordance derivation: {" ".join(deriv.split()) or "(none)"}')
    print(f'affordance predicate: {pred}')
    assert '_trimmedToolRoundCount' in pred


@pytest.mark.skipif(not _node(), reason='node not available')
def test_trimmed_tool_activity_affordance_survives_inbox_injects():
    out = _run()
    lines = [ln for ln in out.splitlines() if ln.startswith(('PASS ', 'FAIL '))]
    assert lines, f'no results:\n{out}'
    failed = [ln for ln in lines if ln.startswith('FAIL ')]
    assert not failed, ('trimmed-affordance failures:\n' + '\n'.join(lines)
                        + '\n\nRAW:\n' + out)
    print('\n'.join(lines))
