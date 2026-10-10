"""Preview subscription, close intent and stale publication through the real TS owner."""
import json
import subprocess
from pathlib import Path
import pytest
from tests._runtime_sections import native_module_path
from tests.support.paper_reader_frontend import ROOT, SETUP

pytestmark = pytest.mark.unit


def test_preview_receives_builds_closes_without_reopening_and_detaches_stale_acceptance(tmp_path):
    bundle = native_module_path('paper-preview-controller.js', ROOT / 'frontend/src/features/paper/preview-controller.ts')
    setup = SETUP.split('(0,eval)')[0]
    script = setup + r"""
require(process.argv[2]);
const assert=require('node:assert/strict');
const host=document.createElement('div');host.className='chat-wrapper';
host.innerHTML='<div class="chat-container"></div><div class="input-area"><textarea></textarea></div>';
document.body.append(host);
let active='conv-a',sequence=0,startImpl=null,taskImpl=null,storedBuild=null,storedReading=null,readingRevision=0;
const starts=[],detaches=[],renewals=[];
const viewers=new Map(),viewerReleases=[];let maximumViewers=0;
const result=()=>({receipt:{schema_version:1,build_id:String(sequence).padStart(32,'0'),document_id:'d'.repeat(64),
  owner_user_id:1,target:'host',source_digest:'s'.repeat(64),version_verified:true,main_file:'main.tex',engine:'pdflatex',
  toolchain:'test',status:'success',started_at:1,completed_at:2,artifacts:[],diagnostics:[]},superseded:false,pdf_url:'/paper.pdf'});
const api={start:async body=>{starts.push(body);sequence++;return startImpl?startImpl(body):
  {task_id:'task-'+sequence,document_id:'d'.repeat(64),subscriber_id:body.subscriber_id}},
  task:async(id,signal)=>{const value=taskImpl?await taskImpl(id,signal):{status:'done',result:result()};if(value.status==='done'&&value.result?.receipt.status==='success')storedBuild=value.result;return value},
  viewer:async(d,b,body)=>{if(body.operation==='retain')viewers.set(body.viewer_id,b);
    if(body.operation==='release'){viewers.delete(body.viewer_id);viewerReleases.push(b)}
    maximumViewers=Math.max(maximumViewers,viewers.size);
    return{retained:viewers.has(body.viewer_id),expires_in_seconds:240}},
  renew:async(id,subscriber)=>{renewals.push([id,subscriber]);return{renewed:true}},detach:async(id,subscriber)=>detaches.push([id,subscriber]),
  resolve:url=>url,bytes:async()=>new Uint8Array([37,80,68,70]),log:async()=>''};
const selectedSource={source:'local',path:'main.tex',main_file:'main.tex',project_root:'remote:device:project',label:'main.tex'};
const documentsApi={discover:async()=>({documents:[selectedSource],selected:selectedSource,diagnostics:[]}),select:async()=>({selected:selectedSource}),
 open:async()=>({document:storedBuild?{document_id:'d'.repeat(64),source:selectedSource,generation:1,status:'ready',build:storedBuild,reading:storedReading,reading_revision:readingRevision,can_compile:true}:null}),
 saveReading:async(id,body)=>({revision:++readingRevision,state:storedReading=body.state})};
const config={api,documentsApi,host:()=>host,activeConversation:()=>active,translate:key=>key,loadPdfJs:async()=>window.pdfjsLib,
  debounceMs:1,pollMs:1};
const preview=createPaperPreview(config);
const change=id=>({conversationId:active,changeId:id,source:'local',path:'main.tex',projectRoot:'remote:device:project'});
const tick=()=>new Promise(resolve=>setTimeout(resolve,2));
async function until(test){for(let i=0;i<100&&!test();i++)await tick();assert.ok(test(),'expected asynchronous outcome');}
(async()=>{
  preview.observeChanges([change('first')]);await until(()=>!!host.querySelector('canvas'));
  assert.equal(starts[0].conv_id,'conv-a');assert.equal(starts[0].request_id,'first');
  assert.equal(starts[0].project_root,'remote:device:project');
  assert.ok(starts[0].subscriber_id);assert.ok(rasterized.length<20);
  await until(()=>!!host.querySelector('#paperPreviewPanel')?.dataset.buildId);
  const before=host.querySelector('#paperPreviewPanel').dataset.buildId;
  assert.deepEqual([...viewers.values()],[before]);
  const opensBefore=opened.length;
  taskImpl=async()=>({status:'error',result:{...result(),receipt:{...result().receipt,status:'failed'}}});
  preview.observeChanges([change('broken')]);
  await until(()=>host.querySelector('.paper-preview-status')?.textContent==='preview.failed');
  assert.equal(host.querySelector('#paperPreviewPanel').dataset.buildId,before,'failed build preserves the displayed document');
  assert.equal(opened.length,opensBefore,'failed build must not reopen the PDF');
  // Even a successful compile with an unreadable replacement keeps the old lease.
  taskImpl=null;renderFault=()=>true;preview.observeChanges([change('bad-pdf')]);
  await until(()=>viewerReleases.includes(String(sequence).padStart(32,'0')));
  assert.equal(host.querySelector('#paperPreviewPanel').dataset.buildId,before);
  assert.deepEqual([...viewers.values()],[before]);assert.equal(maximumViewers,2);
  renderFault=null;
  let releaseStart;
  startImpl=body=>new Promise(resolve=>{releaseStart=()=>resolve({task_id:'held',document_id:'d'.repeat(64),subscriber_id:body.subscriber_id})});
  preview.observeChanges([change('held')]);await until(()=>!!releaseStart);
  preview.close();assert.equal(host.querySelectorAll('canvas').length,0);
  assert.equal(viewers.size,0);
  releaseStart();await until(()=>detaches.some(row=>row[0]==='held'));
  assert.equal(host.querySelector('#paperPreviewPanel'),null,'late acceptance must not reopen the panel');
  const count=starts.length;
  preview.observeChanges([change('closed-edit')]);await tick();await tick();assert.equal(starts.length,count);
  assert.ok(host.querySelector('.paper-preview-reopen'));
  startImpl=null;taskImpl=null;
  host.querySelector('.paper-preview-reopen').click();await until(()=>!!host.querySelector('canvas'));
  assert.equal(starts.length,count,'explicit reopen reads the saved PDF without compilation');
  preview.close();preview.destroy();
  const restored=createPaperPreview(config);restored.observeChanges([change('reload')]);await tick();await tick();
  assert.equal(starts.length,count,'close intent survives owner recreation');
  assert.equal(host.querySelector('#paperPreviewPanel'),null);
  restored.destroy();
  localStorage.removeItem('tofu_paper_preview_closed');
  let oldResponse;
  startImpl=body=>new Promise(resolve=>{oldResponse=()=>resolve({task_id:'obsolete',document_id:'d'.repeat(64),subscriber_id:body.subscriber_id})});
  const next=createPaperPreview(config);next.observeChanges([change('burst-a')]);await until(()=>!!oldResponse);
  next.observeChanges([change('burst-b')]);next.observeChanges([change('burst-c')]);
  const beforeRelease=starts.length;startImpl=null;oldResponse();
  await until(()=>!!host.querySelector('canvas'));
  assert.equal(starts.length,beforeRelease+1);assert.equal(starts.at(-1).request_id,'burst-c');
  assert.ok(detaches.some(row=>row[0]==='obsolete'));
  active='conv-b';next.activateConversation(active);
  assert.equal(host.querySelector('#paperPreviewPanel'),null);assert.equal(host.querySelectorAll('canvas').length,0);
  // A refused task renewal is not an acknowledgement of continued ownership.
  const realNow=Date.now;let elapsed=0;
  Date.now=()=>{elapsed+=31000;return elapsed};
  taskImpl=async()=>({status:'running'});api.renew=async()=>({renewed:false});
  try{
     next.observeChanges([change('expired-task')]);
     await until(()=>host.querySelector('.paper-preview-notice')?.textContent.includes('preview.expired'));
    assert.equal(host.querySelectorAll('canvas').length,0);
    assert.ok(detaches.some(row=>row[0]==='task-'+sequence));
  }finally{Date.now=realNow}
  next.destroy();dom.window.close();console.log(JSON.stringify({ok:true}));
})().catch(error=>{preview.destroy();dom.window.close();console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node','-e',script,str(ROOT),str(bundle)], cwd=ROOT,
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {'ok': True}


def test_preview_opens_without_edits_selects_persists_and_fences_discovery(tmp_path):
    bundle = native_module_path('paper-preview-document-controller.js', ROOT / 'frontend/src/features/paper/preview-controller.ts')
    setup = SETUP.split('(0,eval)')[0]
    script = setup + r"""
