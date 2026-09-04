"""Exact owner contract for bounded write-approval presentation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_JS = Path(native_module_path(
    '.native/tool-approval-presentation-contract.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-approval-presentation.ts',
))


_OWNER_HARNESS = r"""
const messages = {
  'toolApproval.awaiting': 'awaiting-local',
  'toolApproval.approve': 'approve-local',
  'toolApproval.reject': 'reject-local',
  'toolApproval.oneEditAcross': '{count} edit-local across {path}',
  'toolApproval.manyEditsAcross': '{count} edits-local across {path}',
  'toolApproval.moreLines': 'more-lines:{count}',
  'toolApproval.editFallback': 'edit-local:{index}',
  'toolApproval.editStats': 'stats:{searchLines}/{replaceLines}',
  'toolApproval.moreLinesWithTotals': 'more-total:{count}/{totalLines}/{totalChars}',
  'toolApproval.moreEdits': 'more-edits:{count}',
  'toolApproval.moreLinesUnknown': 'more-lines-unknown',
  'toolApproval.writeMeta': 'write-meta:{lines}/{chars}',
  'toolApproval.previewLimit': 'preview-limit:{n}',
  'toolApproval.riskFieldsLimit': 'risk-limit:{shown}/{total}',
  'toolApproval.approvalIdLimit': 'id-limit:{n}',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});
function occurrences(value, fragment) {
  return String(value).split(fragment).length - 1;
}

const presentation = createToolApprovalPresentation({ translate });
const header = Object.freeze({
  iconHtml: '<i data-slot="icon"></i>',
  queryHtml: '<b data-slot="query">write_file</b>',
});
const limits = TOOL_APPROVAL_PRESENTATION_LIMITS;
check('limits_and_port_are_frozen_and_narrow',
  Object.isFrozen(limits)
  && limits.approvalIdUnits === 512
  && limits.riskFields === 32
  && limits.riskValueUnits === 2000
  && limits.riskValueLines === 64
  && limits.batchEdits === 16
  && limits.batchPreviewLines === 12
  && limits.singlePreviewLines === 30
  && limits.contentPreviewLines === 12
  && limits.previewInputUnits === 120000
  && limits.contentInputUnits === 65536
  && limits.previewLineUnits === 2000
  && limits.commandUnits === 65536
  && Object.isFrozen(presentation)
  && Object.keys(presentation).length === 1);
check('unrelated_or_incomplete_rounds_fail_closed',
  presentation.renderApprovalHtml(null, header) === ''
  && presentation.renderApprovalHtml({ status: 'done', approvalId: 'a' }, header) === ''
  && presentation.renderApprovalHtml({ status: 'pending_approval' }, header) === '');

const coldHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'cold-1',
}, header);
check('cold_round_keeps_actionable_card_and_trusted_slots',
  coldHtml.includes('data-slot="icon"')
  && coldHtml.includes('data-slot="query"')
  && coldHtml.includes('awaiting-local')
  && coldHtml.includes('approve-local')
  && coldHtml.includes('reject-local'));
check('actions_are_static_and_read_an_escaped_data_attribute',
  coldHtml.includes('data-approval-id="cold-1"')
  && coldHtml.includes('resolveWriteApproval(this.dataset.approvalId,true)')
  && coldHtml.includes('resolveWriteApproval(this.dataset.approvalId,false)')
  && !coldHtml.includes("resolveWriteApproval('cold-1'"));

const hostileId = "ap');globalThis.pwned=true;//";
const hostileIdHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: hostileId,
}, header);
check('hostile_identifier_never_enters_action_code',
  hostileIdHtml.includes('data-approval-id="ap&#39;);globalThis.pwned=true;//"')
  && occurrences(hostileIdHtml, 'this.dataset.approvalId') === 2
  && !hostileIdHtml.includes("resolveWriteApproval('ap"));
const oversizedIdHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'A'.repeat(513),
}, header);
check('oversized_identifier_fails_closed_with_visible_reason',
  oversizedIdHtml.includes('id-limit:512')
  && occurrences(oversizedIdHtml, ' disabled') === 2
  && !oversizedIdHtml.includes('data-tofu-action'));

