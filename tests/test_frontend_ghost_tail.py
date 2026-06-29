"""Regression test: a TRAILING assistant turn that was interrupted (server
crash / restart) before producing any content must NOT render as a blank,
finish-tag-less bubble forever.

WHY
---
An autopilot follow-up (or any) turn can stream a stray reasoning fragment
(e.g. ``thinking:'I'``) and then die while ``task_results.status='running'``.
``_sync_partial_to_conversation`` had already written a husk into
``conversations.messages``::

    {role:'assistant', content:'', thinking:'I', _memoryPrefetch:{...}}

with NO finishReason/usage/timestamp.  On reload the frontend Case-D
ghost-cleanup used to SKIP it (its ``!thinking`` guard is defeated by the
1-char fragment), so the husk was orphaned: ``renderFinishInfo`` returns ''
when there's no finishReason/usage/model, leaving an empty bubble.

``_classifyGhostTail(lastMsg)`` (static/js/main/main_init_tasks.js) is the
pure decision predicate the Case-D reconcile path now uses:
  * 'delete'      — truly empty husk (no thinking): remove it.
  * 'interrupted' — thinking-only husk: STAMP finishReason='interrupted'
                    (preserve recovered thinking) so it renders honestly.
  * null          — settled turn / has content / real tool round: leave alone.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
// main_init_tasks.js only DECLARES functions at load time (no top-level
// execution), so eval'ing it in a bare context is safe — the heavy
// initActiveTasks() globals are never touched until it's CALLED.
eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _classifyGhostTail !== 'function') {
  console.log('FAIL fn_exposed _classifyGhostTail missing');
  process.exit(0);
}
check('fn_exposed', true);

// ── renderFinishInfo MUST render the "Interrupted" badge for a
//    finishReason-only message (no usage/model/preset) — otherwise the
//    stamp from _classifyGhostTail still produces a blank bubble. Load the
//    real shipped finish_info.js (argv[3]) with minimal stubs. ──
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.Icon = () => '';
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global._isThinkingCapable = () => false;
global._providerDisplayName = (p) => p || '';
global.calcCostCny = () => null;
global.window = global;
try {
  eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/finish_info.js
  if (typeof renderFinishInfo === 'function') {
    const fiHtml = renderFinishInfo({ role: 'assistant', content: '', thinking: 'I', finishReason: 'interrupted' });
    check('finishinfo_renders_for_finishreason_only', fiHtml.length > 0);
    check('finishinfo_has_interrupted_badge', fiHtml.includes('Interrupted'));
  } else {
    check('finishinfo_loaded', false);
  }
} catch (e) {
  check('finishinfo_loaded', false);
}

// ── The exact husk from PG conv mqqkiycqei6xvl (interrupted autopilot
//    follow-up): empty content + 1-char thinking + _memoryPrefetch, no
//    finishReason. Must be classified 'interrupted' (stamp, not delete). ──
check('husk_thinking_only_interrupted',
  _classifyGhostTail({
    role: 'assistant', content: '', thinking: 'I',
    _memoryPrefetch: { phase: 'done' },
  }) === 'interrupted');

// ── Truly empty husk (no thinking) → delete. ──
check('empty_husk_delete',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: '' }) === 'delete');
check('empty_husk_no_thinking_key_delete',
  _classifyGhostTail({ role: 'assistant', content: '' }) === 'delete');

// ── Settled turns must be LEFT ALONE (null). ──
check('has_content_null',
  _classifyGhostTail({ role: 'assistant', content: 'hello', thinking: 'x' }) === null);
check('has_finishReason_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I', finishReason: 'stop' }) === null);
check('already_interrupted_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I', finishReason: 'interrupted' }) === null);
check('has_usage_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I', usage: { input_tokens: 10 } }) === null);
check('has_error_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I', error: { kind: 'internal' } }) === null);

// ── A REAL tool round (done / has results / toolContent) means work happened
//    → not a ghost, leave alone even with empty content. ──
check('real_round_done_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I',
    toolRounds: [{ status: 'done', toolName: 'run_command' }] }) === null);
check('real_round_results_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I',
    toolRounds: [{ status: 'searching', results: [{ title: 't' }] }] }) === null);
check('real_round_toolcontent_null',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I',
    toolRounds: [{ status: 'searching', toolContent: 'out' }] }) === null);

// ── An EMPTY / still-searching round is NOT real work → still a ghost. ──
check('empty_searching_round_interrupted',
  _classifyGhostTail({ role: 'assistant', content: '', thinking: 'I',
    toolRounds: [{ status: 'searching' }] }) === 'interrupted');
check('empty_searching_round_no_thinking_delete',
  _classifyGhostTail({ role: 'assistant', content: '',
    toolRounds: [{ status: 'searching' }] }) === 'delete');

// ── Non-assistant / nullish → null. ──
check('user_role_null',
  _classifyGhostTail({ role: 'user', content: '' }) === null);
check('null_msg_null', _classifyGhostTail(null) === null);
check('undefined_msg_null', _classifyGhostTail(undefined) === null);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_classify_ghost_tail():
    harness = os.path.join(HERE, '_ghost_tail_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'main', 'main_init_tasks.js'),  # argv[2]
             os.path.join(JS_DIR, 'ui', 'finish_info.js'),        # argv[3]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Ghost-tail classification failures:\n' + output
    assert output.count('PASS') >= 19, f'expected >=19 PASS lines, got:\n{output}'
