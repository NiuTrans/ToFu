"""Research reader transport faults and bounded adapter lifecycles."""
import subprocess

import pytest

from tests._runtime_sections import native_module_path
from tests.support.paper_reader_frontend import ROOT, SETUP

pytestmark = pytest.mark.unit


def test_read_retry_preserves_manuscript_and_repeated_teardown_releases_leases():
    bundle = native_module_path('research-manuscript-reader.js', ROOT /
                               'frontend/src/features/evolution/research/components/manuscript-preview.ts')
    script = SETUP.split('(0,eval)')[0] + r"""
require(process.argv[2]);const assert=require('assert/strict');
const host=document.createElement('div');document.body.append(host);
const holds=new Map();let rejectRetain=true,peak=0,compiles=0,retains=0;
const api={resolve:url=>url,bytes:async()=>new Uint8Array([37,80,68,70]),
 start:async()=>{compiles++;throw Error('reading cannot compile')},
 viewer:async(d,b,body)=>{
  if(body.operation==='retain'){
   retains++;if(rejectRetain){rejectRetain=false;throw Error('temporary read outage')}
   holds.set(body.viewer_id,b);peak=Math.max(peak,holds.size);
  }
  if(body.operation==='release')holds.delete(body.viewer_id);
  return{retained:holds.has(body.viewer_id),expires_in_seconds:240};
 }};
const options={api,translate:key=>key,loadPdfJs:async()=>window.pdfjsLib};
const build=id=>({document_id:'d'.repeat(64),build_id:id.repeat(32)});
let preview=new ManuscriptPreview(host,options);
async function until(check){for(let i=0;i<250&&!check();i++)await new Promise(resolve=>setTimeout(resolve,10));assert.ok(check(),'expected reader state')}
function retry(){host.querySelector('.paper-preview-notice button').click()}
(async()=>{
 preview.show(build('a'));await until(()=>host.querySelector('.paper-preview-notice button'));
 assert.equal(opened.length,0);assert.equal(holds.size,0);retry();
 await until(()=>preview.element.dataset.buildId==='a'.repeat(32));assert.equal(holds.size,1);
 preview.reader.goToPage(15);preview.reader.setScale(1.25);
 const sameRetains=retains;preview.show(build('a'));assert.equal(retains,sameRetains);
 const oldView={...preview.view(),position:{page:1,fraction:0,horizontal:.5,scale:1,fit:true}};
 renderFault=()=>true;preview.show(build('b'),oldView);
 await until(()=>host.querySelector('.paper-preview-notice button') && holds.size===1);
 assert.equal(preview.element.dataset.buildId,'a'.repeat(32));
 assert.equal(preview.view().position.page,15);assert.equal(preview.view().position.scale,1.25);
 renderFault=null;retry();await until(()=>preview.element.dataset.buildId==='b'.repeat(32));
 assert.equal(preview.view().position.page,15);assert.equal(holds.size,1);assert.ok(peak<=2);
 preview.destroy();await until(()=>holds.size===0);
 for(let i=0;i<50;i++){
  preview=new ManuscriptPreview(host,options);preview.show(build('c'));
  await until(()=>preview.element.dataset.buildId==='c'.repeat(32));
  preview.destroy();await until(()=>holds.size===0);
  assert.equal(host.childElementCount,0);
 }
 assert.equal(compiles,0);assert.ok(peak<=2);console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1}).finally(()=>{preview.destroy();dom.window.close()});
"""
    result = subprocess.run(['node', '-e', script, str(ROOT), str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr + result.stdout
