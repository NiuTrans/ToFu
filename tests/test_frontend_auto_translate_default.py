"""Regression test: the frontend ``convAutoTranslate(conv)`` helper is the
single source of truth for the per-conversation auto-translate trigger
decision, and its canonical default is OPT-IN / OFF — matching the backend
``lib.conv_config.AUTO_TRANSLATE_DEFAULT`` so the toolbar toggle display and
every frontend trigger path agree.

WHY
---
Historically ~8 frontend trigger sites read ``conv.autoTranslate !== undefined
? ... : true/false`` with MIXED fallbacks (some defaulted true, two defaulted
false), and the global ``autoTranslate`` itself defaulted ``true`` from
localStorage (core/cost.js). Together with the backend's own divergent
defaults this made auto-translate fire unpredictably. Phase-1 of the
stabilisation routed every frontend trigger through ``convAutoTranslate`` and
flipped the global default to OFF.

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


# core/conv_reducers.js (extracted from conversations.js 2026-07-25) is all
# top-level declarations; eval'ing it bare defines convAutoTranslate with no
# side effects (the Api / ConvCache globals it names are only touched inside
# OTHER function bodies).
_HARNESS = r"""
const fs = require('fs');
global.window = global;
eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/conv_reducers.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof convAutoTranslate !== 'function') {
  console.log('FAIL fn_exposed convAutoTranslate missing');
  process.exit(0);
}
check('fn_exposed', true);

// ── Explicit per-conv value ALWAYS wins (over the global). ──
global.autoTranslate = true;   // global ON …
check('conv_false_wins_over_global_on',
  convAutoTranslate({ autoTranslate: false }) === false);   // … but conv says OFF
global.autoTranslate = false;  // global OFF …
check('conv_true_wins_over_global_off',
  convAutoTranslate({ autoTranslate: true }) === true);     // … but conv says ON

// ── conv flag absent → fall through to the global toolbar flag. ──
global.autoTranslate = true;
check('absent_falls_through_to_global_on',
  convAutoTranslate({}) === true);
check('absent_falls_through_to_global_on_no_key',
  convAutoTranslate({ model: 'x' }) === true);
global.autoTranslate = false;
check('absent_falls_through_to_global_off',
  convAutoTranslate({}) === false);

// ── The keystone: nothing defined anywhere → OFF (opt-in). ──
//    (global undefined + no conv flag → must NOT translate.)
delete global.autoTranslate;
check('all_absent_defaults_off',
  convAutoTranslate({}) === false);
check('null_conv_all_absent_off',
  convAutoTranslate(null) === false);
check('undefined_conv_all_absent_off',
  convAutoTranslate(undefined) === false);

// ── A null/undefined conv with a global ON still honours the global. ──
global.autoTranslate = true;
check('null_conv_global_on',
  convAutoTranslate(null) === true);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_conv_auto_translate_default():
    harness = os.path.join(HERE, '_auto_translate_default_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'conv_reducers.js'),  # argv[2]
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
    assert not fails, 'convAutoTranslate default/precedence failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'
