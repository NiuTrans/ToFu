"""tests/test_frontend_autopilot_baton_survives_splice.py — root-cause
regression for "autopilot VU reply finished but the agent never continues".

WHY
---
On a GOOD network signal the backend hands the frontend the follow-up
``next_task_id`` in the ``done`` SSE event (and again on the poll response).
The frontend must open a fresh stream to it (``connectToTask`` via
``_attachAutopilotFollowup``).  Historically the baton was stamped
POSITIONALLY on ``conv.messages[length-1]`` for the detached kick-carrier
(``sse_pipeline.js`` / ``sse_poll_fallback.js``).  ``finishStream`` later
re-discovers it with ``_findAutopilotPendingCarrier`` (a tail-up scan).

The hole: if that specific message object is SPLICED OUT before
``finishStream`` runs — a ``vu_cancel`` removing the VU bubble, or an edit —
the ``_autopilotPending`` payload leaves with it.  The carrier scan cannot
rescue a baton stamped on a since-deleted object → the loop stalls with the
VU bubble visible, which is EXACTLY the reported symptom.

THE FIX
-------
An AUTHORITATIVE conv-level baton ``conv._apPendingBaton = {nextTaskId,
vuMessage}`` set by the done/poll handlers alongside (not instead of) the
positional stamp.  ``_findAutopilotPendingCarrier`` reads the conv-level
baton FIRST (returning a synthetic ``_convLevel`` carrier) and only falls
back to the per-message scan as a compat reader.  A conv-level field cannot
be spliced away by a message deletion → the baton survives.

This harness slices the REAL shipped ``_findAutopilotPendingCarrier`` out of
``static/js/ui/conversation_list.js`` and evals it (bites the actual logic,
not a copy).

TRIPLE-NEUTER (each proven to bite by hand, then restored byte-identical):
  • NC-1 — revert to message-only scan (drop the ``conv._apPendingBaton``
    branch): the splice-survival case FAILS (baton lost with the message).
  • NC-2 — make the conv-level branch return null instead of the synthetic
    carrier: the "conv-level baton alone is sufficient" case FAILS.
  • NC-3 — flip the scan direction / stop preferring conv-level (return the
    stale message stamp over the fresh conv baton): the "conv baton wins over
    a stale message stamp" case FAILS.
The neuters are applied to a COPY in tmp; the shipped file is untouched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SRC = runtime_section_path('ui/conversation_list.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_carrier(src_text: str) -> str:
    """Slice the real _findAutopilotPendingCarrier out of the shipped file."""
    m = re.search(
        r'function _findAutopilotPendingCarrier\(conv\) \{.*?\n\}\n',
        src_text, re.DOTALL,
    )
    if not m:
        raise AssertionError('could not locate _findAutopilotPendingCarrier')
    return m.group(0)


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

eval(fs.readFileSync(process.argv[2], 'utf8'));   // _findAutopilotPendingCarrier (real or neutered)
check('carrier_fn_exposed', typeof _findAutopilotPendingCarrier === 'function');

const BATON = { nextTaskId: 'task-next-123', vuMessage: { content: 'go on' } };

// ── Case A: baton stamped POSITIONALLY on a message, message then SPLICED. ──
//    With the conv-level baton set too, the carrier must still resolve.
(function () {
  const vuMsg = { role: 'user', _isVirtualUser: true, _autopilotPending: BATON };
  const conv = { id: 'c1', messages: [{ role: 'assistant' }, vuMsg] };
  conv._apPendingBaton = BATON;                       // authoritative copy
  // vu_cancel removes the VU bubble that carried the positional stamp:
  conv.messages.splice(1, 1);
  const carrier = _findAutopilotPendingCarrier(conv);
  check('A_carrier_survives_splice', !!carrier && !!carrier.msg
        && carrier.msg._autopilotPending
        && carrier.msg._autopilotPending.nextTaskId === 'task-next-123');
})();

// ── Case B: NO positional stamp at all, only the conv-level baton. ──
//    (The robust design: backend fact held on the conv, not a message.)
(function () {
  const conv = { id: 'c2', messages: [{ role: 'assistant' }, { role: 'user' }] };
  conv._apPendingBaton = BATON;
  const carrier = _findAutopilotPendingCarrier(conv);
  check('B_conv_baton_alone_sufficient', !!carrier
        && carrier.msg._autopilotPending.nextTaskId === 'task-next-123'
        && carrier._convLevel === true);
})();

// ── Case C: conv baton is FRESH, a STALE positional stamp lingers on an old
//    message → conv-level must WIN (it's authoritative). ──
(function () {
  const stale = { nextTaskId: 'task-STALE-000', vuMessage: { content: 'old' } };
  const conv = {
    id: 'c3',
    messages: [{ role: 'assistant', _autopilotPending: stale }, { role: 'user' }],
  };
  conv._apPendingBaton = BATON;                       // fresh, authoritative
  const carrier = _findAutopilotPendingCarrier(conv);
  check('C_conv_baton_wins_over_stale_stamp',
        !!carrier && carrier.msg._autopilotPending.nextTaskId === 'task-next-123');
})();

// ── Case D: compat — no conv baton, only a per-message stamp (pre-fix batons
//    or a path that only stamped the message) → message scan still finds it. ──
(function () {
  const conv = {
    id: 'c4',
    messages: [{ role: 'assistant' }, { role: 'user', _autopilotPending: BATON }],
  };
  const carrier = _findAutopilotPendingCarrier(conv);
  check('D_message_scan_compat', !!carrier
        && carrier.msg._autopilotPending.nextTaskId === 'task-next-123'
        && carrier.idx === 1);
})();

// ── Case E: nothing pending → null (no false positive). ──
(function () {
  const conv = { id: 'c5', messages: [{ role: 'assistant' }, { role: 'user' }] };
  check('E_no_baton_null', _findAutopilotPendingCarrier(conv) === null);
})();

console.log(out.join('\n'));
"""


