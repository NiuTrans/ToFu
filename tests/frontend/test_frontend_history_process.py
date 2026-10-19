"""Surface-owned historical final/process residency, safety and reader contracts."""
import json
import shutil
import subprocess

import pytest

from tests.frontend.test_frontend_round_outline import ROOT, HELPERS, round_bundle as round_bundle  # noqa: F401

pytestmark = pytest.mark.unit


def run_history(bundle, source):
    code = f"""
const {{JSDOM}}=require('jsdom');
const dom=new JSDOM('<!doctype html><body><main id="surface"></main></body>',{{pretendToBeVisual:true}});
global.document=dom.window.document;global.window=dom.window;
const feature=require({json.dumps(str(bundle))});Object.assign(global,feature);
{HELPERS}
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const reveal=async()=>{{const process=document.querySelector('.conversation-history-process');process.open=true;await tick();return process;}};
(async()=>{{{source}}})().then(result=>{{console.log(JSON.stringify(result));dom.window.close();}})
.catch(error=>{{console.error(error);process.exitCode=1;dom.window.close();}});
"""
    result = subprocess.run([shutil.which('node'), '-e', code], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_settled_history_starts_with_compact_process_and_keeps_answer_visible(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main'), calls=[],intents=[];
const blocks=Array.from({length:800},(_,i)=>block('tool-'+i,i,'a','tool',{
 round:{toolName:'read_files',status:'done'},text:'Evidence '+i}));
blocks.splice(3,0,block('narration',1,'a','text',{markdown:'Intermediate narration'}));
blocks.push(block('final-a',900,'a','text',{deliverable:true,markdown:'Final answer'}),
 block('final-b',900,'a','text',{terminal:true,markdown:'Final conclusion'}));
const original=JSON.stringify(blocks),model=vm(turn(blocks));
const surface=createConversationSurface(root,{windowing:{maxBlocksPerTurn:40},
 renderBlock(node,b){calls.push(b.blockId);node.textContent=b.markdown||b.text;},
 onIntent:intent=>intents.push(intent)});
surface.render(model);
const initial={ids:[...root.querySelectorAll('[data-block-id]')].map(n=>n.dataset.blockId),calls:calls.length,
 toolLeaves:root.querySelectorAll('.conversation-block--tool').length,
 notice:!!root.querySelector('[data-conversation-part="turn-history-notice"]'),
 processBeforeAnswer:!!(root.querySelector('.conversation-history-process').compareDocumentPosition(root.querySelector('[data-block-id="final-a"]')) & window.Node.DOCUMENT_POSITION_FOLLOWING)};
const process=await reveal(),visited=new Set(),pages=[];
const observe=()=>{const ids=[...process.querySelectorAll('[data-block-id]')].map(n=>n.dataset.blockId);ids.forEach(id=>visited.add(id));pages.push(ids);};
observe();let maxLeaves=process.querySelectorAll('[data-block-id]').length;
while(!process.querySelector('[data-process-page="next"]').disabled){process.querySelector('[data-process-page="next"]').click();observe();maxLeaves=Math.max(maxLeaves,process.querySelectorAll('[data-block-id]').length);}
const last=process.querySelector('[data-block-id]'),before=calls.length;
for(let i=0;i<15;i++)surface.render(vm(turn(JSON.parse(original))));
const retained=last===process.querySelector('[data-block-id]'),stable=before===calls.length;
process.querySelector('.conversation-history-process__body').dispatchEvent(new window.Event('scroll',{bubbles:true}));
const noOuterPaging=[...root.querySelectorAll('[data-conversation-part="turn-blocks-earlier"],[data-conversation-part="turn-blocks-later"]')].every(node=>node.hidden);
process.open=false;await tick();
const closedLeaves=process.querySelectorAll('[data-block-id]').length;
const immutable=original===JSON.stringify(blocks);surface.dispose();
return {initial,visited:visited.size,maxLeaves,firstPage:pages[0].slice(0,5),lastPage:pages.at(-1),retained,stable,noOuterPaging,closedLeaves,immutable,intents:intents.length};
""")
    assert result['initial'] == {'ids': ['final-a', 'final-b'], 'calls': 2, 'toolLeaves': 0,
                                 'notice': False, 'processBeforeAnswer': True}
    assert result['visited'] == 801
    assert result['maxLeaves'] == 40
    assert result['firstPage'] == ['tool-0', 'tool-1', 'tool-2', 'narration', 'tool-3']
    assert result['lastPage'] == ['tool-799']
    assert all(result[key] for key in ('retained', 'stable', 'noOuterPaging', 'immutable'))
    assert result['closedLeaves'] == result['intents'] == 0


def test_actionable_turns_stay_visible_and_settled_result_details_stay_in_process(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main');const rendered=[];
const surface=createConversationSurface(root,{renderBlock(node,b){rendered.push(b);node.textContent=b.markdown||b.text;}});
const answer=block('answer',10,'a','text',{terminal:true,markdown:'Answer'});
const action=(round)=>block('action',0,'a','tool',{round});
const cases=[
 turn([action({toolName:'read_files',status:'running'}),answer]),
 turn([action({toolName:'run_command',status:'done',results:[{backgrounded:true,exitCode:'background'}]}),answer]),
 turn([action({toolName:'ask_human',status:'done'}),answer]),
 turn([answer],'failed'),turn([answer],'running'),
 {...turn([answer]),commandPending:{commandId:'approval'}},
 {...turn([answer]),metadata:{translation:{pending:false,streaming:false},waitingOn:{kind:'approval'}}}];
const guarded=cases.map(t=>{surface.render(vm(t));return !root.querySelector('.conversation-history-process')&&!root.querySelector('.conversation-history-notice:not([hidden])');});
surface.render(vm(turn([block('narration',0,'a','text',{markdown:'Keep all this narration'}),block('thought',0)])));
const fallback={notice:root.querySelector('.conversation-history-notice').hidden===false,
 narration:!!root.querySelector('[data-block-id="narration"]'),process:!!root.querySelector('.conversation-history-process')};
surface.render(vm(turn([action({toolName:'run_command',query:'Write externally',status:'done',results:[{exitCode:3}]}),answer])));
const risk={notice:!!root.querySelector('[data-conversation-part="turn-history-notice"]'),
 outsideToolLeaf:!!root.querySelector('[data-block-id="action"]')};
const process=await reveal();
risk.inspectable=!!process.querySelector('[data-block-id="action"]');
risk.exitCodePreserved=rendered.findLast(b=>b.blockId==='action').round.results[0].exitCode===3;
surface.dispose();return {guarded,fallback,risk};
""")
    assert result['guarded'] == [True] * 7
    assert result['fallback'] == {'notice': True, 'narration': True, 'process': False}
    assert result['risk'] == {'notice': False, 'outsideToolLeaf': False,
                              'inspectable': True, 'exitCodePreserved': True}


def test_short_process_has_no_paging_chrome_and_retains_keyboard_focus(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main');
const tools=Array.from({length:8},(_,i)=>block('tool-'+i,i,'a','tool',{
 round:{toolName:'read_files',status:'done'},text:'Evidence '+i}));
const final=block('answer',10,'a','text',{terminal:true,markdown:'Answer'});
const surface=createConversationSurface(root,{windowing:{maxBlocksPerTurn:4},
 renderBlock(node,b){node.textContent=b.markdown||b.text;}});
surface.render(vm(turn([...tools,final])));const process=await reveal();
const controls=process.querySelector('nav'),next=controls.querySelector('[data-process-page="next"]');
const pagedVisible=!controls.hidden&&!next.disabled;
next.focus();surface.render(vm(turn([...tools.slice(0,2),final])));
const shortHidden=controls.hidden,focusReturned=document.activeElement===process.querySelector('summary');
const count=process.querySelectorAll('[data-block-id]').length;
surface.dispose();return {pagedVisible,shortHidden,focusReturned,count};
""")
    assert result == {'pagedVisible': True, 'shortHidden': True, 'focusReturned': True, 'count': 2}


def test_close_unmount_abort_lazy_reads_and_keyed_reader_state_restores(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main'),requests=[];
const reference={ownerScope:'personal',conversationId:'c',turnId:'t',projectionRevision:1,roundIndex:0,toolCallId:'read-0'};
const tool=block('read',0,'a','tool',{round:{toolName:'read_files',status:'done',toolCallId:'read-0',_snapshotToolResult:reference}});
const final=block('answer',1,'a','text',{terminal:true});
// Real native result cancellation port, not a simulated dispose callback.
const native=createClassicConversationRenderers({
 renderSafeMarkdownHtml:value=>value,
 renderToolBlockHtml:()=>'<details class="ptool-result-block" data-lazy-tool-result><summary>read_files</summary><div class="ptool-result-body">Evidence</div></details>',
 loadToolResult(ref,signal){requests.push({ref,signal});return new Promise(()=>{});}});
const surface=createConversationSurface(root,{renderBlock:(node,block,context)=>{if(block.kind==='tool')native.renderBlock(node,block,context);else node.textContent=block.markdown||block.text;},disposeBlock:native.disposeBlock,
 windowing:{maxBlocksPerTurn:4}});
const model=vm(turn([tool,final]));surface.render(model);const closedRequests=requests.length;
let process=await reveal();let inner=process.querySelector('details[data-lazy-tool-result]');
inner.open=true;await tick();const started=requests.length;
inner.querySelector('summary').focus();const focused=document.activeElement;
surface.render(vm(turn(JSON.parse(JSON.stringify([tool,final])))));await tick();
const repaintFocus=document.activeElement===focused,retained=inner===process.querySelector('details[data-lazy-tool-result]');
process.open=false;await tick();const closeAbort=requests[0]?.signal.aborted,closedLeaves=process.querySelectorAll('[data-block-id]').length;
process=await reveal();inner=process.querySelector('details[data-lazy-tool-result]');await tick();
const restored=inner.open,retried=requests.length;
// Turn-window detach drops all leaves/readers, but leaves keyed UI choices.
surface.render({...model,mainLane:{...model.mainLane,turns:[]}});const unmountAbort=requests.at(-1).signal.aborted;
surface.render(model);await tick();process=document.querySelector('.conversation-history-process');
const remountOpen=process.open,remountInner=process.querySelector('details[data-lazy-tool-result]').open;
surface.dispose();const disposeAbort=requests.at(-1).signal.aborted;
return {closedRequests,started,repaintFocus,retained,closeAbort,closedLeaves,restored,retried,unmountAbort,remountOpen,remountInner,disposeAbort};
""")
    assert result['closedRequests'] == result['closedLeaves'] == 0
    assert result['started'] == 1
    assert result['retried'] == 2
    for key in ('repaintFocus', 'retained', 'closeAbort', 'restored',
                'unmountAbort', 'remountOpen', 'remountInner', 'disposeAbort'):
        assert result[key], key


def test_only_last_accepted_final_scope_stays_outside_and_empty_final_falls_back(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main'),surface=createConversationSurface(root,{renderBlock:(node,b)=>node.textContent=b.markdown||b.blockId});
const blocks=[block('old-final',12,'old-attempt','text',{terminal:true,markdown:'Old answer'}),
 block('attachment',0,'accepted','attachments',{attachments:[]}),
 block('old-narration',11,'accepted','text',{markdown:'Work narration'}),
 block('final-a',12,'accepted','text',{deliverable:true,markdown:'New answer'}),
 block('final-b',12,'accepted','text',{terminal:true,markdown:'New conclusion'})];
surface.render(vm(turn(blocks)));
const outside=[...root.querySelectorAll('[data-block-id]')].map(n=>n.dataset.blockId);
const process=await reveal(),chronology=[...process.querySelectorAll('[data-block-id]')].map(n=>n.dataset.blockId);
surface.render(vm(turn([block('narration',0,'a','text',{markdown:'Keep me'}),
 block('empty-final',1,'a','text',{terminal:true,markdown:'  '})])));
const fallback=root.querySelector('.conversation-history-notice').hidden===false,
 retained=!!root.querySelector('[data-block-id="narration"]');surface.dispose();return {outside,chronology,fallback,retained};
""")
    assert result == {'outside': ['final-a', 'final-b', 'attachment'],
                      'chronology': ['old-final', 'old-narration'], 'fallback': True, 'retained': True}


def test_process_wheel_never_schedules_outer_history_and_follow_latest_keeps_choices(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main'),viewport=root.parentElement,scheduled=[];
window.requestAnimationFrame=fn=>{scheduled.push(fn);return scheduled.length;};
window.cancelAnimationFrame=()=>{};
Object.defineProperty(viewport,'clientHeight',{value:900});
const surface=createConversationSurface(root,{scrollViewport:viewport,scrollAnchor:{capture:()=>null,restore:()=>{},followLatest:()=>{}},windowing:{maxTurns:4,maxBlocksPerTurn:4}});
const model=vm(turn([block('read',0,'a','tool',{round:{toolName:'read_files',status:'done'}}),
 block('answer',1,'a','text',{terminal:true})]));
surface.render(model);const process=await reveal(),panel=process.querySelector('.conversation-history-process__body');
const baseline=scheduled.length;
panel.dispatchEvent(new window.WheelEvent('wheel',{deltaY:-150,bubbles:true}));
const innerScheduled=scheduled.length-baseline;
surface.followLatest();const choicesPreserved=process.open&&panel===process.querySelector('.conversation-history-process__body');
viewport.dispatchEvent(new window.WheelEvent('wheel',{deltaY:-150,bubbles:true}));
const outerScheduled=scheduled.length-baseline;
surface.dispose();return {innerScheduled,outerScheduled,choicesPreserved};
""")
    assert result == {'innerScheduled': 0, 'outerScheduled': 1, 'choicesPreserved': True}


def test_process_has_hard_residency_cap_even_with_large_legacy_block_option(round_bundle):
    result = run_history(round_bundle, r"""
const root=document.querySelector('main'),surface=createConversationSurface(root,{windowing:{maxBlocksPerTurn:5000}});
const blocks=Array.from({length:1200},(_,i)=>block('read-'+i,i,'a','tool',{round:{toolName:'read_files',status:'done'}}));
blocks.push(block('answer',1200,'a','text',{terminal:true}));surface.render(vm(turn(blocks)));
const process=await reveal(),first=process.querySelectorAll('[data-block-id]').length;
process.querySelector('[data-process-page="next"]').click();
const next=process.querySelectorAll('[data-block-id]').length,
 start=process.querySelector('[data-block-id]').dataset.blockId;
surface.dispose();return {first,next,start};
""")
    assert result == {'first': 200, 'next': 200, 'start': 'read-200'}
