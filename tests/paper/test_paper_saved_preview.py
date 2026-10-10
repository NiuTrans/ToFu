"""Saved preview orchestration, degraded discovery and explicit reading-save retry."""
import subprocess

import pytest

from tests._runtime_sections import native_module_path
from tests.support.paper_reader_frontend import ROOT, SETUP

pytestmark = pytest.mark.unit


def test_saved_pdf_survives_discovery_failure_and_reading_retry_does_not_reload():
    bundle = native_module_path('paper-saved-preview.js', ROOT / 'frontend/src/features/paper/preview-controller.ts')
    script = SETUP.split('(0,eval)')[0] + r"""
require(process.argv[2]);const assert=require('assert/strict');
Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});
const host=document.createElement('div');host.className='chat-wrapper';
host.innerHTML='<div class="chat-container"></div><div class="input-area"></div>';document.body.append(host);
const documentId='d'.repeat(64),buildId='b'.repeat(32),writes=[];let builds=0,failSave=true,failDiscovery;
const source={source:'local',path:'main.tex',main_file:'main.tex',project_root:'/owned',label:'main.tex',document_id:documentId};
const record={document_id:documentId,source,generation:1,status:'ready',can_compile:true,reading_revision:0,
 reading:{build_id:buildId,page:15,fraction:.35,horizontal:.5,scale:1.25,fit:false,reading_mode:true},
 build:{superseded:false,retained:true,pdf_url:'/saved.pdf',receipt:{build_id:buildId,document_id:documentId,
  status:'success',main_file:'main.tex',version_verified:true,artifacts:[]}}};
const api={start:async()=>{builds++;throw new Error('opening must not compile')},
 viewer:async(d,b,body)=>({retained:body.operation!=='release',expires_in_seconds:240}),
 resolve:url=>url,bytes:async()=>new Uint8Array([37,80,68,70]),detach:async()=>{}};
const documentsApi={open:async()=>({document:structuredClone(record)}),
 discover:()=>new Promise((_,reject)=>{failDiscovery=reject}),select:async()=>({selected:source}),
 saveReading:async(id,body)=>{writes.push(body);if(failSave)throw new Error('temporary save failure');
  record.reading=body.state;record.reading_revision++;return{revision:record.reading_revision,state:record.reading}}};
const preview=createPaperPreview({api,documentsApi,host:()=>host,activeConversation:()=> 'c',
 translate:key=>key,loadPdfJs:async()=>window.pdfjsLib});
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));
async function until(test){for(let n=0;n<250&&!test();n++)await tick();assert.ok(test(),'expected saved preview state');}
(async()=>{
 preview.openDocument('c');await until(()=>host.querySelector('#paperPreviewPanel')?.dataset.buildId===buildId);
 assert.equal(host.querySelector('.paper-preview-page-number').value,'15');
 assert.equal(host.querySelector('.paper-preview-zoom').value,'125%');
 assert.equal(host.dataset.paperPreviewReading,'true');assert.equal(builds,0);
 failDiscovery(new Error('discovery offline'));await until(()=>host.querySelector('.paper-preview-notice').textContent==='preview.discoveryFailed');
 assert.notEqual(host.querySelector('.paper-preview-status').textContent,'preview.failed');
 const zoom=host.querySelector('.paper-preview-zoom');zoom.value='150%';zoom.dispatchEvent(new window.Event('change'));
 await until(()=>host.querySelector('.paper-preview-notice button'));
 assert.equal(host.querySelector('.paper-preview-notice button').textContent,'paper.retry');
 const opens=opened.length;failSave=false;host.querySelector('.paper-preview-notice button').click();
 await until(()=>record.reading.scale===1.5);
 assert.equal(opened.length,opens,'saving a position never reloads the PDF');assert.equal(builds,0);
 assert.notEqual(writes.at(-1).writer_id,writes[0].writer_id);
 preview.close();preview.openDocument('c');await until(()=>host.querySelector('#paperPreviewPanel')?.dataset.buildId===buildId);
 assert.equal(host.querySelector('.paper-preview-zoom').value,'150%');assert.equal(builds,0);
 preview.destroy();dom.window.close();console.log('ok');
})().catch(error=>{preview.destroy();dom.window.close();console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(ROOT), str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