def _run(carrier_js_text: str) -> str:
    carrier_js = os.path.join(HERE, '_ap_baton_carrier.js')
    harness = os.path.join(HERE, '_ap_baton_harness.js')
    with open(carrier_js, 'w', encoding='utf-8') as f:
        f.write(carrier_js_text)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, carrier_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (carrier_js, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_baton_survives_splice_positive():
    with open(SRC, encoding='utf-8') as f:
        carrier = _extract_carrier(f.read())
    output = _run(carrier)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'baton carrier failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_baton_survives_splice_neuters_bite():
    """Prove each neuter breaks the assertion it should — the guards are
    load-bearing, not decorative."""
    with open(SRC, encoding='utf-8') as f:
        real = _extract_carrier(f.read())

    # NC-1: revert to message-only scan (drop the conv-level branch entirely).
    nc1 = re.sub(
        r"  const _baton = conv\._apPendingBaton;.*?\n  for \(let i",
        "  for (let i",
        real, flags=re.DOTALL,
    )
    assert nc1 != real, 'NC-1 did not modify the source'
    out1 = _run(nc1)
    assert 'FAIL A_carrier_survives_splice' in out1, \
        'NC-1 (message-only scan) should FAIL the splice-survival case:\n' + out1

    # NC-2: conv-level branch returns null instead of the synthetic carrier.
    nc2 = real.replace(
        "return { msg: { _autopilotPending: _baton }, idx: -1, _convLevel: true };",
        "return null;",
    )
    assert nc2 != real, 'NC-2 did not modify the source'
    out2 = _run(nc2)
    assert 'FAIL B_conv_baton_alone_sufficient' in out2, \
        'NC-2 (conv branch returns null) should FAIL the conv-baton-alone case:\n' + out2

    # NC-3: stop preferring the conv baton — if a message stamp exists, return
    #        it FIRST (stale wins). Achieved by moving the message scan above
    #        the conv-baton check is awkward via regex; instead neuter the
    #        conv-baton to yield the stale message's payload by disabling the
    #        early return so the message scan (which sees the stale stamp) wins.
    nc3 = real.replace(
        "  if (_baton && _baton.nextTaskId) {\n"
        "    return { msg: { _autopilotPending: _baton }, idx: -1, _convLevel: true };\n"
        "  }",
        "  if (_baton && _baton.nextTaskId && false) {\n"
        "    return { msg: { _autopilotPending: _baton }, idx: -1, _convLevel: true };\n"
        "  }",
    )
    assert nc3 != real, 'NC-3 did not modify the source'
    out3 = _run(nc3)
    assert 'FAIL C_conv_baton_wins_over_stale_stamp' in out3, \
        'NC-3 (conv branch disabled) should FAIL the conv-baton-wins case:\n' + out3

    # Sanity: the REAL source passes all of A/B/C.
    out_real = _run(real)
    for case in ('A_carrier_survives_splice', 'B_conv_baton_alone_sufficient',
                 'C_conv_baton_wins_over_stale_stamp'):
        assert f'PASS {case}' in out_real, f'real source should PASS {case}:\n{out_real}'
