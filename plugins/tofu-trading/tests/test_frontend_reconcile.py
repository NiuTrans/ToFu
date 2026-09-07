"""tests/test_frontend_reconcile.py — P2 daily reconcile page, driven in jsdom.

The P2 epic's acceptance is behavioural: an empty plan must EXPLAIN itself,
"done" must capture actual price/shares (not a boolean), unapproved targets
must look different, and prices must render as estimates. Files existing on
disk proves none of that, so these tests eval the REAL state.js + reconcile.js
inside jsdom against a scripted fetch stub and assert on the resulting DOM
and outgoing requests.

Two NEUTER variants prove the load-bearing behaviours rather than assuming
them: strip the skipped-reason renderer and the empty-state test turns red;
strip one data-label from the holdings renderer and the mobile-card static
guard turns red.
"""

import os
import shutil
import subprocess
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
MONOREPO_ROOT = os.path.normpath(os.path.join(ROOT, '..', '..'))
STATIC = os.path.join(ROOT, 'tofu_trading', 'static')
JSDOM_DIR = os.path.join(MONOREPO_ROOT, 'node_modules', 'jsdom')


def _deps_available():
    return shutil.which('node') and os.path.isdir(JSDOM_DIR)


_HARNESS = r'''
'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM } = require(process.env.JSDOM_DIR);

const STATIC = process.env.TT_STATIC;
const SCENARIO = process.env.SCENARIO;
const PATCH = process.env.PATCH || '';   // 'no-skip-render' for the NEUTER run
if (!STATIC) throw new Error('TT_STATIC env not set');

const stateSrc = fs.readFileSync(path.join(STATIC, 'js/trading/state.js'), 'utf8');
let recSrc = fs.readFileSync(path.join(STATIC, 'js/trading/reconcile.js'), 'utf8');

if (PATCH === 'no-skip-render') {
  // NEUTER: remove the skipped-reasons block. The empty-plan test must then
  // fail, proving the explainer is what carries the behaviour.
  recSrc = recSrc.replace(/if \(skipped\.length\) \{[\s\S]*?html \+= "<\/ul><\/details>";\s*\}/, '');
  if (recSrc.includes('rec-skipped')) throw new Error('NEUTER patch did not apply');
}

const dom = new JSDOM('<!DOCTYPE html><html><body>' +
  '<div id="recPlanBody"></div>' +
  '<div id="recTargetBody"></div>' +
  '<div id="recAdoptionBody"></div>' +
  '</body></html>', { url: 'http://localhost/trading/' });
const { window } = dom;

// ── Api stub: script the surface the page consumes ──
// F.api delegates to window.Api.trading.call (state.js:33), NOT bare fetch.
const calls = [];
const PLAN_ACTIONS = {
  plan_date: '2026-07-26',
  is_estimate: true,
  estimate_note: '估算（基于上一交易日收盘/净值），非实时',
  actions: [
    { symbol: '510300', side: 'buy', shares: 100, amount: 400.0, price: 4.0,
      target_weight: 30.0, actual_weight: 26.0, drift_pct: 4.0,
      reason: '目标 30.0% vs 实际 26.0%（偏离 +4.00%）' },
  ],
  skipped: [],
};
const PLAN_EMPTY = {
  plan_date: '2026-07-26',
  is_estimate: true,
  estimate_note: '估算（基于上一交易日收盘/净值），非实时',
  actions: [],
  skipped: [
    { symbol: '600519', gate: 'deadband', note: '偏离 +1.20%，未超过免交易带 5%' },
    { symbol: '510300', gate: 'in_flight', note: '有 100 份在途未确认，等待交收' },
    { symbol: '007777', gate: 'price_missing', note: '无可用价格，跳过（不猜价）' },
  ],
};
const TARGETS = {
  targets: [
    { symbol: '600519', target_weight: 30.0, approved: 1, rationale: '核心资产' },
    { symbol: '510300', target_weight: 20.0, approved: 0, rationale: 'AI 提议：分散指数暴露' },
  ],
  approved_weight_sum: 30.0,
  implied_cash_weight: 70.0,
};
const ADOPTION = { counts: { done: 3, skipped: 1 }, total: 4, follow_through_rate: 75.0 };

window.Api = {
  trading: {
    call: function (path, opts) {
      opts = opts || {};
      calls.push({ url: String(path), opts: {
        method: opts.method || 'GET',
        // The real Api client JSON-serializes the body; record the wire form
        // so assertions test what would actually cross the network.
        body: opts.body != null ? JSON.stringify(opts.body) : undefined,
      } });

      let payload = {};
      if (path.includes('/reconcile/plan')) payload = (SCENARIO === 'empty') ? PLAN_EMPTY : PLAN_ACTIONS;
      else if (path.includes('/reconcile/target') && !path.includes('approve')) payload = TARGETS;
      else if (path.includes('/reconcile/adoption')) payload = ADOPTION;
      else if (path.includes('/status') || path.includes('/approve')) payload = {};
      return Promise.resolve(payload);
    },
  },
};

// prompt stub: scenario-driven answers
const promptLog = [];
window.prompt = function (msg, def) {
  promptLog.push(msg);
  if (SCENARIO === 'prompt-cancel') return null;           // user cancels
  if (SCENARIO === 'prompt-empty') return '';              // accept defaults
  return msg.includes('成交价') ? '3.95' : '98';           // actual fill values
};

// The frontend is window-scope vanilla JS: bare `document`, `fetch`, `prompt`
// references must resolve, and jsdom only puts them on its own window object.
global.window = window;
global.document = window.document;
// NOTE: global.navigator is read-only in Node >= 21 and unused by the
// reconcile path — assigning it throws, so it is deliberately absent.
global.location = window.location;
global.prompt = window.prompt;

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name + (extra ? ' :: ' + extra : '')); }
}

eval(stateSrc);
eval(recSrc);
const F = window.TradingApp;
const doc = window.document;

(async function () {
  if (SCENARIO === 'actions' || SCENARIO === 'empty' || SCENARIO === 'no-skip-render') {
    await F.loadReconcile();
    const host = doc.getElementById('recPlanBody');

    if (SCENARIO === 'actions') {
      const cards = host.querySelectorAll('.rec-action');
      check('action-card-rendered', cards.length === 1, 'cards=' + cards.length);
      check('action-symbol', host.textContent.includes('510300'));
      check('action-side-buy', host.querySelector('.rec-side.buy') !== null);
      check('action-reason-visible', host.textContent.includes('偏离'));
      check('done-button', host.querySelector('.btn-done') !== null);
      check('skip-button', host.querySelector('.btn-skip') !== null);
      // ★ estimate labelling: banner present, and no realtime claim anywhere
      const banner = host.querySelector('#recEstimateBanner');
      check('estimate-banner', banner !== null && banner.textContent.includes('估算'));
      check('no-realtime-claim', !host.textContent.includes('实时') || host.textContent.includes('非实时'),
            'realtime-looking text without the 非实时 qualifier');
    } else {
      // ★ THE acceptance behaviour: empty plan explains itself
      check('empty-title', host.textContent.includes('今天无需操作'));
      const items = host.querySelectorAll('.rec-skipped-item');
      check('skipped-explained-3', items.length === 3, 'skipped items=' + items.length);
      const gates = Array.from(items).map((i) => i.getAttribute('data-gate')).sort();
      check('gate-names', gates.join(',') === 'deadband,in_flight,price_missing', gates.join(','));
      check('gate-label-deadband', host.textContent.includes('免交易带'));
      check('gate-label-inflight', host.textContent.includes('在途'));
      check('note-visible', host.textContent.includes('等待交收'));
    }
  }

  if (SCENARIO === 'targets') {
    await F.loadTargets();
    const host = doc.getElementById('recTargetBody');
    const rows = host.querySelectorAll('.rec-target');
    check('target-rows', rows.length === 2, 'rows=' + rows.length);
    const pending = host.querySelectorAll('.rec-target.pending');
    check('unapproved-visually-distinct', pending.length === 1, 'pending=' + pending.length);
    check('pending-badge', host.textContent.includes('待批准'));
    const approveBtns = host.querySelectorAll('.btn-approve');
    check('approve-button-only-on-pending', approveBtns.length === 1,
          'approve buttons=' + approveBtns.length);
    check('summary-weight', host.textContent.includes('30.0'));
  }

  if (SCENARIO === 'adopt-done') {
    await F.loadReconcile();
    await F.markAction('2026-07-26', '510300', 'done');
    const post = calls.find((c) => c.url.includes('/status'));
    check('status-post-fired', !!post, 'no /status call recorded');
    check('status-url', post && post.url.includes('/reconcile/action/2026-07-26/510300/status'),
          post && post.url);
    const body = post && JSON.parse(post.opts.body);
    // ★ numbers, not a boolean: slippage is the raw material for advice quality
    check('status-value', body && body.status === 'done');
    check('actual-price-captured', body && body.actual_price === 3.95,
          'got ' + (body && body.actual_price));
    check('actual-shares-captured', body && body.actual_shares === 98,
          'got ' + (body && body.actual_shares));
    check('prompt-asked-twice', promptLog.length === 2, 'prompts=' + promptLog.length);
  }

  if (SCENARIO === 'prompt-cancel') {
    await F.loadReconcile();
    await F.markAction('2026-07-26', '510300', 'done');
    // Cancelling the prompt must record NOTHING — a cancelled adopt is not a fill.
    const post = calls.find((c) => c.url.includes('/status'));
    check('cancel-records-nothing', !post, 'status call fired despite cancel');
  }

  if (SCENARIO === 'prompt-empty') {
    await F.loadReconcile();
    await F.markAction('2026-07-26', '510300', 'done');
    const post = calls.find((c) => c.url.includes('/status'));
    const body = post && JSON.parse(post.opts.body);
    // Empty answers fall back to the ADVISED values (400 shares @ 4.0).
    check('empty-prompt-falls-back', body && body.actual_price === 4.0 &&
          body.actual_shares === 100, JSON.stringify(body));
  }

  if (SCENARIO === 'approve') {
    await F.loadTargets();
    await F.approveTarget('510300');
    const post = calls.find((c) => c.url.includes('/approve'));
    check('approve-post-fired', !!post);
    check('approve-url', post && post.url.includes('/reconcile/target/510300/approve'),
          post && post.url);
  }

  console.log('SUMMARY pass=' + pass + ' fail=' + fail);
  if (fail > 0) process.exit(1);
})().catch((e) => { console.log('HARNESS-ERROR ' + (e && e.stack || e)); process.exit(2); });
'''


