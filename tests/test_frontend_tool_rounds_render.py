"""Characterization (regression) tests for the tool-round RENDER layer in
``static/js/ui/tool_rounds.js``.

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

This harness is the missing TWIN: it drives real ``rounds`` arrays (the exact
shape the SSE dispatcher produces) through the PUBLIC entry
``renderToolRoundsHTML(rounds, isStreaming)`` and asserts the resulting DOM
structure for every tool family + status. It locks the render contract so the
eventual decomposition of ``tool_rounds.js`` (next monolith target) has a
no-regression safety net for the shared typed Turn presentation path.

Runs the REAL shipped JS under jsdom via the shared harness; the swarm panel
builder lives in ui/streaming_swarm_panel.js, so that file is loaded first
(extra_target) in the same window scope, exactly as the production bundle
concatenates it before tool_rounds.js.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


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
      _rejected: { attempted: 'search_web', suggestions: ['web_search'] },
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
// The v2 action tools were missing from _isRoundBrowser: they fell to the
// generic lightning icon, a name-mangled label, and a spurious "✓ done"
// badge. Pin the whole family + the family glyph + the badge carve-out.
{
  const v2 = ['browser_click', 'browser_type', 'browser_press_key',
    'browser_menu_click', 'browser_fill_form'];
  check('browser_family_v2_actions',
    v2.every((n) => _isRoundBrowser({ toolName: n })));
  const legacy = ['browser_read_tab', 'browser_keyboard', 'browser_hover',
    'browser_wait', 'browser_summarize_page', 'browser_get_app_state',
    'browser_get_interactive_elements', 'browser_hover_and_click',
    'browser_right_click_menu'];
  check('browser_family_legacy_kept',
    legacy.every((n) => _isRoundBrowser({ toolName: n })));

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

// ── 15. batch-edit rows carry per-edit operation pills ──
// The header summarizes ops as text ("(2 edits: replace, …)"); each sub-row
// must carry a DESIGNED indicator: an icon + enum-label pill, amber for
// replace, green for pure insertions. Data source is the server-stamped
// editSummaries[].operation (index-aligned by construction), with the
// legacy batch tools deriving the op from the tool itself.
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'edit_file', status: 'done',
      query: 'Edit a.py (2 edits: replace, insert_after)',
      toolArgs: JSON.stringify({ edits: [
        { path: 'a.py', operation: 'replace', anchor: 'old', content: 'new' },
        { path: 'a.py', operation: 'insert_after', anchor: 'x', content: 'y' },
      ] }),
      results: [{ toolName: 'edit_file', badge: '2/2 edits', writeOk: true,
        editOperations: ['replace', 'insert_after'],
        editSummaries: [
          { path: 'a.py', description: '', status: 'ok', detail: '', operation: 'replace' },
          { path: 'a.py', description: '', status: 'ok', detail: '', operation: 'insert_after' },
        ] }] },
  ], false);
  const d = frag(html);
  const pills = d.querySelectorAll('.ptool-op');
  check('op_pill_per_row', pills.length === 2);
  check('op_pill_replace_kind', !!d.querySelector('.ptool-op--replace'));
  check('op_pill_insert_kind', !!d.querySelector('.ptool-op--insert'));
  check('op_pill_enum_labels', html.includes('>replace</span>') &&
    html.includes('>insert_after</span>'));
  check('op_pill_has_icon', d.querySelector('.ptool-op svg') !== null);
  check('op_pill_title_attr', !!d.querySelector('[title="operation: insert_after"]'));
}

// ── 15b. legacy batch tools derive the pill from the tool itself ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'insert_contents', status: 'done',
      query: 'Insert into a.py (2 insertions)',
      toolArgs: JSON.stringify({ edits: [
        { path: 'a.py', anchor: 'x', content: 'y', position: 'after' },
        { path: 'a.py', anchor: 'z', content: 'w', position: 'before' },
      ] }),
      results: [{ toolName: 'insert_contents', badge: '2/2 inserted', writeOk: true,
        editSummaries: [
          { path: 'a.py', description: '', status: 'ok', detail: '' },
          { path: 'a.py', description: '', status: 'ok', detail: '' },
        ] }] },
    { roundNum: 2, toolName: 'apply_diffs', status: 'done',
      query: 'Patch b.py (2 edits)',
      toolArgs: JSON.stringify({ edits: [
        { path: 'b.py', search: 's1', replace: 'r1' },
        { path: 'b.py', search: 's2', replace: 'r2' },
      ] }),
      results: [{ toolName: 'apply_diffs', badge: '2/2 edits', writeOk: true,
        editSummaries: [
          { path: 'b.py', description: '', status: 'ok', detail: '' },
          { path: 'b.py', description: '', status: 'ok', detail: '' },
        ] }] },
  ], false);
  const d = frag(html);
  check('legacy_insert_directions', html.includes('>insert_after</span>') &&
    html.includes('>insert_before</span>'));
  check('legacy_apply_diffs_replace_pill',
    d.querySelectorAll('.ptool-op--replace').length === 2);
  check('legacy_no_pill_spillover',
    d.querySelectorAll('.ptool-op').length === 4);
}

// ── 16. generic result viewer — settled rounds with toolContent expand ──
// read_files / grep_search / find_files / list_dir / browser reads / MCP
// tools used to fall through to a bare .ptool-line with the ENTIRE result
// invisible. The catch-all viewer renders any done round with a non-empty
// toolContent as a native <details>: standard row as summary, verbatim
// monospace result pane (stats + copy header) as body.
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'read_files', status: 'done',
      query: 'Read server.py',
      toolContent: 'File: server.py (lines 1-3 of 3)\nimport os\n<div>html</div>\nprint("hi")',
      results: [{ toolName: 'read_files', badge: '3L' }] },
  ], false);
  const d = frag(html);
  check('result_block_present', !!d.querySelector('details.ptool-result-block'));
  check('result_summary_row', !!d.querySelector('.ptool-result-block > summary.ptool-line'));
  check('result_content_visible', html.includes('import os'));
  check('result_html_escaped', html.includes('&lt;div&gt;html&lt;/div&gt;')
    && !html.includes('<div>html</div>'));
  check('result_pre_code', !!d.querySelector('.ptool-result-pre code'));
  check('result_copy_btn', !!d.querySelector('.ptool-result-block .copy-btn'));
  check('result_header_stats', !!d.querySelector('.ptool-result-block .code-header'));
  check('result_badge_kept', html.includes('3L'));
}

// ── 16b. grep_search done round → expandable result, badge preserved ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'grep_search', status: 'done',
      query: '/foo/ in *.py',
      toolContent: 'a.py:12: foo()\nb.py:30: foo(x)',
      results: [{ toolName: 'grep_search', badge: '2 matches' }] },
  ], false);
  const d = frag(html);
  check('grep_result_block', !!d.querySelector('.ptool-result-block'));
  check('grep_matches_visible', html.includes('a.py:12: foo()')
    && html.includes('b.py:30: foo(x)'));
  check('grep_badge_kept', html.includes('2 matches'));
}

// ── 16c. done round with EMPTY toolContent → bare line, no viewer ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'list_dir', status: 'done',
      query: 'List .', toolContent: '   ' },
  ], false);
  const d = frag(html);
  check('empty_content_no_block', !d.querySelector('.ptool-result-block'));
  check('empty_content_bare_line', !!d.querySelector('.ptool-line'));
}

// ── 16d. JSON toolContent is pretty-printed for readability ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'mcp__overleaf__compile', status: 'done',
      query: 'Compile project', toolContent: '{"ok":true,"pages":2}' },
  ], false);
  check('json_pretty_printed', html.includes('{\n  "ok": true,\n  "pages": 2\n}'));
}

// ── 16e. over-long results are soft-capped with a stated truncation note ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'read_files', status: 'done',
      query: 'Read huge.log', toolContent: 'x'.repeat(130000) },
  ], false);
  const d = frag(html);
  check('trunc_note_shown', !!d.querySelector('.ptool-result-trunc'));
  check('trunc_cap_enforced', d.querySelector('.ptool-result-pre code')
    .textContent.length === 120000);
}

// ── 16f. in-flight (searching) rounds never get the result viewer ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'read_files', status: 'searching',
      query: 'Reading…', toolContent: 'partial buffer' },
  ], true);
  check('searching_no_result_block', !frag(html).querySelector('.ptool-result-block'));
}

report();
"""


def test_tool_rounds_render_characterization():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_BODY,
        extra_targets=[
            os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),
            os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js'),
        ],
        min_pass=78,
        label='tool_rounds render',
    )