require(process.argv[2]);const assert=require('node:assert/strict');
const host=document.createElement('div');host.className='chat-wrapper';
host.innerHTML='<div class="chat-container"></div><div class="input-area"><textarea></textarea></div>';document.body.append(host);
let active='c',saved=null,held=null,discoverImpl=null,selectImpl=null;const starts=[],calls=[];
const a={source:'local',path:'CARD_project/main.tex',main_file:'CARD_project/main.tex',project_root:'/owned',label:'CARD_project/main.tex'};
const b={...a,path:'CARD_iclr2027/main.tex',main_file:'CARD_iclr2027/main.tex',label:'CARD_iclr2027/main.tex'};
const response=()=>({documents:[a,b],selected:saved,diagnostics:[]});
const api={start:async body=>{starts.push(body);return{task_id:'task-'+starts.length,document_id:'d'.repeat(64),subscriber_id:body.subscriber_id}},
 task:async()=>({status:'aborted'}),detach:async()=>{},renew:async()=>{},resolve:url=>url,bytes:async()=>new Uint8Array(),log:async()=>''};
const documentsApi={open:async()=>({document:null}),saveReading:async(id,body)=>({revision:1,state:body.state}),discover:async(body,signal)=>{calls.push(body);return discoverImpl?discoverImpl(body,signal):response()},
 select:async body=>{if(selectImpl)return selectImpl(body);saved=body.path===b.path?b:a;return{selected:saved}}};
