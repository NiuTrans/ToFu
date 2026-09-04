"""Tool-round presentation owner and retained-adapter behavior contracts.

WHY
---
``test_frontend_sse_dispatch.py`` deeply covers how SSE events MUTATE the
assistant-message STATE (round.status, _swarmAgents, _rejected, …). But the
RENDER layer that turns that state into DOM — ``renderToolRoundsHTML`` →
``_renderUnifiedGroup`` → ``_renderToolGroupsHTML`` → ``_renderToolSlot`` →
``_renderUnifiedToolLine`` / ``_buildSwarmPanelHTML`` — is the largest file in
the frontend (2283L) and had NO direct test. A regression there (a renderer
emitting the wrong CSS class / dropping a badge / mis-grouping a parallel
batch) would ship silently because the SSE-state test never inspects HTML.

The exact typed tool-result owner contract covers compaction, write/edit diff,
batch summary, escaping, immutability, and bounded result-viewer policy without
a DOM. A narrower retained harness still drives real ``rounds`` arrays (the
exact shape the SSE dispatcher produces) through the PUBLIC entry
``renderToolRoundsHTML(rounds, isStreaming)`` and asserts the resulting DOM
structure for the remaining tool families and statuses. The separate
wire-parity battery locks dispatcher integration byte-for-byte.

Runs the REAL shipped JS under jsdom via the shared harness; the swarm panel
builder lives in ui/streaming_swarm_panel.js, so that file is loaded first
(extra_target) in the same window scope, exactly as the production bundle
concatenates it before tool_rounds.js.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
TOOL_RESULT_OWNER = (
    ROOT / 'frontend/src/conversation/presentation/tool-result-presentation.ts'
)
TOOL_RESULT_OWNER_JS = Path(native_module_path(
    '.native/tool-result-presentation-contract.js',
    TOOL_RESULT_OWNER,
))


_OWNER_HARNESS = r"""
eval(process.env.OWNER_SOURCE);

const checks = [];
function check(name, condition) {
  checks.push((condition ? 'PASS ' : 'FAIL ') + name);
}

