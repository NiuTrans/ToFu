"""Typed endpoint composition and direct-HTTP request identity/lifecycle outcomes."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def registry_bundle():
    entry = """
import { createEndpointRegistry } from './index';
import { apiTransport } from '../transport';
import { waitForRetry } from './retry-delay';
(window as any).registryTest={createEndpointRegistry,apiTransport,waitForRetry};
"""
    with compiled_typescript(ROOT / 'frontend/src/api/endpoint-registry/index.ts', contents=entry) as bundle:
        yield bundle


def run_registry(bundle, script):
    preamble = r"""
const fs=require('fs'),assert=require('node:assert/strict');
global.window=globalThis;global.location={pathname:'/'};
global.document={getElementById:()=>null};
global.sessionStorage={getItem:()=>null,setItem(){}};
(new Function(fs.readFileSync(process.argv[1],'utf8')))();
const {createEndpointRegistry,apiTransport,waitForRetry}=registryTest;
const api=createEndpointRegistry(apiTransport,()=> 'en');
(async()=>{
"""
    completed = subprocess.run(['node', '-e', preamble + script + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
""", bundle], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_direct_conversation_reads_share_only_identical_requests_and_release_on_settlement(registry_bundle):
    run_registry(registry_bundle, r"""
const pending=[];global.fetch=(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}));
const options={query:{window:96,before_seq:7},headers:{'X-View':'first'},timeout:1000};
const first=api.conversations.get('same/id',options),joined=api.conversations.get('same/id',options);
assert.equal(first,joined);assert.equal(pending.length,1);
const variants=[{...options,headers:{'X-View':'second'}},{...options,timeout:2000},
 {...options,query:{...options.query,branch:'alternate'}},{...options,credentials:'omit'},
 {...options,parse:'text'},{...options,onError:'throw'}];
const reads=variants.map(value=>api.conversations.get('same/id',value));
assert.equal(pending.length,7);assert.ok(pending[0].url.includes('same%2Fid'));
pending.forEach((row,index)=>row.resolve(new Response(JSON.stringify({data:{index},meta:{}}),{headers:{'content-type':'application/json'}})));
await Promise.all([first,...reads]);
const next=api.conversations.get('same/id',options);assert.notEqual(next,first);assert.equal(pending.length,8);
pending.at(-1).resolve(new Response(JSON.stringify({data:{index:8},meta:{}}),{headers:{'content-type':'application/json'}}));await next;
""")


def test_signal_and_raw_response_reads_never_share_a_consumable_body(registry_bundle):
    run_registry(registry_bundle, r"""
let calls=0;global.fetch=async()=>{calls++;return new Response(JSON.stringify({data:{value:'body'},meta:{}}),{headers:{'content-type':'application/json'}});};
const controller=new AbortController(),options={signal:controller.signal};
const [a,b]=await Promise.all([api.conversations.get('id',options),api.conversations.get('id',options)]);
assert.notEqual(a,b);assert.equal(calls,2);
const [rawA,rawB]=await Promise.all([api.conversations.get('id',{parse:'response'}),api.conversations.get('id',{parse:'response'})]);
assert.notEqual(rawA,rawB);assert.equal((await rawA.json()).value,'body');assert.equal((await rawB.json()).value,'body');assert.equal(calls,4);
""")


def test_get_singleflight_capacity_limits_retention_without_limiting_requests(registry_bundle):
    run_registry(registry_bundle, r"""
const pending=[];global.fetch=()=>new Promise(resolve=>pending.push(resolve));
const reads=Array.from({length:160},(_,i)=>api.conversations.get('id-'+i));
assert.equal(pending.length,160);assert.equal(api.conversations.get('id-0'),reads[0]);
const overflow=api.conversations.get('id-159');assert.notEqual(overflow,reads[159]);assert.equal(pending.length,161);
pending.forEach(resolve=>resolve(new Response(JSON.stringify({data:{},meta:{}}),{headers:{'content-type':'application/json'}})));
await Promise.all([...reads,overflow]);
""")


def test_registry_construction_is_idle_and_language_stays_a_live_port(registry_bundle):
    run_registry(registry_bundle, r"""
let calls=0,timers=0,lang='zh';const originalTimeout=global.setTimeout;
global.setTimeout=()=>{timers++;throw new Error('unexpected idle timer');};
const records=[];const owner={...apiTransport,request:async(path,options)=>{calls++;records.push({path,options});return {};}};
const first=createEndpointRegistry(owner,()=>lang),second=createEndpointRegistry(owner,()=>lang);
assert.equal(calls,0);assert.equal(timers,0);assert.notEqual(first.project,second.project);assert.equal(first.ApiError,apiTransport.ApiError);
await first.conversations.generateTitle('id');lang='en';await first.conversations.generateTitle('id');
assert.equal(records[0].options.json.lang,'zh');assert.equal(records[1].options.json.lang,'en');assert.equal(timers,0);
global.setTimeout=originalTimeout;
""")


def test_working_facts_keep_scope_signal_and_revision_without_retrying_unknown_decisions(registry_bundle):
    run_registry(registry_bundle, r"""
const rows=[],abort=new AbortController(),command={action:'correct',id:'fact',revision:7,text:'new',note:'owner'};
const owner={...apiTransport,request:async(path,options)=>{
 rows.push({path,options});if(options.method==='POST')throw new Error('acknowledgement lost');return {revision:7};
}};
const local=createEndpointRegistry(owner,()=> 'en');
await local.conversations.workingFacts('a/b',abort.signal);
await local.conversations.workingFactConsumers('a/b','cursor',abort.signal);
await assert.rejects(local.conversations.saveWorkingFact('a/b',command,abort.signal),/acknowledgement lost/);
assert.equal(rows.length,3);assert.equal(rows[0].path,'/api/v4/conversations/a%2Fb/working-facts');
assert.equal(rows[1].path,rows[0].path+'/consumers');assert.deepEqual(rows[1].options.query,{cursor:'cursor',limit:50});
assert.equal(rows[2].options.json,command);assert.equal(rows[2].options.method,'POST');
for(const row of rows)assert.equal(row.options.signal,abort.signal);
""")


def test_idempotent_write_preserves_key_response_and_caller_options(registry_bundle):
    run_registry(registry_bundle, r"""
const delays=[],requests=[];let keys=0;
global.setTimeout=(callback,delay)=>{delays.push(delay);queueMicrotask(callback);return delays.length;};global.clearTimeout=()=>{};
const owner={...apiTransport,newIdempotencyKey:()=> 'stable-'+(++keys),request:async(path,options)=>{
 requests.push({path,options});return new Response('{}',{status:requests.length===1?503:200,headers:{'Retry-After':'0'}});
}};
const local=createEndpointRegistry(owner,()=> 'en'),patch={title:'value'};
const response=await local.conversations.patchSettings('a/b',patch);
assert.equal(response.status,200);assert.equal(keys,1);assert.equal(requests.length,2);
assert.equal(requests[0].options.headers['Idempotency-Key'],requests[1].options.headers['Idempotency-Key']);
assert.equal(requests[0].options.json,patch);assert.equal(requests[0].path,'/api/v4/conversations/a%2Fb/settings');assert.deepEqual(delays,[0]);assert.deepEqual(patch,{title:'value'});
""")


def test_abortable_retry_releases_wait_and_closes_registration_race(registry_bundle):
    run_registry(registry_bundle, r"""
const timers=new Map();let serial=0;global.setTimeout=(callback)=>{const id=++serial;timers.set(id,callback);return id;};global.clearTimeout=id=>timers.delete(id);
const controller=new AbortController();const pending=waitForRetry(2000,controller.signal);assert.equal(timers.size,1);controller.abort();assert.equal(await pending,false);assert.equal(timers.size,0);
let removed=0;const racing={aborted:false,addEventListener(){this.aborted=true;},removeEventListener(){removed++;}};
assert.equal(await waitForRetry(2000,racing),false);assert.equal(timers.size,0);assert.equal(removed,1);
""")


def test_project_path_singleflight_keeps_full_selection_and_independent_responses(registry_bundle):
    run_registry(registry_bundle, r"""
const pending=[];const owner={...apiTransport,request:(path,options)=>new Promise(resolve=>pending.push({path,options,resolve}))};
const first=createEndpointRegistry(owner,()=> 'en'),second=createEndpointRegistry(owner,()=> 'en');
const paths=Array.from({length:64},(_,i)=>'/project/'+i),recent=Array.from({length:40},(_,i)=>'/recent/'+i);
const a=first.project.setPaths(paths,[],recent),b=first.project.setPaths(paths,[],recent),c=second.project.setPaths(paths,[],recent);
assert.equal(pending.length,2);assert.equal(pending[0].options.json.paths.length,64);assert.equal(pending[0].options.json.recentPaths.length,32);assert.equal(paths.length,64);assert.equal(recent.length,40);
pending.forEach(row=>row.resolve(new Response('{"ok":true}')));const [ra,rb,rc]=await Promise.all([a,b,c]);
assert.notEqual(ra,rb);assert.notEqual(ra,rc);assert.deepEqual(await ra.json(),await rb.json());assert.equal((await rc.json()).ok,true);
""")


def test_caller_abort_does_not_cancel_other_reads_and_failed_shared_reads_release(registry_bundle):
    run_registry(registry_bundle, r"""
const pending=[];global.fetch=(url,options)=>new Promise((resolve,reject)=>{
 pending.push({resolve,reject,options});options.signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
});
const controller=new AbortController();const owned=api.conversations.get('id',{signal:controller.signal});
const shared=api.conversations.get('id'),joined=api.conversations.get('id');assert.equal(shared,joined);assert.equal(pending.length,2);
controller.abort();assert.equal(await owned,null);assert.equal(Boolean(pending[1].options.signal?.aborted),false);
pending[1].resolve(new Response(JSON.stringify({data:{survived:true},meta:{}}),{headers:{'content-type':'application/json'}}));assert.equal((await shared).survived,true);
const failure=api.conversations.get('id',{onError:'throw'}),sameFailure=api.conversations.get('id',{onError:'throw'});assert.equal(failure,sameFailure);
pending.at(-1).reject(new TypeError('offline'));await assert.rejects(failure,error=>error instanceof api.ApiError&&error.code==='network');
const retry=api.conversations.get('id',{onError:'throw'});assert.notEqual(retry,failure);assert.equal(pending.length,4);
pending.at(-1).resolve(new Response(JSON.stringify({data:{recovered:true},meta:{}}),{headers:{'content-type':'application/json'}}));assert.equal((await retry).recovered,true);
""")


def test_boot_retry_uses_existing_phase_command_and_preserves_refusal(registry_bundle):
    run_registry(registry_bundle, r"""
const calls=[];
global.fetch=async (url,options)=>{
  calls.push({url,options});
  return new Response(JSON.stringify({ok:true}),{headers:{'content-type':'application/json'}});
};
await api.health.retryBoot({timeout:1000});
assert.equal(calls.length,1);
assert.equal(calls[0].url,'/api/v4/boot/retry');
assert.equal(calls[0].options.method,'POST');
assert.equal(calls[0].options.body,'{}');
global.fetch=async ()=>new Response(JSON.stringify({error:'server_boot_failed'}),{
  status:503,headers:{'content-type':'application/json'}});
await assert.rejects(api.health.retryBoot({timeout:1000}),error=>error.status===503);
""")
