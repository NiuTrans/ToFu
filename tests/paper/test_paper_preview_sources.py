"""Drawer source interaction: problems jump, tree, layout and inverse SyncTeX."""
import subprocess

import pytest
from tests._runtime_sections import native_module_path
from tests.support.paper_reader_frontend import ROOT, SETUP

pytestmark = pytest.mark.unit


def test_preview_problems_sources_layout_and_inverse_jump(tmp_path):
    bundle = native_module_path('paper-preview-sources-controller.js',
                                ROOT / 'frontend/src/features/paper/preview-controller.ts')
    setup = SETUP.split('(0,eval)')[0]
    script = setup + r"""
require(process.argv[2]);const assert=require('node:assert/strict');
const host=document.createElement('div');host.className='chat-wrapper';
host.innerHTML='<div class="chat-container"></div><div class="input-area"><textarea></textarea></div>';document.body.append(host);
const active='c';const starts=[];
const receipt={schema_version:1,build_id:'b'.repeat(32),document_id:'d'.repeat(64),owner_user_id:1,
 target:'host',source_digest:'s'.repeat(64),version_verified:true,main_file:'main.tex',engine:'xelatex',
 toolchain:'test',status:'success',started_at:1,completed_at:2,artifacts:[{name:'sources.json',sha256:'x'.repeat(64),size_bytes:2}],
 diagnostics:[
  {severity:'error',code:'tex_error',message:'Undefined control sequence.',file:'chapters/intro.tex',line:12},
  {severity:'warning',code:'undefined_citation',message:"Citation `doe' undefined",file:'main.tex',line:30}]};
const selected={source:'local',path:'main.tex',main_file:'main.tex',project_root:'/owned',label:'main.tex'};
const manifest=JSON.stringify([{path:'main.tex',sha256:'a',size_bytes:3},{path:'chapters/intro.tex',sha256:'b',size_bytes:5}]);
const sourceReads=[],inverseCalls=[],layoutCalls=[];
const api={start:async body=>{starts.push(body);return{task_id:'task-1',document_id:receipt.document_id,subscriber_id:body.subscriber_id}},
 task:async()=>({status:'done',result:{receipt,superseded:false,pdf_url:'/paper.pdf'}}),
 viewer:async(d,b,body)=>({retained:true,expires_in_seconds:240}),
 renew:async()=>({renewed:true}),detach:async()=>{},resolve:url=>url,log:async()=>'',
 bytes:async(url)=>url.includes('sources.json')?new TextEncoder().encode(manifest).buffer:new Uint8Array([37,80,68,70]).buffer,
 inverse:async(d,b,body,signal)=>{inverseCalls.push(body);return{build_id:receipt.build_id,page:body.page,file:'main.tex',line:7}},
 layout:async(d,b,signal)=>{layoutCalls.push(1);return{build_id:receipt.build_id,pages:8,
  sections:[{level:'section',title:'Method',file:'main.tex',line:10,page:4}],
  end:{page:8,fullness_page:0.42,fullness_text:null}}}};
const documentsApi={discover:async()=>({documents:[selected],selected,diagnostics:[]}),
 select:async()=>({selected}),open:async()=>({document:null}),
 saveReading:async(id,body)=>({revision:1,state:body.state}),
 importArxiv:async()=>null,
 readSource:async(body,signal)=>{sourceReads.push(body);
  return{path:body.path,content:'line one\nline two\nline three',truncated:false,size_bytes:30,main_file:'main.tex'}}};
const config={api,documentsApi,host:()=>host,activeConversation:()=>active,translate:key=>key,
 loadPdfJs:async()=>window.pdfjsLib,debounceMs:1,pollMs:1};
const preview=createPaperPreview(config);
const tick=()=>new Promise(r=>setTimeout(r,2));
async function until(test){for(let i=0;i<100&&!test();i++)await tick();assert.ok(test(),'expected async outcome');}
(async()=>{
 preview.observeChanges([{conversationId:active,changeId:'one',source:'local',path:'main.tex',projectRoot:'/owned'}]);
 await until(()=>host.querySelector('#paperPreviewPanel')?.dataset.buildId);
 // Problems groups diagnostics errors-first and jumps to file:line.
 host.querySelector('button[aria-label="preview.details"]').click();
 const buttons=[...host.querySelectorAll('#paperPreviewDrawer button')];
 buttons.find(b=>b.getAttribute('aria-label')==='preview.problems').click();
 await until(()=>host.querySelectorAll('.paper-preview-problem').length===2);
 assert.ok(host.querySelector('.paper-preview-problem').className.includes('error'));
 host.querySelector('.paper-preview-problem').click();
 await until(()=>sourceReads.length===1);
 assert.deepEqual(sourceReads[0],{document_id:receipt.document_id,path:'chapters/intro.tex'});
 await until(()=>host.querySelectorAll('.paper-source-line').length===3);
 assert.ok(host.querySelector('.paper-source-lineno').textContent==='1');
 // Sources tree renders the frozen manifest and opens files.
 host.querySelector('button[aria-label="preview.details"]').click();
 [...host.querySelectorAll('#paperPreviewDrawer button')].find(b=>b.getAttribute('aria-label')==='preview.sources').click();
 await until(()=>host.querySelectorAll('.paper-source-tree-file').length===2);
 assert.ok(host.querySelector('.paper-source-tree-dir').textContent==='chapters/');
 // Root-level files render before directory groups: [0]=main.tex, [1]=chapters/intro.tex.
 host.querySelectorAll('.paper-source-tree-file')[0].click();
 await until(()=>sourceReads.length===2&&sourceReads[1].path==='main.tex');
 // Layout renders the page map.
 host.querySelector('button[aria-label="preview.details"]').click();
 [...host.querySelectorAll('#paperPreviewDrawer button')].find(b=>b.getAttribute('aria-label')==='preview.layout').click();
 await until(()=>layoutCalls.length===1&&host.querySelector('.paper-preview-layout-row'));
 assert.ok(host.querySelector('.paper-preview-layout-title').textContent.includes('Method'));
 // Inverse SyncTeX: ctrl+click on the PDF page opens the mapped source line.
 const page=host.querySelector('.paper-reader-page');assert.ok(page,'pdf page exists');
 const rect={left:0,top:0,width:600,height:800};page.getBoundingClientRect=()=>({...rect,right:600,bottom:800});
 page.dispatchEvent(new window.MouseEvent('click',{bubbles:true,ctrlKey:true,clientX:300,clientY:400}));
 await until(()=>inverseCalls.length===1);
 assert.equal(inverseCalls[0].page,1);
 // The 50%/50% click lands inside the page: positive bp coordinates, mapped result.
 assert.ok(inverseCalls[0].h>0&&inverseCalls[0].v>0,JSON.stringify(inverseCalls[0]));
 await until(()=>sourceReads.length===3&&sourceReads[2].path==='main.tex');
 preview.destroy();dom.window.close();console.log('ok');
})().catch(error=>{preview.destroy();dom.window.close();console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(ROOT), str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip().splitlines()[-1] == 'ok'
