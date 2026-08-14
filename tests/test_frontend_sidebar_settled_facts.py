"""tests/test_frontend_sidebar_settled_facts.py — the sidebar's incomplete/
errored dot on a messages-stripped (?meta=1) shell.

WHY
---
The sidebar list loads conversations WITHOUT their ``messages`` array (the
``?meta=1`` metadata path strips bodies). ``_convStatusFlags`` computes the
amber "incomplete" / red "errored" dot from the tail assistant message — which
isn't present on a shell. So a crash-interrupted conversation showed NO dot
until the user opened it (loading full messages). The fix: the backend stamps
RAW settled-turn facts into ``settings`` (surfaced onto the conv as
``lastFinishReason`` / ``lastMsgError`` / ``lastMsgHasOutput``), and
``_convStatusFlags`` gains a fallback branch that runs the SAME
``_FINISH_ERR`` / ``_FINISH_NORMAL`` classifier when ``c.messages`` is absent
or is the empty array used by real lazy sidebar shells.

This harness slices the REAL shipped ``_convStatusFlags`` out of
``static/js/ui/conversation_list.js`` and evals it (bites the actual logic),
supplying the module globals it closes over.

NEUTER: delete the fallback ``else if`` branch → the interrupted shell must go
``incomplete===false`` (the bug), proving the branch is load-bearing; restored
byte-identical after.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
SRC = os.path.join(JS_DIR, 'ui', 'conversation_list.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_flags(src_text: str) -> str:
    """Slice the real _convStatusFlags out of the shipped file."""
    m = re.search(
        r'function _convStatusFlags\(c\) \{.*?\n\}\n',
        src_text, re.DOTALL,
    )
    if not m:
        raise AssertionError('could not locate _convStatusFlags')
    return m.group(0)


# Harness: provide the globals _convStatusFlags closes over, then drive it with
# messages-stripped shells (only settings-derived facts present on the conv).
_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Module globals _convStatusFlags references:
global.activeStreams = new Map();
global._FINISH_NORMAL = new Set(['stop', 'end_turn', 'stop_sequence', 'tool_use', 'tool_calls']);
global._FINISH_ERR = new Set(['error', 'server_offline']);
global._autopilotRunConcluded = function () { return false; };
global.convIsBusy = function (conv) {
  if (!conv) return false;
  return activeStreams.has(conv.id) || !!conv.activeTaskId;
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // _convStatusFlags (real or neutered)
check('fn_exposed', typeof _convStatusFlags === 'function');

// ── interrupted shell (no messages, only settings facts) → incomplete ──
(function () {
  const c = { id: 'i1', lastMsgRole: 'assistant', lastFinishReason: 'interrupted',
              lastMsgError: false, lastMsgHasOutput: true };
  const f = _convStatusFlags(c);
  check('interrupted_shell_incomplete', f.incomplete === true && f.errored === false);
})();

// ── real lazy shell shape uses messages:[]; it must hit the same fallback ──
(function () {
  const c = { id: 'i2', messages: [], _needsLoad: true,
              lastMsgRole: 'assistant', lastFinishReason: 'interrupted',
              lastMsgError: false, lastMsgHasOutput: true };
  const f = _convStatusFlags(c);
  check('empty_array_shell_incomplete', f.incomplete === true && f.errored === false);
})();

// ── error shell → errored ──
(function () {
  const c = { id: 'e1', lastMsgRole: 'assistant', lastFinishReason: 'error',
              lastMsgError: false, lastMsgHasOutput: false };
  const f = _convStatusFlags(c);
  check('error_shell_errored', f.errored === true);
})();

// ── lastMsgError bool → errored even with a benign finishReason ──
(function () {
  const c = { id: 'e2', lastMsgRole: 'assistant', lastFinishReason: null,
              lastMsgError: true, lastMsgHasOutput: true };
  const f = _convStatusFlags(c);
  check('error_bool_shell_errored', f.errored === true);
})();

// ── completed (stop) shell → neither ──
(function () {
  const c = { id: 's1', lastMsgRole: 'assistant', lastFinishReason: 'stop',
              lastMsgError: false, lastMsgHasOutput: true };
  const f = _convStatusFlags(c);
  check('stop_shell_clean', f.incomplete === false && f.errored === false);
})();

// ── dangling empty placeholder (mra8htdw edge): no finishReason, no output ──
(function () {
  const c = { id: 'd1', lastMsgRole: 'assistant', lastFinishReason: null,
              lastMsgError: false, lastMsgHasOutput: false };
  const f = _convStatusFlags(c);
  check('empty_placeholder_incomplete', f.incomplete === true);
})();

// ── a user-role tail shell (turn not yet answered) → neither (no false dot) ──
(function () {
  const c = { id: 'u1', lastMsgRole: 'user', lastFinishReason: null,
              lastMsgError: false, lastMsgHasOutput: false };
  const f = _convStatusFlags(c);
  check('user_tail_shell_clean', f.incomplete === false && f.errored === false);
})();

console.log(out.join('\n'));
"""


def _run(flags_js_text: str) -> str:
    flags_js = os.path.join(HERE, '_conv_flags_fn.js')
    harness = os.path.join(HERE, '_conv_flags_harness.js')
    with open(flags_js, 'w', encoding='utf-8') as f:
        f.write(flags_js_text)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, flags_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (flags_js, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_settled_facts_classify_stripped_shell():
    with open(SRC, encoding='utf-8') as f:
        flags = _extract_flags(f.read())
    output = _run(flags)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'settled-facts flag failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_fallback_branch_is_load_bearing_neuter():
    """Delete the messages-stripped fallback branch → the interrupted shell
    must go incomplete===false (the pre-fix bug), proving the branch bites."""
    with open(SRC, encoding='utf-8') as f:
        real = _extract_flags(f.read())

    # NC: remove the entire `else if (!streaming && !hasLoadedMessages...)`
    #     fallback block that classifies a stripped shell.
    nc = re.sub(
        r"  else if \(!streaming && !hasLoadedMessages && c\.lastMsgRole === 'assistant'\) \{.*?\n  \}\n",
        "\n",
        real, flags=re.DOTALL,
    )
    assert nc != real, 'NC did not modify the source (branch not found)'
    out_nc = _run(nc)
    assert 'FAIL interrupted_shell_incomplete' in out_nc, (
        'removing the fallback branch should FAIL the interrupted-shell case '
        '(the dot would be invisible on the sidebar):\n' + out_nc)

    # Sanity: the REAL source passes the interrupted case.
    out_real = _run(real)
    assert 'PASS interrupted_shell_incomplete' in out_real, (
        'real source should PASS the interrupted-shell case:\n' + out_real)