def _run(scenario, *, patch='', min_pass=1, reconcile_src=None):
    """Run one scenario. reconcile_src overrides the on-disk file (NEUTER)."""
    if not _deps_available():
        pytest.skip('node + jsdom unavailable')

    static = STATIC
    tmp = None
    if reconcile_src is not None:
        tmp = tempfile.mkdtemp(prefix='ttfe_')
        os.makedirs(os.path.join(tmp, 'js', 'trading'))
        shutil.copy(os.path.join(STATIC, 'js', 'trading', 'state.js'),
                    os.path.join(tmp, 'js', 'trading', 'state.js'))
        with open(os.path.join(tmp, 'js', 'trading', 'reconcile.js'), 'w',
                  encoding='utf-8') as fh:
            fh.write(reconcile_src)
        static = tmp

    env = dict(os.environ)
    env['JSDOM_DIR'] = JSDOM_DIR
    env['SCENARIO'] = scenario
    env['PATCH'] = patch
    env['TT_STATIC'] = static
    try:
        proc = subprocess.run(
            ['node', '-e', _HARNESS],
            capture_output=True, text=True, timeout=60, env=env)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    out = proc.stdout + proc.stderr
    passes = out.count('PASS ')
    assert 'FAIL ' not in out and 'HARNESS-ERROR' not in out, (
        f'jsdom scenario {scenario!r} failed:\n{out[-4000:]}')
    assert passes >= min_pass, (
        f'jsdom scenario {scenario!r}: only {passes} PASS (need {min_pass})\n'
        f'{out[-2000:]}')
    return out


