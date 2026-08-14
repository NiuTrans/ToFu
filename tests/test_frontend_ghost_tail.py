"""Regression test: a TRAILING assistant turn that was interrupted (server
crash / restart) before producing any content must render HONESTLY as an
"Interrupted"-badged bubble, not a blank finish-tag-less one.

WHY THIS CHANGED (2026-07-11)
-----------------------------
The trailing-ghost VERDICT ('delete' an empty husk, 'interrupt'-stamp a
thinking-only husk) used to be computed in the frontend by
``_classifyGhostTail`` (static/js/main/main_init_tasks.js) on the Case-D reload
path. That was frontend lifecycle INFERENCE — the separation-of-concerns
violation. It is now applied ENTIRELY server-side by
``lib.conversations.reconcile.reconcile_conversation_messages`` on EVERY render
path (single-conv GET, ?meta=1&prefetch=, startup recovery), which persists the
cleaned list AND stamps ``settings._reconciledAt`` in the same commit. So
``_classifyGhostTail`` was DELETED from the frontend.

This suite now guards the TWO facts that survive that removal:
  1. ``_classifyGhostTail`` is GONE from main_init_tasks.js (removal tripwire —
     if a refactor reintroduces frontend lifecycle inference, this fails).
     The classification-logic coverage moved to
     tests/test_reconcile_js_backend_equivalence.py (15-fixture golden + teeth
     neuters) and tests/test_reconcile_conversation.py.
  2. ``renderFinishInfo`` STILL renders a visible "Interrupted" badge for a
     ``finishReason='interrupted'``-only message (no usage/model/preset) — this
     is what makes the backend's interrupt STAMP render honestly instead of as a
     blank bubble. This is the load-bearing end-to-end guard.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
// main_init_tasks.js only DECLARES functions at load time (no top-level
// execution), so eval'ing it in a bare context is safe.
eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── 1. REMOVAL TRIPWIRE: the frontend ghost-tail classifier must be GONE.
//    The verdict is backend-only now (lib/conversations/reconcile.py). If a
//    refactor reintroduces frontend lifecycle inference, this fails loudly. ──
check('classifier_removed', typeof _classifyGhostTail === 'undefined');

// ── 2. renderFinishInfo MUST render the "Interrupted" badge for a
//    finishReason-only message (no usage/model/preset) — otherwise the
//    backend's interrupt STAMP still produces a blank bubble. Load the real
//    shipped finish_info.js (argv[3]) with minimal stubs. ──
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

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_ghost_tail_classifier_removed_and_interrupt_renders():
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
    assert not fails, 'Ghost-tail removal / interrupt-render failures:\n' + output
    assert 'PASS classifier_removed' in output, (
        '_classifyGhostTail is still present in main_init_tasks.js — the '
        'frontend lifecycle-inference belt was NOT removed:\n' + output)
    assert output.count('PASS') >= 3, f'expected >=3 PASS lines, got:\n{output}'
