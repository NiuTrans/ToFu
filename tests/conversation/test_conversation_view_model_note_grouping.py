#!/usr/bin/env python3
"""tests/conversation/test_conversation_view_model_note_grouping.py — the TYPED surface
(``selectTurnBlocks`` → ``conversation-surface``) merges each maximal run of
adjacent context-injection ``system-note`` blocks into ONE
``system-note-group`` block (display-only), parity with the legacy runtime
path pinned by tests/context/test_system_note_context_grouping.py.

THE bug this pins
-----------------
The typed conversation surface renders one collapsed row per durable
``system_note`` block, so a session-start stack of N appended-context notes
became N stacked ``系统介入 · 已追加上下文`` rows. Plan A coalesces adjacent
context notes in the view-model — never touching Turn authority — so:

  * a run of >=2 contiguous ``context-injection`` / ``session-context`` notes
    collapses into ONE ``system-note-group`` block carrying its members in
    wire order (the first member's blockId stays the DOM identity, so a
    live-growing run re-renders in place);
  * intervention notes (intent-stall, todo-continuation, system-intervention,
    and reason-specialized active_checklist / modified_files) and LONE
    context notes keep their own blocks — grouping never crosses a non-note
    block, so a mid-turn group still sits at its exact wire slot;
  * member order and member content are preserved verbatim.

Run::
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/conversation/test_conversation_view_model_note_grouping.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))

_HAS_NODE = shutil.which('node') is not None

pytestmark = pytest.mark.unit


def _run_node(harness: str, env_extra: dict) -> str:
    env = dict(os.environ, **env_extra)
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\nSTDOUT:{proc.stdout}'
    return proc.stdout.strip()


def _view_model_module() -> str:
    return native_module_path(
        '.native/conversation-view-model.js',
        os.path.join(ROOT, 'frontend', 'src', 'conversation', 'presentation',
                     'conversation-view-model.ts'),
    )


_HARNESS = r"""
eval(require('node:fs').readFileSync(process.env.VM_PATH, 'utf8'));
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function note(id, kind, reason){
  const s = { type: 'system_note', blockId: id, noteKind: kind, action: 'prompt',
              text: 'TEXT_' + id, llmRound: 0 };
  if (reason) s.reason = reason;
  return s;
}
function prose(id, text){ return { type: 'text', blockId: id, text, llmRound: 0 }; }

function turn(segments){
  return {
    turnId: 'turn-test', actor: 'assistant', status: 'completed',
    createdAt: '2026-09-21T00:00:00Z',
    projection: { segments },
  };
}
function kinds(blocks){ return blocks.map((b) => b.kind); }

// ── A contiguous context stack collapses into ONE system-note-group ──
const stack = selectTurnBlocks(turn([
  note('system-note:session-context-0', 'session-context'),
  note('system-note:context-injection-1', 'context-injection', 'relevant_memories'),
  note('system-note:context-injection-2', 'context-injection', 'skills_index'),
  prose('p0', 'answer'),
]));
check('stack_collapses_to_group', kinds(stack).join(',') === 'system-note-group,text');
const group = stack[0];
check('group_blockId_is_first_member', group.blockId === 'system-note:session-context-0');
check('group_members_in_order',
      group.items.map((i) => i.blockId).join(',')
        === 'system-note:session-context-0,system-note:context-injection-1,system-note:context-injection-2');
check('group_members_keep_text',
      group.items.map((i) => i.text).join('|') === 'TEXT_system-note:session-context-0|TEXT_system-note:context-injection-1|TEXT_system-note:context-injection-2');
check('group_member_reasons',
      (group.items[1].reason === 'relevant_memories') && (group.items[2].reason === 'skills_index'));

// ── A lone context note is never wrapped ──
const lone = selectTurnBlocks(turn([
  note('system-note:context-injection-0', 'context-injection'),
  prose('p0', 'answer'),
]));
check('lone_note_stays_solo', kinds(lone).join(',') === 'system-note,text');

// ── Intervention notes break the run (never absorbed) ──
const mixed = selectTurnBlocks(turn([
  note('system-note:context-injection-0', 'context-injection'),
  note('system-note:context-injection-1', 'context-injection'),
  note('system-note:system-intervention-2', 'system-intervention'),
  note('system-note:context-injection-3', 'context-injection'),
  prose('p0', 'answer'),
]));
check('intervention_breaks_run',
      kinds(mixed).join(',') === 'system-note-group,system-note,system-note,text');
check('first_group_has_two_members', mixed[0].items.length === 2);
check('intervention_kept_solo', mixed[1].noteKind === 'system-intervention');
check('trailing_lone_context_solo',
      mixed[2].kind === 'system-note' && mixed[2].noteKind === 'context-injection');

// ── A checklist re-pin (reason-specialized) is an intervention, not context ──
const checklist = selectTurnBlocks(turn([
  note('system-note:context-injection-0', 'context-injection', 'active_checklist'),
  note('system-note:context-injection-1', 'context-injection'),
  prose('p0', 'answer'),
]));
check('checklist_reason_breaks_group',
      kinds(checklist).join(',') === 'system-note,system-note,text');

const legacy = selectTurnBlocks(turn([
  ...['session_context_committed','relevant_memories','memory_guidance','current_date',
    'skills_index','parallel_execution','project_rules','credential_vault','environment']
    .map((reason,i)=>note('legacy-'+i,'system-intervention',reason)),
  prose('p0','answer'), note('warning','system-intervention','other_reason'),
]));
check('legacy_stack_is_one_group', kinds(legacy).join(',') === 'system-note-group,text,system-note');
check('legacy_full_text_survives', legacy[0].items.length===9 && legacy[0].items[8].text==='TEXT_legacy-8');
const stopNote=note('stop','system-intervention','environment');stopNote.action='stop';
check('legacy_stop_never_absorbed', selectTurnBlocks(turn([note('ctx','context-injection'),stopNote]))[1].kind==='system-note');

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_typed_view_model_merges_adjacent_context_notes():
    out = _run_node(_HARNESS, {'VM_PATH': _view_model_module()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'typed view-model grouping failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS, got:\n{out}'
