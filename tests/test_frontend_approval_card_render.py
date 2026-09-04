"""Public retained-dispatch contract for typed write-approval cards.

The typed owner carries projection, escaping, localization, and resource
policy. This jsdom fixture proves the retained ordered dispatcher supplies its
trusted header slots and preserves command/write branch selection.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness


pytestmark = pytest.mark.unit
TOOL_ROUNDS = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')


_HARNESS = r"""
const messages = {
  'toolApproval.awaiting': 'awaiting approval',
  'toolApproval.approve': 'Approve',
  'toolApproval.reject': 'Reject',
  'toolApproval.oneEditAcross': '{count} edit across {path}',
  'toolApproval.manyEditsAcross': '{count} edits across {path}',
  'toolApproval.moreLines': '… {count} more lines',
  'toolApproval.editFallback': 'Edit {index}',
  'toolApproval.editStats': '{searchLines}→{replaceLines} lines',
  'toolApproval.moreLinesWithTotals': '… {count} more lines ({totalLines} lines · {totalChars} chars total)',
  'toolApproval.moreEdits': '… and {count} more edits',
  'toolApproval.moreLinesUnknown': '… more lines',
  'toolApproval.writeMeta': '{lines} lines · {chars} chars',
  'toolApproval.previewLimit': 'Preview truncated to the first {n} characters.',
  'toolApproval.riskFieldsLimit': 'Showing first {shown} of {total} risk fields.',
  'toolApproval.approvalIdLimit': 'Approval unavailable: identifier exceeds {n} characters.',
};
function translate(key, paramsOrFallback) {
  if (typeof paramsOrFallback === 'string') {
    return messages[key] || paramsOrFallback;
  }
  let value = messages[key] || key;
  if (!paramsOrFallback) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(paramsOrFallback, name)
      ? String(paramsOrFallback[name]) : token
  ));
}
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
  globals: {
    t: translate,
    _featureFlags: { debug_mode: false },
    projectState: { extraRoots: [] },
  },
});

const commandHtml = _renderUnifiedToolLine({
  status: 'pending_approval', approvalId: 'ap2', toolName: 'run_command',
  query: 'run_command', results: [],
  approvalMeta: {
    toolName: 'run_command', command: 'rm foo.py', description: 'delete foo',
  },
}, false);
check('command_card_reaches_typed_owner',
  commandHtml.includes('ptool-cmd-code')
  && commandHtml.includes('$ rm foo.py')
  && commandHtml.includes('delete foo'));
check('command_card_keeps_static_approve_action',
  commandHtml.includes('data-approval-id="ap2"')
  && commandHtml.includes('resolveWriteApproval(this.dataset.approvalId,true)'));
check('command_card_keeps_static_reject_action',
  commandHtml.includes('resolveWriteApproval(this.dataset.approvalId,false)'));
check('command_card_localizes_status_and_buttons',
  commandHtml.includes('awaiting approval')
  && commandHtml.includes('> Approve</button>')
  && commandHtml.includes('> Reject</button>'));
check('command_branch_does_not_fall_into_diff_preview',
  !commandHtml.includes('ptool-diff-del'));

const writeHtml = _renderUnifiedToolLine({
  status: 'pending_approval', approvalId: 'ap3', toolName: 'write_file',
  query: 'write_file', results: [],
  approvalMeta: {
    toolName: 'write_file', path: 'x.py',
    contentPreview: 'print(1)\nprint(2)', contentLines: 2, contentChars: 17,
  },
}, false);
check('write_card_uses_content_preview_branch',
  writeHtml.includes('print(1)')
  && writeHtml.includes('2 lines · 17 chars'));
check('write_card_does_not_render_command_shell',
  !writeHtml.includes('ptool-cmd-code'));
check('write_card_keeps_action_authority',
  writeHtml.includes('data-approval-id="ap3"')
  && writeHtml.includes('resolveWriteApproval(this.dataset.approvalId,true)'));

const unrelatedHtml = _renderUnifiedToolLine({
  status: 'done', approvalId: 'stale', toolName: 'write_file',
  query: 'write_file', results: [],
}, false);
check('non_pending_round_falls_through_without_approval_buttons',
  !unrelatedHtml.includes('ptool-approval-btns'));

report();
"""


def test_retained_dispatcher_routes_pending_approvals_to_typed_owner():
    run_harness(
        target_js=TOOL_ROUNDS,
        body_js=_HARNESS,
        expect_pass=9,
        label='retained approval dispatcher',
    )
