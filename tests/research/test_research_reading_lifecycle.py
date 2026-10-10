"""Research reader restores before paint and fences saves across visibility/races."""
import subprocess

import pytest

from tests._runtime_sections import native_module_path
from tests.support.paper_reader_frontend import ROOT, SETUP

pytestmark = pytest.mark.unit


def test_saved_position_first_paint_retry_navigation_and_bounded_visibility():
    bundle = native_module_path('research-reading-lifecycle.js', ROOT /
                               'frontend/src/features/evolution/research/controllers/manuscript.ts')
    script = SETUP.split('(0,eval)')[0] + r"""
require(process.argv[2]);const assert=require('assert/strict');
const host=document.createElement('div');document.body.append(host);
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}};
const opens=[],writes=[],previews=[];let current=null,live=0,peak=0,failSave=false;
const state={build_id:'a'.repeat(32),page:15,fraction:.3,horizontal:.5,scale:1.25,fit:false,reading_mode:true};
const doc=id=>id.repeat(64),build=(id='d',b='a')=>({document_id:doc(id),build_id:b.repeat(32)});
const context={direction:'saved Research',lang:'en'};
const record=(id='d',reading=state,revision=3)=>({document:{document_id:doc(id),reading,reading_revision:revision}});
const api={openResearch:(body,signal)=>{const d=deferred();opens.push({...d,body,signal});return d.promise},
 saveReading:async(id,body)=>{writes.push({id,body});if(failSave)throw Error('conflict');return{reading:body.state,revision:body.expected_revision+1}}};
const preview=(host,onReading)=>{
 const p={host,calls:[],current:null,problem:null,
  show(b,saved){this.calls.push({b,saved});if(!this.current)this.current=saved??{documentId:b.document_id,buildId:b.build_id,reading:false,position:{page:1,fraction:0,horizontal:.5,scale:1,fit:true}}},
  view(){return this.current},setReadingProblem(retry,unavailable){this.problem=retry?{retry,unavailable}:null},
  move(page){this.current={...this.current,position:{...this.current.position,page}};onReading(this.current)},
  destroy(){live--;this.dead=true}};
 live++;peak=Math.max(peak,live);previews.push(p);current=p;return p;
};
async function settle(){for(let i=0;i<8;i++)await Promise.resolve()}
let owner=new ResearchManuscript({api,preview});
(async()=>{
 owner.show(host,context,build());assert.equal(opens.length,1);assert.equal(current.calls.length,0);
 opens[0].resolve(record());await settle();
 assert.equal(current.calls[0].saved.position.page,15);assert.equal(current.calls[0].saved.reading,true);
 assert.equal(current.calls[0].saved.position.scale,1.25);
 current.move(16);owner.hide();await settle();assert.equal(live,0);
 assert.equal(writes.at(-1).body.expected_revision,3);assert.equal(writes.at(-1).body.state.page,16);
 const writer=writes.at(-1).body.writer_id;
 owner.show(host,context,build());assert.equal(opens.length,1);assert.equal(current.calls[0].saved.position.page,16);
 current.move(17);window.dispatchEvent(new window.Event('pagehide'));await settle();
 assert.equal(live,0);assert.equal(writes.at(-1).body.state.page,17);assert.equal(writes.at(-1).body.writer_id,writer);
 window.dispatchEvent(new window.Event('pageshow'));assert.equal(live,1);assert.equal(current.calls[0].saved.position.page,17);
 failSave=true;current.move(18);document.dispatchEvent(new window.Event('visibilitychange'));
 owner.hide();await settle();owner.show(host,context,build());assert.ok(current.problem);
 failSave=false;current.problem.retry();assert.equal(opens.length,2);current.move(19);
 const visible=current;opens[1].resolve(record('d',state,9));await settle();
 assert.equal(current,visible);assert.equal(current.current.position.page,19);assert.equal(current.problem,null);
 owner.hide();await settle();assert.equal(writes.at(-1).body.expected_revision,9);
 assert.equal(writes.at(-1).body.state.page,19);assert.notEqual(writes.at(-1).body.writer_id,writer);
 owner.show(host,{...context,direction:'second'},build('e'));
 const stale=opens.at(-1);owner.show(host,{...context,direction:'third'},build('f'));
 assert.equal(stale.signal.aborted,true);const fresh=opens.at(-1);
 stale.resolve(record('e'));await settle();assert.equal(current.calls.length,0);
 fresh.reject(Error('metadata unavailable'));await settle();assert.equal(current.calls[0].b.document_id,doc('f'));
 assert.equal(current.problem.unavailable,true);current.move(7);current.problem.retry();
 opens.at(-1).resolve(record('f',null,0));await settle();assert.equal(current.current.position.page,7);
 for(let i=0;i<50;i++){owner.hide();owner.show(host,{...context,direction:'third'},build('f'))}
 assert.equal(live,1);assert.equal(peak,1);owner.destroy();await settle();assert.equal(live,0);
 const oldCount=opens.length;window.dispatchEvent(new window.Event('pageshow'));assert.equal(opens.length,oldCount);
 owner=new ResearchManuscript({api,preview});owner.show(host,context,build());opens.at(-1).resolve(record('d',{...state,page:19},10));await settle();
 assert.equal(current.calls[0].saved.position.page,19);console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1}).finally(()=>{owner.destroy();dom.window.close()});
"""
    result = subprocess.run(['node', '-e', script, str(ROOT), str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr + result.stdout