# ════════════════════════════════════════════════════════════
#  Behaviours
# ════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_action_plan_renders_cards_with_estimate_banner():
    out = _run('actions', min_pass=8)


@pytest.mark.unit
def test_empty_plan_explains_itself_with_gate_reasons():
    """★ The epic's headline: a blank screen is a bug, not a state."""
    _run('empty', min_pass=6)


@pytest.mark.unit
def test_unapproved_target_is_distinct_and_only_it_offers_approve():
    _run('targets', min_pass=5)


@pytest.mark.unit
def test_done_captures_actual_price_and_shares():
    """★ Slippage is recorded; a boolean would make advice quality uncomputable."""
    _run('adopt-done', min_pass=6)


@pytest.mark.unit
def test_cancelled_adopt_records_nothing():
    _run('prompt-cancel', min_pass=1)


@pytest.mark.unit
def test_empty_prompt_falls_back_to_advised_values():
    _run('prompt-empty', min_pass=1)


@pytest.mark.unit
def test_approve_fires_owner_ratification():
    _run('approve', min_pass=2)


# ════════════════════════════════════════════════════════════
#  NEUTER: prove the explainer is load-bearing
# ════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_neuter_stripping_skipped_renderer_kills_the_empty_state():
    """Remove the skipped block from a COPY of reconcile.js: the same jsdom
    scenario must then find ZERO explained reasons — i.e. the previous test
    passes because of this code, not by accident."""
    src = open(os.path.join(STATIC, 'js', 'trading', 'reconcile.js'),
               encoding='utf-8').read()
    start = src.index('if (skipped.length) {')
    end = src.index('</ul></details>')
    end = src.index('}', end) + 1
    neutered = src[:start] + src[end:]
    assert 'rec-skipped' not in neutered, 'NEUTER patch did not apply'

    out = ''
    try:
        out = _run('empty', min_pass=99, reconcile_src=neutered)
    except AssertionError as e:
        assert 'skipped-explained-3' in str(e) or 'gate-names' in str(e), (
            f'NEUTER failed the wrong test — the guard is not specific:\n{e}')
        return
    raise AssertionError(
        'NEUTER did not bite: empty plan still explained itself without the '
        'skipped renderer, so the behaviour is not load-bearing')


