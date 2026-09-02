#!/usr/bin/env python3
"""Interleaved per-tool timeline render (epic pt_8b406df8fbe24ae5, step 5).

THE user-visible objective: a finished multi-tool assistant turn must render
each tool's PRECEDING thinking + narration ADJACENT to that tool (inline),
instead of the legacy three grouped blocks (all tools / all thinking / all
content). This test extracts the REAL shipped `renderSegmentTimelineHTML`
(+ its helpers `_renderTimelineBatch` / `_roundsByToolCallId` /
`_segTimelineEnabled`) from tool_rounds.js and evals it in node with the
tool/markdown renderers stubbed to emit identifiable ORDER MARKERS, so we
assert the interleaving ORDER — not styling.

Asserted:
  1. INTERLEAVE: for a 2-batch turn, the output order is
     thinking0 → narration0 → tool(s of batch0) → narration1 → tool(batch1),
     i.e. each batch's prose sits immediately before its tool rows — NOT all
     thinking then all tools.
  2. DELIVERABLE EXCLUDED: the terminal deliverable text is NOT in the timeline
     (the caller renders the answer separately, after the panel).
  3. FALLBACK: renderSegmentTimelineHTML returns "" when segments carry tools
     that can't be matched to toolRounds (caller uses the legacy grouped path).
  4. NC: neuter the batch-flush so ALL segments collapse into one batch →
     the per-batch interleaving assertion fails → proves batching is
     load-bearing for the adjacency.
  5. CARD-MODEL ALIGNMENT (2026-07-08 redesign): the tools render as discrete
     quiet cards (no megabox, no spine/gutter), so narration/thinking flow as
     ordinary message body flush-left (x=0); a neuter re-injects the retired
     44px gutter and proves the invariant bites.
  6. COMPACT THINKING (2026-07-08): per-round thinking in the timeline is a
     quiet CHROME-LESS disclosure (no bordered card), so stacking many rounds
     doesn't tower over the tool cards; a neuter re-injects a border to prove
     the invariant bites (base + tofu theme).
  7. FLAT COMMAND BLOCK (2026-07-08): run_command / code_exec /
     browser_execute_js render as FLAT content inside the single `.ptool-turn`
     card — no nested bordered/rounded/shadowed inner card (the 'two boxes'
     the owner reported). A neuter re-injects `border-radius:12px` and proves
     the invariant bites (tofu + light).

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.dirname(HERE)
ROOT = _legacy_test_root()
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _extract_timeline_fns() -> str:
    """Extract the contiguous timeline helper block from tool_rounds.js:
    from `function _roundsByToolCallId(` through the end of
    `renderSegmentTimelineHTML` (just before `function _renderUnifiedGroup(`).
    The interleaved timeline is now the ONLY render path — the former
    `_segTimelineEnabled` feature-flag helper was removed, so extraction starts
    at the first surviving timeline helper."""
    src = open(TR_JS, encoding='utf-8').read()
    start = src.index('function _roundsByToolCallId(')
    end = src.index('\nfunction _renderUnifiedGroup(')
    chunk = src[start:end]
    assert 'renderSegmentTimelineHTML' in chunk, 'extraction missed the timeline fns'
    assert '_renderTimelineBatch' in chunk, 'extraction missed _renderTimelineBatch'
    # renderSegmentTimelineHTML now calls _isSupersededOrphanRound (shared with
    # renderToolRoundsHTML) to drop superseded orphan tool_use segments. That
    # predicate is defined LATER in the file, outside the timeline block, so
    # append the REAL function (not a stub) to the extracted chunk.
    sup_start = src.index('function _isSupersededOrphanRound(')
    sup_end = src.index('\nfunction renderToolRoundsHTML(')
    sup_chunk = src[sup_start:sup_end]
    assert '_isSupersededOrphanRound' in sup_chunk, 'extraction missed the shared predicate'
    # The timeline now consumes the shared todo-write display projection,
    # which lives with the grouped tool renderer earlier in this file. Include
    # the real helper so this isolated harness follows the shipped dependency
    # graph instead of failing before any timeline contract is exercised.
    todo_start = src.index('function _projectTodoRoundsForDisplay(')
    todo_end = src.index('\nfunction ', todo_start + 1)
    todo_chunk = src[todo_start:todo_end]
    return todo_chunk + '\n' + chunk + '\n' + sup_chunk


# Stubs for the collaborators the timeline helper calls. Each emits an
# identifiable marker so we can assert ORDER. _renderToolGroupsHTML emits one
# TOOL[name] marker per round it's given (preserving batch grouping).
_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
function t(k){ return k; }
function getToolRoundsFromMsg(m){ return (m && m.toolRounds) || []; }
function _toolPanelHeaderLabel(rounds, active){ return 'HDR'; }
// Decorative glyph factory (the shipped unicode→Icon migration) — the order
// assertions only need it to exist, not to draw anything.
function Icon(){ return ''; }
function _renderToolGroupsHTML(rounds, allRounds){
  return (rounds || []).map(function(r){ return '<TOOL name=' + (r.toolName||'') + '>'; }).join('');
}
// Real strip (mirrors static/js/ui/conversation_list.js) so the timeline's
// defense-in-depth call has the same behaviour it has in production.
function stripNoTranslateTags(text){
  if (!text) return text;
  return text
    .replace(/<\/?notranslate>/gi, '')
    .replace(/<\/?nt>/gi, '')
    .replace(/[\u27E6\[\(\{\u3010\u3014\u300A\u300C\u300E]\s*N\s*T\s*_\s*[0-9\uFF10-\uFF19]+\s*[\u27E7\]\)\}\u3011\u3015\u300B\u300D\u300F]/gi, '');
}
var localStorage = { _v: {}, getItem: function(k){ return this._v[k] == null ? null : this._v[k]; },
                     setItem: function(k,v){ this._v[k] = String(v); },
                     removeItem: function(k){ delete this._v[k]; } };
var config = {};
"""

