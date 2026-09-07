"""jsdom regression: the swarm panel's per-agent phase pill localizes the
'retrying' phase (pt_18ebee9c9ea64cf3).

Before the fix, ``phaseMap`` in ``static/js/ui/streaming_swarm_panel.js`` had
NO 'retrying' entry, so a retrying sub-agent card fell through to the raw
English phase string — the panel-side half of the raw-English retry leak
family (the bubble-side half is covered by tests/test_stream_phase_i18n.py
and the wire fields by tests/test_swarm_retry_phase_i18n.py).

This harness loads the REAL shipped ``streaming_swarm_panel.js`` under jsdom
with a stub ``t()`` carrying the zh ``swarm.phase.*`` strings, drives the
REAL ``_buildSwarmPanelHTML`` on a round containing a retrying agent, and
asserts the pill renders the localized label — never the raw token.

NEUTER (scratch copy, shipped file byte-identical after): drop the
``retrying:`` phaseMap line → the raw English 'retrying' pill leaks back,
proving the line is load-bearing.
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness

pytestmark = pytest.mark.unit


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[2]],
  globals: {
    /* Minimal zh table for the swarm.phase.* family — the REAL i18n.js key
       existence is pinned python-side (test_i18n_key_registered). */
    t: (k) => ({
      'swarm.phase.thinking': '思考中…',
      'swarm.phase.tool_use': '调用工具中',
      'swarm.phase.writing': '撰写中…',
      'swarm.phase.searching': '搜索中…',
      'swarm.phase.coding': '编码中…',
      'swarm.phase.analyzing': '分析中…',
      'swarm.phase.complete': '完成',
      'swarm.phase.failed': '失败',
      'swarm.phase.error': '错误',
      'swarm.phase.queued': '排队中',
      'swarm.phase.running': '工作中…',
      'swarm.phase.retrying': '重试中…',
      'swarm.phase.noResult': '无结果',
    })[k] || k,
    escapeHtml: (s) => String(s == null ? '' : s),
    conversations: [],
    activeConvId: null,
  },
});

if (typeof _buildSwarmPanelHTML !== 'function') {
  console.log('FAIL entry_exposed _buildSwarmPanelHTML missing');
  report();
  return;
}
check('entry_exposed', true);

const round = {
  _swarm: true, _swarmActive: true, _swarmStartTime: Date.now(),
  _swarmAgents: [
    { id: 'a1', role: 'coder', model: 'kimi-k3', objective: 'do x',
      status: 'running', phase: 'retrying', preview: '', tools: [] },
    { id: 'a2', role: 'general', model: 'kimi-k3', objective: 'do y',
      status: 'running', phase: 'thinking', preview: '', tools: [] },
    /* status/phase desync guard: a DONE agent whose phase got stranded at
       'retrying' must show the terminal label, not a retry pill. */
    { id: 'a3', role: 'general', model: 'kimi-k3', objective: 'do z',
      status: 'done', phase: 'retrying', preview: 'finished', tools: [] },
  ],
};

const html = _buildSwarmPanelHTML(round, [round]);

// (1) retrying agent card renders the LOCALIZED retry label
check('retry_pill_localized', html.includes('重试中…'), html.slice(0, 400));
// (2) the raw English phase token never reaches the pill
check('retry_pill_no_raw_english', !html.includes('>retrying<'), html.slice(0, 400));
// (3) a neighbouring phase still localizes (map not broken)
check('thinking_pill_localized', html.includes('思考中…'), html.slice(0, 400));
// (4) status/phase desync: DONE + stranded retrying phase → terminal label
//     wins (the done agent shows 完成; exactly ONE retry pill remains).
check('done_agent_shows_complete', html.includes('完成'), html.slice(0, 400));
check('exactly_one_retry_pill',
  html.split('重试中…').length - 1 === 1, String(html.split('重试中…').length - 1));

report();
"""


def test_swarm_retry_pill_localized():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),
        body_js=_BODY,
        min_pass=6,
        label='swarm retry pill',
    )


# NEUTER body: with the phaseMap 'retrying:' line dropped, the pill MUST fall
# back to the raw English token — expressed as PASSes (run_harness treats any
# FAIL line as a hard error).
_NEUTER_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: {
    t: (k) => ({ 'swarm.phase.retrying': '重试中…', 'swarm.phase.complete': '完成' })[k] || k,
    escapeHtml: (s) => String(s == null ? '' : s),
    conversations: [],
    activeConvId: null,
  },
});
const round = {
  _swarm: true, _swarmActive: true, _swarmStartTime: Date.now(),
  _swarmAgents: [
    { id: 'a1', role: 'coder', model: 'kimi-k3', objective: 'do x',
      status: 'running', phase: 'retrying', preview: '', tools: [] },
  ],
};
const html = _buildSwarmPanelHTML(round, [round]);
// Under NEUTER the phaseMap entry is gone → raw English leaks back …
check('NC_raw_english_leaks', html.includes('>retrying<'), html.slice(0, 400));
// … and the localized label is GONE.
check('NC_zh_label_absent', !html.includes('重试中…'), html.slice(0, 400));
report();
"""


def test_NC_retry_pill_phasemap_line_is_load_bearing(tmp_path):
    """NEUTER: remove the ``retrying:`` phaseMap line from a SCRATCH copy of
    streaming_swarm_panel.js → the raw English 'retrying' pill leaks back.
    Shipped file is byte-identical afterwards."""
    src = os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')
    with open(src, encoding='utf-8') as f:
        original = f.read()
    anchor = '        retrying: t("swarm.phase.retrying"),\n'
    assert anchor in original, 'retrying phaseMap anchor missing'
    patched = original.replace(anchor, '', 1)
    assert patched != original, 'NC patch did not apply'
    nc_path = tmp_path / 'streaming_swarm_panel_nc.js'
    nc_path.write_text(patched, encoding='utf-8')
    try:
        out = run_harness(
            target_js=str(nc_path),
            body_js=_NEUTER_BODY,
            min_pass=2,
            label='swarm retry pill NEUTER',
        )
        assert 'PASS NC_raw_english_leaks' in out, out
        assert 'PASS NC_zh_label_absent' in out, out
    finally:
        with open(src, encoding='utf-8') as f:
            assert f.read() == original, 'shipped streaming_swarm_panel.js must be byte-identical'


def test_i18n_key_registered():
    """The REAL i18n tables carry the new key in both languages (the harness
    stubs t(), so pin the shipped tables statically). Post-Vite the tables
    are the locale JSONs under frontend/src/i18n/locales/."""
    locales = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales')
    with open(os.path.join(locales, 'zh.json'), encoding='utf-8') as f:
        zh = json.load(f)
    with open(os.path.join(locales, 'en.json'), encoding='utf-8') as f:
        en = json.load(f)
    assert zh['swarm.phase.retrying'] == '重试中…'
    assert en['swarm.phase.retrying'] == 'Retrying…'


def test_phasemap_consumes_the_key():
    with open(os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'retrying: t("swarm.phase.retrying"),' in src


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
