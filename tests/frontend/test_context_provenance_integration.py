"""Context witnesses own acquisition metadata; no competing provenance strip."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests.frontend.test_frontend_conversation_surface_vite import conversation_bundle as conversation_bundle, _run

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

_JS = r"""
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<main id="root"></main>');
global.document=dom.window.document;
global.HTMLElement=dom.window.HTMLElement;
const {selectTurnBlocks,selectConversationViewModel,createClassicConversationRenderers,createConversationSurface}=feature;
const reasons=['session_context_committed','relevant_memories','memory_guidance','current_date','skills_index','parallel_execution','programmatic_execution','project_rules','credential_vault','environment'];
const notes=reasons.map((reason,i)=>({type:'system_note',blockId:'n'+i,noteKind:'system-intervention',reason,action:'prompt',llmRound:0,text:'WIRE_'+reason}));
const turn=(segments,provenance,revision=1)=>({turnId:'t',conversationId:'c',laneId:'main',ordinal:1,actor:'assistant',kind:'reply',status:'completed',currentAttemptId:null,projectionRevision:revision,createdAt:1,updatedAt:2,projection:{segments,provenance}});
const memory={blockId:'prov',memoryPrefetch:{phase:'done',selected:2,memories:[{name:'M1'},{name:'M2'}]}};
const checks=[];const check=(name,ok)=>checks.push([name,Boolean(ok)]);
const input=turn(notes,memory);const before=JSON.stringify(input);
let blocks=selectTurnBlocks(input),group=blocks.find(b=>b.kind==='system-note-group');
check('legacy_10_notes_form_one_group',blocks.length===1&&group.items.length===10);
check('memory_has_exactly_one_owner',group.items[1].provenance.memoryPrefetch.selected===2&&!blocks.some(b=>b.kind==='provenance'));
check('legacy_note_kind_repaired_display_only',group.items[0].noteKind==='session-context'&&group.items[6].noteKind==='context-injection'&&JSON.stringify(input)===before);
const failed={blockId:'prov',memoryPrefetch:{phase:'failed',reason:'selection_failed'}};
blocks=selectTurnBlocks(turn(notes,failed));group=blocks[0];
check('failed_selection_is_unpaired_metadata',group.kind==='system-note-group'&&group.provenance.memoryPrefetch.phase==='failed'&&!group.items[1].provenance);
blocks=selectTurnBlocks(turn([],failed));
check('provenance_only_turn_keeps_context_fallback',blocks[0].kind==='provenance'&&blocks[0].value.memoryPrefetch.phase==='failed');
blocks=selectTurnBlocks(turn(notes,{...failed,mcpLoginHint:{phase:'awaiting_approval'},preferencesLearned:[{id:'p1',summary:'approve',pending:true}]}));
check('pending_actions_stay_outside_fold',blocks.filter(b=>b.kind==='provenance').length===1&&blocks.find(b=>b.kind==='provenance').value.preferencesLearned.length===1&&blocks.some(b=>b.kind==='system-note-group'));
const n=(id,round=0,attempt='a',reason='current_date',action='prompt')=>({type:'system_note',blockId:id,noteKind:'context-injection',llmRound:round,attemptId:attempt,reason,action,text:id});
blocks=selectTurnBlocks(turn([n('a'),n('b'),{type:'thinking',blockId:'think',text:'REASONING',llmRound:0},n('c'),n('d')],memory));
check('never_merge_across_thinking',blocks.filter(b=>b.kind==='system-note-group').length===2&&blocks[1].kind==='thinking');
blocks=selectTurnBlocks(turn([n('a'),n('b',1),n('c',1,'b')],null));
check('never_merge_across_round_or_attempt',blocks.every(b=>b.kind==='system-note'));
blocks=selectTurnBlocks(turn([n('a'),n('b',0,'a','modified_files'),n('c',0,'a','unknown','stop'),n('d')],null));
check('interventions_and_non_prompt_are_not_absorbed',blocks.every(b=>b.kind==='system-note'));
blocks=selectTurnBlocks(turn([notes[1],{...notes[1],blockId:'other',llmRound:1}],memory));
check('ambiguous_memory_witness_not_falsely_paired',!blocks.some(b=>b.kind==='system-note'&&b.provenance)&&blocks[0].kind==='system-note-group'&&blocks[0].provenance.memoryPrefetch);
const state=t=>({conversationId:'c',conversationRevision:t.projectionRevision,transport:'live',turnsById:{t},laneOrder:{main:['t']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}});
const surface=createConversationSurface(document.getElementById('root'),createClassicConversationRenderers({renderSafeMarkdownHtml:s=>s}));
surface.render(selectConversationViewModel(state(input)));
let details=document.querySelector('.sw-note-group');details.open=true;
const updated=turn(notes,{blockId:'prov',memoryPrefetch:{phase:'done',selected:3,memories:[{name:'M3'}]}},2);
surface.render(selectConversationViewModel(state(updated)));
details=document.querySelector('.sw-note-group');
check('live_metadata_refresh_preserves_open_and_updates_details',details.open&&details.textContent.includes('M3')&&!details.textContent.includes('M1'));
check('no_external_provenance_strip',document.querySelectorAll('.tp-segs,.turn-prov,.conversation-block--provenance').length===0);
surface.render(selectConversationViewModel(state(turn([notes[1]],memory,3))));
let solo=document.querySelector('.sw-system-note-row');solo.open=true;
surface.render(selectConversationViewModel(state(turn([notes[1]],{blockId:'prov',memoryPrefetch:{phase:'done',selected:4,memories:[{name:'SOLO UPDATED'}]}},4))));
solo=document.querySelector('.sw-system-note-row');
check('single_note_reused_source_updates_metadata_and_open',solo.open&&solo.textContent.includes('SOLO UPDATED')&&!solo.textContent.includes('M1'));
surface.dispose();
console.log(JSON.stringify(checks));
"""


def test_context_record_single_owner_and_live_updates(conversation_bundle):
    results = _run(conversation_bundle, _JS)
    assert not [name for name, ok in results if not ok], results
    assert len(results) == 13


def test_registry_owns_every_snapshot_display_policy():
    from lib.tasks_pkg.context_composer.registry import INJECTION_REGISTRY, note_display_policies
    policies = note_display_policies()
    for reason, spec in INJECTION_REGISTRY.items():
        if spec.lane == 'block':
            assert policies[reason]['noteKind'] == 'context-injection'
            assert policies[reason]['groupContext'] == (reason not in {'active_checklist','modified_files'})
    assert policies['programmatic_execution']['groupContext']
    assert policies['relevant_memories']['provenanceField'] == 'memoryPrefetch'
    assert policies['tool_stagnation']['noteKind'] == 'system-intervention'
    result = subprocess.run(['node', 'scripts/gen_system_note_presentation.mjs', '--check'], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