_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A 2-batch finished turn: batch0 = thinking + narration + 2 tools; batch1 =
// narration + 1 tool; then the terminal deliverable answer.
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'narrate0', deliverable: false, llmRound: 0, translatedText: '第零轮译文' },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: { content: 'x', status: 'done' } },
  { type: 'tool_use', id: 'a2', name: 'read_files', input: '{}', llmRound: 0, result: { content: 'y', status: 'done' } },
  { type: 'text', text: 'narrate1', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', input: '{}', llmRound: 1, result: { content: 'z', status: 'done' } },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = {
  role: 'assistant', content: 'THE ANSWER', thinking: 'reason0',
  toolRounds: [
    { toolCallId: 'a1', toolName: 'grep_search', status: 'done', llmRound: 0 },
    { toolCallId: 'a2', toolName: 'read_files', status: 'done', llmRound: 0 },
    { toolCallId: 'b1', toolName: 'apply_diff', status: 'done', llmRound: 1 },
  ],
};

const html = renderSegmentTimelineHTML(segments, msg, 0);

check('nonempty', !!html && html.indexOf('ptool-panel') !== -1);

// Extract the ORDER of markers: thinking(reason0), narration(<md>narrateN),
// and TOOL[name].
function idx(needle){ return html.indexOf(needle); }
const iReason0   = idx('reason0');
// batch0's narration carries translatedText → renders the Chinese projection
// (第零轮译文), not the English source. The interleave ORDER assertions below
// track that translated node as batch0's narration position.
const iNarr0     = idx('<md>第零轮译文</md>');
const iToolGrep  = idx('<TOOL name=grep_search>');
const iToolRead  = idx('<TOOL name=read_files>');
const iNarr1     = idx('<md>narrate1</md>');
const iToolDiff  = idx('<TOOL name=apply_diff>');

// 1. INTERLEAVE: batch0 prose precedes batch0 tools, which precede batch1
//    prose, which precedes batch1 tool. This is the whole point.
check('thinking0_before_narr0', iReason0 >= 0 && iReason0 < iNarr0);
check('narr0_before_tools0',    iNarr0 >= 0 && iNarr0 < iToolGrep);
check('tools0_before_narr1',    iToolGrep < iNarr1 && iToolRead < iNarr1);
check('narr1_before_tool1',     iNarr1 >= 0 && iNarr1 < iToolDiff);
check('narr1_after_tools0',     iNarr1 > iToolRead);

// 2. DELIVERABLE EXCLUDED from the timeline (rendered separately by caller).
check('deliverable_excluded', html.indexOf('THE ANSWER') === -1);

// 3. All three tools present, in order.
check('all_tools_present', iToolGrep >= 0 && iToolRead >= 0 && iToolDiff >= 0);
check('tools_in_order', iToolGrep < iToolRead && iToolRead < iToolDiff);

// 4. PER-ROUND TRANSLATION (auto-translate ON): batch0's narration segment
//    carries translatedText → renders the Chinese in the .seg-narration slot,
//    NOT the English. batch1's narration has NO translatedText → stays English.
//    This is what keeps the SETTLED render interleaved-in-Chinese, matching the
//    streaming preview (no de-interleaved snap-back at finalize).
check('round0_narration_shows_translated', html.indexOf('<md>第零轮译文</md>') >= 0);
check('round0_english_not_shown', html.indexOf('<md>narrate0</md>') === -1);
check('round1_narration_falls_back_english', html.indexOf('<md>narrate1</md>') >= 0);

console.log(out.join('\n'));
"""

_FALLBACK_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Segments reference tools, but msg.toolRounds can't match ANY of them
// (unmatchable ids + no positional fallback because there ARE rounds but with
// different ids). Expect "" → caller falls back to the legacy grouped render.
const segments = [
  { type: 'tool_use', id: 'zzz', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
];
const msg = { role: 'assistant', content: 'a',
              toolRounds: [{ toolCallId: 'DIFFERENT', toolName: 'x', llmRound: 0 }] };
const html = renderSegmentTimelineHTML(segments, msg, 0);
check('fallback_empty_when_unmatchable', html === '');

// Empty segments → "".
check('empty_segments_empty', renderSegmentTimelineHTML([], msg, 0) === '');

// (The former `_segTimelineEnabled` flag helper was removed — the interleaved
// timeline is now the only user-facing render path, so there is no toggle
// contract left to assert here. renderSegmentTimelineHTML still returns "" for
// segment-less / unmatchable rows, which is the sole remaining fallback.)
console.log(out.join('\n'));
"""

_NC_HARNESS = _STUBS + r"""
// NEUTER: collapse the llmRound batch key so EVERY segment lands in one batch.
// Then the batch1 narration is emitted with batch0's prose (all prose before
// all tools) — the per-batch adjacency ('narr1 after tools0') breaks.
let src = process.env.TL_SRC;
// Force the batch key constant → single batch.
const neutered = src.replace(
  /const key = \(s\.llmRound != null\) \? \("L" \+ s\.llmRound\) : "S";/,
  'const key = "L0";');
if (neutered === src) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
  { type: 'text', text: 'narrate1', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', input: '{}', llmRound: 1, result: {} },
];
const msg = { role: 'assistant', content: 'a', toolRounds: [
  { toolCallId: 'a1', toolName: 'grep_search', llmRound: 0 },
  { toolCallId: 'b1', toolName: 'apply_diff', llmRound: 1 },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
const iNarr1 = html.indexOf('<md>narrate1</md>');
const iToolGrep = html.indexOf('<TOOL name=grep_search>');
// With one collapsed batch, ALL prose (incl. narrate1) is emitted BEFORE all
// tools → narrate1 comes BEFORE grep_search, breaking the interleave.
check('nc_collapsed_breaks_interleave', iNarr1 >= 0 && iToolGrep >= 0 && iNarr1 < iToolGrep);
console.log(out.join('\n'));
"""


_NC_TRANSLATED_HARNESS = _STUBS + r"""
// NEUTER: strip the per-round translatedText read from _renderTimelineBatch so
// it ALWAYS renders the English `s.text`, never the Chinese projection. This
// proves the translatedText branch is what carries the translation into the
// settled .seg-narration slot (not incidental). With it neutered, a segment
// carrying translatedText must show ENGLISH, so the SETTLED view would snap
// back to English narration at finalize — the exact regression we guard.
let src = process.env.TL_SRC;
const neutered = src.replace(
  /const _segText = \(s\.translatedText && s\.translatedText\.trim\(\)\) \? s\.translatedText : s\.text;/,
  'const _segText = s.text;');
if (neutered === src) { console.log('FAIL nc_translated_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const segments = [
  { type: 'text', text: 'narrate0', deliverable: false, llmRound: 0, translatedText: '第零轮译文' },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'a', toolRounds: [
  { toolCallId: 'a1', toolName: 'grep_search', llmRound: 0 },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
// Neutered: English shown, Chinese NOT — proving the translatedText read is
// load-bearing for the interleaved settled translation.
check('nc_neuter_shows_english', html.indexOf('<md>narrate0</md>') >= 0);
check('nc_neuter_hides_translated', html.indexOf('<md>第零轮译文</md>') === -1);
console.log(out.join('\n'));
"""


