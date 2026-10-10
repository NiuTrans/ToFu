"""Parsed HTTP requests own deadlines/cancellation until body consumption ends."""
import json
from pathlib import Path
import subprocess
import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(scope='module')
def transport_bundle(tmp_path_factory):
    path = tmp_path_factory.mktemp('body-lifetime') / 'transport.cjs'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    built = subprocess.run(['node', 'scripts/vite_test_bundle.mjs',
        'frontend/src/api/transport.ts', '--bundle', '--format=cjs', '--platform=node',
        f'--outfile={path}'], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stderr
    return path

@pytest.mark.parametrize('parse,status,cancel,on_error', [
    ('json', 200, False, None), ('text', 200, False, None),
    ('blob', 200, False, None), ('json', 503, False, None),
    ('json', 200, True, None), ('json', 200, False, 'null'),
])
def test_response_body_obeys_request_lifetime(transport_bundle, parse, status, cancel, on_error):
    harness = r'''
const assert=require('node:assert/strict');
global.window=globalThis; global.TOFU_API_V4=true;
global.location={pathname:'/'};
global.document={getElementById:()=>null};
global.sessionStorage={getItem:()=>null,setItem:()=>{}};
const transport=require(BUILT);
const timers=new Map(); let timerId=0;
global.setTimeout=(callback)=>{const id=++timerId;timers.set(id,callback);return id;};
global.clearTimeout=(id)=>timers.delete(id);
const user=new AbortController(); let forwardingListeners=0;
const add=user.signal.addEventListener.bind(user.signal);
const remove=user.signal.removeEventListener.bind(user.signal);
user.signal.addEventListener=(type,...args)=>{if(type==='abort') forwardingListeners++;return add(type,...args);};
user.signal.removeEventListener=(type,...args)=>{if(type==='abort') forwardingListeners--;return remove(type,...args);};
let headersReceived=false;
global.fetch=async (url, init)=>{
 const body=new ReadableStream({start(controller){
   init.signal.addEventListener('abort',()=>controller.error(init.signal.reason),{once:true});
 }});
 headersReceived=true;
 return new Response(body,{status:STATUS,headers:{'Content-Type':'application/json'}});
};
async function main(){
 const pending=transport.request('/api/v4/test',{parse:PARSE,timeout:50,signal:user.signal,onError:ON_ERROR})
   .then(value=>({value}),error=>({error}));
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(headersReceived,true);
 assert.equal(timers.size,1,'deadline must remain active after headers');
 assert.equal(forwardingListeners,1,'caller cancellation must remain connected during body read');
 if(CANCEL) user.abort(new Error('caller cancelled'));
 else [...timers.values()][0]();
 const result=await pending;
 if(ON_ERROR==='null') assert.equal(result.value,null);
 else assert.equal(result.error?.code,CANCEL?'aborted':'timeout');
 assert.equal(timers.size,0,'settlement releases the timer');
 assert.equal(forwardingListeners,0,'settlement releases caller signal listeners');
 global.fetch=async()=>new Response(JSON.stringify({data:{status:'done'},meta:{}}));
 assert.deepEqual(await transport.request('/api/v4/test',{timeout:50,signal:user.signal.aborted?undefined:user.signal}),{ok:true,status:'done'});
 assert.equal(timers.size,0,'successful body consumption also releases its timer');
 assert.equal(forwardingListeners,0);
 let cancelledBodies=0;
 global.fetch=async()=>new Response(new ReadableStream({cancel(){
   cancelledBodies++; return Promise.reject(new Error('peer cancellation failed'));
 }}));
 assert.equal(await transport.request('/api/v4/test',{parse:'none',timeout:50}),null);
 assert.equal(cancelledBodies,1,'unused successful bodies must be released');
 assert.equal(timers.size,0);
 await new Promise(resolve=>setImmediate(resolve));
 console.log('BODY_LIFETIME_COMPLETE');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
'''
    for key, value in {'BUILT':str(transport_bundle),'PARSE':parse,'STATUS':status,
                       'CANCEL':cancel,'ON_ERROR':on_error}.items():
        harness=harness.replace(key,json.dumps(value))
    result=subprocess.run(['node','-e',harness],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'BODY_LIFETIME_COMPLETE' in result.stdout, 'unfinished promise must not look like a pass'