const config={api,documentsApi,host:()=>host,activeConversation:()=>active,translate:key=>key,loadPdfJs:async()=>window.pdfjsLib,debounceMs:1,pollMs:1};
let preview=createPaperPreview(config);
const tick=()=>new Promise(r=>setTimeout(r,2));
async function until(test){for(let i=0;i<100&&!test();i++)await tick();assert.ok(test(),'asynchronous outcome');}
const compile=()=>host.querySelector('[aria-label="preview.compile"]');
const choose=()=>{
 const value=host.querySelector('select[aria-label="preview.chooseDocument"]');
 if(!value||value.closest('[hidden]'))host.querySelector('button[aria-label="preview.chooseDocument"]')?.click();
 return host.querySelector('select[aria-label="preview.chooseDocument"]');
};
(async()=>{
 preview.discover('c',{path:'/owned/CARD_project/main.tex'});await until(()=>host.querySelector('.paper-preview-reopen'));
 assert.equal(starts.length,0);assert.equal(host.querySelector('#paperPreviewPanel'),null);
 assert.equal(calls[0].path,undefined,'passive hint cannot hide template siblings');
 host.querySelector('.paper-preview-reopen').click();assert.ok(choose());assert.ok(compile().disabled);
 choose().value='1';choose().dispatchEvent(new window.Event('change'));await until(()=>saved===b);
 assert.equal(starts.length,0,'selection is not compilation');assert.equal(compile().disabled,false);
 compile().click();await until(()=>starts.length===1);
 assert.equal(starts[0].path,b.path);assert.equal(starts[0].main_file,b.main_file);assert.equal(starts[0].project_root,'/owned');
 preview.observeChanges([{conversationId:'c',source:'local',path:'/owned/CARD_project/main.tex',changeId:'other'}]);
 await tick();await tick();assert.equal(starts.length,1,'other document does not silently switch');
 preview.close();preview.destroy();preview=createPaperPreview(config);
 preview.discover('c');await until(()=>host.querySelector('.paper-preview-reopen'));
 assert.equal(host.querySelector('#paperPreviewPanel'),null,'closed intent survives recreation');
 preview.openDocument('c');await until(()=>choose()?.value==='1');assert.equal(starts.length,1,'revisit never recompiles history');
 // One active query and only one trailing latest demand.
 let release;discoverImpl=()=>new Promise(resolve=>{release=resolve});
 const baseline=calls.length;preview.discover('c');await until(()=>release);
 preview.discover('c');preview.discover('c');assert.equal(calls.length,baseline+1);
 discoverImpl=null;release(response());await until(()=>calls.length===baseline+2);
 await tick();assert.equal(calls.length,baseline+2);
 // A late selection response cannot open or compile in another conversation.
 let releaseSelect;selectImpl=()=>new Promise(resolve=>{releaseSelect=resolve});
 choose().value='0';choose().dispatchEvent(new window.Event('change'));await until(()=>releaseSelect);
 active='other';preview.activateConversation(active);preview.openDocument('other');
 await until(()=>choose()?.value==='1');
 releaseSelect({selected:a});await tick();
 assert.equal(choose().value,'1','old selection cannot overwrite new conversation');assert.equal(starts.length,1);selectImpl=null;
    // Explicit open creates an error surface without any receipt or selected
    // file; the notice carries the reason plus the copy-debug affordance.
    discoverImpl=async()=>{throw new Error('source unavailable')};preview.openDocument('other');
    await until(()=>host.querySelector('.paper-preview-notice')?.textContent.includes('source unavailable'));
    discoverImpl=()=>new Promise(resolve=>{held=resolve});preview.close();preview.openDocument('other');await until(()=>held);
 preview.close();held(response());await tick();assert.equal(host.querySelector('#paperPreviewPanel'),null);
 preview.destroy();dom.window.close();console.log('ok');
})().catch(error=>{preview.destroy();dom.window.close();console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(ROOT), str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip().splitlines()[-1] == 'ok'


def test_selected_main_dependencies_and_changed_discovery_invalidate_old_build(tmp_path):
    bundle = native_module_path('paper-preview-selection-races.js', ROOT / 'frontend/src/features/paper/preview-controller.ts')
    script = SETUP.split('(0,eval)')[0] + r"""
require(process.argv[2]);const assert=require('node:assert/strict');
const host=document.createElement('div');document.body.append(host);
const a={source:'local',path:'main.tex',main_file:'main.tex',project_root:'/owned',label:'main.tex'};
const b={...a,path:'sibling.tex',main_file:'sibling.tex',label:'sibling.tex'};
let selected=a,release,finishFirst;const starts=[],detached=[];
let discovery=()=>new Promise(resolve=>{finishFirst=resolve});
const api={start:async body=>{starts.push(body);return{task_id:'held',document_id:'a'.repeat(64),subscriber_id:body.subscriber_id}},
 task:()=>new Promise(resolve=>{release=resolve}),detach:async id=>detached.push(id),renew:async()=>({renewed:true}),
 viewer:async(d,b,body)=>({retained:body.operation!=='release',expires_in_seconds:240}),resolve:u=>u,bytes:async()=>new Uint8Array(),log:async()=>''};
const preview=createPaperPreview({api,host:()=>host,activeConversation:()=> 'c',translate:k=>k,debounceMs:1,pollMs:1,
 documentsApi:{open:async()=>({document:null}),saveReading:async(id,body)=>({revision:1,state:body.state}),discover:async()=>discovery?discovery():({documents:[a,b],selected,diagnostics:[]}),
 select:async()=>{throw new Error('compile must not depend on a selection write')}},loadPdfJs:async()=>window.pdfjsLib});
const tick=()=>new Promise(r=>setTimeout(r,2));
async function until(test){for(let i=0;i<100&&!test();i++)await tick();assert.ok(test(),'expected asynchronous outcome');}
const selection=()=>{
 if(!host.querySelector('select'))host.querySelector('button[aria-label="preview.chooseDocument"]')?.click();
 return host.querySelector('select')?.value;
};
(async()=>{
 preview.openDocument('c');await until(()=>finishFirst);
 const opening=host.querySelector('#paperPreviewPanel');
 host.querySelector('button[aria-label="preview.chooseDocument"]').click();
 const sourceAction=host.querySelector('button[aria-label="preview.discover"]');sourceAction.focus();
 discovery=null;finishFirst({documents:[a,b],selected,diagnostics:[]});
 await until(()=>host.querySelector('select')?.value==='0');
 assert.equal(host.querySelector('#paperPreviewPanel'),opening,'first discovery preserves the open drawer');
 assert.equal(host.querySelector('.paper-preview-drawer').hidden,false);
 assert.equal(document.activeElement,sourceAction,'discovery does not steal focus');
 preview.observeChanges([{conversationId:'c',changeId:'section',source:'local',path:'/owned/sections/intro.tex'}]);
 await until(()=>release);assert.equal(starts.length,1);assert.equal(starts[0].main_file,'main.tex');
 preview.discover('c');await tick();assert.equal(host.querySelector('.paper-preview-status').textContent,'preview.compiling');
 selected=b;preview.discover('c');await until(()=>selection()==='1');
 assert.ok(detached.includes('held'),'new selection detaches the old build');
 release({status:'done',result:{superseded:false,pdf_url:'/old.pdf',receipt:{document_id:'a'.repeat(64),build_id:'1'.repeat(32),status:'success',main_file:'main.tex',version_verified:true}}});
 await tick();assert.equal(host.querySelector('#paperPreviewPanel').dataset.buildId,undefined,'old build cannot publish under new selection');
 host.querySelector('[aria-label="preview.compile"]').click();await until(()=>starts.length===2);
 assert.equal(starts.at(-1).path,'sibling.tex');
 preview.destroy();dom.window.close();console.log('ok');
})().catch(e=>{preview.destroy();dom.window.close();console.error(e);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(ROOT), str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip().splitlines()[-1] == 'ok'