_NT_STRIP_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A settled narration whose persisted translatedText still carries protective
// markers the translation LLM mangled/localized: a raw <nt> tag, a full-width
// ⟦NT_0⟧ placeholder, and a CJK-localized 【NT_1】 form (all documented in
// lib/translate/notranslate.py as things cheap models leave behind). The
// SETTLED tool-log render must strip them before renderMarkdown, exactly like
// the streaming preview + bilingual view do — otherwise the marker leaks into
// the interleaved timeline (the reported bug).
const dirty = '第零轮<nt></nt>译文 ⟦NT_0⟧ 保留段 【NT_1】 末尾';
const segments = [
  { type: 'text', text: 'narrate0', deliverable: false, llmRound: 0, translatedText: dirty },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'a', toolRounds: [
  { toolCallId: 'a1', toolName: 'grep_search', llmRound: 0 },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);

// The kept text survives; every marker form is gone.
check('kept_text_present', html.indexOf('第零轮') >= 0 && html.indexOf('末尾') >= 0);
check('nt_tag_stripped', html.indexOf('<nt>') === -1 && html.indexOf('</nt>') === -1);
check('placeholder_stripped', html.indexOf('⟦NT_0⟧') === -1);
check('localized_placeholder_stripped', html.indexOf('【NT_1】') === -1);
check('no_bare_NT_token', html.indexOf('NT_') === -1);
console.log(out.join('\n'));
"""


_NC_NT_STRIP_HARNESS = _STUBS + r"""
// NEUTER: remove the stripNoTranslateTags call from _renderTimelineBatch so the
// raw translatedText goes straight to renderMarkdown. The marker must then LEAK
// into the output — proving the strip is load-bearing (the exact regression).
let src = process.env.TL_SRC;
const neutered = src.replace(
  /const _segClean = \(typeof stripNoTranslateTags === 'function'\) \? stripNoTranslateTags\(_segText\) : _segText;/,
  'const _segClean = _segText;');
if (neutered === src) { console.log('FAIL nc_nt_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const segments = [
  { type: 'text', text: 'narrate0', deliverable: false, llmRound: 0, translatedText: '第零轮 ⟦NT_0⟧ 末尾' },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'a', toolRounds: [
  { toolCallId: 'a1', toolName: 'grep_search', llmRound: 0 },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
// Without the strip, the placeholder leaks — this is what the user saw.
check('nc_neuter_leaks_placeholder', html.indexOf('⟦NT_0⟧') >= 0);
console.log(out.join('\n'));
"""


_SUPERSEDED_DROP_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Reproduce conversation mrx0kdufyo5niq's exact shape ──
// A FloorRetry/stream-retry duplicate: the model announced tc 'dup' (a
// list_dir), the attempt was discarded, and the backend reconcile stamped it
// badge='superseded' (result-less, status='aborted'). Its ADOPTED twin 'real'
// (same tool, same args) is the call that actually ran and completed. BOTH
// appear in msg.toolRounds; assemble_segments minted a tool_use segment for
// EACH (segment.result carries only {content,status} — NO badge). This is the
// segment-timeline render path (msg has segments), which does NOT run
// renderToolRoundsHTML's filter — so before the fix the superseded twin drew a
// misleading "interrupted" chip on a normally-completed (finishReason=stop) turn.
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  // superseded orphan tool_use — MUST be dropped (no chip)
  { type: 'tool_use', id: 'dup', name: 'list_dir', input: '{"path":"lib"}', llmRound: 0,
    result: { content: null, status: 'aborted' } },
  // the real adopted twin — MUST render
  { type: 'tool_use', id: 'real', name: 'list_dir', input: '{"path":"lib"}', llmRound: 0,
    result: { content: 'Directory: lib', status: 'done' } },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'THE ANSWER', finishReason: 'stop', toolRounds: [
  // superseded twin: result-less + badge='superseded' (what reconcile_announced_rounds stamps)
  { toolCallId: 'dup', toolName: 'list_dir', status: 'aborted', llmRound: 0, toolContent: null,
    results: [{ badge: 'superseded', source: 'Interrupted', interrupted: true,
                fetched: false, fetchedChars: 0 }] },
  // adopted twin: the real completed call
  { toolCallId: 'real', toolName: 'list_dir', status: 'done', llmRound: 0,
    toolContent: 'Directory: lib', results: [{ source: 'Project' }] },
]};

const html = renderSegmentTimelineHTML(segments, msg, 0);
check('nonempty', !!html && html.indexOf('ptool-panel') !== -1);
// The superseded twin renders NO chip; but a plain-stub _renderToolGroupsHTML
// emits '<TOOL name=list_dir>' per resolved round, so count the markers.
const nTools = (html.match(/<TOOL name=list_dir>/g) || []).length;
check('exactly_one_tool_rendered', nTools === 1);          // only the real twin
check('no_interrupted_chip', html.indexOf('ptool-badge-interrupted') === -1);
// Header count / data-full-count must EXCLUDE the superseded round (filtered
// out of realRounds EARLY), so render and count agree — not "2 tools" for 1 chip.
const m = html.match(/data-full-count="(\d+)"/);
check('data_full_count_present', !!m);
check('data_full_count_is_1', !!m && m[1] === '1');
console.log(out.join('\n'));
"""


_NC_SUPERSEDED_DROP_HARNESS = _STUBS + r"""
// NEUTER: force _isSupersededOrphanRound to ALWAYS return false, so the
// superseded twin is NOT filtered out of realRounds and its tool_use segment
// is NOT skipped. The misleading second chip must then reappear AND the header
// count must inflate to 2 — proving the shared-predicate filter is load-bearing
// for BOTH the render and the count (the exact coverage-gap regression).
let src = process.env.TL_SRC;
const neutered = src.replace(
  /function _isSupersededOrphanRound\(r\) \{/,
  'function _isSupersededOrphanRound(r) { return false;');
if (neutered === src) { console.log('FAIL nc_superseded_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const segments = [
  { type: 'tool_use', id: 'dup', name: 'list_dir', input: '{"path":"lib"}', llmRound: 0,
    result: { content: null, status: 'aborted' } },
  { type: 'tool_use', id: 'real', name: 'list_dir', input: '{"path":"lib"}', llmRound: 0,
    result: { content: 'Directory: lib', status: 'done' } },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'THE ANSWER', finishReason: 'stop', toolRounds: [
  { toolCallId: 'dup', toolName: 'list_dir', status: 'aborted', llmRound: 0, toolContent: null,
    results: [{ badge: 'superseded', source: 'Interrupted', interrupted: true,
                fetched: false, fetchedChars: 0 }] },
  { toolCallId: 'real', toolName: 'list_dir', status: 'done', llmRound: 0,
    toolContent: 'Directory: lib', results: [{ source: 'Project' }] },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
const nTools = (html.match(/<TOOL name=list_dir>/g) || []).length;
const m = html.match(/data-full-count="(\d+)"/);
// Neutered: BOTH twins render (2 tools) and the count inflates to 2.
check('nc_neuter_renders_both', nTools === 2);
check('nc_neuter_count_inflates_to_2', !!m && m[1] === '2');
console.log(out.join('\n'));
"""


def _run(harness: str) -> str:
    env = dict(os.environ, TL_SRC=_extract_timeline_fns())
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_timeline_interleaves_prose_adjacent_to_tools():
    out = _run(_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'interleave failures:\n' + out
    assert out.count('PASS') >= 12, f'expected >=12 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_timeline_fallback_when_unmatchable():
    out = _run(_FALLBACK_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'fallback failures:\n' + out
    assert out.count('PASS') >= 2, f'expected >=2 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_collapsed_batch_breaks_interleave():
    out = _run(_NC_HARNESS)
    assert 'PASS nc_collapsed_breaks_interleave' in out, (
        'NC control failed — collapsing batches did not break the interleave '
        '(so the per-batch flush is not what produces adjacency):\n' + out)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_stripping_translatedText_shows_english():
    out = _run(_NC_TRANSLATED_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, (
        'NC control failed — neutering the translatedText read did not fall '
        'back to English (so the translatedText branch is not what carries the '
        'settled per-round translation):\n' + out)
    assert out.count('PASS') >= 2, f'expected >=2 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_settled_narration_strips_notranslate_markers():
    """The settled interleaved tool-log render must strip <notranslate>/<nt>
    tags and ⟦NT_n⟧ placeholders (incl. mangled/localized 【NT_n】 forms) from
    the per-round translatedText before renderMarkdown — the same defense every
    other translated-content site applies. This was the one site that leaked
    the raw marker into the interleaved timeline at finalize (clean→dirty snap
    after a clean streaming preview)."""
    out = _run(_NT_STRIP_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'notranslate-strip failures:\n' + out
    assert out.count('PASS') >= 5, f'expected >=5 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_stripping_the_strip_leaks_the_marker():
    """NEUTER: drop the stripNoTranslateTags call from _renderTimelineBatch and
    confirm the ⟦NT_0⟧ placeholder LEAKS into the settled output — proving the
    strip is load-bearing, not incidental."""
    out = _run(_NC_NT_STRIP_HARNESS)
    assert 'PASS nc_neuter_leaks_placeholder' in out, (
        'NC control failed — removing the strip did not leak the placeholder '
        '(so the strip is not what prevents the marker leak):\n' + out)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_superseded_orphan_dropped_from_segment_timeline():
    """Root-fix for conversation mrx0kdufyo5niq: a FloorRetry/stream-retry
    superseded orphan (badge='superseded', result-less) must NOT render an
    'interrupted' chip in the SEGMENT-TIMELINE render path — parity with
    renderToolRoundsHTML's existing filter, via the SHARED
    _isSupersededOrphanRound predicate. Its adopted twin (same tool, done) is
    the real call and IS rendered. The header count / data-full-count must
    exclude the superseded round too (filtered out of realRounds early), so
    render and count agree (1 tool, count=1)."""
    out = _run(_SUPERSEDED_DROP_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'superseded-drop failures:\n' + out
    assert out.count('PASS') >= 5, f'expected >=5 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_unfiltered_superseded_orphan_reappears_and_inflates_count():
    """NEUTER: force _isSupersededOrphanRound → false and confirm the
    superseded twin reappears as a second chip AND the header count inflates to
    2 — proving the shared-predicate filter is load-bearing for BOTH the render
    and the count in the segment-timeline path (the coverage gap this fixes)."""
    out = _run(_NC_SUPERSEDED_DROP_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, (
        'NC control failed — neutering the superseded predicate did not '
        'reintroduce the second chip / inflate the count (so the filter is not '
        'what drops the orphan in the timeline path):\n' + out)
    assert out.count('PASS') >= 2, f'expected >=2 PASS, got:\n{out}'


# ── Live-status leak: a superseded husk whose LIVE status is 'done' ──
# The reconcile emits a tool_result SSE event (no status field), so the pure
# reducer's 'tool_result' case settles the LIVE round to status='done'. Only
# the backend's LOCAL entry becomes status='aborted' (→ what the persisted
# snapshot carries). The OLD predicate gated on status==='aborted', so during
# the live turn (status 'done') the husk LEAKED a misleading chip; it only
# vanished after the done-event/reload rewrote status to 'aborted'. The fix
# keys on badge+result-less regardless of status → dropped on BOTH paths.
_LIVE_DONE_LEAK_HARNESS = _STUBS + r"""
const src = process.env.TL_SRC;
eval(src);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A superseded husk exactly as the LIVE reducer leaves it: badge='superseded',
// result-less, but status='done' (the tool_result event carried no status, so
// reduceStreamState's tool_result case set status='done').
const liveHusk = { toolCallId: 'dup', toolName: 'list_dir', status: 'done',
  llmRound: 0, toolContent: null,
  results: [{ badge: 'superseded', source: 'Interrupted', interrupted: true,
              fetched: false, fetchedChars: 0 }] };
const realTwin = { toolCallId: 'real', toolName: 'list_dir', status: 'done',
  llmRound: 0, toolContent: 'Directory: lib', results: [{ source: 'Project' }] };

// The predicate must drop the live-'done' husk (the leak) …
check('drops_live_done_husk', _isSupersededOrphanRound(liveHusk) === true);
// … and must NOT drop the real completed twin …
check('keeps_real_twin', _isSupersededOrphanRound(realTwin) === false);
// … nor a still-in-flight round (searching, results=null → no badge).
check('keeps_inflight', _isSupersededOrphanRound(
  { toolCallId: 'x', toolName: 'grep_search', status: 'searching', results: null }) === false);

// End-to-end: the segment-timeline render with a LIVE-'done' husk must still
// render exactly ONE chip (the real twin), not two.
const segments = [
  { type: 'tool_use', id: 'dup', name: 'list_dir', input: '{"path":"lib"}', llmRound: 0,
    result: { content: null, status: 'done' } },
  { type: 'tool_use', id: 'real', name: 'list_dir', input: '{"path":"lib"}', llmRound: 0,
    result: { content: 'Directory: lib', status: 'done' } },
  { type: 'text', text: 'ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'ANSWER', finishReason: 'stop',
  toolRounds: [liveHusk, realTwin] };
const html = renderSegmentTimelineHTML(segments, msg, 0);
const nTools = (html.match(/<TOOL name=list_dir>/g) || []).length;
check('live_done_exactly_one_tool', nTools === 1);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_superseded_husk_dropped_even_when_live_status_done():
    """Root-fix: a superseded orphan whose LIVE status is 'done' (because the
    reconcile's tool_result SSE event carried no status, so the reducer settled
    the live round to 'done') must STILL be dropped. The old predicate gated on
    status==='aborted' and thus leaked the husk for the whole live turn — it
    only disappeared after the done-event/reload rewrote status to 'aborted'.
    Keying on badge+result-less drops it on both the live and persisted paths."""
    out = _run(_LIVE_DONE_LEAK_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'live-done husk leak failures:\n' + out
    assert out.count('PASS') >= 4, f'expected >=4 PASS, got:\n{out}'


def test_predicate_not_gated_on_aborted_status():
    """Guard the fix in source: _isSupersededOrphanRound must NOT gate on
    status==='aborted' (the old check that leaked the live-'done' husk). The
    authoritative signal is the 'superseded' badge + result-less."""
    src = open(TR_JS, encoding='utf-8').read()
    start = src.index('function _isSupersededOrphanRound(')
    end = src.index('\nfunction renderToolRoundsHTML(')
    body = src[start:end]
    assert 'r.status !== "aborted"' not in body, (
        'the predicate must not early-return on status!=="aborted" — that gate '
        'leaked the live-"done" husk (fixed by keying on badge+result-less)')
    assert 'meta.badge === "superseded"' in body, (
        'the predicate must key on the authoritative superseded badge')


def test_source_has_timeline_helper():
    src = open(TR_JS, encoding='utf-8').read()
    assert 'function renderSegmentTimelineHTML(' in src
    # The feature-flag helper was removed — the timeline is the only path now.
    assert 'function _segTimelineEnabled(' not in src, \
        '_segTimelineEnabled should be gone — the timeline is the only render path'
    # The settled narration render must apply the notranslate strip (the fix).
    assert 'stripNoTranslateTags(_segText)' in src, \
        'the settled seg-narration render must strip notranslate markers before renderMarkdown'
    # The segment-timeline path must reuse the SHARED superseded-orphan predicate
    # (parity with renderToolRoundsHTML) — not a duplicate/divergent check.
    assert '_isSupersededOrphanRound(r)' in src, \
        'renderSegmentTimelineHTML must filter superseded orphans via the shared predicate'
    assert '_supersededTcIds' in src, \
        'the segment walk must skip superseded tool_use segments by tc_id'


# ═══════════════════════════════════════════════════════════════════════════
#  SETTLED render — prose is a SIBLING of the tool card, NOT nested in it.
#
#  OWNER DIRECTIVE (2026-07-08): thinking + narration + each tool must be
#  INDEPENDENT sibling blocks — nothing may wrap "thinking + narration + a
#  tool" in one bordered box. In renderSegmentTimelineHTML the prose
#  (.seg-thinking / .seg-narration) is concatenated BEFORE `_renderToolGroups
#  HTML`'s `.ptool-turn` card, so it lands as a flat sibling in the panel body.
#  This test parses the REAL settled output into a jsdom DOM (with a
#  `_renderToolGroupsHTML` that emits real `.ptool-turn` markup) and asserts
#  NO .seg-thinking / .seg-narration node is a descendant of ANY .ptool-turn,
#  and each round's prose sits immediately BEFORE its card. NC: wrap the prose
#  inside the tool card and confirm the non-nesting assertion FAILS.
# ═══════════════════════════════════════════════════════════════════════════
def _run_jsdom(harness_body: str, extra_env=None) -> str:
    """Run a self-contained jsdom harness (requires node_modules/jsdom).

    TL_SRC (the extracted timeline fns) is passed via env, exactly like _run.
    """
    env = dict(os.environ, TL_SRC=_extract_timeline_fns())
    if extra_env:
        env.update(extra_env)
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE, delete=False,
                                     encoding='utf-8') as fh:
        harness_path = fh.name
        fh.write(harness_body)
    try:
        proc = subprocess.run(['node', harness_path, ROOT], capture_output=True,
                              text=True, timeout=30, env=env)
    finally:
        try:
            os.remove(harness_path)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


# jsdom harness: eval the timeline fns with a _renderToolGroupsHTML that emits
# REAL `.ptool-turn` cards (one per llmRound batch), parse the output into a
# DOM, and inspect structure. A `WRAP_PROSE` env flag turns on the NEUTER that
# nests the prose inside the card so we can prove the invariant bites.
_SETTLED_NESTING_HARNESS = r"""
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
if (typeof global.CSS === 'undefined') global.CSS = { escape: (s) => s };

function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<span class="md">' + String(s) + '</span>'; }
function t(k){ return k; }
function getToolRoundsFromMsg(m){ return (m && m.toolRounds) || []; }
function _toolPanelHeaderLabel(rounds, active){ return 'HDR'; }
function Icon(){ return ''; }
function stripNoTranslateTags(s){ return s; }
var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
var config = { segmentTimeline: true };

const WRAP = process.env.WRAP_PROSE === '1';
// Emit a REAL .ptool-turn card per round (grouped by llmRound), matching the
// shipped _renderToolGroupsHTML shape closely enough for the nesting check.
function _renderToolGroupsHTML(rounds, allRounds){
  const byRound = {};
  for (const r of (rounds || [])) {
    const k = (r.llmRound != null) ? ('L' + r.llmRound) : ('S' + r.roundNum);
    (byRound[k] = byRound[k] || []).push(r);
  }
  let html = '';
  for (const k in byRound) {
    const slots = byRound[k].map(r =>
      '<div data-prn="' + r.roundNum + '"><div class="ptool-line">' + (r.toolName||'') + '</div></div>').join('');
    html += '<div class="ptool-turn" data-llm-round="' + k + '">' + slots + '</div>';
  }
  return html;
}

let src = process.env.TL_SRC;
if (WRAP) {
  // NEUTER: make _renderTimelineBatch NEST the prose inside a .ptool-turn card
  // by wrapping the whole batch output — the "box the three together"
  // regression the owner rejected. Proves the sibling (non-nesting) assertion
  // has teeth.
  const _before = src;
  src = src.replace(
    /  return html;\n\}\n\n\/\* Render the interleaved/,
    '  return "<div class=\\"ptool-turn\\">" + html + "</div>";\n}\n\n/* Render the interleaved');
  if (src === _before) { console.log('FAIL nc_wrap_pattern_matched'); process.exit(0); }
}
eval(src);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Two-batch settled turn.
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'narrate0', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', input: '{}', llmRound: 0, result: {} },
  { type: 'text', text: 'narrate1', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', input: '{}', llmRound: 1, result: {} },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const msg = { role: 'assistant', content: 'THE ANSWER', thinking: 'reason0', toolRounds: [
  { toolCallId: 'a1', toolName: 'grep_search', status: 'done', roundNum: 1, llmRound: 0 },
  { toolCallId: 'b1', toolName: 'apply_diff', status: 'done', roundNum: 2, llmRound: 1 },
]};

const html = renderSegmentTimelineHTML(segments, msg, 0);
const wrap = document.createElement('div');
wrap.innerHTML = html;

const panelBody = wrap.querySelector('.ptool-panel-body');
check('nesting_panel_body_present', !!panelBody);

const thinks = wrap.querySelectorAll('.seg-thinking');
const narrs = wrap.querySelectorAll('.seg-narration');
check('nesting_has_thinking', thinks.length >= 1);
check('nesting_has_narration', narrs.length >= 2);

// ★ THE INVARIANT: NO prose block is a descendant of ANY .ptool-turn card.
let anyNested = false;
for (const el of thinks) if (el.closest('.ptool-turn')) anyNested = true;
for (const el of narrs)  if (el.closest('.ptool-turn')) anyNested = true;
check('prose_not_nested_in_any_ptool_turn', !anyNested);

// Each round's prose sits immediately BEFORE its tool card (interleave order).
const g0 = wrap.querySelector('.ptool-turn[data-llm-round="L0"]');
if (g0 && panelBody) {
  const kids = Array.prototype.slice.call(panelBody.children);
  const th0 = wrap.querySelector('.seg-thinking');
  const iTh0 = kids.indexOf(th0), iG0 = kids.indexOf(g0);
  check('prose_precedes_its_card', iTh0 >= 0 && iG0 >= 0 && iTh0 < iG0);
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(
    not (shutil.which('node')
         and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))),
    reason='node + jsdom not installed')
def test_settled_prose_is_sibling_not_nested_in_tool_card():
    """The settled render must keep thinking/narration as SIBLINGS of the
    .ptool-turn card (in the panel body), never nested inside it — the owner's
    "don't box the three together" directive."""
    out = _run_jsdom(_SETTLED_NESTING_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'settled nesting failures:\n' + out
    assert 'PASS prose_not_nested_in_any_ptool_turn' in out, out
    assert out.count('PASS') >= 5, f'expected >=5 PASS, got:\n{out}'


@pytest.mark.skipif(
    not (shutil.which('node')
         and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))),
    reason='node + jsdom not installed')
def test_NC_settled_nesting_prose_in_card_bites():
    """NEUTER: nest the prose inside the tool card and confirm the non-nesting
    invariant FAILS — proving the sibling assertion has teeth."""
    out = _run_jsdom(_SETTLED_NESTING_HARNESS, extra_env={'WRAP_PROSE': '1'})
    assert 'FAIL prose_not_nested_in_any_ptool_turn' in out, (
        'NC control failed — wrapping the prose in a .ptool-turn did not trip '
        'the non-nesting assertion (so the assertion is a tautology):\n' + out)


# ── Alignment invariant (env-independent; no browser needed) ──
# REDESIGN 2026-07-08 — chrome-less card model. The spine/ordinal-gutter grid
# was retired: each tool round is now a DISCRETE `.ptool-turn` card (display
# block, its own border + radius, breathing room), and there is no 28px
# ordinal gutter for prose to track. So the connective narration/thinking must
# now read as ORDINARY message body — flush to the message's LEFT EDGE (x=0),
# NOT pushed into the old 44px/28px tool-content column (which no longer
# exists). This test re-derives that from the source rules so a later edit
# can't silently reintroduce the "prose trapped in the panel gutter" look the
# owner reported, and a NEUTER proves the assertion actually bites.
_CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')


def _decl_value(css: str, selector: str, prop: str) -> str:
    """Return the value of `prop` in the FIRST rule whose selector text
    contains `selector` (exact-ish substring match on the selector list)."""
    # Find the rule block.
    i = 0
    while True:
        j = css.find(selector, i)
        assert j != -1, f'selector {selector!r} not found in styles.css'
        brace = css.find('{', j)
        # ensure the selector is the one immediately before this brace (no
        # intervening '}' — i.e. selector belongs to THIS rule head)
        if css.find('}', j, brace) == -1 and brace != -1:
            end = css.find('}', brace)
            block = css[brace + 1:end]
            m = re.search(r'(?:^|;)\s*' + re.escape(prop) + r'\s*:\s*([^;]+)', block)
            if m:
                return m.group(1).strip()
        i = brace + 1 if brace != -1 else j + len(selector)


def _px_tokens(value: str):
    return [float(x) for x in re.findall(r'(-?\d+(?:\.\d+)?)px', value)]


def _margin_tokens(value: str):
    """All length tokens of a margin shorthand, INCLUDING unitless zeros
    (`margin: 2px 0 8px 0` → [2.0, 0.0, 8.0, 0.0]). _px_tokens would drop the
    bare `0`s and misalign the shorthand, so parse the full token stream."""
    toks = []
    for tok in value.strip().split():
        m = re.match(r'^(-?\d+(?:\.\d+)?)(px)?$', tok)
        if m:
            toks.append(float(m.group(1)))
    return toks


def _seg_left_offsets(css: str):
    """Left margin of narration + thinking in the seg-timeline (margin: T R B L).
    A `margin: 2px 0 8px 0` (all-zero L) means prose flows flush with the
    message body — the new card-model target."""
    narr_margin = _margin_tokens(_decl_value(css, '.seg-timeline .seg-narration {', 'margin'))
    think_margin = _margin_tokens(_decl_value(css, '.seg-timeline .seg-thinking {', 'margin'))
    # margin shorthand: 1 token = all sides; 2 = V H; 3 = T H B; 4 = T R B L.
    def _left(toks):
        return toks[3] if len(toks) == 4 else (toks[1] if len(toks) >= 2 else toks[0])
    return _left(narr_margin), _left(think_margin)


def test_seg_narration_flows_as_message_body_flush_left():
    """New card-model invariant: narration + thinking sit flush to the message
    body (left offset 0), NOT in the retired 44/28px tool-content gutter."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    narr_left, think_left = _seg_left_offsets(css)

    assert narr_left == 0, (
        f'narration left ({narr_left}px) must be 0 — connective prose now flows '
        f'as ordinary message body, flush left. A 44px value is the RETIRED '
        f'tool-column gutter math (spine/grid model) the redesign removed.')
    assert think_left == 0, (
        f'thinking-block left ({think_left}px) must be 0 — same flush-left body '
        f'flow as narration (the 28px tool-box rail no longer exists).')

    # The round is a DISCRETE CARD, not a grid-with-gutter: display block +
    # its own border + radius (so rounds read as separated units, not a slab).
    turn_display = _decl_value(css, '.ptool-turn {', 'display')
    assert turn_display.strip() == 'block', (
        f'.ptool-turn display is {turn_display!r}; the card model needs "block" '
        f'(the retired spine model used "grid" with a 28px ordinal gutter).')
    turn_border = _decl_value(css, '.ptool-turn {', 'border')
    assert turn_border and turn_border.strip() != 'none', (
        '.ptool-turn must carry a hairline border so each round reads as a '
        'self-contained card.')
    assert _decl_value(css, '.ptool-turn {', 'border-radius'), (
        '.ptool-turn must have a border-radius (discrete rounded card).')

    # The spine that welded rounds into one column must be GONE.
    assert '.ptool-turn[data-batch-size="1"]::before' not in css, (
        'the timeline spine (.ptool-turn[data-batch-size="1"]::before) must be '
        'removed — it welded the rounds into a single slab.')

    # The megabox must be gone: the base .ptool-panel carries no border/bg.
    panel_border = _decl_value(css, '.ptool-panel {', 'border')
    assert panel_border.strip() == 'none', (
        f'.ptool-panel border is {panel_border!r}; the enclosing megabox must '
        f'be removed (border:none) so tools render as discrete cards, not one '
        f'colored box.')
    panel_bg = _decl_value(css, '.ptool-panel {', 'background')
    assert panel_bg.strip() == 'none', (
        f'.ptool-panel background is {panel_bg!r}; must be none (no megabox).')


def test_NC_seg_alignment_bites_on_reintroduced_gutter():
    """NEUTER: re-inject the retired 44px gutter into the narration margin and
    confirm the flush-left invariant FAILS — proving the assertion has teeth
    and isn't a tautology."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    # Rewrite ONLY the seg-narration margin to the retired gutter value.
    neutered = re.sub(
        r'(\.seg-timeline \.seg-narration \{\s*\n\s*margin:\s*)2px 0 8px 0',
        r'\g<1>2px 0 8px 44px', css, count=1)
    assert neutered != css, 'NC pattern did not match — test is stale'
    narr_left, _ = _seg_left_offsets(neutered)
    assert narr_left == 44, (
        'neuter did not reintroduce the gutter offset — cannot prove the '
        'invariant bites')
    # And the real assertion the positive test makes must now be violated.
    assert not (narr_left == 0), (
        'the flush-left invariant must FAIL on the neutered (44px) CSS')


def test_seg_thinking_is_compact_chromeless_disclosure():
    """2026-07-08 — per-round thinking in the interleaved timeline must be a
    QUIET inline disclosure, NOT the full bordered .thinking-block card.
    Stacking 12 full cards (2px border + clay shadow + 14px radius under tofu)
    was the 'too huge' look the owner reported. The .seg-timeline .seg-thinking
    rule must strip the card chrome (border/background) so the collapsed rows
    read as slim muted one-liners, and the tofu override must do the same."""
    css = open(_CSS_PATH, encoding='utf-8').read()

    # Base: chrome stripped from the interleaved variant.
    border = _decl_value(css, '.seg-timeline .seg-thinking {', 'border')
    assert border.strip() == 'none', (
        f'.seg-timeline .seg-thinking border is {border!r}; must be "none" so '
        f'per-round thinking is a chrome-less inline disclosure, not a stacked '
        f'card.')
    bg = _decl_value(css, '.seg-timeline .seg-thinking {', 'background')
    assert bg.strip() == 'none', (
        f'.seg-timeline .seg-thinking background is {bg!r}; must be "none".')

    # Tofu theme carries a higher-specificity clay card, so it needs its OWN
    # override or the box comes back on the tofu theme.
    tofu_border = _decl_value(css, '[data-theme="tofu"] .seg-timeline .seg-thinking{', 'border')
    assert tofu_border.strip() == 'none', (
        f'tofu .seg-timeline .seg-thinking border is {tofu_border!r}; the tofu '
        f'clay-card chrome ([data-theme="tofu"] .thinking-block) has higher '
        f'specificity and must be neutralized for the interleaved variant.')


def test_NC_seg_thinking_chrome_bites():
    """NEUTER: re-inject a card border onto .seg-timeline .seg-thinking and
    confirm the compact-disclosure invariant FAILS — proving it has teeth."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    neutered = re.sub(
        r'(\.seg-timeline \.seg-thinking \{[^}]*?\n\s*border:\s*)none',
        r'\g<1>1px solid red', css, count=1)
    assert neutered != css, 'NC pattern did not match — test is stale'
    border = _decl_value(neutered, '.seg-timeline .seg-thinking {', 'border')
    assert border.strip() != 'none', (
        'neuter did not reintroduce a border — cannot prove the invariant bites')


def test_command_blocks_are_flat_no_nested_card():
    """2026-07-08 — the run_command / code_exec / browser_execute_js blocks must
    be FLAT content inside the single `.ptool-turn` card, NOT a second bordered/
    rounded/shadowed card nested inside it (the 'two boxes' the owner reported).

    The base (dark) `.ptool-cmd-block` was already flat; only the tofu 'Washi
    Terminal v3' block and the light override re-painted an inner card. This
    re-derives from the rules that NO command-block selector in tofu OR light
    declares nested-card chrome (its own border-radius or a card background) —
    the `.ptool-turn` cream shell is the one and only card. Same 'no megabox'
    invariant this test already encodes for `.ptool-panel`, applied to the
    command block so the nested card can't silently return.

    `.ptool-cmd-js` (browser_execute_js) reuses `.ptool-cmd-block` (added as a
    second class), so the `.ptool-cmd-block` rules cover it too."""
    css = open(_CSS_PATH, encoding='utf-8').read()

    # Selector heads as they appear in styles.css (tofu = pretty multi-line,
    # light = minified single-line — both end with `{` right after the name).
    for sel in ('[data-theme="tofu"] .ptool-cmd-block{',
                '[data-theme="tofu"] .code-exec-block{',
                '[data-theme="light"] .ptool-cmd-block{',
                '[data-theme="light"] .code-exec-block{'):
        radius = _decl_value(css, sel, 'border-radius')
        assert radius is not None and radius.strip() in ('0', '0px'), (
            f'{sel} border-radius is {radius!r}; a flat command block must NOT '
            f'carry its own rounded corners — the .ptool-turn shell is the card.')
        bg = _decl_value(css, sel, 'background')
        assert bg is not None and bg.strip() == 'none', (
            f'{sel} background is {bg!r}; must be "none" (no nested parchment/'
            f'tint card behind the .ptool-turn shell).')

    # The tofu blocks must also drop the elevation shadow + hover elevation
    # (a shadow reads as a floating inner card even without a border).
    tofu_shadow = _decl_value(css, '[data-theme="tofu"] .ptool-cmd-block{', 'box-shadow')
    assert tofu_shadow is not None and tofu_shadow.strip() == 'none', (
        f'tofu .ptool-cmd-block box-shadow is {tofu_shadow!r}; must be "none" '
        f'(a drop shadow makes the block read as a nested card).')
    assert '[data-theme="tofu"] .ptool-cmd-block:hover{' not in css, (
        'the tofu .ptool-cmd-block:hover card-elevation rule must be removed — '
        'there is no nested card to elevate.')
    assert '[data-theme="tofu"] .code-exec-block:hover{' not in css, (
        'the tofu .code-exec-block:hover card-elevation rule must be removed.')


def test_NC_command_block_flat_bites_on_reintroduced_radius():
    """NEUTER: re-inject `border-radius: 12px` into the tofu .ptool-cmd-block
    rule and confirm the flat-card invariant FAILS — proving the assertion has
    teeth and isn't a tautology (the double-box regression is actually caught)."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    neutered = re.sub(
        r'(\[data-theme="tofu"\] \.ptool-cmd-block\{[^}]*?border-radius:\s*)0',
        r'\g<1>12px', css, count=1)
    assert neutered != css, 'NC pattern did not match — test is stale'
    radius = _decl_value(neutered, '[data-theme="tofu"] .ptool-cmd-block{', 'border-radius')
    assert radius.strip() == '12px', (
        'neuter did not reintroduce the nested-card radius — cannot prove the '
        'invariant bites')
    # And the real assertion the positive test makes must now be violated.
    assert radius.strip() not in ('0', '0px'), (
        'the flat-card invariant must FAIL on the neutered (12px radius) CSS')


def test_settings_toggle_is_wired():
    """The interleaved timeline is now the ONLY render path — the owner-facing
    toggle was removed. This test guards that the switch is fully gone end-to-
    end so no dead control, i18n key, sync, persist, or config gate lingers.
    (A leftover half-wired toggle is exactly the drift this asserts against.)"""
    idx_html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    assert 'id="settingSegmentTimeline"' not in idx_html, \
        'the Settings toggle checkbox should be removed from index.html'
    assert 'settings.segmentTimeline' not in idx_html, 'toggle i18n label still referenced'

    for locale in ('en.json', 'zh.json'):
        i18n = open(os.path.join(
            SOURCE_ROOT, 'frontend', 'src', 'i18n', 'locales', locale,
        ), encoding='utf-8').read()
        assert '"settings.segmentTimeline"' not in i18n \
            and '"settings.segmentTimelineDesc"' not in i18n, \
            f'segment-timeline i18n keys should be removed from {locale}'

    core = open(os.path.join(ROOT, 'static', 'js', 'settings', 'core_panel.js'), encoding='utf-8').read()
    assert 'settingSegmentTimeline' not in core and 'config.segmentTimeline' not in core, \
        'openSettings should no longer sync a segment-timeline toggle'

    save = open(os.path.join(ROOT, 'static', 'js', 'settings', 'save_export.js'), encoding='utf-8').read()
    assert 'settingSegmentTimeline' not in save and 'config.segmentTimeline' not in save, \
        'saveSettings should no longer persist a segment-timeline toggle'

    # The feature-flag reader must be gone (timeline is unconditional now).
    tr = open(TR_JS, encoding='utf-8').read()
    assert 'config.segmentTimeline' not in tr, \
        'the removed _segTimelineEnabled config gate still lingers in tool_rounds.js'


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        print(_run(_HARNESS))
        print(_run(_FALLBACK_HARNESS))
        print(_run(_NC_HARNESS))
