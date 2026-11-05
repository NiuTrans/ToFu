"""Native timeline value projection: occurrence identity, injection slots and bounded render work."""
import json
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def timeline_bundle():
    entry = """
import * as owner from './index';
import { computeExecutionBatches } from '../../presentation/tool-execution-groups';
(window as any).timelineOwner=owner;(window as any).groupSegments=computeExecutionBatches;
"""
    with compiled_typescript(ROOT / 'frontend/src/conversation/ui/segment-timeline/index.ts', contents=entry) as bundle:
        yield bundle


def run_timeline(bundle, script):
    preamble = r"""
const fs=require('fs'),assert=require('node:assert/strict');global.window=global;
(new Function(fs.readFileSync(process.argv[2],'utf8')))();
const owner=timelineOwner;let sessions=0,slots=0,lines=0;
const ports={groupSegments,isSuperseded:owner.isSupersededToolOrphan,
 createSession:()=>{sessions++;return {renderSlot:r=>{slots++;return '[INJECT:'+r.query+']';},
  renderGroups:(rows,narration)=>{assert.equal(narration,undefined);return rows.map(r=>{lines++;return '[TOOL:'+r.query+']';}).join('');}};},
 renderBatch:(segments,rows,_all,renderGroups)=>segments.filter(s=>s.type!=='tool_use').map(s=>'[PROSE:'+s.text+']').join('')+renderGroups(rows),
 panel:rows=>({active:false,collapsed:false,attention:'routine',html:'[COUNT:'+rows.length+']'})};
const render=(segments,rows)=>owner.renderToolSegmentTimeline(segments,rows,ports);
const tool=(id,query,extra={})=>({toolName:'read_files',toolCallId:id,query,status:'done',...extra});
"""
    result = subprocess.run(['node', '-e', preamble + script, str(ROOT), bundle], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_duplicate_id_occurrences_and_superseded_positional_fallback(timeline_bundle):
    run_timeline(timeline_bundle, r"""
const rows=[tool('repeat','first'),tool('discard','discarded',{results:[{badge:'superseded'}]}),tool('repeat','second'),tool(undefined,'legacy')];
const segs=[{type:'tool_use',id:'repeat',llmRound:0},{type:'tool_use',id:'discard',llmRound:1},
 {type:'tool_use',id:'repeat',llmRound:2},{type:'tool_use',id:'missing-id',llmRound:3}];
const html=render(segs,rows);
assert.ok(html.indexOf('[TOOL:first]')<html.indexOf('[TOOL:second]')&&html.indexOf('[TOOL:second]')<html.indexOf('[TOOL:legacy]'));
assert.ok(!html.includes('discarded'));assert.ok(html.includes('[COUNT:3]'));assert.equal(sessions,1);
""")


def test_injection_anchor_is_once_across_attempts_and_numeric_wire_forms(timeline_bundle):
    run_timeline(timeline_bundle, r"""
const rows=[tool(undefined,'steering',{_userSteerInject:true,steerRound:1}),tool('one','attempt A'),tool('two','attempt B')];
const segs=[{type:'tool_use',id:'one',llmRound:'0',attemptId:'A'},{type:'tool_use',id:'two',llmRound:0,attemptId:'B'}];
const html=render(segs,rows);assert.equal(slots,1);assert.equal(lines,2);assert.equal(sessions,1);
assert.ok(html.indexOf('[INJECT:steering]')<html.indexOf('[TOOL:attempt A]'));assert.ok(html.includes('[COUNT:2]'));
""")


def test_context_coalescing_preserves_a_mid_run_injection_slot(timeline_bundle):
    run_timeline(timeline_bundle, r"""
const segs=[{type:'system_note',noteKind:'context-injection',text:'before',llmRound:0},
 {type:'system_note',noteKind:'context-injection',text:'after',llmRound:1}];
const html=render(segs,[tool(undefined,'peer',{_peerInject:true,peerRound:2})]);
assert.ok(html.indexOf('[PROSE:before]')<html.indexOf('[INJECT:peer]')&&html.indexOf('[INJECT:peer]')<html.indexOf('[PROSE:after]'));
assert.equal(slots,1);assert.ok(html.includes('[COUNT:0]'));assert.equal(segs.length,2);
""")


def test_stall_and_inbox_duplicates_terminal_answer_and_unmatched_tail(timeline_bundle):
    run_timeline(timeline_bundle, r"""
const segs=[{type:'system_note',noteKind:'intent-stall',text:'durable stall',llmRound:0},
 {type:'system_note',noteKind:'context-injection',reason:'inbox_delivery',text:'duplicate inbox',llmRound:0},
 {type:'tool_use',id:'one',llmRound:0},{type:'text',text:'DELIVERABLE',deliverable:true,terminal:true}];
const rows=[tool(undefined,'stall chip',{_stallNudge:true,stallRound:1}),tool(undefined,'inbox',{_inboxInject:true,inboxRound:1,inboxCount:1,inboxPreviews:[{text:'duplicate inbox'}]}),
 tool('one','real'),tool(undefined,'tail',{_peerInject:true,peerRound:99})];
const html=render(segs,rows);assert.ok(html.includes('durable stall')&&!html.includes('stall chip'));
assert.ok(!html.includes('duplicate inbox')&&!html.includes('DELIVERABLE'));assert.equal(slots,2);
assert.ok(html.indexOf('[TOOL:real]')<html.indexOf('[INJECT:tail]'));
""")


def test_checklist_history_keeps_audits_and_avoids_tuple_collisions(timeline_bundle):
    run_timeline(timeline_bundle, r"""
let reads=0;
const result=()=>({todoUpdateCount:1,todos:[{get status(){reads++;return 'completed';}}]});
const a=tool('a','A',{toolName:'todo_write',attemptId:'owner:worker',agentId:'root',results:[result()]}),
 b=tool('b','B',{toolName:'todo_write',attemptId:'owner',agentId:'worker:root',results:[result()]}),
 reject=tool('c','C',{toolName:'todo_write',results:[{todoRejected:true,todos:[],todoRejectReason:'Keep reason'}]}),
 noop=tool('d','D',{toolName:'todo_write',results:[{todoNoop:true,todos:[]}]});
const rows=[a,b,reject,noop];rows.forEach(Object.freeze);Object.freeze(rows);
const projected=owner.projectChecklistHistory(rows);
assert.equal(projected.length,4);assert.equal(projected[0],a);assert.equal(projected[1],b);assert.equal(projected[2],reject);
assert.equal(projected[3].results[0].todoDisplayHistory.length,2);assert.equal(reads,2);
assert.equal(projected[3].results[0].todoDisplayUpdateCount,2);assert.equal(noop.results[0].todoDisplayHistory,undefined);
assert.equal(reject.results[0].todoRejectReason,'Keep reason');
""")


def test_empty_unmatchable_program_parent_and_no_id_checklists_fall_back(timeline_bundle):
    run_timeline(timeline_bundle, r"""
assert.equal(render(null,[]),'');assert.equal(render({length:1},[]),'');
assert.equal(render([{type:'tool_use',id:'missing'}],[tool('real','real')]),'');
assert.equal(render([{type:'text',text:'narration'}],[tool('parent','program',{_programSynthetic:true})]),'');
assert.equal(render([{type:'tool_use'}],[tool(undefined,'one',{toolName:'todo_write'}),tool(undefined,'two',{toolName:'todo_write'})]),'');
assert.equal(sessions,0);
""")


def test_long_retry_timeline_emits_one_injection_and_one_group_context(timeline_bundle):
    run_timeline(timeline_bundle, r"""
const n=2000,rows=[tool(undefined,'steer',{_userSteerInject:true,steerRound:1})],segments=[];
for(let i=0;i<n;i++){rows.push(tool(String(i),'result '+i,{attemptId:'attempt-'+i,llmRound:0}));
 segments.push({type:'tool_use',id:String(i),attemptId:'attempt-'+i,llmRound:0});}
const html=render(segments,rows);assert.equal(sessions,1);assert.equal(slots,1);assert.equal(lines,n);
assert.equal(html.split('[INJECT:steer]').length-1,1);assert.ok(html.includes('[COUNT:2000]'));
""")


def test_inbox_note_dedup_requires_same_scope_round_and_full_preview_payload(timeline_bundle):
    run_timeline(timeline_bundle, r"""
const scope={attemptId:'attempt',taskId:'producer'};
const note={type:'system_note',noteKind:'context-injection',reason:'inbox_delivery',
 action:'prompt',text:'completion',llmRound:1,...scope};
const receipt=tool(undefined,'dedicated completion',{_inboxInject:true,inboxRound:2,
 inboxCount:1,inboxPreviews:[{text:'completion'}],...scope});
const other={...note,text:'agent message'};
const before=JSON.stringify([note,other,receipt]);
const html=render([note,other],[receipt]);
assert.ok(!html.includes('[PROSE:completion]')&&html.includes('[PROSE:agent message]'));
assert.equal(before,JSON.stringify([note,other,receipt]));
for(const change of [{inboxRound:3},{attemptId:'retry'},{taskId:'other-producer'},
 {attemptId:undefined},{inboxPreviews:[]},{inboxCount:2},
 {inboxPreviews:[{text:'completion',textTruncated:true}]},
 {inboxPreviews:[{text:'completion '}]}, {inboxRound:undefined}]){
 assert.ok(render([note],[{...receipt,...change}]).includes('[PROSE:completion]'),JSON.stringify(change));
}
assert.ok(render([{...note,reason:'future_inbox_reason'}],[receipt]).includes('[PROSE:completion]'));
assert.ok(render([{...note,action:'stop'}],[receipt]).includes('[PROSE:completion]'));
const peer=tool(undefined,'dedicated peer',{_peerInject:true,peerRound:2,peerCount:1,
 peerPreviews:[{text:'peer update'}],...scope});
assert.ok(!render([{...note,text:'completion\n\npeer update'},other],[receipt,peer]).includes('[PROSE:completion'));
assert.ok(render([{...note,text:'completion\n\npeer update\n\nagent message'}],[receipt,peer]).includes('[PROSE:completion'));
""")


def test_retained_receipt_rehydration_preserves_dedup_scope(timeline_bundle):
    from tests.tasks_pkg.test_inject_row_positioning import _extract_core_inject_fns

    run_timeline(timeline_bundle,
                 'const runtimeScope = {}; eval(' + json.dumps(_extract_core_inject_fns()) + ');\n' + r"""
const note={type:'system_note',noteKind:'context-injection',reason:'inbox_delivery',
 text:'same payload',llmRound:0};
for(const field of ['_peerInjects','_inboxInjects']) {
 for(const scope of [{taskId:'producer',attemptId:'attempt'},
                    {_taskId:'producer',_attemptId:'attempt'}]) {
  const msg={[field]:[{round:1,count:1,previews:[{text:'same payload'}],...scope}]};
  const before=JSON.stringify(msg);
  const rows=_rehydrateInjectRows(msg,[]);
  assert.equal(rows.length,1);
  assert.equal(rows[0].taskId,'producer');
  assert.equal(rows[0].attemptId,'attempt');
  assert.ok(render([note],rows).includes('[PROSE:same payload]'), 'unknown scope cannot match explicit scope');
  assert.ok(!render([{...note,taskId:'producer',attemptId:'attempt'}],rows).includes('[PROSE:same payload]'));
  assert.ok(render([{...note,taskId:'other',attemptId:'attempt'}],rows).includes('[PROSE:same payload]'));
  assert.equal(before,JSON.stringify(msg));
 }
}
""")