"""Retained receipt UI contract; real-browser coverage lives in tests/server."""
import json
from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import ROOT

pytestmark = pytest.mark.unit
VIEWER = str(Path(JS_DIR) / 'compaction-viewer.js')
FIXTURE = ROOT / 'tests/fixtures/compaction/receipt_v1.json'

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const locale = LOCALE;
const receiptFixture = RECEIPT;
const t = (key, params = {}) => (locale[key] || key).replace(/\{(\w+)\}/g, (m, k) => params[k] ?? m);
const clone = value => JSON.parse(JSON.stringify(value));
let archive;
const { document, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  globals: {
    runtimeScope: {}, t, _applyI18n: () => {},
    Api: { compactions: {
      list: async () => ({compactions: [archive]}),
      getSummary: async () => ({archive}),
      getPost: async () => ({archive, postMessages: []}),
    }},
  },
});
const label = key => t('compactionViewer.' + key);
const values = key => [...document.querySelectorAll('.cd-receipt-metric')]
  .filter(el => el.querySelector('span').textContent === label(key))
  .map(el => el.querySelector('strong').textContent);
const first = key => values(key)[0];
let sequence = 0;
async function show(receipt, summary = 'Complete fixture summary') {
  archive = {id: 'receipt-' + (++sequence), receipt, summary,
    model: 'main-task-not-summary', tokensBefore: 20000, tokensAfter: 8000};
  await runtimeScope.openCompactionViewer('receipt-conversation-' + sequence);
  document.querySelector('[data-tab="summary"]').click();
  await new Promise(resolve => setImmediate(resolve));
}
(async () => {
  await show(clone(receiptFixture));
  check('Completion and adoption independent', first('receiptComplete') === label('yes') && first('receiptAccepted') === label('no'));
  check('Summary route is not main task', first('receiptModel') === 'fixture-summary-model' && first('receiptProvider') === 'fixture-summary-provider');
  check('Route policy translated', first('receiptRoutePolicy') === label('policy.qualifiedCheapest'));
  check('Unknown total is not known subtotal', first('receiptUsd') === label('receiptUnknown') && first('receiptKnownSubtotal') === '$0.002000');
  check('Economics needs pricing evidence', first('receiptSummaryCost') === label('receiptUnknown') && first('receiptPayback') === label('receiptUnknown'));
  check('Context separates physical and economic', first('receiptWindow') === '128.0k' && first('receiptCeiling') === '120.0k' && first('receiptReserve') === '8.0k' && first('receiptRendered') === '96.0k' && first('receiptEconomicTarget') === '64.0k');
  check('Economic decision localized', first('receiptDecision') === label('decision.economic'));

  check('Budget is only the recorded main-route snapshot', document.querySelector('.cd-receipt').textContent.includes(label('receiptBudgetCaveat')));
  check('Attempt route evidence retained', document.querySelector('.cd-receipt').textContent.includes('fixture-first-provider'));
  check('Caveat and complete rejected text visible', document.querySelector('.cd-summary').textContent === 'Complete fixture summary' && document.querySelector('.cd-receipt').textContent.includes(label('receiptCostCaveat')));

  const legacy = clone(receiptFixture);
  ['complete', 'route', 'cost'].forEach(key => delete legacy.summary[key]);
  legacy.contextBudget = { decisionKind: 'future_decision' };
  legacy.economics = {summaryCostTokens: 123, summaryCostUsd: 0, paybackRounds: 0.5};
  await show(legacy);
  check('Legacy summary fields unknown', first('receiptComplete') === label('receiptUnknown') && first('receiptModel') === label('receiptUnknown') && first('receiptUsd') === label('receiptUnknown'));
  check('Missing budget values unknown not zero', ['receiptWindow','receiptCeiling','receiptReserve','receiptRendered','receiptEconomicTarget','receiptDecision'].every(key => first(key) === label('receiptUnknown')));
  check('Legacy payback and cost not asserted', first('receiptSummaryCost') === label('receiptUnknown') && first('receiptPayback') === label('receiptUnknown'));
  check('No unresolved translation key', !document.querySelector('.cd-receipt').textContent.includes('compactionViewer.'));

  const failed = clone(receiptFixture);
  Object.assign(failed.summary, {generated: false, complete: false, accepted: false,
    route: {modelId: '<img src=x onerror="throw 1">', providerId: 'provider & <scope>', reason: '<unsafe>'},
    cost: {known: true, costUsd: 0.0000002}});
  await show(failed, 'UNUSABLE_PARTIAL_OUTPUT');
  check('Failed paid attempt remains visible', first('receiptComplete') === label('no') && first('receiptUsd') === '< $0.000001');
  check('Explicitly incomplete body withheld', !document.querySelector('.cd-summary') && document.querySelector('.cd-empty').textContent === label('incompleteSummaryHidden'));
  check('Route fields escaped', first('receiptModel').startsWith('<img ') && !document.querySelector('.cd-receipt img') && first('receiptRouteReason') === '<unsafe>');
  check('No false success claim on failure', first('receiptAccepted') === label('no'));

  const known = clone(receiptFixture);
  Object.assign(known.summary.cost, {known: true, costUsd: 0});
  Object.assign(known.economics, {pricingKnown: true, summaryCostUsd: 0.002, cacheRewriteCostUsd: 0.001, paybackRounds: 0.5});
  known.contextBudget = {decisionKind: 'safety', economicTargetTokens: 0};
  await show(known);
  check('Recorded zero is distinct from unknown', first('receiptUsd') === '$0.000000');
  check('Known monetary estimates formatted', first('receiptSummaryCost') === '$0.002000' && first('receiptCacheRewriteUsd') === '$0.001000');
  check('Payback rounds localized', first('receiptPayback') === t('compactionViewer.receiptRounds', {n: 0.5}));
  check('Physical safety decision localized', first('receiptDecision') === label('decision.safety') && first('receiptEconomicTarget') === '0');
  known.economics.paybackRounds = null;
  known.contextBudget.decisionKind = 'manual';
  await show(known);
  check('No finite payback distinct from unknown pricing', first('receiptPayback') === label('receiptNoFinitePayback') && first('receiptDecision') === label('decision.manual'));

  delete known.economics.paybackRounds;
  await show(known);
  check('Absent payback stays unknown even with known pricing', first('receiptPayback') === label('receiptUnknown'));
  known.economics.paybackRounds = -1;
  await show(known);
  check('Invalid payback stays unknown rather than asserting no break-even', first('receiptPayback') === label('receiptUnknown'));
  known.economics.paybackRounds = 0;
  await show(known);
  check('Explicit zero payback is preserved', first('receiptPayback') === t('compactionViewer.receiptRounds', {n: 0}));
  const invalid = clone(receiptFixture);
  invalid.summary.cost = {known: true, costUsd: -1};
  invalid.contextBudget = {routeWindowTokens: '128000', reserveTokens: null};
  invalid.economics = {pricingKnown: false, paybackRounds: 1, summaryCostUsd: 0};
  await show(invalid, '');
  check('Invalid cost and budget values stay unknown', first('receiptUsd') === label('receiptUnknown') && first('receiptWindow') === label('receiptUnknown') && first('receiptReserve') === label('receiptUnknown'));
  check('False pricing flag overrides stale payback', first('receiptPayback') === label('receiptUnknown') && first('receiptSummaryCost') === label('receiptUnknown'));
  check('Empty summary does not imply no model attempt', document.querySelector('.cd-empty').textContent.includes(label('noSummary2')));
  report();
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_receipt_completion_route_cost_and_budget_rendering(language):
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    body = _BODY.replace('LOCALE', json.dumps(locale)).replace('RECEIPT', FIXTURE.read_text())
    run_harness(target_js=VIEWER, body_js=body, expect_pass=29,
                label=f'compaction receipt semantics ({language})')
