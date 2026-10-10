#!/usr/bin/env python3
"""tests/context/test_system_note_injection_rounds.py — engine-authored injection
notes render as their OWN injection rounds in the settled legacy timeline:
correct per-kind labels, reason-specialized titles, action badges, and wire
positions between (never inside) the llmRound batches they precede.

THE bug this pins
-----------------
Before injection rounds (docs/RENDER_CONTRACT.md §System interventions), the
settled segment timeline (``renderSegmentTimelineHTML``) folded a
``system_note`` segment into the consuming round's execution batch, and the
legacy note renderer mapped EVERY note kind onto one of two labels —
``context-injection`` / ``session-context`` / ``system-intervention`` notes
all rendered as "系统介入 · 停滞纠偏" (intent-stall). Both confusions are
pinned fixed here:

  * batching: notes keyed by durable blockId form singleton batches
    (``computeExecutionBatches`` INJ keys), so a turn-start note stack sits
    ahead of round 0 and a mid-turn note sits between the two rounds it
    separates, each as its own collapsed row;
  * labels: all five note kinds resolve their own localized label/icon, the
    durable ``reason`` (registry id) specializes known titles
    (active_checklist / modified_files), and the action badge follows the
    recorded action — parity with the typed surface renderer.

Run::
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/context/test_system_note_injection_rounds.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
TR_JS = runtime_section_path('ui/tool_rounds.js')

_HAS_NODE = shutil.which('node') is not None

pytestmark = pytest.mark.unit


def _run_node(harness: str, env_extra: dict) -> str:
    env = dict(os.environ, **env_extra)
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\nSTDOUT:{proc.stdout}'
    return proc.stdout.strip()


def _extract_timeline_fns() -> str:
    """Same extraction contract as tests/tasks_pkg/test_inject_row_positioning.py leg 4:
    the vanilla-JS timeline functions plus the compiled native batching owner
    (its IIFE footer publishes computeExecutionBatches onto globalThis)."""
    src = open(TR_JS, encoding='utf-8').read()
    groups_native = open(native_module_path(
        '.native/tool-execution-groups.js',
        os.path.join(ROOT, 'frontend', 'src', 'conversation', 'presentation',
                     'tool-execution-groups.ts'),
    ), encoding='utf-8').read()
    timeline_native = open(native_module_path(
        '.native/segment-timeline.js',
        os.path.join(ROOT, 'frontend', 'src', 'conversation', 'ui',
                     'segment-timeline', 'index.ts'),
    ), encoding='utf-8').read()
    start = src.index('const _NOTE_STALL_ICON_SVG')
    end = src.index('\nfunction _renderUnifiedGroup(')
    chunk = timeline_native + '\n' + src[start:end]
    assert 'renderSegmentTimelineHTML' in chunk
    assert '_renderSegSystemNoteHTML' in chunk
    context_native = open(native_module_path(
        '.native/context-note-grouping.js',
        os.path.join(ROOT, 'frontend', 'src', 'conversation', 'presentation', 'context-note-grouping.ts'),
    ), encoding='utf-8').read()
    return groups_native + '\n' + context_native + '\n' + chunk


_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
// Echo the i18n key (and :n) so label/badge selection is directly observable.
function t(k, o){ return k + (o && o.n != null ? ':' + o.n : ''); }
function Icon(name, size){ return '<ICON name=' + name + ' size=' + size + '>'; }
function getToolRoundsFromMsg(m){ return (m && m.toolRounds) || []; }
function _createToolGroupRenderer(allRounds){ return {renderSlot:r=>_renderToolSlot(r,allRounds),renderGroups:rounds=>_renderToolGroupsHTML(rounds,allRounds)}; }
function _renderToolGroupsHTML(rounds, allRounds){
  return (rounds || []).map(function(r){ return '<TOOL name=' + (r.toolName||'') + '>'; }).join('');
}
function stripNoTranslateTags(text){ return text; }
function _isSupersededOrphanRound(r){ return false; }
function _renderToolSlot(r, ctx){ return '<SLOT name=' + (r.toolName||'') + '>'; }
"""

