"""Subscription-quota footer facts belong to the calls that returned them.

A turn can mix routes: the main model on a metered API key plus an internal
compaction call on a ChatGPT (Codex) subscription.  Only the subscription
call's response carries ``usage._subscription_quota``.  The finish row must
name that call and count only its tokens, instead of presenting the
subscription as if it had served the whole turn ("订阅 · 7 天余99% …
本轮 6.4m tok" on a turn whose only subscription traffic was a 17k-token
compaction summary).  A turn served entirely by the subscription, and rows
whose snapshot exists only on the turn usage, keep the unattributed form.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section
from tests._jsdom import require_node_modules


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
ZH_LOCALE = ROOT / 'frontend/src/i18n/locales/zh.json'

_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const copy = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
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

const quota = () => ({
  provider: 'codex', source: 'response_headers', plan_type: 'pro',
  primary: { window_minutes: 10080, used_percent: 1, remaining_percent: 99,
             has_previous_snapshot: true, observed_delta_percent: 0 },
  secondary: { used_percent: 0, remaining_percent: 100,
               has_previous_snapshot: true, observed_delta_percent: 0 },
});
const meteredRound = (round) => ({
  round, model: 'claude-opus-5.5',
  usage: { prompt_tokens: 166000, completion_tokens: 2300,
           _dispatch: { model: 'kunlunbedrock.claude-opus-5.5-m17',
                        provider_id: 'example-corp', key_tail: '4861' } },
});
const subscriptionRound = (round, prompt, completion) => ({
  round, model: 'gpt-6.1-sol',
  usage: { prompt_tokens: prompt, completion_tokens: completion,
           _dispatch: { model: 'gpt-6.1-sol', provider_id: 'oauth_codex' },
           _subscription_quota: quota() },
});
const turn = (apiRounds, usage) => ({
  _turnStatus: 'completed', _turnSettlement: {},
  model: 'claude-opus-5.5', providerId: 'example-corp',
  usage: usage || { prompt_tokens: 6316908, completion_tokens: 89470 },
  apiRounds,
});

const meteredWithSubscriptionCompaction = [];
for (let i = 1; i <= 38; i += 1)
  meteredWithSubscriptionCompaction.push(meteredRound(i));
meteredWithSubscriptionCompaction.push({
  round: 'compaction-1', kind: 'compaction', model: 'gpt-6-luna',
  provider_id: 'oauth_codex', tag: 'COMPACTION-L2',
  usage: { prompt_tokens: 16018, completion_tokens: 1151, total_tokens: 17169,
           _dispatch: { model: 'gpt-6-luna', provider_id: 'oauth_codex' },
           _subscription_quota: quota() },
});

const subscriptionWithMeteredCompaction = [
  subscriptionRound(1, 40000, 900),
  subscriptionRound(2, 52000, 1100),
  { round: 'compaction-1', kind: 'compaction', model: 'claude-opus-5.5',
    provider_id: 'example-corp', tag: 'COMPACTION-L2',
    usage: { prompt_tokens: 30000, completion_tokens: 800 } },
];

const wholeTurnOnSubscription = [
  subscriptionRound(1, 50000, 1000),
  // A failed attempt with no tokens is not a non-subscription call.
  { round: 2, model: 'gpt-6.1-sol', usage: {} },
  subscriptionRound(3, 60000, 2000),
];

const turnUsageOnlySnapshot = [
  { round: 1, model: 'gpt-6.1-sol',
    usage: { prompt_tokens: 40000, completion_tokens: 1000 } },
  { round: 2, model: 'gpt-6.1-sol',
    usage: { prompt_tokens: 50000, completion_tokens: 1500 } },
];

console.log(JSON.stringify({
  meteredWithSubscriptionCompaction: renderFinishInfo(
    turn(meteredWithSubscriptionCompaction), false),
  subscriptionWithMeteredCompaction: renderFinishInfo(
    turn(subscriptionWithMeteredCompaction), false),
  wholeTurnOnSubscription: renderFinishInfo(
    turn(wholeTurnOnSubscription), false),
  turnUsageOnlySnapshot: renderFinishInfo(turn(turnUsageOnlySnapshot, {
    prompt_tokens: 90000, completion_tokens: 2500,
    _subscription_quota: quota(),
  }), false),
}));
"""


def _quota_tags(html: str) -> list[str]:
    return re.findall(r'class="subscription-quota-tag" title="[^"]*">([^<]*)</span>', html)


def _quota_title(html: str) -> str:
    titles = set(re.findall(r'class="subscription-quota-tag" title="([^"]*)"', html))
    assert len(titles) == 1
    return titles.pop()


@pytest.fixture(scope='module')
def rendered(tmp_path_factory):
    require_node_modules('vite')
    tmp_path = tmp_path_factory.mktemp('finish-quota-attribution')
    finish_owner = runtime_section('ui/finish_info.js')
    fn_start = finish_owner.index('function _quotaPct(value) {')
    fn_end_marker = (
        '  if (parts.length === 0) return "";\n'
        '  return `<div class="message-finish">${parts.join("")}</div>`;\n'
        '}'
    )
    fn_end = finish_owner.index(fn_end_marker, fn_start) + len(fn_end_marker)
    source_path = tmp_path / 'finish-quota.js'
    token_usage = Path(native_module_path(
        '.native/token-usage.js', ROOT / 'frontend/src/token-usage.ts')).read_text()
    source_path.write_text(
        token_usage + '\n' + finish_owner[fn_start:fn_end], encoding='utf-8')
    harness = tmp_path / 'finish-quota-attribution-harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    run = subprocess.run(
        [shutil.which('node'), str(harness), str(source_path), str(ZH_LOCALE)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_metered_turn_names_the_subscription_compaction_call(rendered):
    html = rendered['meteredWithSubscriptionCompaction']
    assert _quota_tags(html) == [
        '订阅（内部压缩 gpt-6-luna） · 7 天余99%',
        '当前周期余100%',
        '订阅调用 17.2k tok',
        '本轮未跨额度刻度',
    ]
    title = _quota_title(html)
    assert '额度读数来自本轮 1 次订阅调用（内部压缩 gpt-6-luna）' in title
    assert '其余 38 次调用没有返回订阅额度读数' in title
    assert '本轮精确合计' not in title


def test_subscription_main_rounds_exclude_metered_compaction_tokens(rendered):
    html = rendered['subscriptionWithMeteredCompaction']
    assert _quota_tags(html) == [
        '订阅（gpt-6.1-sol） · 7 天余99%',
        '当前周期余100%',
        '订阅调用 94.0k tok',
        '本轮未跨额度刻度',
    ]
    assert '额度读数来自本轮 2 次订阅调用（gpt-6.1-sol）' in _quota_title(html)


def test_turn_served_by_subscription_keeps_unattributed_prefix(rendered):
    html = rendered['wholeTurnOnSubscription']
    assert _quota_tags(html) == [
        '订阅 · 7 天余99%',
        '当前周期余100%',
        '本轮 113.0k tok',
        '本轮未跨额度刻度',
    ]
    title = _quota_title(html)
    assert '本轮精确合计 113.0k token' in title
    assert '额度读数来自' not in title


def test_turn_usage_snapshot_cannot_be_split_by_call(rendered):
    html = rendered['turnUsageOnlySnapshot']
    assert _quota_tags(html) == [
        '订阅 · 7 天余99%',
        '当前周期余100%',
        '本轮 92.5k tok',
        '本轮未跨额度刻度',
    ]
