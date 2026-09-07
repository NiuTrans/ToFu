"""Subscription-quota footer facts wrap at fact granularity, not as one blob.

The finish row (``.message-finish``) is a flex row whose children are all
``white-space: nowrap``.  Flex-wrap can only break BETWEEN items, so when the
whole quota telemetry was one joined span (``订阅 · 7 天余61% · 当前周期余100%
· 本轮 97.3k tok · 本轮未跨额度刻度``) it dropped whole to the next line
whenever it missed the remaining space — stranding half of line 1 empty.
Each fact must be its own flex item so the row packs greedily and wraps one
fact at a time.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_quota_facts_render_as_individual_wrappable_items(tmp_path):
    finish_owner = runtime_section('ui/finish_info.js')
    fn_start = finish_owner.index('function _quotaPct(value) {')
    fn_end_marker = (
        '  if (parts.length === 0) return "";\n'
        '  return `<div class="message-finish">${parts.join("")}</div>`;\n'
        '}'
    )
    fn_end = finish_owner.index(fn_end_marker, fn_start) + len(fn_end_marker)
    source_path = tmp_path / 'finish-quota.js'
    source_path.write_text(finish_owner[fn_start:fn_end], encoding='utf-8')

    harness = tmp_path / 'finish-quota-harness.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const copy = {
  'quota.window5h': '5 小时',
  'quota.window7d': '7 天',
  'quota.windowDays': '{n} 天',
  'quota.windowHours': '{n} 小时',
  'quota.windowMinutes': '{n} 分钟',
  'quota.windowUnknown': '当前周期',
  'finishInfo.quotaPrefix': '订阅',
  'finishInfo.quotaRemainingShort': '{window}余{remaining}%',
  'finishInfo.quotaWindowDetail': '{window}周期：已用 {used}%，剩余 {remaining}%',
  'finishInfo.quotaObservedNoTick': '本轮未跨额度刻度',
  'finishInfo.quotaObservedDelta': '本轮观测约 {delta}%',
  'finishInfo.quotaTurnTokens': '本轮 {tokens} tok',
  'finishInfo.quotaTurnTokensDetail': '本轮精确合计 {tokens} token',
  'finishInfo.quotaCaveat': 'quota caveat',
};
global.t = (key, params) => {
  let value = Object.hasOwn(copy, key) ? copy[key] : key;
  for (const [name, replacement] of Object.entries(params || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
global.Icon = () => '';
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global._isThinkingCapable = () => false;
global._providerDisplayName = (value) => String(value || '');
global.calcCostCny = () => ({ costCny: 0 });
global.ConversationTurnStore = { finishPresentation: () => null };

eval(fs.readFileSync(process.argv[2], 'utf8'));

const win = (over) => ({
  window_minutes: 10080, remaining_percent: 61, used_percent: 39,
  has_previous_snapshot: true, observed_delta_percent: 0, ...over,
});
const msg = (primary, secondary) => ({
  _turnStatus: 'completed', _turnSettlement: {}, model: 'gpt-5.6-sol',
  usage: {
    prompt_tokens: 97300, completion_tokens: 1600,
    _subscription_quota: { primary, secondary },
  },
});
const full = renderFinishInfo(
  msg(win({}), win({ window_minutes: 300, remaining_percent: 100,
                     used_percent: 0 })), false);
const delta = renderFinishInfo(
  msg(win({ observed_delta_percent: 2.5 }),
      win({ window_minutes: 300, remaining_percent: 100, used_percent: 0 })),
  false);
const single = renderFinishInfo(msg(win({}), null), false);
console.log(JSON.stringify({ full, delta, single }));
""", encoding='utf-8')

    run = subprocess.run(
        [shutil.which('node'), str(harness), str(source_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    rendered = json.loads(run.stdout.strip().splitlines()[-1])

    full = rendered['full']
    # Every fact is its own flex item — the row can pack them greedily and
    # wrap one fact at a time instead of dropping a half-row-wide blob.
    assert full.count('class="subscription-quota-tag"') == 4
    assert '>订阅 · 7 天余61%</span>' in full          # prefix rides fact 1
    assert '>5 小时余100%</span>' in full
    assert '>本轮 98.9k tok</span>' in full
    assert '>本轮未跨额度刻度</span>' in full
    # The monolithic joined blob is gone for good.
    assert '订阅 · 7 天余61% · 5 小时余100%' not in full
    # Every fact keeps the same full detail tooltip.
    titles = re.findall(r'class="subscription-quota-tag" title="([^"]*)"', full)
    assert len(titles) == 4 and len(set(titles)) == 1
    assert '7 天周期：已用 39%，剩余 61%' in titles[0]
    assert 'quota caveat' in titles[0]

    delta = rendered['delta']
    assert delta.count('class="subscription-quota-tag"') == 4
    assert '>本轮观测约 2.5%</span>' in delta

    single = rendered['single']
    assert single.count('class="subscription-quota-tag"') == 3
    assert '>订阅 · 7 天余61%</span>' in single
