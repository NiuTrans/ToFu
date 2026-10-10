"""Reading writes coalesce, preserve final order and stop on cross-window conflicts."""
import subprocess

import pytest

from tests._runtime_sections import native_module_path
from tests.support.paper_reader_frontend import ROOT

pytestmark = pytest.mark.unit


def test_reading_writer_orders_final_save_bounds_requests_and_recovers_explicitly():
    bundle = native_module_path('paper-saved-reading.js', ROOT / 'frontend/src/features/paper/saved-reading.ts')
    script = r"""
const assert=require('assert/strict');require(process.argv[1]);
const calls=[],errors=[];
const api={saveReading:(id,body)=>new Promise((resolve,reject)=>calls.push({id,body,resolve,reject}))};
const writer=new SavedReading(api,(id,error)=>errors.push({id,error}));
const documentId='a'.repeat(64),buildId='b'.repeat(32);
const state=page=>({build_id:buildId,page,fraction:.3,horizontal:.5,scale:1.25,fit:false,reading_mode:true});
const record={document_id:documentId,reading_revision:0,reading:state(1)};
const tick=()=>new Promise(resolve=>setTimeout(resolve,1));
(async()=>{
 writer.open(record);
 writer.capture(documentId,{reading_mode:true,fit:false,scale:1.25,horizontal:.5,fraction:.3,page:1,build_id:buildId});
 writer.flush();assert.equal(calls.length,0,'JSON property order is not a reading change');
 writer.capture(documentId,state(2));writer.flush();assert.equal(calls.length,1);
 for(let page=3;page<=20;page++)writer.capture(documentId,state(page));
 writer.flush(documentId,true);assert.equal(calls.length,2,'one final request may overlap the ordinary save');
 assert.equal(calls[1].body.state.page,20);assert.equal(calls[1].body.sequence,19);
 assert.equal(calls[1].body.writer_id,calls[0].body.writer_id);
 assert.equal(calls[1].body.expected_revision,0);
 assert.ok(JSON.stringify(calls[1].body).length<1024);
 calls[1].resolve({revision:1,state:state(20)});await tick();await tick();
 calls[0].reject(new Error('old response lost'));await tick();await tick();
 assert.equal(errors.length,0,'failure of an already superseded save is harmless');
 assert.equal(calls.length,2);
 writer.open({...record,reading_revision:1,reading:state(20)});
 writer.capture(documentId,state(21));writer.flush();assert.equal(calls.length,3);
 const windowWriter=calls[2].body.writer_id;
 calls[2].reject({status:409,message:'Another window advanced the reading position'});await tick();await tick();
 assert.equal(errors.length,1);
 writer.capture(documentId,state(22));writer.flush();assert.equal(calls.length,3,'no automatic retry can overwrite another window');
 writer.open({...record,reading_revision:2,reading:state(30)});
 writer.capture(documentId,state(22));writer.flush();assert.equal(calls.length,4);
 assert.equal(calls[3].body.expected_revision,2);assert.notEqual(calls[3].body.writer_id,windowWriter);
 calls[3].resolve({revision:3,state:state(22)});await tick();await tick();writer.close();
 writer.capture(documentId,state(40));writer.flush();assert.equal(calls.length,4);
 console.log('ok');
})().catch(error=>{writer.close();console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip().endswith('ok')


def test_independent_document_saves_start_on_demand_and_close_drains_all():
    from lib.paper.builds.contract import limit
    bundle = native_module_path('paper-saved-reading-budget.js', ROOT / 'frontend/src/features/paper/saved-reading.ts')
    script = r"""
const assert=require('assert/strict');require(process.argv[1]);const maximum=Number(process.argv[2]);
const calls=[],errors=[];let active=0,peak=0;
const writer=new SavedReading({saveReading:(id,body)=>new Promise(resolve=>{
 active++;peak=Math.max(peak,active);calls.push({id,body,resolve:()=>{active--;resolve({revision:1,state:body.state})}});
})},(id,error)=>errors.push(id));
const id=n=>n.toString(16).padStart(64,'0');
const state={build_id:'b'.repeat(32),page:5,fraction:0,horizontal:.5,scale:1,fit:true,reading_mode:false};
const tick=()=>new Promise(resolve=>setTimeout(resolve,1));
(async()=>{
 for(let n=1;n<=maximum;n++){
  writer.open({document_id:id(n),reading_revision:0,reading:null});writer.capture(id(n),state);
  if(n===1)writer.flush();
 }
 writer.flush();assert.equal(calls.length,maximum,'independent documents start without a global request permit');
 writer.open({document_id:id(maximum+1),reading_revision:0,reading:null});
 writer.capture(id(maximum+1),state);writer.flush();
 assert.deepEqual(errors,[]);
 writer.close(id(maximum));assert.equal(calls.length,maximum+1);
 for(let n=0;n<=maximum;n++){
  for(let turn=0;turn<100&&calls.length<=n;turn++)await tick();
  assert.ok(calls[n]);calls[n].resolve();await tick();
 }
 await tick();assert.equal(calls.length,maximum+1);assert.equal(active,0);assert.equal(peak,maximum+1);
 console.log('ok');
})().catch(error=>{writer.close();console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(bundle), str(256)],
                            cwd=ROOT, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr + result.stdout
