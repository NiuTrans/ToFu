"""tests/test_frontend_overview_priority.py — the front screen must serve
the RETURNING user, not only the first-time visitor.

The problem
-----------
``page-overview`` is the landing page and it is ordered for a stranger:
a full-height hero ("让 AI 帮你赚钱"), then a 3-step "怎么用？超简单"
explainer, then the track record, then the AI view — and only at the very
bottom, ``#ovPortfolioSection``, the user's own money.

That ordering is right exactly once. This is a tool people open daily, and
from the second visit onward the explainer is pure noise standing between
the user and the only thing they came for: what do I hold, and am I up or
down. The section already hides itself when there are no holdings
(overview.js ``_loadPortfolioPeek``: ``if (holdings.length === 0 && cash
=== 0) return;``), so the data needed to make this decision is ALREADY
fetched on every load — what is missing is acting on it.

The contract these tests pin
----------------------------
Driven by holdings state, evaluated against the REAL overview.js in jsdom:

* invested user  → portfolio visible AND ahead of the onboarding blocks in
  document order; the 3-step explainer is not shown.
* brand-new user → onboarding intact (hero + steps + empty track record),
  no empty portfolio shell.

Both directions are asserted on purpose. A guard that only says "hide the
steps" is satisfied by deleting the onboarding outright, which would break
the first-run experience that is the whole reason the hero exists.

Discipline
----------
The harness evals the SHIPPED overview.js — it does not re-implement the
ordering rule. NEUTER variants patch the production source to prove each
assertion is load-bearing rather than merely compatible.
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
TEMPLATES = os.path.join(ROOT, 'tofu_trading', 'templates')
JSDOM_DIR = os.path.join(MONOREPO_ROOT, 'node_modules', 'jsdom')


def _deps_available():
    return shutil.which('node') and os.path.isdir(JSDOM_DIR)


_HARNESS = r'''
'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM } = require(process.env.JSDOM_DIR);

const STATIC = process.env.TT_STATIC;
const TEMPLATES = process.env.TT_TEMPLATES;
const SCENARIO = process.env.SCENARIO;      // 'invested' | 'newcomer' | 'sell-off'
const PATCH = process.env.PATCH || '';

const stateSrc = fs.readFileSync(path.join(STATIC, 'js/trading/state.js'), 'utf8');
let ovSrc = fs.readFileSync(path.join(STATIC, 'js/trading/overview.js'), 'utf8');

// ── NEUTER patches: applied to the PRODUCTION source, not to a copy of it ──
if (PATCH === 'no-reorder') {
  // Remove the promotion (move-to-front) of the portfolio section.
  const before = ovSrc;
  ovSrc = ovSrc.replace(/\n[^\n]*_promoteHoldingsFirst\([^)]*\);[^\n]*/g, '');
  if (ovSrc === before) throw new Error('NEUTER no-reorder did not apply');
}
if (PATCH === 'no-onboarding-hide') {
  // Keep the onboarding visible even for an invested user.
  const before = ovSrc;
  ovSrc = ovSrc.replace(/\n[^\n]*_setOnboardingVisible\([^)]*\);[^\n]*/g, '');
  if (ovSrc === before) throw new Error('NEUTER no-onboarding-hide did not apply');
}
if (PATCH === 'empty-early-return') {
  // Reinstate the original defect: the empty branch does nothing instead of
  // restoring, so the previous visit's mutations survive forever.
  const before = ovSrc;
  ovSrc = ovSrc.replace(
    /if \(holdings\.length === 0 && cash === 0\) \{[\s\S]*?\n      \}/,
    'if (holdings.length === 0 && cash === 0) { return; }');
  if (ovSrc === before) throw new Error('NEUTER empty-early-return did not apply');
}
if (PATCH === 'always-hide-onboarding') {
  // Complement: hide onboarding unconditionally, including for a newcomer.
  // Rewrite CALL SITES only — a blanket replace would also rewrite the
  // `function _setOnboardingVisible(visible)` declaration into a syntax
  // error, which would fail the run for the wrong reason and prove nothing.
  const before = ovSrc;
  ovSrc = ovSrc.replace(/(?<!function )_setOnboardingVisible\([^)]*\)/g,
                        '_setOnboardingVisible(false)');
  // Force the hide to run on every load, not only the has-holdings path.
  ovSrc = ovSrc.replace(/(function loadOverview\(\) \{)/,
                        '$1\n    _setOnboardingVisible(false);');
  if (ovSrc === before) throw new Error('NEUTER always-hide did not apply');
}

// Build the DOM from the REAL template's overview section, so the test sees
// the shipped markup (and breaks if an id it depends on is renamed) rather
// than a hand-written mock that would drift from the page.
const tpl = fs.readFileSync(path.join(TEMPLATES, 'trading.html'), 'utf8');
const start = tpl.indexOf('<section class="page active" id="page-overview">');
if (start < 0) throw new Error('overview section not found in trading.html');
const endMarker = '</section>';
const end = tpl.indexOf('<section class="page" id="page-reconcile">');
if (end < 0) throw new Error('reconcile section not found (used as end bound)');
const overviewHtml = tpl.slice(start, tpl.lastIndexOf(endMarker, end) + endMarker.length);

const dom = new JSDOM('<!DOCTYPE html><html><body>' + overviewHtml + '</body></html>',
                      { url: 'http://localhost/trading.html' });
const { window } = dom;

const HOLDINGS_INVESTED = {
  holdings: [
    { symbol: '510300', shares: 100, buy_price: 4.0, current_nav: 4.4 },
    { symbol: '600519', shares: 10, buy_price: 100.0, current_nav: 96.0 },
  ],
  available_cash: 5000,
};
const HOLDINGS_EMPTY = { holdings: [], available_cash: 0 };
// Flipped mid-run by the sell-off scenario to emulate the account emptying
// between two visits to the tab.
let SOLD = false;

window.Api = {
  trading: {
    call: function (p) {
      let payload = {};
      if (p.includes('/holdings')) {
        payload = (SCENARIO === 'newcomer' || SOLD) ? HOLDINGS_EMPTY : HOLDINGS_INVESTED;
      } else if (p.includes('/sim/sessions')) {
        payload = { sessions: [] };
      } else if (p.includes('/brain') || p.includes('/intel')) {
        payload = {};
      }
      return Promise.resolve(payload);
    },
  },
};

global.window = window;
global.document = window.document;
global.location = window.location;

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name + (extra ? ' :: ' + extra : '')); }
}

(async function () {
  window.eval(stateSrc);
  window.eval(ovSrc);
  await window.TradingApp.loadOverview();
  // let the three independent async loaders settle
  await new Promise(r => setTimeout(r, 60));

  const d = window.document;
  const portfolio = d.getElementById('ovPortfolioSection');
  const howto = d.querySelector('.ov-howto');
  const hero = d.querySelector('.ov-hero');

  function visible(el) {
    return !!el && el.style.display !== 'none';
  }
  // Document order: does A come before B in the rendered page?
  function precedes(a, b) {
    if (!a || !b) return false;
    return !!(a.compareDocumentPosition(b) & window.Node.DOCUMENT_POSITION_FOLLOWING);
  }

  if (SCENARIO === 'sell-off') {
    // Second pass with an emptied account, on the SAME document — this is
    // what actually happens: loadOverview() re-runs on every return to the
    // tab, so the previous visit's mutations are still on the page.
    SOLD = true;
    await window.TradingApp.loadOverview();
    await new Promise(r => setTimeout(r, 60));

    check('onboarding_returns_after_selling_everything', visible(howto) && visible(hero),
          'user sold out but the hero/explainer stayed hidden — the front '
          + 'screen is stuck in its previous state forever');
    check('no_stale_portfolio_after_selling', !visible(portfolio),
          'an emptied portfolio box is still displayed from the earlier visit');
    check('document_order_restored', precedes(howto, portfolio),
          'holdings stayed promoted above the onboarding despite being empty');
    console.log('SUMMARY pass=' + pass + ' fail=' + fail);
    process.exit(fail === 0 ? 0 : 1);
  }

  if (SCENARIO === 'invested') {
    check('portfolio_visible_for_invested_user', visible(portfolio),
          'a user with holdings must see their own money on the front screen');
    check('portfolio_precedes_onboarding', precedes(portfolio, howto),
          'holdings must come BEFORE the how-it-works explainer in document order');
    check('howto_hidden_for_invested_user', !visible(howto),
          'the 3-step explainer is noise for a returning user');
    check('portfolio_has_real_numbers',
          (d.getElementById('ovPortfolioPeek') || {}).innerHTML.indexOf('总资产') >= 0,
          'portfolio rendered but shows no total-assets figure');
  } else {
    check('onboarding_intact_for_newcomer', visible(howto) && visible(hero),
          'a first-time visitor still needs the hero + the 3-step explainer');
    check('no_empty_portfolio_shell', !visible(portfolio),
          'an empty portfolio box must not be shown to someone with nothing');
  }

  console.log('SUMMARY pass=' + pass + ' fail=' + fail);
  process.exit(fail === 0 ? 0 : 1);
})().catch(function (e) {
  console.log('HARNESS_ERROR ' + (e && e.stack || e));
  process.exit(2);
});
'''


def _run(scenario, patch=''):
    with tempfile.TemporaryDirectory() as td:
        js = os.path.join(td, 'h.js')
        with open(js, 'w', encoding='utf-8') as f:
            f.write(_HARNESS)
        env = dict(os.environ, JSDOM_DIR=JSDOM_DIR, TT_STATIC=STATIC,
                   TT_TEMPLATES=TEMPLATES, SCENARIO=scenario, PATCH=patch)
        return subprocess.run(['node', js], capture_output=True, text=True,
                              env=env, timeout=120)


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_invested_user_sees_holdings_before_onboarding():
    r = _run('invested')
    print(r.stdout, r.stderr)
    assert r.returncode == 0, f'invested-user contract failed:\n{r.stdout}\n{r.stderr}'


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_newcomer_still_gets_the_onboarding():
    r = _run('newcomer')
    print(r.stdout, r.stderr)
    assert r.returncode == 0, f'newcomer contract failed:\n{r.stdout}\n{r.stderr}'


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_selling_everything_restores_the_front_screen():
    """State TRANSITION, not just state.

    loadOverview() re-runs on every return to the tab, so the empty-holdings
    branch must actively RESTORE the front screen rather than simply doing
    nothing. If it returns early, a user who sells out keeps a stale
    portfolio box and never sees the onboarding again — permanently, since
    no later visit can undo it either.
    """
    r = _run('sell-off')
    print(r.stdout, r.stderr)
    assert r.returncode == 0, f'sell-off contract failed:\n{r.stdout}\n{r.stderr}'


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_NEUTER_early_return_on_empty_holdings_is_caught():
    """The exact defect this suite caught: a bare early return."""
    r = _run('sell-off', patch='empty-early-return')
    print(r.stdout, r.stderr)
    assert r.returncode == 1, (
        'NEUTER did not bite: the front screen recovered even with the empty '
        f'branch short-circuited\n{r.stdout}')
    assert 'FAIL onboarding_returns_after_selling_everything' in r.stdout, r.stdout


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_NEUTER_removing_the_reorder_is_caught():
    r = _run('invested', patch='no-reorder')
    print(r.stdout, r.stderr)
    assert r.returncode == 1, (
        'NEUTER did not bite: holdings still preceded the onboarding without '
        f'the promotion call, so the ordering is not load-bearing\n{r.stdout}')
    assert 'FAIL portfolio_precedes_onboarding' in r.stdout, r.stdout


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_NEUTER_removing_the_onboarding_gate_is_caught():
    r = _run('invested', patch='no-onboarding-hide')
    print(r.stdout, r.stderr)
    assert r.returncode == 1, (
        'NEUTER did not bite: the explainer was hidden for an invested user '
        f'even without the gate\n{r.stdout}')
    assert 'FAIL howto_hidden_for_invested_user' in r.stdout, r.stdout


@pytest.mark.unit
@pytest.mark.skipif(not _deps_available(), reason='node + jsdom required')
def test_NEUTER_hiding_onboarding_unconditionally_is_caught():
    """Complement: 'fix' by deleting onboarding for everyone must NOT pass."""
    r = _run('newcomer', patch='always-hide-onboarding')
    print(r.stdout, r.stderr)
    assert r.returncode == 1, (
        'NEUTER did not bite: a first-time visitor got no onboarding and the '
        f'suite stayed green — the newcomer path is unprotected\n{r.stdout}')
    assert 'FAIL onboarding_intact_for_newcomer' in r.stdout, r.stdout