const riskFields = Array.from({ length: 35 }, (_, index) => ({
  label: index === 0 ? '<label>' : `risk-${index}`,
  value: index === 0 ? '<value>\nsecond' : `value-${index}`,
}));
const riskRound = Object.freeze({
  status: 'pending_approval', approvalId: 'risk-1',
  approvalMeta: Object.freeze({
    description: '<danger-note>',
    riskFields: Object.freeze(riskFields),
    command: 'must-not-win',
  }),
});
const riskBefore = JSON.stringify(riskRound);
const riskHtml = presentation.renderApprovalHtml(riskRound, header);
check('generic_risk_fields_have_priority_and_escape_every_value',
  riskHtml.includes('&lt;danger-note&gt;')
  && riskHtml.includes('&lt;label&gt;')
  && riskHtml.includes('&lt;value&gt;')
  && !riskHtml.includes('must-not-win')
  && !riskHtml.includes('<danger-note>'));
check('risk_field_count_is_bounded_and_visible',
  occurrences(riskHtml, 'class="ptool-risk-field"') === 32
  && riskHtml.includes('risk-limit:32/35')
  && !riskHtml.includes('risk-32'));
check('owner_never_mutates_inputs', JSON.stringify(riskRound) === riskBefore);

const longRiskHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'risk-long',
  approvalMeta: { riskFields: [{
    label: 'L'.repeat(512) + 'LABEL_TAIL',
    value: 'V'.repeat(2000) + 'VALUE_TAIL',
  }] },
}, header);
check('risk_labels_and_values_have_visible_exact_bounds',
  !longRiskHtml.includes('LABEL_TAIL')
  && !longRiskHtml.includes('VALUE_TAIL')
  && occurrences(longRiskHtml, '…') >= 2);

const edits = Array.from({ length: 35 }, (_, index) => ({
  path: index === 0 ? '<unsafe.py>' : `f-${index}.py`,
  description: index === 0 ? '' : `edit-${index}`,
  search: 'old\n'.repeat(13), replace: 'new\n'.repeat(13),
  searchLines: 14, replaceLines: 14,
}));
const batchHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'batch-1',
  approvalMeta: { batchMode: true, path: '<root>', editSummaries: edits },
}, header);
check('batch_preview_is_escaped_and_uses_localized_fallbacks',
  batchHtml.includes('35 edits-local across &lt;root&gt;')
  && batchHtml.includes('&lt;unsafe.py&gt;')
  && batchHtml.includes('edit-local:1')
  && batchHtml.includes('stats:14/14'));
check('batch_edit_and_line_counts_are_bounded_and_visible',
  occurrences(batchHtml, 'class="ptool-batch-edit"') === 16
  && batchHtml.includes('more-edits:19')
  && batchHtml.includes('more-lines:2')
  && !batchHtml.includes('f-16.py'));

const singleHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'single-1',
  approvalMeta: {
    search: 'S'.repeat(2001), replace: 'R',
    searchLines: 31, searchChars: 2001,
    replaceLines: 1, replaceChars: 1,
  },
}, header);
check('single_diff_has_line_and_total_bounds_with_visible_notices',
  singleHtml.includes('preview-limit:2000')
  && singleHtml.includes('more-total:1/31/2001')
  && !singleHtml.includes('S'.repeat(2001)));

const commandHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'command-1',
  approvalMeta: {
    description: '<desc>', command: '<cmd>' + 'C'.repeat(65536) + 'TAIL',
  },
}, header);
check('command_preview_is_escaped_bounded_and_visible',
  commandHtml.includes('&lt;desc&gt;')
  && commandHtml.includes('$ &lt;cmd&gt;')
  && commandHtml.includes('preview-limit:65536')
  && !commandHtml.includes('TAIL'));

const contentHtml = presentation.renderApprovalHtml({
  status: 'pending_approval', approvalId: 'content-1',
  approvalMeta: {
    contentPreview: Array.from({ length: 14 }, (_, i) => `<line-${i}>`).join('\n'),
    contentLines: 14, contentChars: 130,
  },
}, header);
check('content_preview_is_escaped_line_bounded_and_localized',
  contentHtml.includes('&lt;line-0&gt;')
  && !contentHtml.includes('&lt;line-12&gt;')
  && contentHtml.includes('more-lines-unknown')
  && contentHtml.includes('write-meta:14/130'));

let malformedHtml = '';
let malformedThrew = false;
try {
  malformedHtml = presentation.renderApprovalHtml({
    status: 'pending_approval', approvalId: 42,
    approvalMeta: { batchMode: true, editSummaries: 'not-an-array' },
  }, header);
} catch { malformedThrew = true; }
check('malformed_legacy_shapes_degrade_without_throwing',
  malformedThrew === false
  && malformedHtml.includes('data-approval-id="42"')
  && malformedHtml.includes('ptool-approval-btns'));

report();
"""


def test_tool_approval_owner_contract():
    run_harness(
        target_js=str(OWNER_JS),
        body_js=_OWNER_HARNESS,
        expect_pass=16,
        label='tool approval presentation owner',
    )
