"""Actual retained program renderer exposes handoff evidence without fake savings."""

from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


def test_program_handoff_rendering_and_legacy_measurement_absence():
    run_harness(
        target_js=str(Path(JS_DIR) / 'ui/tool_rounds.js'),
        body_js=r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  html: '<!doctype html><body><div id="chatInner"></div></body>',
  globals: { _convRenderFingerprint: () => 0, conversations: [], activeConvId: null,
    t: (key, values) => values ? key + ' ' + JSON.stringify(values) : key },
});
function render(round) {
  const node = document.createElement('div');
  node.innerHTML = renderToolRoundsHTML([round], false);
  return node;
}
const round = {
  roundNum: 1, llmRound: 1, _programSynthetic: true,
  _programCallId: 'test-program', status: 'done', programStatus: 'completed',
  programCode: 'tools.observe("choose", {ids:["a","b"]});',
  programResult: {kind:'observation', reason:'Choose <script>unsafe()</script>', data:{ids:['a','b']}},
  programStats: {outcome:'needs_model', result_delivery:{outputBytes:0,truncatedResults:0}, result_bytes:250,
    call_stages:0,parallel_stages:0},
  childCallIds: [], childToolNames: [],
};
const before = JSON.stringify(round);
const node = render(round);
const card = node.querySelector('.ptc-card[data-program-attention="handoff"]');
check('handoff_card', !!card);
check('handoff_status', card.querySelector('.ptc-status').textContent === 'ptc.status.needsModel');
check('reason_escaped', card.querySelector('.ptc-observation').textContent === 'Choose <script>unsafe()</script>' && !card.querySelector('script'));
check('evidence_open', card.querySelector('.ptc-result').open);
check('source_collapsed', !card.querySelector('.ptc-code').open);
check('evidence_precedes_source', !!(card.querySelector('.ptc-result').compareDocumentPosition(card.querySelector('.ptc-code')) & 4));
check('evidence_without_protocol_wrapper', JSON.parse(card.querySelector('.ptc-result pre').textContent).ids.join(',') === 'a,b');
const measured = card.querySelector('.ptc-measurements').textContent;
check('actual_measurements', measured.includes('"bytes":"0"') && measured.includes('"bytes":"250"'));
check('no_savings_claim', !/saved|saving|%/i.test(card.querySelector('.ptc-measurements').textContent));
check('projection_immutable', JSON.stringify(round) === before);
const legacy = render({...round, programStats:undefined, programResult:{answer:'done'}});
check('legacy_no_fake_measurements', !legacy.querySelector('.ptc-measurements'));
check('legacy_no_handoff', !legacy.querySelector('[data-program-attention="handoff"]'));
const failed = render({...round, status:'error', programStatus:'error'});
check('failed_never_handoff', !failed.querySelector('[data-program-attention="handoff"]') && !!failed.querySelector('.ptc-status-error'));
const named = render({...round, programResult:{kind:'observation',reason:'constructor',data:[]}});
check('reason_cannot_select_inherited_property', named.querySelector('.ptc-observation').textContent === 'constructor');
report();
''',
        min_pass=14,
    )
