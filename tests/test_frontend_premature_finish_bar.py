"""tests/test_frontend_premature_finish_bar.py — regression for the
premature/incomplete "finish tag" that appears on an UNFINISHED assistant
message rendered statically (poor-signal / cross-device reload before the SSE
stream reconnects).

WHY
---
`renderFinishInfo(msg)` (static/js/ui/finish_info.js) USED TO emit a finish bar
whenever ANY of finishReason / usage / model / preset / effort was truthy. But
the backend mid-stream checkpoint (`_sync_partial_to_conversation`,
lib/tasks_pkg/manager.py) DELIBERATELY persists `model` while WITHHOLDING the
terminal-only fields (finishReason / usage / toolSummary) until the task
completes. So a still-running assistant message — persisted mid-stream, then
fetched by another device or re-rendered before SSE reconnects — carries
`model` but no finishReason/usage, and the static `renderMessage()` path drew a
bogus "finished" bar showing ONLY the model tag while the turn was still live.

THE FIX
-------
renderFinishInfo now takes a second arg `isLiveTail`. It emits the bar whenever
a TERMINAL signal (finishReason || usage) is present. A model-only message (no
terminal signal) renders NO bar ONLY when it is the live running tail
(isLiveTail true, computed at the single call site in chat_render.js as
"last message of a conv with an active stream / activeTaskId"). A
finished-but-model-only message that is NOT the live tail — a legacy message
persisted before usage/cost-persistence, or a degenerate empty completion whose
finishReason came back None — is NOT the live tail, so it KEEPS its bar (no
regression).

This harness loads the REAL shipped `finish_info.js`, extracts the
`renderFinishInfo` function body via a brace-matched slice, evals it with the
cost/format helpers stubbed, and drives it with each message shape:
  • model-only + isLiveTail   → "" (suppressed — the bug case)
  • model-only + NOT live tail → bar (legacy / degenerate-done — no regression)
  • finishReason present       → bar (the ✓ case)
  • usage present (no model)   → bar (token bar)
  • nothing at all             → "" (unchanged)

SOURCE-LEVEL DOUBLE-NEUTER (on a MUTATED copy; the shipped file is untouched):
  • Remove the `if (!_terminal && isLiveTail) return "";` guard → the
    model-only-on-live-tail case now EMITS a bar (the premature-bar bug
    reproduces), proving the guard is load-bearing.
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
FINISH_INFO = os.path.join(JS_DIR, 'ui', 'finish_info.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    """Return the full `function <name>(...) { ... }` source via brace match."""
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\(', src)
    assert m, f'{name} not found in source'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Stub every helper renderFinishInfo may reference in its branches.
//    The guard under test runs BEFORE any of these, but they must exist so
//    the function body evals and the bar-emitting branches don't throw. ──
global.escapeHtml = (s) => String(s == null ? '' : s);
global.calcCostCny = () => null;
global.calcCostUsd = () => null;
global.t = (k) => k;
global._brandFromModelId = () => '';
global._modelBrandIcon = () => '';
global.formatClockTime = () => '';
global._fmtAbsoluteDateTime = () => '';
global.MODEL_PRICING = {};
global.config = {};

const FN = fs.readFileSync(process.argv[2], 'utf8');  // extracted renderFinishInfo source

function loadFn(fnSrc) { (0, eval)(fnSrc); }

function isEmptyBar(html) { return html === '' || html == null; }

(async () => {
  loadFn(FN);
  if (typeof renderFinishInfo !== 'function') {
    console.log('FAIL fn_exposed renderFinishInfo missing'); process.exit(0);
  }
  check('fn_exposed', true);
  check('fn_arity_2', renderFinishInfo.length === 2);

  // ══ 1. THE BUG CASE: model-only + isLiveTail → suppressed ══
  {
    const html = renderFinishInfo({ model: 'claude-opus-4' }, true);
    check('modelonly_livetail_suppressed', isEmptyBar(html));
  }
  {
    const html = renderFinishInfo({ preset: 'opus' }, true);
    check('presetonly_livetail_suppressed', isEmptyBar(html));
  }
  {
    const html = renderFinishInfo({ effort: 'high' }, true);
    check('effortonly_livetail_suppressed', isEmptyBar(html));
  }

  // ══ 2. NO REGRESSION: model-only but NOT the live tail → bar kept ══
  //    (legacy pre-usage-persistence message, or a degenerate empty completion)
  {
    const html = renderFinishInfo({ model: 'claude-opus-4' }, false);
    check('modelonly_not_livetail_keeps_bar', !isEmptyBar(html) && html.length > 0);
  }
  {
    // isLiveTail omitted (undefined) → also NOT live → keep bar.
    const html = renderFinishInfo({ model: 'claude-opus-4' });
    check('modelonly_undefined_livetail_keeps_bar', !isEmptyBar(html));
  }

  // ══ 3. FINISHED (finishReason) → bar even on the live tail ══
  {
    const html = renderFinishInfo({ model: 'x', finishReason: 'stop' }, true);
    check('finishreason_livetail_keeps_bar', !isEmptyBar(html));
  }
  {
    const html = renderFinishInfo({ finishReason: 'stop' }, false);
    check('finishreason_only_keeps_bar', !isEmptyBar(html));
  }

  // ══ 4. FINISHED (usage only, no model) → token bar even on live tail ══
  {
    const html = renderFinishInfo({ usage: { input_tokens: 10, output_tokens: 5 } }, true);
    check('usageonly_livetail_keeps_bar', !isEmptyBar(html));
  }

  // ══ 5. NOTHING AT ALL → "" (unchanged) ══
  {
    check('empty_msg_no_bar', isEmptyBar(renderFinishInfo({}, false)));
    check('empty_msg_livetail_no_bar', isEmptyBar(renderFinishInfo({}, true)));
  }

  // ══ 6. DOUBLE-NEUTER: strip the live-tail guard → the bug reproduces ══
  {
    const GUARD = 'if (!_terminal && isLiveTail) return "";';
    const neutered = FN.replace(GUARD, '/* NEUTERED live-tail guard */');
    check('neuter_patch_applied', neutered !== FN);
    loadFn(neutered);
    // model-only + isLiveTail would NOW emit a bar (premature-finish-bar bug).
    const html = renderFinishInfo({ model: 'claude-opus-4' }, true);
    check('neuter_emits_premature_bar', !isEmptyBar(html));
    // restore the real fn for good measure
    loadFn(FN);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_premature_finish_bar_guard():
    src = open(FINISH_INFO, encoding='utf-8').read()
    fn_src = _extract_fn(src, 'renderFinishInfo')
    # Sanity: the guard we neuter must be present in the extracted body.
    assert 'if (!_terminal && isLiveTail) return "";' in fn_src, \
        'live-tail guard missing from renderFinishInfo — test is stale'

    harness = os.path.join(HERE, '_premature_finish_bar_harness.js')
    fn_file = os.path.join(HERE, '_premature_finish_bar_fn.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    with open(fn_file, 'w') as f:
        f.write(fn_src)
    try:
        proc = subprocess.run(
            ['node', harness, fn_file],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (harness, fn_file):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'premature-finish-bar guard failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS lines, got:\n{output}'
