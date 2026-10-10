#!/usr/bin/env python3
"""tests/context/test_system_note_context_grouping.py — adjacent context-injection
notes merge into ONE disclosure row (display-only), in the settled legacy
timeline. Plan A: position-blind adjacency grouping.

THE bug this pins
-----------------
Every session-start context injection is recorded as its own durable
``system_note`` segment and rendered as its own collapsed ``系统介入 ·
已追加上下文`` row at its wire position (correct for timeline causality, but
pure noise when several arrive contiguously — the "user tail" of a session
start renders as N stacked rows). Plan A groups only *adjacent* context notes
at render time, so:

  * a run of >=2 contiguous ``context-injection`` / ``session-context`` notes
    collapses into ONE ``sw-note-group`` disclosure with a count pill and a
    per-item list inside (each item keeps its own reason + verbatim text);
  * the group still sits at the exact wire slot of its first member —
    positions, batch keys, and attempt ordinals are untouched (grouping runs
    inside the HTML builder, after ``computeExecutionBatches``);
  * intervention notes (stall nudge, todo-continuation, checklist re-pin,
    modified-files recall) and LONE context notes keep their own rows;
  * a run of one is never grouped (no gratuitous wrapper).

Run::
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/context/test_system_note_context_grouping.py -v
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
    """Same extraction contract as tests/context/test_system_note_injection_rounds.py:
    the vanilla-JS timeline functions plus the compiled native batching
    owner."""
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
    assert '_renderSegSystemNoteGroupHTML' in chunk
    assert '_groupSegContextNotes' in chunk
    context_notes_native = open(native_module_path(
        '.native/context-note-grouping.js',
        os.path.join(ROOT, 'frontend', 'src', 'conversation', 'presentation',
                     'context-note-grouping.ts'),
    ), encoding='utf-8').read()
    return groups_native + '\n' + context_notes_native + '\n' + chunk


_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
// Echo the i18n key (and :n) so label/badge/count selection is observable.
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

// ── Pure grouping policy (_groupSegContextNotes) ──
function note(id, kind, reason){
  const s = { type: 'system_note', blockId: id, noteKind: kind || 'context-injection',
              action: 'prompt', text: 'TEXT_' + id, llmRound: 0 };
  if (reason) s.reason = reason;
  return s;
}
const grouped = _groupSegContextNotes([
  note('n0', 'session-context'), note('n1', 'context-injection', 'relevant_memories'),
  note('n2', 'context-injection', 'skills_index'),
]);
check('grp_run_of_3_merges_to_one', grouped.length === 1 && grouped[0]._noteGroup === true);
check('grp_run_preserves_member_order',
      grouped[0].notes.map(function(n){ return n.blockId; }).join(',') === 'n0,n1,n2');

const soloAndRun = _groupSegContextNotes([
  note('a', 'system-intervention'),               // intervention: never grouped
  note('b', 'context-injection'), note('c', 'context-injection'),
  note('d', 'intent-stall'),
]);
check('grp_intervention_not_absorbed',
      soloAndRun.length === 3 && soloAndRun[0].blockId === 'a'
      && soloAndRun[1]._noteGroup === true && soloAndRun[2].blockId === 'd');
check('grp_run_between_interventions',
      soloAndRun[1].notes.map(function(n){ return n.blockId; }).join(',') === 'b,c');

const singleton = _groupSegContextNotes([ note('only', 'context-injection') ]);
check('grp_singleton_not_grouped', singleton.length === 1 && singleton[0]._noteGroup !== true);

// ── Settled timeline: one group row at the first member's wire slot ──
const segments = [
  { type: 'system_note', blockId: 'system-note:session-context-0',
    noteKind: 'session-context', action: 'prompt', text: 'NOTE_SESSION', llmRound: 0 },
  { type: 'system_note', blockId: 'system-note:context-injection-1',
    noteKind: 'context-injection', action: 'prompt', reason: 'relevant_memories',
    text: 'NOTE_MEM', llmRound: 0 },
  { type: 'system_note', blockId: 'system-note:context-injection-2',
    noteKind: 'context-injection', action: 'prompt', reason: 'skills_index',
    text: 'NOTE_SKILLS', llmRound: 0 },
  { type: 'thinking', text: 'think0', llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'system_note', blockId: 'system-note:system-intervention-3',
    noteKind: 'system-intervention', action: 'prompt', text: 'NOTE_LOOP', llmRound: 1 },
  { type: 'text', text: 'narr1', llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'read_files', llmRound: 1 },
  { type: 'text', text: 'ANSWER', deliverable: true, terminal: true },
];
const msg = { toolRounds: [
  { roundNum: 1, llmRound: 0, toolCallId: 'a1', toolName: 'grep_search', status: 'done' },
  { roundNum: 2, llmRound: 1, toolCallId: 'b1', toolName: 'read_files', status: 'done' },
]};
const html = renderSegmentTimelineHTML(segments, msg, 0);
function idx(n){ return html.indexOf(n); }
function count(n){ return html.split(n).length - 1; }

check('timeline_rendered', !!html);
// One group disclosure, one count pill, all three member bodies inside.
check('group_row_present', idx('sw-note-group') >= 0);
check('group_count_pill', idx('systemNote.groupItemsTitle:3') >= 0);
check('group_label', idx('systemNote.groupLabel') >= 0);
check('all_members_present',
      idx('NOTE_SESSION') >= 0 && idx('NOTE_MEM') >= 0 && idx('NOTE_SKILLS') >= 0);
check('members_in_order',
      idx('NOTE_SESSION') < idx('NOTE_MEM') && idx('NOTE_MEM') < idx('NOTE_SKILLS'));
// The three-note stack collapses to ONE group row (it carries both
// `sw-system-note-row` and `sw-note-group`); the only other note row in the
// timeline is the mid-turn intervention's own solo row. No three sibling
// context rows remain.
check('one_group_row', count('sw-inbox-row sw-system-note-row sw-note-group"') === 1);
check('no_context_sibling_rows',
      count('sw-inbox-row sw-system-note-row"') === 1  // only NOTE_LOOP's solo row
      && count('data-note-kind="system-intervention"') === 1);
// Group sits ahead of round-0 prose; the mid-turn intervention keeps its own
// row between rounds; the deliverable stays out of the timeline.
check('group_before_round0_prose', idx('sw-note-group') < idx('think0'));
check('intervention_keeps_own_row',
      idx('NOTE_LOOP') > idx('<TOOL name=grep_search>') && idx('NOTE_LOOP') < idx('narr1'));
check('deliverable_excluded', idx('ANSWER') === -1);
check('tools_present', idx('<TOOL name=grep_search>') >= 0 && idx('<TOOL name=read_files>') >= 0);
check('header_counts_real_only', idx('toolPanel.toolsUsed:2') >= 0);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_adjacent_context_notes_merge_into_one_row():
    out = _run_node(_HARNESS, {'FN_SRC': _extract_timeline_fns()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'context-note grouping failures:\n' + out
    assert out.count('PASS') >= 16, f'expected >=16 PASS, got:\n{out}'


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_registry_classifier_unknown_keys_actions_and_scope_fail_closed():
    harness = _STUBS + r'''
    eval(process.env.FN_SRC);
    const results = [];
    const note = (reason, action='prompt') => ({type:'system_note',text:'body',
      blockId:'id',noteKind:'system-intervention',llmRound:0,reason,action});
    for (const reason of ['programmatic_execution','programmatic_inputs','user_context']) {
      results.push(isContextInjectionNote(note(reason)));
    }
    for (const reason of ['__proto__','constructor','unknown','tool_stagnation']) {
      results.push(!isContextInjectionNote(note(reason)));
      const html=_renderSegSystemNoteHTML(note(reason,'constructor'));
      results.push(html.includes('systemNote.interventionLabel')
        && html.includes('systemNote.actionPrompt') && !html.includes('undefined'));
    }
    for (const action of ['',false,0,null,'stop','tool-policy']) {
      results.push(!isContextInjectionNote(note('programmatic_execution',action)));
    }
    results.push(!sameContextNoteScope({},{}));
    results.push(!sameContextNoteScope({llmRound:0,attemptId:'a'},{llmRound:0,attemptId:'b'}));
    results.push(!sameContextNoteScope({llmRound:0},{llmRound:1}));
    console.log(results.every(Boolean) ? 'PASS' : JSON.stringify(results));
    '''
    assert _run_node(harness, {'FN_SRC': _extract_timeline_fns()}) == 'PASS'
