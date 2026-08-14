#!/usr/bin/env python3
"""tests/test_frontend_finish_tag_fallback.py — pin the finish-tag's robust
legacy fallback in renderFinishInfo.

Epic pt_a4484f3ad3134ea8. The finish_info.js finish-tag is now verdict-driven
(via computeTurnSettlement + finishLabelForSettlement) — but core/turn_
settlement.js is a BUNDLE-ONLY module (no index.html script tag, matching
core/conv_state_reducer.js). So in any context that loads finish_info.js
WITHOUT turn_settlement.js (the dev-mode script-tag fallback when bundling
fails, or a JSDOM harness that loads finish_info.js alone), the verdict
functions are absent. finish_info must therefore GRACEFULLY DEGRADE to a
legacy kind-derivation so the finish-tag renders correctly in EVERY context.

This harness loads the REAL shipped finish_info.js WITHOUT turn_settlement.js
(stubbing window/document/i18n/icons) and drives renderFinishInfo for the key
finishReasons, asserting the finish-tag HTML carries the right label + class —
proving the fallback produces the SAME labels as the verdict path.

Run standalone:  python3 tests/test_frontend_finish_tag_fallback.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests._runtime_sections import runtime_section_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
FINISH_INFO = runtime_section_path('ui/finish_info.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// NOTE: turn_settlement.js is DELIBERATELY NOT loaded — forces the legacy
// kind-derivation fallback in the finish-tag block.
global.window = {};
global.document = {};
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// t() returns the KEY so we can assert which label was chosen.
global.t = (k, o) => {
  o = o || {};
  if (k && k.indexOf('{') !== -1 && o && Object.keys(o).length) {
    let s = k; for (const kk in o) s = s.replace('{' + kk + '}', o[kk]); return s;
  }
  return k;
};
global.Icon = (name, size) => '<ICON:' + name + '>';
global.formatCny = (n) => '¥' + Number(n || 0).toFixed(4);
global.calcCostCny = () => 0.01;
global.calcCost = () => 0.01;
eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
// msg with ONLY finishReason → model/route/usage tags skip, the finish-tag runs.
const tag = (fr, ir) => {
  const m = { role: 'assistant', finishReason: fr, content: 'partial', toolRounds: [] };
  if (ir) m.interruptedReason = ir;
  return renderFinishInfo(m, false);
};

let h;
h = tag('stop');
check('stop_ok_class', h.indexOf('finish-tag ok') !== -1);
check('stop_ok_mark', h.indexOf('✓') !== -1);

h = tag('error');
check('error_class', h.indexOf('finish-tag err') !== -1);
check('error_label', h.indexOf('finishInfo.reasonError') !== -1);

h = tag('aborted');
check('aborted_warn', h.indexOf('finish-tag warn') !== -1);
check('aborted_label', h.indexOf('finishInfo.reasonStopped') !== -1);

h = tag('interrupted', 'killed');
check('int_killed_label', h.indexOf('finishInfo.reasonInterruptedKilled') !== -1);
check('int_killed_not_restart', h.indexOf('finishInfo.reasonInterruptedRestart') === -1);

h = tag('interrupted', 'manual');
check('int_restart_label', h.indexOf('finishInfo.reasonInterruptedRestart') !== -1);

h = tag('interrupted');  // absent interruptedReason → honest UNKNOWN, not restart
check('int_unknown_label', h.indexOf('finishInfo.reasonInterruptedUnknown') !== -1);
check('int_unknown_not_restart', h.indexOf('finishInfo.reasonInterruptedRestart') === -1);

h = tag('server_offline');
check('offline_err', h.indexOf('finish-tag err') !== -1);
check('offline_label', h.indexOf('finishInfo.reasonServerOffline') !== -1);
check('offline_reconnect_btn', h.indexOf('finish-reconnect-btn') !== -1);

h = tag('incomplete');
check('incomplete_label', h.indexOf('finishInfo.reasonIncomplete') !== -1);
check('incomplete_icon', h.indexOf('<ICON:alertTriangle>') !== -1);

h = tag('length');
check('length_warn', h.indexOf('finish-tag warn') !== -1);
check('length_label', h.indexOf('finishInfo.reasonTruncated') !== -1);

h = tag('tool_rounds_exhausted');
check('toollimit_label', h.indexOf('finishInfo.reasonToolLimit') !== -1);

h = tag('premature_close');
check('gateway_label', h.indexOf('msg.gatewayInterrupt') !== -1);

h = tag('abnormal_stop');
check('abnormal_label', h.indexOf('msg.abnormalInterrupt') !== -1);

console.log(out.join('\n'));
"""


def _run() -> str:
    harness = os.path.join(HERE, '_finish_tag_fallback_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, FINISH_INFO],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_finish_tag_legacy_fallback_renders_all_labels():
    """renderFinishInfo WITHOUT turn_settlement.js must still render the
    finish-tag correctly via the legacy kind-derivation fallback — the
    robustness guarantee for dev-mode / JSDOM contexts."""
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'finish-tag fallback render failures:\n' + output
    assert output.count('PASS') >= 20, f'expected >=20 PASS, got:\n{output}'
    print(output)


if __name__ == '__main__':
    print(_run())