_HARNESS = _STUBS + r"""
eval(process.env.FN_SRC);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Turn-start note stack (llmRound 0) ahead of round 0, one mid-turn note
// between rounds 0 and 1, and a trailing todo-continuation before the
// deliverable. Entry order within the turn-start stack must be preserved.
const segments = [
  { type: 'system_note', blockId: 'system-note:session-context-0',
    noteKind: 'session-context', action: 'prompt', text: 'NOTE_SESSION', llmRound: 0 },
  { type: 'system_note', blockId: 'system-note:context-injection-1',
    noteKind: 'context-injection', action: 'prompt', reason: 'relevant_memories',
    text: 'NOTE_MEM', llmRound: 0 },
  { type: 'thinking', text: 'think0', llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'system_note', blockId: 'system-note:system-intervention-2',
    noteKind: 'system-intervention', action: 'prompt', text: 'NOTE_LOOP', llmRound: 1 },
  { type: 'text', text: 'narr1', llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'read_files', llmRound: 1 },
  { type: 'system_note', blockId: 'system-note:context-injection-3',
    noteKind: 'context-injection', action: 'prompt', reason: 'active_checklist',
    text: 'NOTE_CHECKLIST', llmRound: 2 },
  { type: 'system_note', blockId: 'system-note:todo-continuation-4',
    noteKind: 'todo-continuation', action: 'continue', text: 'NOTE_TODO', llmRound: 2 },
  { type: 'system_note', blockId: 'system-note:intent-stall-5',
    noteKind: 'intent-stall', action: 'prompt', text: 'NOTE_STALL', llmRound: 2 },
  { type: 'text', text: 'ANSWER', deliverable: true, terminal: true },
];
const msg = { toolRounds: [
  { roundNum: 1, llmRound: 0, toolCallId: 'a1', toolName: 'grep_search', status: 'done' },
  { roundNum: 2, llmRound: 1, toolCallId: 'b1', toolName: 'read_files', status: 'done' },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
function idx(n){ return html.indexOf(n); }

check('timeline_rendered', !!html);
// ── Injection-round positions: every note at its wire position ──
check('session_note_first', idx('NOTE_SESSION') >= 0);
check('turn_start_stack_order',
      idx('NOTE_SESSION') >= 0 && idx('NOTE_SESSION') < idx('NOTE_MEM'));
check('notes_before_round0_prose',
      idx('NOTE_MEM') >= 0 && idx('think0') >= 0 && idx('NOTE_MEM') < idx('think0'));
check('midturn_note_between_rounds',
      idx('<TOOL name=grep_search>') >= 0 && idx('NOTE_LOOP') > idx('<TOOL name=grep_search>')
      && idx('narr1') >= 0 && idx('NOTE_LOOP') < idx('narr1'));
check('trailing_notes_after_round1',
      idx('NOTE_CHECKLIST') > idx('<TOOL name=read_files>')
      && idx('NOTE_TODO') > idx('NOTE_CHECKLIST')
      && idx('NOTE_STALL') > idx('NOTE_TODO'));
check('deliverable_excluded', idx('ANSWER') === -1);
// ── Per-kind labels (parity with the typed surface) ──
check('label_session_context', idx('systemNote.sessionContextLabel') >= 0);
check('label_context_injection', idx('systemNote.contextInjectionLabel') >= 0);
check('label_system_intervention', idx('systemNote.interventionLabel') >= 0);
check('label_todo_continuation', idx('systemNote.todoContinuationLabel') >= 0);
check('label_intent_stall', idx('systemNote.intentStallLabel') >= 0);
// ── Reason-specialized title beats the generic kind label ──
check('reason_active_checklist_title', idx('systemNote.activeChecklistLabel') >= 0);
// ── Action badges follow the recorded action ──
check('badge_prompt', idx('systemNote.actionPrompt') >= 0);
check('badge_continue', idx('systemNote.actionContinue') >= 0);
// ── Exactly one intent-stall label (its own note), not a blanket fallback ──
check('no_blanket_stall_label', html.split('systemNote.intentStallLabel').length - 1 === 1);
// ── Tool rows still resolve and header counts real rounds only ──
check('tools_present', idx('<TOOL name=grep_search>') >= 0 && idx('<TOOL name=read_files>') >= 0);
check('header_counts_real_only', idx('toolPanel.toolsUsed:2') >= 0);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_settled_timeline_renders_injection_rounds_with_kind_labels():
    out = _run_node(_HARNESS, {'FN_SRC': _extract_timeline_fns()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'injection-round timeline failures:\n' + out
    assert out.count('PASS') >= 18, f'expected >=18 PASS, got:\n{out}'


_NC_HARNESS = _STUBS + r"""
// NEUTER: force the generic-fallback presentation (drop kind + reason
// resolution) so every note renders the generic intervention label, proving
// the kind/reason presentation maps are load-bearing for the labels above.
let src = process.env.FN_SRC;
const needle = 'const presentation = (Object.prototype.hasOwnProperty.call(_SEG_NOTE_REASON_PRESENTATION, reason)';
const start = src.indexOf(needle);
const endToken = "_SEG_NOTE_KIND_PRESENTATION['system-intervention'];";
const endIdx = src.indexOf(endToken, start);
if (start < 0 || endIdx < 0) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
const neutered = src.slice(0, start)
  + "const presentation = _SEG_NOTE_KIND_PRESENTATION['system-intervention'];"
  + src.slice(endIdx + endToken.length);
eval(neutered);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const segments = [
  { type: 'system_note', blockId: 'n0', noteKind: 'session-context',
    action: 'prompt', text: 'NOTE_SESSION', llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'text', text: 'ANSWER', deliverable: true, terminal: true },
];
const msg = { toolRounds: [
  { roundNum: 1, llmRound: 0, toolCallId: 'a1', toolName: 'grep_search', status: 'done' },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
check('nc_session_label_gone', html.indexOf('systemNote.sessionContextLabel') === -1);
check('nc_generic_label_present', html.indexOf('systemNote.interventionLabel') >= 0);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_NC_kind_presentation_map_is_load_bearing():
    out = _run_node(_NC_HARNESS, {'FN_SRC': _extract_timeline_fns()})
    assert 'FAIL nc_pattern_matched' not in out, (
        'NEUTER pattern drifted — update the presentation assignment sentinel:\n' + out)
    assert 'PASS nc_session_label_gone' in out, (
        'NEUTER control failed — kind presentation map is not load-bearing:\n' + out)
