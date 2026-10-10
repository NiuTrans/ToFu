"""Track B browser editing: proposal review card, source editor chrome,
bubble-menu message construction and the chat composer bridge — all through
the real typed TS owners under node/jsdom."""
import shutil
import subprocess
from pathlib import Path
import pytest
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

BOOT = r"""
const fs=require('fs'),path=require('path');
const {JSDOM}=require(path.join(process.argv[1],'node_modules/jsdom'));
const dom=new JSDOM('<textarea id="userInput"></textarea>',{url:'http://localhost/'});
global.window=dom.window;global.document=window.document;
for(const key of ['HTMLElement','Element','Node','AbortController','AbortSignal','localStorage',
  'CustomEvent','Event','KeyboardEvent','MutationObserver','getComputedStyle','DocumentFragment',
  'navigator','Document','CSS','Window','Selection','Range','Text'])global[key]=window[key];
global.getComputedStyle=window.getComputedStyle.bind(window);
global.requestAnimationFrame=callback=>setTimeout(callback,0);global.cancelAnimationFrame=clearTimeout;
window.requestAnimationFrame=global.requestAnimationFrame;window.cancelAnimationFrame=global.cancelAnimationFrame;
// jsdom omits layout APIs real browsers provide; the owners call them on
// line-jump and bubble placement.
window.HTMLElement.prototype.scrollIntoView=window.HTMLElement.prototype.scrollIntoView||function(){};
"""

HELPERS = r"""
const assert=require('node:assert/strict');
const tick=()=>new Promise(resolve=>setTimeout(resolve,2));
async function until(test,label){for(let i=0;i<200&&!test();i++)await tick();
  assert.ok(test(),label||'expected asynchronous outcome');}
const tr=(key,params)=>params?key+' '+JSON.stringify(params):key;
"""