const messages = {
  'tool.backendResult': 'Backend result',
  'tool.resultStats': '{lines} lines · {chars} chars',
  'tool.resultTruncated': 'Result too long — showing first {n} chars',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
const writeGateRefusal = {
  resolveRefusal: (_round, metadata) => metadata && metadata.refusal || null,
  renderNoticeHtml: (refusal) => refusal
    ? '<aside data-gate="' + String(refusal.kind) + '">gate notice</aside>'
    : '',
};
const presentation = createToolResultPresentation({
  translate,
  writeGateRefusal,
});
const header = Object.freeze({
  iconHtml: '<i data-slot="icon"></i>',
  queryHtml: '<b>Projected query</b>',
  rootPillHtml: '<span data-slot="root"></span>',
  badgeHtml: '<span data-slot="badge"></span>',
  repairedBadgeHtml: '<span data-slot="repaired"></span>',
  rightControlsHtml: '<span data-slot="controls"></span>',
  toolDisplayLabel: 'Project',
});

check('immutable_narrow_public_port',
  Object.isFrozen(presentation)
  && Object.keys(presentation).length === 3
  && typeof presentation.renderCompactionLabelHtml === 'function'
  && typeof presentation.renderWriteResultHtml === 'function'
  && typeof presentation.renderGenericResultHtml === 'function');

const compaction = presentation.renderCompactionLabelHtml({
  compactionLayer: 'L1', compactedFromChars: 112000, compactedToChars: 800,
});
check('compaction_layer_and_token_reduction_are_explicit',
  compaction.includes('COMPACTED L1')
  && compaction.includes('28k→200')
  && compaction.includes('Aged out of the hot tail'));
check('all_authoritative_compaction_layers_have_copy',
  presentation.renderCompactionLabelHtml({ compactionLayer: 'L0' })
    .includes('never entered context')
  && presentation.renderCompactionLabelHtml({ compactionLayer: 'L3' })
    .includes('LLM-generated summary'));
const hostileCompaction = presentation.renderCompactionLabelHtml({
  compactionLayer: 'L1" onclick="injected',
});
check('future_or_hostile_compaction_layer_is_visible_but_safe',
  hostileCompaction.includes('COMPACTED L1&quot; onclick=&quot;injected')
  && !hostileCompaction.includes('class="ptool-compaction-label ptool-compaction-l1"'));

const frozenWrite = Object.freeze({
  roundNum: 7,
  toolName: 'write_file',
  toolArgs: JSON.stringify({ content: '<script>\nsecond' }),
});
const frozenWriteMetadata = Object.freeze({
  refusal: Object.freeze({ kind: 'stale' }),
});
const writeBefore = JSON.stringify([frozenWrite, frozenWriteMetadata, header]);
const writeHtml = presentation.renderWriteResultHtml(
  frozenWrite, frozenWriteMetadata, header,
);
check('write_file_is_a_collapsible_added_line_diff',
  writeHtml.includes('ptool-batch-done-block')
  && writeHtml.match(/bdiff-add/g).length === 2);
check('write_file_escapes_content_and_preserves_trusted_header_slots',
  writeHtml.includes('&lt;script&gt;') && !writeHtml.includes('<script>')
  && writeHtml.includes('<i data-slot="icon"></i>')
  && writeHtml.includes('<b>Projected query</b>'));
check('write_file_places_typed_gate_notice',
  writeHtml.includes('<aside data-gate="stale">gate notice</aside>'));
check('presentation_does_not_mutate_projection_or_header',
  JSON.stringify([frozenWrite, frozenWriteMetadata, header]) === writeBefore);

const diffHtml = presentation.renderWriteResultHtml({
  roundNum: 8,
  toolName: 'apply_diff',
  toolArgs: { search: 'keep\nold', replace: 'keep\nnew' },
}, {}, header);
check('single_diff_uses_lcs_context_delete_and_add_rows',
  diffHtml.includes('bdiff-ctx')
  && diffHtml.includes('bdiff-del')
  && diffHtml.includes('bdiff-add'));
const insertBeforeHtml = presentation.renderWriteResultHtml({
  roundNum: 9,
  toolName: 'insert_content',
  toolArgs: { anchor: 'anchor', content: 'before', position: 'before' },
}, {}, header);
const insertAfterHtml = presentation.renderWriteResultHtml({
  roundNum: 10,
  toolName: 'insert_content',
  toolArgs: { anchor: 'anchor', content: 'after', position: 'after' },
}, {}, header);
check('insert_content_preserves_direction',
  insertBeforeHtml.indexOf('before') < insertBeforeHtml.indexOf('anchor')
  && insertAfterHtml.indexOf('anchor') < insertAfterHtml.indexOf('after'));

const batchHtml = presentation.renderWriteResultHtml({
  roundNum: 11,
  toolName: 'edit_file',
  toolArgs: { edits: [
    { operation: 'replace', search: 'old', content: 'new' },
    { operation: 'insert_after', anchor: 'x', content: 'y' },
  ] },
}, {
  refusal: { kind: 'partial_stale' },
  editSummaries: [
    {
      path: 'src/a.py', description: 'src/a.py: replace <unsafe>',
      status: 'ok', operation: 'replace',
    },
    {
      path: 'lib/b.py', description: 'b.py: skipped',
      status: 'fail', operation: 'insert_after',
    },
  ],
}, header);
check('batch_edit_renders_each_status_and_gate_notice',
  batchHtml.match(/ptool-batch-done-edit/g).length === 2
  && batchHtml.includes('ptool-batch-ok')
  && batchHtml.includes('ptool-batch-fail')
  && batchHtml.includes('data-gate="partial_stale"'));
check('multi_file_rows_show_safe_basenames_not_redundant_prefixes',
  batchHtml.includes('title="src/a.py">a.py</span>')
  && batchHtml.includes('title="lib/b.py">b.py</span>')
  && batchHtml.includes('replace &lt;unsafe&gt;')
  && !batchHtml.includes('src/a.py: replace'));
check('batch_operations_use_designed_enum_pills',
  batchHtml.includes('ptool-op--replace')
  && batchHtml.includes('>replace</span>')
  && batchHtml.includes('ptool-op--insert')
  && batchHtml.includes('>insert_after</span>'));
const failedSegment = batchHtml.slice(batchHtml.indexOf('ptool-batch-fail'));
check('failed_batch_edit_does_not_invent_a_diff',
  failedSegment && !failedSegment.includes('bdiff-block'));

const legacyInsertHtml = presentation.renderWriteResultHtml({
  roundNum: 12,
  toolName: 'insert_contents',
  toolArgs: { edits: [
    { anchor: 'a', content: 'b', position: 'after' },
    { anchor: 'c', content: 'd', position: 'before' },
  ] },
}, { editSummaries: [
  { path: 'x.py', status: 'ok' },
  { path: 'x.py', status: 'ok' },
] }, header);
const legacyReplaceHtml = presentation.renderWriteResultHtml({
  roundNum: 13,
  toolName: 'apply_diffs',
  toolArgs: { edits: [
    { search: 'a', replace: 'b' },
    { search: 'c', replace: 'd' },
  ] },
}, { editSummaries: [
  { path: 'x.py', status: 'ok' },
  { path: 'x.py', status: 'ok' },
] }, header);
check('legacy_batch_tools_derive_operation_pills',
  legacyInsertHtml.includes('>insert_after</span>')
  && legacyInsertHtml.includes('>insert_before</span>')
  && legacyReplaceHtml.match(/ptool-op--replace/g).length === 2);
check('unified_edit_accepts_one_summary_but_legacy_batch_requires_many',
  presentation.renderWriteResultHtml({
    roundNum: 14, toolName: 'edit_file',
    toolArgs: { edits: [{ search: 'a', content: 'b', operation: 'replace' }] },
  }, { editSummaries: [{ path: 'x.py', status: 'ok', operation: 'replace' }] }, header)
    .includes('ptool-batch-done-edit')
  && presentation.renderWriteResultHtml({
    roundNum: 15, toolName: 'apply_diffs',
    toolArgs: { edits: [{ search: 'a', replace: 'b' }] },
  }, { editSummaries: [{ path: 'x.py', status: 'ok' }] }, header) === '');
check('leaked_edit_metadata_cannot_reclassify_another_tool',
  presentation.renderWriteResultHtml({
    roundNum: 16, toolName: 'run_command', toolArgs: '{}',
  }, { editSummaries: [{}, {}] }, header) === '');
check('malformed_or_irrelevant_write_inputs_fail_closed',
  presentation.renderWriteResultHtml(null, null, header) === ''
  && presentation.renderWriteResultHtml({
    toolName: 'write_file', toolArgs: '{broken',
  }, {}, header) === '');

const genericHtml = presentation.renderGenericResultHtml({
  roundNum: 17,
  toolName: 'read_files',
  status: 'done',
  toolContent: '<div>\nsecond',
  compactionLayer: 'L0',
}, {}, header);
check('generic_result_preserves_every_explicit_header_slot',
  genericHtml.includes('ptool-result-block')
  && genericHtml.includes('data-slot="icon"')
  && genericHtml.includes('data-slot="root"')
  && genericHtml.includes('data-slot="repaired"')
  && genericHtml.includes('data-slot="badge"')
  && genericHtml.includes('data-slot="controls"')
  && genericHtml.includes('COMPACTED L0'));
check('generic_result_escapes_content_and_localizes_typed_stats',
  genericHtml.includes('&lt;div&gt;') && !genericHtml.includes('<div>')
  && genericHtml.includes('project · 2 lines · 12 chars'));
const jsonHtml = presentation.renderGenericResultHtml({
  roundNum: 18, toolName: 'mcp__compile', status: 'done',
  toolContent: '{"ok":true,"pages":2}',
}, {}, header);
check('json_result_is_pretty_printed_and_safely_escaped',
  jsonHtml.includes('json · 4 lines')
  && jsonHtml.includes('{\n  &quot;ok&quot;: true,\n  &quot;pages&quot;: 2\n}'));
const structuredTruthHtml = presentation.renderGenericResultHtml({
  roundNum: 19, toolName: 'mcp__history', status: 'done',
  toolContent: { contractVersion: 'tofu.tool-result/v2',
    summary: 'OBJECT_BACKEND_TRUTH' },
}, { output: 'UNRELATED_METADATA_OUTPUT' }, header);
check('structured_tool_content_is_serialized_from_the_round',
  structuredTruthHtml.includes('OBJECT_BACKEND_TRUTH')
  && structuredTruthHtml.includes('data-tool-result-authority="toolContent"')
  && !structuredTruthHtml.includes('UNRELATED_METADATA_OUTPUT'));
const roundResultTruthHtml = presentation.renderGenericResultHtml({
  roundNum: 19, toolName: 'legacy_tool', status: 'done',
  result: { content: 'ROUND_RESULT_BACKEND_TRUTH' },
}, { output: 'UNRELATED_LEGACY_METADATA' }, header);
check('round_result_is_the_only_legacy_authority',
  roundResultTruthHtml.includes('ROUND_RESULT_BACKEND_TRUTH')
  && roundResultTruthHtml.includes('data-tool-result-authority="roundResult"')
  && !roundResultTruthHtml.includes('UNRELATED_LEGACY_METADATA'));
check('present_but_empty_tool_content_never_falls_through_to_metadata',
  presentation.renderGenericResultHtml({
    roundNum: 19, toolName: 'read_files', status: 'done', toolContent: ' ',
  }, { output: 'UNRELATED_EMPTY_FALLBACK' }, header) === '');
const truncatedHtml = presentation.renderGenericResultHtml({
  roundNum: 20, toolName: 'read_files', status: 'done',
  toolContent: 'x'.repeat(130000),
}, {}, header);
const truncatedCode = truncatedHtml.match(/<code>(x+)<\/code>/);
check('generic_result_has_a_visible_120k_character_bound',
  truncatedHtml.includes('ptool-result-trunc')
  && truncatedHtml.includes('120,000')
  && truncatedCode && truncatedCode[1].length === 120000);
check('in_flight_and_empty_results_do_not_open_a_viewer',
  presentation.renderGenericResultHtml({
    status: 'searching', toolContent: 'partial',
  }, {}, header) === ''
  && presentation.renderGenericResultHtml({
    status: 'done', toolContent: '   ',
  }, {}, header) === '');

const largeOld = Array.from({ length: 151 }, (_, i) => 'old-' + i).join('\n');
const largeNew = Array.from({ length: 151 }, (_, i) => 'new-' + i).join('\n');
check('large_diff_uses_bounded_before_after_fallback',
  presentation.renderWriteResultHtml({
    roundNum: 21, toolName: 'apply_diff',
    toolArgs: { search: largeOld, replace: largeNew },
  }, {}, header).includes('bdiff-sep'));
const hostileRoundIdHtml = presentation.renderGenericResultHtml({
  roundNum: '1" onmouseover="injected',
  toolName: 'read_files', status: 'done', toolContent: 'safe',
}, {}, header);
check('round_identity_attribute_is_escaped',
  hostileRoundIdHtml.includes('data-rn="1&quot; onmouseover=&quot;injected"')
  && !hostileRoundIdHtml.includes('data-rn="1" onmouseover="injected"'));
check('invalid_compaction_and_generic_inputs_fail_closed',
  presentation.renderCompactionLabelHtml(null) === ''
  && presentation.renderCompactionLabelHtml({ compactionLayer: 1 }) === ''
  && presentation.renderGenericResultHtml(null, null, header) === '');

console.log(checks.join('\n'));
"""


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  // argv[4] = ui/streaming_swarm_panel.js (defines _buildSwarmPanelHTML),
  // argv[2] = ui/tool_rounds.js (the core file under test),
  // argv[5] = ui/tool_rounds_rich.js (structured checklist/rich cards).
  // Same window scope, in retained-runtime order.
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    // tool_rounds.js calls a few helpers from sibling files at RUNTIME.
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
  },
});

// Parse a render result into a detached container so we can querySelector it.
function frag(html) {
  const d = document.createElement('div');
  d.innerHTML = html;
  return d;
}

if (typeof renderToolRoundsHTML !== 'function') {
  console.log('FAIL entry_exposed renderToolRoundsHTML missing');
  report();
  return;
}
check('entry_exposed', true);

// ── 0. empty / null rounds → empty string ──
check('empty_rounds_blank', renderToolRoundsHTML([], false) === '' &&
  renderToolRoundsHTML(null, false) === '');

// ── 1. a single done web_search round → one ptool-line inside a ptool-panel ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'web_search', status: 'done',
      query: 'hello', results: [{ title: 'r1' }] },
  ], false);
  const d = frag(html);
  check('panel_built', !!d.querySelector('.ptool-panel'));
  check('panel_body_full_count', d.querySelector('.ptool-panel-body')
    && d.querySelector('.ptool-panel-body').getAttribute('data-full-count') === '1');
  check('single_line_rendered', !!d.querySelector('.ptool-line'));
  check('query_text_present', html.includes('hello'));
  // not active → no active class
  check('not_active_class', !d.querySelector('.ptool-panel-active'));
}

// ── 2. an active (searching) round → ptool-panel-active ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'web_search', status: 'searching', query: 'q' },
  ], true);
  check('active_panel_class', frag(html).querySelector('.ptool-panel-active') !== null);
}

// ── 3. rejected (hallucinated) tool → .ptool-rejected with badge ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'search_web', status: 'rejected',
      _rejected: { kind: 'hallucinated', attempted: 'search_web',
        suggestions: ['web_search'] },
      results: [] },
  ], false);
  const d = frag(html);
  check('rejected_class', !!d.querySelector('.ptool-rejected'));
  check('rejected_badge', !!d.querySelector('.ptool-badge-reject'));
  check('rejected_suggestion_chip', !!d.querySelector('.ptool-reject-sugg')
    && html.includes('web_search'));
}

// ── 4. ask_human skipped (task ended unanswered) → .hg-skipped-line ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'ask_human', status: 'done', _hgSkipped: true,
      guidanceQuestion: 'Which option?' },
  ], false);
  const d = frag(html);
  check('hg_skipped_line', !!d.querySelector('.hg-skipped-line'));
  check('hg_skipped_badge', !!d.querySelector('.ptool-badge-skip'));
}

// ── 5. ask_human submitted (answered, awaiting confirm) → .hg-submitted-line ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'ask_human', status: 'submitted',
      _hgUserResponse: 'my answer' },
  ], false);
  const d = frag(html);
  check('hg_submitted_line', !!d.querySelector('.hg-submitted-line'));
  check('hg_submitted_spinner', !!d.querySelector('.hg-submitted-spinner'));
  check('hg_submitted_answer', html.includes('my answer'));
}

// ── 6. a parallel batch (same llmRound) → ptool-turn with collapsible head ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 7, toolName: 'read_files', status: 'done', query: 'a' },
    { roundNum: 2, llmRound: 7, toolName: 'grep_search', status: 'done', query: 'b' },
    { roundNum: 3, llmRound: 7, toolName: 'list_dir', status: 'done', query: 'c' },
  ], false);
  const d = frag(html);
  const turns = d.querySelectorAll('.ptool-turn');
  check('one_turn_for_batch', turns.length === 1);
  check('batch_size_attr', turns[0].getAttribute('data-batch-size') === '3');
  check('parallel_head_present', !!d.querySelector('.ptool-turn-head'));
  check('three_lines_in_turn', d.querySelectorAll('.ptool-line').length === 3);
  check('routine_parallel_batch_auto_collapsed',
    turns[0].classList.contains('collapsed')
    && d.querySelector('.ptool-turn-head').getAttribute('aria-expanded') === 'false');
  document.body.appendChild(d);
  d.querySelector('.ptool-turn-head').dispatchEvent(new Event('click', { bubbles: true }));
  check('routine_parallel_batch_reader_can_expand',
    !turns[0].classList.contains('collapsed')
    && d.querySelector('.ptool-turn-head').getAttribute('aria-expanded') === 'true');
  d.remove();
}

// ── 7. solo turns get NO parallel header (each its own ptool-turn, size 1) ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 1, toolName: 'web_search', status: 'done', query: 'a' },
    { roundNum: 2, llmRound: 2, toolName: 'web_search', status: 'done', query: 'b' },
  ], false);
  const d = frag(html);
  check('two_solo_turns', d.querySelectorAll('.ptool-turn').length === 2);
  check('no_parallel_head_for_solo', d.querySelector('.ptool-turn-head') === null);
}

// ── 7b. attention hierarchy: noisy observation-only panels fold; anything
// active, failed, interactive, or semantically important stays exposed. ──
{
  const routine = Array.from({ length: 4 }, (_, i) => ({
    roundNum: i + 1, llmRound: i, toolCallId: 'routine-' + i,
    toolName: 'read_files', attentionKind: 'routine', status: 'done',
    query: 'read ' + i, results: [{}],
  }));
  const routineDom = frag(renderToolRoundsHTML(routine, false));
  const panel = routineDom.querySelector('.ptool-panel');
  check('routine_panel_auto_collapsed', panel.classList.contains('collapsed'));
  check('routine_panel_disclosure_state',
    panel.getAttribute('data-attention') === 'routine'
    && panel.querySelector('.ptool-panel-header').getAttribute('aria-expanded') === 'false'
    && !!panel.querySelector('.ptool-panel-routine'));
  document.body.appendChild(routineDom);
  const routineHeader = panel.querySelector('.ptool-panel-header');
  routineHeader.dispatchEvent(new Event('click', { bubbles: true }));
  check('routine_panel_reader_can_expand',
    !panel.classList.contains('collapsed')
    && routineHeader.getAttribute('aria-expanded') === 'true');
  routineHeader.dispatchEvent(new Event('click', { bubbles: true }));
  check('routine_panel_reader_can_collapse_again',
    panel.classList.contains('collapsed')
    && routineHeader.getAttribute('aria-expanded') === 'false');
  routineDom.remove();

  const writeDom = frag(renderToolRoundsHTML([
    ...routine,
    { roundNum: 5, llmRound: 4, toolCallId: 'write-1',
      toolName: 'write_file', attentionKind: 'important', status: 'done',
      query: 'write result', results: [{}] },
  ], false));
  check('important_panel_stays_exposed',
    !writeDom.querySelector('.ptool-panel').classList.contains('collapsed'));

  const errorDom = frag(renderToolRoundsHTML([
    ...routine,
    { roundNum: 5, llmRound: 4, toolCallId: 'error-1',
      toolName: 'read_files', attentionKind: 'routine', status: 'error',
      query: 'failed read', toolContent: 'boom' },
  ], false));
  check('error_panel_stays_exposed',
    !errorDom.querySelector('.ptool-panel').classList.contains('collapsed')
    && errorDom.querySelector('.ptool-panel').getAttribute('data-attention') === 'error');

  check('unknown_legacy_tool_fails_visible',
    toolRoundAttention({ toolName: 'future_mutator', status: 'done' }) === 'important');
  check('live_state_overrides_routine_semantics',
    toolRoundAttention({ toolName: 'read_files', attentionKind: 'routine',
      status: 'searching' }) === 'active');
}

// ── 8. truncation: >100 inactive rounds → ptool-truncated marker + only 50 shown ──
{
  const rounds = [];
  for (let i = 1; i <= 130; i++) {
    rounds.push({ roundNum: i, llmRound: i, toolName: 'web_search',
      status: 'done', query: 'q' + i });
  }
  const html = renderToolRoundsHTML(rounds, false);
  const d = frag(html);
  check('truncated_marker', !!d.querySelector('.ptool-truncated'));
  check('truncated_hidden_count', d.querySelector('.ptool-truncated')
    .getAttribute('data-hidden-count') === '80');   // 130 - 50
  check('truncated_full_count', d.querySelector('.ptool-panel-body')
    .getAttribute('data-full-count') === '130');
  check('truncated_shows_50', d.querySelectorAll('.ptool-line').length === 50);
}

// ── 9. a swarm round renders the swarm panel inline (data-prn-kind="swarm") ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'spawn_agents', status: 'done', _swarm: true,
      _swarmActive: false,
      _swarmAgents: [
        { id: 'a1', role: 'coder', status: 'done', objective: 'X' },
        { id: 'a2', role: 'researcher', status: 'done', objective: 'Y' },
      ] },
  ], false);
  const d = frag(html);
  check('swarm_slot_kind', !!d.querySelector('[data-prn-kind="swarm"]'));
  check('swarm_panel_inline', !!d.querySelector('.sw-panel'));
}

// ── 10b. aborted round (dangling, swept by backend) → interrupted, NOT running ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'run_command', status: 'aborted',
      query: '$ sleep 30', results: [{ toolName: 'run_command',
        interrupted: true, source: 'Interrupted' }] },
  ], false);
  const d = frag(html);
  check('aborted_interrupted_line', !!d.querySelector('.ptool-interrupted'));
  check('aborted_interrupted_badge', !!d.querySelector('.ptool-badge-interrupted'));
  // The cardinal symptom of the bug: it must NOT render the "Running…" block.
  check('aborted_no_running_block', !d.querySelector('.ptool-cmd-running'));
  check('aborted_no_spinner', !d.querySelector('.ptool-spinner'));
  check('aborted_query_present', html.includes('sleep 30'));
}

// ── 10c. an aborted round that DID get real results still renders them ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'run_command', status: 'aborted',
      query: '$ echo hi',
      results: [{ toolName: 'run_command', command: 'echo hi',
        output: 'hi', exitCode: '0' }] },
  ], false);
  // Has real results (no `interrupted` flag) → should fall through to the
  // normal command renderer, not the interrupted stub.
  check('aborted_with_results_not_interrupted',
    !frag(html).querySelector('.ptool-interrupted'));
}

// ── 10d. superseded orphan (FloorRetry/stream-retry dup) → DROPPED entirely ──
{
  // A result-less round reconcile stamped badge='superseded'. It must NOT
  // render — no interrupted chip, no line. Its real twin is the adopted call.
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'read_files', status: 'aborted',
      query: 'Read a.py',
      results: [{ toolName: 'read_files', badge: 'superseded',
        source: 'Interrupted', interrupted: true, fetched: false, fetchedChars: 0 }] },
  ], false);
  // The ONLY round was a superseded orphan → whole panel collapses to ''.
  check('superseded_orphan_dropped_blank', html === '');
}

// ── 10e. superseded orphan among real rounds → only it is dropped ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 1, toolName: 'read_files', status: 'done',
      query: 'real one', results: [{ title: 'ok' }] },
    { roundNum: 2, llmRound: 1, toolName: 'grep_search', status: 'aborted',
      query: 'orphan dup',
      results: [{ badge: 'superseded', source: 'Interrupted',
        interrupted: true, fetched: false, fetchedChars: 0 }] },
  ], false);
  const d = frag(html);
  check('superseded_sibling_dropped', d.querySelectorAll('.ptool-line').length === 1);
  check('superseded_real_kept', html.includes('real one'));
  check('superseded_orphan_text_absent', !html.includes('orphan dup'));
  check('superseded_no_interrupted_chip', !d.querySelector('.ptool-interrupted'));
  check('superseded_count_reflects_drop', d.querySelector('.ptool-panel-body')
    .getAttribute('data-full-count') === '1');
}

// ── 10f. genuine user-Stop interruption (badge='interrupted') → KEPT ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'run_command', status: 'aborted',
      query: '$ sleep 99', results: [{ toolName: 'run_command',
        badge: 'interrupted', interrupted: true, source: 'Interrupted' }] },
  ], false);
  const d = frag(html);
  // A real Stop is NOT a superseded orphan → keeps its interrupted affordance.
  check('genuine_interrupted_kept', !!d.querySelector('.ptool-interrupted'));
  check('genuine_interrupted_badge', !!d.querySelector('.ptool-badge-interrupted'));
}

// ── 10. mixed timeline: tool + swarm + tool keeps chronological order ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 1, toolName: 'web_search', status: 'done', query: 'first' },
    { roundNum: 2, llmRound: 2, toolName: 'spawn_agents', status: 'done', _swarm: true,
      _swarmActive: false, _swarmAgents: [{ id: 'a1', role: 'coder', status: 'done' }] },
    { roundNum: 3, llmRound: 3, toolName: 'read_files', status: 'done', query: 'last' },
  ], false);
  const d = frag(html);
  const slots = [...d.querySelectorAll('[data-prn]')];
  check('three_slots', slots.length === 3);
  check('chrono_order', slots[0].getAttribute('data-prn') === '1' &&
    slots[1].getAttribute('data-prn') === '2' &&
    slots[2].getAttribute('data-prn') === '3');
  check('middle_is_swarm', slots[1].getAttribute('data-prn-kind') === 'swarm');
}

// ── 11. browser family classification (2026-08-05 'Read ?' incident) ──
// The v2 action tools were once missing from the browser presentation family:
// generic lightning icon, a name-mangled label, and a spurious "✓ done"
// badge. Pin the whole family + the family glyph + the badge carve-out.
{
  const v2 = ['browser_click', 'browser_type', 'browser_press_key',
    'browser_menu_click', 'browser_fill_form'];
  check('browser_family_v2_actions',
    v2.every((n) => isBrowserToolRound({ toolName: n })));
  // A done browser_click renders the CLICK glyph, not the generic lightning.
  const clickHtml = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'browser_click', status: 'done',
      query: 'Click current tab: 登录', results: [] },
  ], false);
  check('browser_click_glyph', clickHtml.includes('M4 4l7.07 16.97'));
  check('browser_click_not_generic_glyph', !clickHtml.includes('l9.9-10.2'));
  // Browser family is excluded from the generic "✓ done" fallback badge.
  check('browser_click_no_spurious_done_badge', !clickHtml.includes('✓ done'));
  // browser_press_key renders the keyboard glyph.
  const keyHtml = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'browser_press_key', status: 'done',
      query: 'Press Enter (current tab)', results: [] },
  ], false);
  check('browser_press_key_glyph', keyHtml.includes('width="20" height="12"'));
}

// ── 12. failed tool round (2026-08-06 silent-timeout incident) ──
// A tool that RAN but never produced a result (raised / pool-ceiling
// cancelled) used to ship tool_complete with NO status → reducer promoted it
// to 'done' → the row rendered as a clean success; the failure was visible
// only in the raw debug panel. The backend now stamps status='error' and the
// row must render a static failure affordance with the reason INLINE.
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'get_conversation', status: 'error',
      query: 'Open conversation msebjymx',
      toolContent: 'Tool execution timed out: get_conversation' },
  ], false);
  const d = frag(html);
  check('error_line_class', !!d.querySelector('.ptool-error'));
  check('error_badge_err', !!d.querySelector('.ptool-badge-err'));
  check('error_reason_inline',
    html.includes('Tool execution timed out: get_conversation'));
  // The cardinal symptoms of the incident: no done badge, no spinner, no
  // perpetual-searching panel.
  check('error_no_done_badge', !html.includes('✓ done'));
  check('error_no_spinner', !d.querySelector('.ptool-spinner'));
  check('error_not_active_panel', !d.querySelector('.ptool-panel-active'));
}

// ── 12b. an error round with NO toolContent still renders the badge ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'grep_search', status: 'error', query: 'grep foo' },
  ], false);
  const d = frag(html);
  check('error_no_content_badge', !!d.querySelector('.ptool-badge-err'));
  check('error_no_content_no_done', !html.includes('✓ done'));
}

// ── 12c. a failed sibling inside a parallel batch leaves the done one alone ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 1, toolName: 'grep_search', status: 'error',
      query: 'bad one', toolContent: 'Tool execution error: boom' },
    { roundNum: 2, llmRound: 1, toolName: 'read_files', status: 'done',
      query: 'good one', results: [{ title: 'ok' }] },
  ], false);
  const d = frag(html);
  check('error_batch_error_row', !!d.querySelector('.ptool-error'));
  check('error_batch_done_kept', html.includes('good one'));
  check('error_batch_reason', html.includes('Tool execution error: boom'));
}

// ── 13. local knowledge uses the grounded-search card, not a bare tool row ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'search_knowledge', status: 'done',
      query: 'Searching local knowledge: 报销制度',
      results: [{ title: '差旅政策.pdf', source: '第 3 节',
        snippet: '差旅发票应在三十天内提交。', fetched: true, fetchedChars: 15 }] },
  ], false);
  const d = frag(html);
  check('knowledge_search_result_card', !!d.querySelector('.search-result-item'));
  check('knowledge_search_source_visible', html.includes('差旅政策.pdf') && html.includes('第 3 节'));
  check('knowledge_search_excerpt_visible', html.includes('差旅发票应在三十天内提交'));
  check('knowledge_search_book_glyph', html.includes('M4 19.5A2.5 2.5'));
}

// ── 14. todo revisions project to one live checklist card ──
{
  const todoRound = (n, done, extra = {}) => ({
    roundNum: n, llmRound: n, toolName: 'todo_write', status: 'done',
    toolCallId: `todo-${n}`, query: `Checklist revision ${n}`,
    results: [{ toolName: 'todo_write', source: 'Checklist', badge: `${done}/2`,
      todos: [
        { id: 'a', content: 'A', status: done >= 1 ? 'completed' : 'in_progress' },
        { id: 'b', content: 'B', status: done >= 2 ? 'completed' : 'pending' },
      ], ...extra }],
  });
  const html = renderToolRoundsHTML([
    todoRound(1, 0),
    { roundNum: 2, llmRound: 2, toolName: 'read_files', status: 'done',
      toolCallId: 'read-2', query: 'Read config', results: [{ title: 'ok' }] },
    todoRound(3, 1, { todoRevision: 2 }),
    todoRound(4, 2, { todoRevision: 3, rootCompleted: true }),
  ], false);
  const d = frag(html);
  check('todo_revisions_one_card', d.querySelectorAll('.ptool-todo-block').length === 1);
  check('todo_revisions_latest_state', html.includes('2/2'));
  check('todo_revisions_counted_once', d.querySelector('.ptool-panel-body')
    && d.querySelector('.ptool-panel-body').getAttribute('data-full-count') === '2');
  check('todo_revision_history_chip', !!d.querySelector('.ptool-todo-revisions'));
  check('todo_revision_history_details', !!d.querySelector('.ptool-todo-history'));
  check('todo_non_todo_round_kept', html.includes('Read config'));
}

// ── 14b. rejected checklist protocol remains model/debug-only ──
{
  const accepted = {
    roundNum: 1, llmRound: 1, toolName: 'todo_write', status: 'done',
    toolCallId: 'todo-accepted', query: 'Checklist revision 1',
    results: [{ toolName: 'todo_write', source: 'Checklist', badge: '0/2',
      todoRevision: 1, todoUpdateCount: 1, todos: [
        { id: 'spec', content: 'Add regression', status: 'in_progress' },
        { id: 'verify', content: 'Run tests', status: 'pending' },
      ] }],
  };
  const rejected = {
    ...accepted, roundNum: 2, llmRound: 2, toolCallId: 'todo-rejected',
    results: [{ ...accepted.results[0], todoRejected: true,
      todoRejectReason: 'sync cannot remove unfinished items (verify); use replan with a reason' }],
  };
  const html = renderToolRoundsHTML([accepted, rejected], false);
  const d = frag(html);
  check('todo_rejection_still_projects_one_authoritative_card',
    d.querySelectorAll('.ptool-todo-block').length === 1);
  check('todo_rejection_reason_is_not_human_visible',
    !html.includes('sync cannot remove unfinished')
    && !html.includes('use replan with a reason'));
  check('todo_rejection_has_neutral_human_outcome',
    html.includes('todo.unchanged'));
  check('todo_rejection_does_not_inflate_revision_count',
    !d.querySelector('.ptool-todo-revisions')
    && !d.querySelector('.ptool-todo-history'));
}

// A specialized search card keeps its existing one-click disclosure. It may
// format the same settled result, but must not grow a second nested raw-result
// disclosure (the UI regression reported after the authority fix).
{
  const html = renderToolRoundsHTML([{
    roundNum: 99, toolCallId: 'truth-search', toolName: 'web_search',
    status: 'done', query: 'human-friendly result',
    toolContent: JSON.stringify({ title: 'Human-friendly search card',
      url: 'https://example.com' }),
    results: [{ title: 'Human-friendly search card', url: 'https://example.com' }],
  }], false);
  const d = frag(html);
  check('specialized_slot_stays_single_disclosure',
    d.querySelectorAll('.ptool-results-block').length === 1
    && d.querySelectorAll('.ptool-results-header[aria-expanded]').length === 1
    && !html.includes('conversation-tool__authoritative-result')
    && html.includes('Human-friendly search card'));
}

report();
"""


def test_tool_result_owner_and_retained_round_adapter():
    source = TOOL_RESULT_OWNER.read_text(encoding='utf-8')
    assert 'runtimeScope' not in source
    assert 'globalThis' not in source
    assert 'window.' not in source
    assert 'document.' not in source
    if shutil.which('node') is None:
        pytest.skip('node is required for the typed tool-result contract')
    owner_process = subprocess.run(
        [shutil.which('node'), '-e', _OWNER_HARNESS],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            'OWNER_SOURCE': TOOL_RESULT_OWNER_JS.read_text(encoding='utf-8'),
        },
    )
    assert owner_process.returncode == 0, owner_process.stderr
    owner_failures = [
        line for line in owner_process.stdout.splitlines()
        if line.startswith('FAIL ')
    ]
    assert not owner_failures, owner_process.stdout
    owner_passes = [
        line for line in owner_process.stdout.splitlines()
        if line.startswith('PASS ')
    ]
    assert len(owner_passes) == 29, owner_process.stdout

    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_BODY,
        extra_targets=[
            os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),
            os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js'),
        ],
        expect_pass=86,
        label='tool_rounds render',
    )