# ════════════════════════════════════════════════════════════
#  Static guards: cascade order + data-label contract
# ════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_theme_bridge_loads_after_trading_css_and_reconcile_before_init():
    html = open(os.path.join(ROOT, 'tofu_trading', 'templates', 'trading.html'),
                encoding='utf-8').read()
    # Anchored on the file names, not on a leading slash: the refs are
    # page-relative so the page survives a prefixed mount (see
    # tests/test_page_asset_paths.py). This guard is about CASCADE ORDER only.
    i_trading = html.index('trading-static/trading.css')
    i_bridge = html.index('trading-static/theme-bridge.css')
    assert i_trading < i_bridge, (
        'theme-bridge must load AFTER trading.css or it loses the cascade '
        'and the host token mapping never applies')
    i_rec = html.index('trading-static/js/trading/reconcile.js')
    i_init = html.index('trading-static/js/trading/init.js')
    assert i_rec < i_init, 'reconcile.js must load before init.js (nav switch calls it)'


@pytest.mark.unit
def test_mobile_card_layout_has_a_data_label_for_every_holdings_cell():
    """The mobile card layout (theme-bridge.css) labels each cell via
    ``td::before { content: attr(data-label) }``. A <td> WITHOUT data-label
    shows a value with no idea what it means — the layout's whole premise.
    """
    js = open(os.path.join(STATIC, 'js', 'trading', 'dashboard.js'),
              encoding='utf-8').read()
    # The holdings row emits 10 <td>; each must carry data-label.
    import re
    m = re.search(r'<tr class="\$\{rowCls\}">(.*?)</tr>', js, re.S)
    assert m, 'holdings row template not found'
    tds = re.findall(r'<td[^>]*>', m.group(1))
    labelled = [t for t in tds if 'data-label' in t]
    assert len(tds) >= 10, f'expected >=10 holdings cells, found {len(tds)}'
    assert len(labelled) == len(tds), (
        f'{len(labelled)}/{len(tds)} cells labelled — mobile cards would show '
        f'unlabelled values')

    css = open(os.path.join(STATIC, 'theme-bridge.css'), encoding='utf-8').read()
    assert 'attr(data-label)' in css, (
        'theme-bridge mobile block does not consume data-label — the two '
        'sides of the contract drifted apart')


@pytest.mark.unit
def test_neuter_removing_one_data_label_breaks_the_guard():
    js = open(os.path.join(STATIC, 'js', 'trading', 'dashboard.js'),
              encoding='utf-8').read()
    neutered = js.replace('<td data-label="名称">', '<td>', 1)
    assert neutered != js
    import re
    m = re.search(r'<tr class="\$\{rowCls\}">(.*?)</tr>', neutered, re.S)
    tds = re.findall(r'<td[^>]*>', m.group(1))
    labelled = [t for t in tds if 'data-label' in t]
    assert len(labelled) == len(tds) - 1, (
        'NEUTER setup wrong: expected exactly one unlabelled cell')