def run_node(bundle: str, body: str, label: str) -> None:
    script = BOOT + '\nrequire(' + repr(bundle) + ');\n' + HELPERS + body
    result = subprocess.run(['node', '-e', script, str(ROOT)], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f'{label}: {result.stderr}\n{result.stdout}'
    assert result.stdout.strip().splitlines()[-1] == 'ok', f'{label}: {result.stdout}'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_proposal_card_renders_decides_and_reports_failures():
    bundle = native_module_path(
        'paper-proposal-card.js', ROOT / 'frontend/src/features/paper/proposal-card.ts')
    body = r"""
const pid='a'.repeat(32);
const edits=[
  {index:0,path:'main.tex',search:'old sentence',replace:'new sentence',description:'Tighten intro',status:'pending'},
  {index:1,path:'refs.bib',search:'@article{a,',replace:'@article{b,',description:'Fix key',status:'pending'},
];
const proposal=(status,rows)=>({schema_version:1,proposal_id:pid,document_id:'d'.repeat(64),
  conv_id:'conv-a',created_at:1,status,edits:rows});
const calls={get:0,accept:[],reject:[]};
let applied=0;
const api={
  getProposal:async body=>{calls.get++;assert.equal(body.proposal_id,pid);return proposal('pending',edits.map(e=>({...e})))},
  acceptProposal:async body=>{calls.accept.push(body);
    const rows=edits.map(e=>({...e}));
    for(const index of body.indexes??rows.map(e=>e.index))rows[index].status='applied';
    return {...proposal('applied',rows),results:(body.indexes??[0,1]).map(index=>({index,ok:true}))}},
  rejectProposal:async body=>{calls.reject.push(body);
    const rows=edits.map(e=>({...e,status:'rejected'}));return proposal('rejected',rows)},
};
const host=document.createElement('div');
host.className='paper-proposal';host.dataset.paperProposal=pid;
document.body.append(host);
(async()=>{
  await mountPaperProposalCard(host,{api,tr,onApplied:()=>{applied++}});
  await until(()=>host.querySelectorAll('.paper-proposal-hunk').length===2,'hunks render');
  assert.equal(calls.get,1);
  assert.ok(host.querySelector('.paper-proposal-hunk-diff .bdiff-del'),'search side renders as a deleted diff line');
  assert.ok(host.querySelector('.paper-proposal-hunk-diff .bdiff-add'),'replace side renders as an added diff line');
  assert.equal(host.querySelector('.paper-proposal-hunk-path').textContent,'main.tex');
  assert.equal(host.querySelector('.paper-proposal-status').textContent,'paperProposal.status.pending');
  // Per-hunk accept carries the hunk index and updates the card in place.
  host.querySelector('.paper-proposal-hunk .paper-proposal-accept').click();
  await until(()=>calls.accept.length===1,'accept call');
  assert.deepEqual(calls.accept[0].indexes,[0]);
  await until(()=>host.querySelector('.paper-proposal-status-applied'),'status transition');
  assert.equal(applied,1,'successful accept nudges the rebuild hook');
  assert.equal(host.querySelectorAll('.paper-proposal-hunk-summary .paper-proposal-actions').length,1,
    'the settled hunk loses its decision buttons; the still-pending hunk keeps them');
  // A fresh card in pending state: accept-all decides every pending hunk.
  const second=document.createElement('div');
  second.className='paper-proposal';second.dataset.paperProposal=pid;
  document.body.append(second);
  await mountPaperProposalCard(second,{api,tr,onApplied:()=>{applied++}});
  await until(()=>second.querySelector('.paper-proposal-accept-all'),'accept-all renders');
  second.querySelector('.paper-proposal-accept-all').click();
  await until(()=>calls.accept.length===2,'accept-all call');
  assert.equal(calls.accept[1].indexes,undefined,'accept-all omits the index filter');
  assert.equal(applied,2);
  // Reject-all transitions the card without firing the rebuild hook.
  const third=document.createElement('div');
  third.className='paper-proposal';third.dataset.paperProposal=pid;
  document.body.append(third);
  await mountPaperProposalCard(third,{api,tr,onApplied:()=>{applied++}});
  await until(()=>third.querySelector('.paper-proposal-reject-all'),'reject-all renders');
  third.querySelector('.paper-proposal-reject-all').click();
  await until(()=>calls.reject.length===1,'reject call');
  assert.equal(applied,2,'reject never nudges a rebuild');
  // Fetch failure surfaces the standard apiFailure message inline.
  const broken=document.createElement('div');
  broken.className='paper-proposal';broken.dataset.paperProposal=pid;
  document.body.append(broken);
  await mountPaperProposalCard(broken,{api:{...api,getProposal:async()=>{throw new Error('server offline')}},tr});
  await until(()=>broken.textContent.includes('server offline'),'load failure renders');
  assert.ok(broken.textContent.includes('paperProposal.loadFailed'));
  console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    run_node(bundle, body, 'proposal card')


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_proposal_skeleton_parse_and_render_from_tool_round():
    bundle = native_module_path(
        'paper-tool-result-presentation.js',
        ROOT / 'frontend/src/conversation/presentation/tool-result-presentation.ts')
    body = r"""
const pid='0123456789abcdef0123456789abcdef';
assert.equal(extractPaperProposalId('proposal_id: '+pid+'\n摘要'),pid);
assert.equal(extractPaperProposalId('no marker here'),'');
assert.equal(extractPaperProposalId('proposal_id: '+'g'.repeat(32)),'','non-hex rejected');
assert.equal(extractPaperProposalId('proposal_id: '+pid.slice(0,31)),'','short hex rejected');
const presentation=createToolResultPresentation({translate:tr,
  writeGateRefusal:{resolveRefusal:()=>null,renderNoticeHtml:()=>''}});
const header={iconHtml:'<i></i>',queryHtml:'paper_propose_edits',rootPillHtml:'',badgeHtml:''};
const html=presentation.renderGenericResultHtml(
  {toolName:'paper_propose_edits',status:'done',roundNum:'3',toolContent:'proposal_id: '+pid+'\n把第二节改得更紧凑'},
  {},header);
assert.ok(html.includes('data-paper-proposal="'+pid+'"'),'skeleton carries the proposal id');
assert.ok(html.includes('把第二节改得更紧凑'),'Chinese summary renders under the header');
assert.ok(html.includes('paper-proposal'),'skeleton mount point class');
// Unrelated tools and unparseable content keep the generic viewer.
const other=presentation.renderGenericResultHtml(
  {toolName:'read_files',status:'done',roundNum:'1',toolContent:'proposal_id: '+pid}, {}, header);
assert.ok(!other.includes('data-paper-proposal'),'only paper_propose_edits mints the card');
const plain=presentation.renderGenericResultHtml(
  {toolName:'paper_propose_edits',status:'done',roundNum:'1',toolContent:'plain text result'}, {}, header);
assert.ok(plain.includes('ptool-result-pre') && !plain.includes('data-paper-proposal'),
  'unparseable proposal content falls back to the generic result viewer');
console.log('ok');
"""
    run_node(bundle, body, 'proposal skeleton')


FAKE_ADAPTER = r"""
function makeFakeAdapter(init){
  const area=document.createElement('textarea');
  area.value=init.content;
  const listeners={change:null,selection:null,save:null};
  return {
    element:area,
    content:()=>area.value,
    onDidChange:cb=>{listeners.change=cb},
    onSelectionChange:cb=>{listeners.selection=cb},
    setSaveCommand:cb=>{listeners.save=cb},
    scrollToLine:line=>{makeFakeAdapter.scrolled=line},
    focus:()=>{},
    destroy:()=>{makeFakeAdapter.destroyed=(makeFakeAdapter.destroyed||0)+1},
    edit(text){area.value=text;listeners.change&&listeners.change()},
    select(value){listeners.selection&&listeners.selection(value)},
    pressSave(){listeners.save&&listeners.save()},
  };
}
"""


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_source_editor_conflict_banner_bubble_and_fallback():
    bundle = native_module_path(
        'paper-source-editor.js', ROOT / 'frontend/src/features/paper/source-editor.ts')
    body = FAKE_ADAPTER + r"""
// Pure message shape first: instruction + quoted selection + full-width context.
assert.equal(buildBubbleMessage({instruction:'请扩写：',selectedText:'Some text.',path:'main.tex',fromLine:3,toLine:5}),
  '请扩写：\n\nSome text.\n\n（main.tex:3-5）');
const digest=async text=>'sha:'+text.length;
(async()=>{
  // ── Save flow: optimistic-concurrency fence + dirty lifecycle ──
  const saves=[];let savedNudge=0;const dirtyStates=[];let adapter;
  const editor=await createPaperSourceEditor({
    path:'main.tex',content:'line one\nline two',language:'latex',tr,digest,
    createAdapter:init=>{adapter=makeFakeAdapter(init);return adapter},
    save:async request=>{saves.push(request);return {sha256:'sha:next',size_bytes:request.content.length}},
    reload:()=>{},onSaved:()=>{savedNudge++},onDirtyChange:dirty=>dirtyStates.push(dirty),
  });
  assert.ok(editor);document.body.append(editor.element);
  assert.equal(editor.dirty(),false);
  await editor.save();
  assert.equal(saves.length,0,'a clean buffer never saves');
  adapter.edit('line one\nline two\nchanged');
  assert.equal(editor.dirty(),true);
  assert.deepEqual(dirtyStates,[true]);
  assert.equal(editor.element.querySelector('.paper-source-editor-dirty').hidden,false);
  adapter.pressSave();
  await until(()=>saves.length===1,'save submitted');
  assert.equal(saves[0].content,'line one\nline two\nchanged');
  assert.equal(saves[0].baseSha256,'sha:'+'line one\nline two'.length,'fence is the sha of the loaded bytes');
  await until(()=>savedNudge===1,'rebuild nudge after save');
  assert.equal(editor.dirty(),false);
  assert.deepEqual(dirtyStates,[true,false]);
  assert.equal(editor.element.querySelector('.paper-source-editor-status').textContent,'paperEdit.saved');

  // ── source_conflict: inline banner with reload + force save ──
  let conflictSaves=0;let reloads=0;
  const conflictError=()=>{const error=new Error('bytes drifted');error.code='source_conflict';error.status=409;return error};
  const conflictEditor=await createPaperSourceEditor({
    path:'main.tex',content:'server v1',language:'latex',tr,digest,
    createAdapter:init=>{adapter=makeFakeAdapter(init);return adapter},
    save:async request=>{
      conflictSaves++;
      if(conflictSaves===1)throw conflictError();
      assert.equal(request.baseSha256,undefined,'force save drops the fence');
      return {sha256:'sha:forced',size_bytes:request.content.length};
    },
    reload:()=>{reloads++},onSaved:()=>{},
  });
  document.body.append(conflictEditor.element);
  adapter.edit('server v1\nlocal edit');
  await conflictEditor.save();
  assert.equal(conflictEditor.conflictOpen(),true,'409 source_conflict opens the banner');
  const banner=conflictEditor.element.querySelector('.paper-source-editor-conflict');
  assert.equal(banner.hidden,false);
  assert.equal(banner.querySelector('.paper-source-editor-conflict-text').textContent,'paperEdit.conflict');
  assert.equal(conflictEditor.dirty(),true,'conflicted save keeps the dirty buffer');
  banner.querySelector('.paper-source-editor-conflict-reload').click();
  assert.equal(reloads,1,'reload asks the parent to re-read server bytes');
  adapter.edit('server v1\nlocal edit');
  conflictEditor.save();
  await until(()=>conflictSaves===2,'conflict re-raised');
  banner.querySelector('.paper-source-editor-conflict-force').click();
  await until(()=>conflictSaves===3,'force save retried without the fence');
  await until(()=>!conflictEditor.conflictOpen(),'banner closes on success');
  assert.equal(conflictEditor.dirty(),false);

  // ── Selection bubble menu ──
  const sent=[];
  const bubbleEditor=await createPaperSourceEditor({
    path:'sections/intro.tex',content:'alpha\nbeta\ngamma',language:'latex',tr,digest,
    createAdapter:init=>{adapter=makeFakeAdapter(init);return adapter},
    save:async()=>({sha256:'x'}),reload:()=>{},
    submitChat:message=>sent.push(message),
  });
  document.body.append(bubbleEditor.element);
  const bubble=bubbleEditor.element.querySelector('.paper-source-bubble');
  assert.equal(bubble.hidden,true);
  adapter.select(null);
  assert.equal(bubble.hidden,true,'empty selection never opens the bubble');
  adapter.select({text:' beta ',fromLine:2,toLine:2,rect:{left:8,top:8,bottom:16}});
  assert.equal(bubble.hidden,false,'non-empty selection opens the bubble');
  const labels=[...bubble.querySelectorAll('.paper-source-bubble-action')].map(button=>button.textContent);
  assert.deepEqual(labels,['paperEdit.bubble.academize','paperEdit.bubble.concise','paperEdit.bubble.expand',
    'paperEdit.bubble.fixGrammar','paperEdit.bubble.explain']);
  bubble.querySelectorAll('.paper-source-bubble-action')[3].click();
  assert.equal(sent.length,1);
  assert.equal(sent[0],'paperEdit.instruct.fixGrammar\n\nbeta\n\n（sections/intro.tex:2-2）');
  assert.equal(bubble.hidden,true,'acting hides the bubble');
  adapter.select({text:'gamma',fromLine:3,toLine:3,rect:{left:8,top:8,bottom:16}});
  const custom=bubble.querySelector('.paper-source-bubble-custom');
  custom.value='换成被动语态';
  custom.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  assert.equal(sent.length,2);
  assert.equal(sent[1],'换成被动语态\n\ngamma\n\n（sections/intro.tex:3-3）');
  adapter.select({text:'alpha',fromLine:1,toLine:1,rect:{left:8,top:8,bottom:16}});
  document.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  assert.equal(bubble.hidden,true,'Escape dismisses the bubble');

  // ── CM6 (or any adapter factory) load failure: null handle, caller keeps fallback ──
  const failed=await createPaperSourceEditor({
    path:'main.tex',content:'x',language:'latex',tr,digest,
    createAdapter:async()=>{throw new Error('chunk failed')},
    save:async()=>({sha256:'x'}),reload:()=>{},
  });
  assert.equal(failed,null,'adapter load failure resolves to the read-only fallback');
  console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    run_node(bundle, body, 'source editor')


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_source_view_keeps_read_only_fallback_and_wires_editor():
    bundle = native_module_path(
        'paper-preview-source-view.js', ROOT / 'frontend/src/features/paper/preview-source-view.ts')
    body = r"""
const source={path:'main.tex',content:'\\section{A}\nsecond\nthird',truncated:false,size_bytes:24,main_file:'main.tex'};
const ctxFor=(overrides={})=>({api:{bytes:async()=>new ArrayBuffer(0)},
  documentsApi:{readSource:async()=>({...source}),editSource:async()=>({path:'main.tex',sha256:'s',size_bytes:1})},
  documentId:'d'.repeat(64),buildId:'b'.repeat(32),tr,showTree(){},showFile(){},...overrides});
(async()=>{
  // Editor factory failure: the read-only highlighted view stays authoritative.
  const container=document.createElement('div');document.body.append(container);
  await showSourceFile(container,ctxFor({createEditor:async()=>null}),'main.tex',2,new AbortController().signal);
  assert.equal(container.querySelectorAll('.paper-source-line').length,3,'read-only lines render');
  assert.ok(container.querySelector('[data-line="2"].paper-source-line-hit'),'line jump highlights the row');
  assert.equal(container.querySelector('.paper-source-editor'),null,'no editor chrome on fallback');
  // Binary suffixes never open the editor, factory not even consulted.
  let factoryCalls=0;
  const binContainer=document.createElement('div');document.body.append(binContainer);
  await showSourceFile(binContainer,ctxFor({createEditor:async()=>{factoryCalls++;return null}}),'figure.pdf',undefined,new AbortController().signal);
  assert.equal(factoryCalls,0);
  assert.ok(binContainer.querySelector('.paper-source-code'));
  // Truncated reads stay read-only (a partial buffer must never be saved back).
  const truncContainer=document.createElement('div');document.body.append(truncContainer);
  const truncCtx=ctxFor({createEditor:async()=>{factoryCalls++;return null}});
  truncCtx.documentsApi={readSource:async()=>({...source,truncated:true}),editSource:async()=>({path:'main.tex',sha256:'s',size_bytes:1})};
  await showSourceFile(truncContainer,truncCtx,'main.tex',undefined,new AbortController().signal);
  assert.equal(factoryCalls,0,'truncated content never mounts the editor');
  // Successful mount: editor replaces the pre, line jump and save hook wire through.
  const mountContainer=document.createElement('div');document.body.append(mountContainer);
  const editorEl=document.createElement('div');editorEl.className='paper-source-editor-stub';
  const handle={element:editorEl,dirty:()=>true,conflictOpen:()=>false,
    scrollToLine:line=>{handle.scrolled=line},save:async()=>{},focus(){},destroy(){handle.destroyed=true}};
  let gotOptions=null;let saved=0;
  await showSourceFile(mountContainer,ctxFor({
    onSourceSaved:()=>{saved++},
    createEditor:async options=>{gotOptions=options;return handle},
  }),'main.tex',3,new AbortController().signal);
  assert.ok(mountContainer.querySelector('.paper-source-editor-stub'),'editor element replaces the pre');
  assert.equal(mountContainer.querySelector('.paper-source-code'),null);
  assert.equal(handle.scrolled,3,'line jump scrolls the editor');
  await gotOptions.onSaved();assert.equal(saved,1,'save hook reaches the panel rebuild nudge');
  const response=await gotOptions.save({content:'new bytes',baseSha256:'fence'});
  assert.equal(response.sha256,'s');
  // Dirty buffer + back affordance: the confirm guard decides.
  let asks=0;
  const guardContainer=document.createElement('div');document.body.append(guardContainer);
  let trees=0;
  const guardCtx=ctxFor({createEditor:async()=>({...handle,destroyed:false,destroy(){this.destroyed=true}}),
    confirmDiscard:()=>{asks++;return false},showTree(){trees++}});
  await showSourceFile(guardContainer,guardCtx,'main.tex',undefined,new AbortController().signal);
  guardContainer.querySelector('.paper-source-back').click();
  assert.equal(asks,1,'dirty guard asks once');
  assert.equal(trees,0,'cancel keeps the editor open');
  console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    run_node(bundle, body, 'source view integration')


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_chat_composer_prefill_send_and_draft_preservation():
    bundle = native_module_path(
        'paper-chat-composer.js', ROOT / 'frontend/src/features/paper/chat-composer.ts')
    body = r"""
(async()=>{
  const input=document.getElementById('userInput');
  const sent=[];
  window.sendMessage=()=>sent.push(input.value);
  assert.equal(prefillAndSendChatMessage('编译错误：x（main.tex:3）。请修复。'),true);
  assert.equal(input.value,'编译错误：x（main.tex:3）。请修复。');
  assert.deepEqual(sent,[input.value],'the composer send command fires once');
  input.value='existing draft';
  assert.equal(prefillAndSendChatMessage('第二条消息'),true);
  assert.equal(sent[1],'existing draft\n\n第二条消息','an existing draft is appended, never clobbered');
  input.value='';
  delete window.sendMessage;
  assert.equal(prefillAndSendChatMessage('no sender'),false,'missing send command fails closed');
  assert.equal(input.value,'','failed send leaves the composer untouched');
  assert.equal(prefillAndSendChatMessage('   '),false,'blank text is a no-op');
  console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    run_node(bundle, body, 'chat composer')
