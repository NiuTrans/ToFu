#!/usr/bin/env python3
"""tests/test_frontend_finish_info_interrupted.py — the model-only finish bar
is suppressed on interrupted stubs, not just on the live tail (epic
pt_f5771a2e, fix F3).

WHY
---
``renderFinishInfo`` (static/js/ui/finish_info.js) only suppressed the bogus
model-only "finished" bar for the LIVE streaming tail
(``if (!_terminal && isLiveTail) return ""``). An interrupted stub — a message
the crash/restart recovery stamped with ``interruptedReason`` but no
``finishReason``/``usage`` — is rendered STATICALLY, so the guard let the bar
through and it FROZE: no live stream ever tears it down. In the ms43foj3
double-restart incident BOTH bubbles on screen carried a frozen bare
"K kimi-k3" bar, reading as "finished" on turns that were actually dead.

The fix: suppress the model-only bar whenever the message is non-terminal
AND (live tail OR interruptedReason set). A legitimately finished turn always
carries finishReason/usage; a legacy model-only FINISHED message (no
interruptedReason) deliberately keeps its bar.

Slices the REAL shipped ``renderFinishInfo`` verbatim, runs it under node
with minimal stub globals. NEUTER: drop ``|| msg.interruptedReason`` from the
guard → the interrupted-stub case renders the bogus bar again (FAIL).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_FILE = runtime_section_path('ui/finish_info.js')

_FN_START = 'function renderFinishInfo(msg, isLiveTail) {'
_FN_END = '  if (parts.length === 0) return "";\n  return `<div class="message-finish">${parts.join("")}</div>`;\n}'


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract(src: str) -> str:
    start = src.index(_FN_START)
    end = src.index(_FN_END, start) + len(_FN_END)
    return src[start:end]


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Minimal globals renderFinishInfo touches on the model-only path ──
global.escapeHtml = (s) => String(s);
global.t = (k) => String(k);
global.Icon = () => '';
global._detectBrand = () => 'kimi';
global._brandSvg = () => '';
global._isThinkingCapable = () => false;
global._providerDisplayName = (p) => String(p || '');
global.calcCostCny = () => ({ costCny: 0 });
// computeTurnSettlement / finishLabelForSettlement / _featureFlags stay
// undefined — the function typeof-guards all three.

const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src);   // defines renderFinishInfo

if (typeof renderFinishInfo !== 'function') {
  console.log('FAIL fn_exposed renderFinishInfo missing'); process.exit(0);
}
check('fn_exposed', true);

// 1. THE FIX: an interrupted stub (model only, no finishReason/usage)
//    rendered statically must NOT show the bogus model-only "finished" bar.
check('interrupted_manual_suppressed',
      renderFinishInfo({ model: 'kimi-k3', interruptedReason: 'manual' }, false) === '');
check('interrupted_killed_suppressed',
      renderFinishInfo({ model: 'kimi-k3', interruptedReason: 'killed' }, false) === '');

// 2. The deliberate legacy contract: a FINISHED-but-model-only message (no
//    interruptedReason, not the live tail) KEEPS its bar.
check('legacy_model_only_keeps_bar',
      renderFinishInfo({ model: 'kimi-k3' }, false) !== '');

// 3. Pre-existing: the live streaming tail stays suppressed.
check('live_tail_suppressed',
      renderFinishInfo({ model: 'kimi-k3' }, true) === '');

// 4. A settled turn (finishReason + usage) renders normally.
check('settled_renders',
      renderFinishInfo(
        { model: 'kimi-k3', finishReason: 'stop',
          usage: { prompt_tokens: 10, completion_tokens: 5 } },
        false) !== '');

console.log(out.join('\n'));
"""


def _run(tag: str, transform=None) -> str:
    with open(JS_FILE, encoding='utf-8') as f:
        fn = _extract(f.read())
    if transform is not None:
        fn = transform(fn)
    fn_copy = os.path.join(HERE, f'_finfo_fn_{tag}.js')
    harness = os.path.join(HERE, f'_finfo_harness_{tag}.js')
    with open(fn_copy, 'w', encoding='utf-8') as f:
        f.write(fn)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, fn_copy],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (fn_copy, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_finish_info_suppresses_bar_on_interrupted_stub():
    out = _run('real')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'finish-info interrupted-stub guard failures:\n' + out


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_finish_info_neuter_drops_interrupted_guard():
    """NEUTER: drop ``|| msg.interruptedReason`` from the guard → the
    interrupted stub renders the bogus model-only bar again."""
    anchor = 'if (!_terminal && (isLiveTail || msg.interruptedReason)) return "";'
    with open(JS_FILE, encoding='utf-8') as f:
        shipped = f.read()
    assert anchor in shipped, 'NC anchor drifted — the guard no longer looks like the pinned shape'

    def _neuter(fn: str) -> str:
        return fn.replace(anchor, 'if (!_terminal && isLiveTail) return "";', 1)

    out = _run('neuter', _neuter)
    assert 'FAIL interrupted_manual_suppressed' in out, (
        'NC did not bite: dropping the interruptedReason guard still suppressed '
        'the bar — the test does not pin the fix:\n' + out)
    # The legacy-keep control must survive the neuter (the guard only ever
    # widened suppression for interrupted stubs).
    assert 'PASS legacy_model_only_keeps_bar' in out, out
