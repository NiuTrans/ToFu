"""Every browser JSON consumption mode normalizes only the addressed v4 wire."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_v4_raw_response_json_preserves_http_stream_and_clone_contracts(tmp_path):
    built = tmp_path / 'transport.cjs'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    compilation = subprocess.run(['node', 'scripts/vite_test_bundle.mjs',
        'frontend/src/api/transport.ts', '--bundle', '--format=cjs', '--platform=node',
        f'--outfile={built}'], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert compilation.returncode == 0, compilation.stderr
    harness = r'''
const assert=require('assert/strict');
global.window=globalThis; global.TOFU_API_V4=true;
global.location={pathname:'/proxy/9000/'};
global.document={getElementById:()=>null};
global.sessionStorage={getItem:()=>null,setItem:()=>{}};
const transport=require(BUILT);
const domain={ok:true,items:[{id:'one'}],data:{nested:'domain field'}};
const wire={data:domain,meta:{requestId:'request-one',serverTimeMs:1}};
let source;
global.fetch=async()=>source;
async function main(){
 source=new Response(JSON.stringify(wire),{headers:{'Content-Type':'application/json','X-Total-Count':'1','ETag':'"v1"'}});
 const raw=await transport.request('/api/v1/conversations',{parse:'response'});
 assert.equal(raw,source); assert.equal(raw.bodyUsed,false);
 assert.equal(raw.status,200); assert.equal(raw.headers.get('X-Total-Count'),'1');
 const clone=raw.clone(), textClone=raw.clone();
 assert.deepEqual(await raw.json(),domain);
 assert.deepEqual(await clone.json(),domain);
 assert.deepEqual(JSON.parse(await textClone.text()),wire);
 await assert.rejects(()=>raw.json());
 source=new Response(JSON.stringify(wire));
 assert.deepEqual(await transport.request('/api/v4/test'),domain);
 for(const path of ['/api/v3/snapshot','https://outside.example/data']){
  source=new Response(JSON.stringify(wire));
  assert.deepEqual(await transport.request(path,{parse:'response'}).then(r=>r.json()),wire);
  source=new Response(JSON.stringify(wire));
  assert.deepEqual(await transport.request(path),wire);
 }
 source=new Response(JSON.stringify({data:{status:'connected'},meta:{}}));
 assert.deepEqual(await transport.request('/api/v4/mcp/connect',{parse:'response'}).then(r=>r.json()),{ok:true,status:'connected'});
 source=new Response(JSON.stringify({data:{status:'connected'},meta:{}}));
 assert.deepEqual(await transport.request('/api/v4/mcp/connect'),{ok:true,status:'connected'});
 for(const domain of [null,[1,2],{ok:false,error:'logical_failure'},
   {data:{nested:true},meta:{domain:'field'}}]){
  const expected=domain && !Array.isArray(domain) && typeof domain==='object' && !('ok' in domain)
   ? {ok:true,...domain} : domain;
  const wire={data:domain,meta:{requestId:'example',serverTimeMs:1}};
  source=new Response(JSON.stringify(wire));
  assert.deepEqual(await transport.request('/api/v4/example'),expected);
  source=new Response(JSON.stringify(wire));
  assert.deepEqual(await transport.request('/api/v4/example',{parse:'response'}).then(r=>r.json()),expected);
 }
 global.TOFU_API_V4=false;
 source=new Response(JSON.stringify(wire));
 assert.deepEqual(await transport.request('/api/v1/test',{parse:'response'}).then(r=>r.json()),wire);
 source=new Response(JSON.stringify(wire));
 assert.deepEqual(await transport.request('/api/v4/test',{parse:'response'}).then(r=>r.json()),domain);
 source=new Response(JSON.stringify({data:{taskId:'task-affinity',convId:'conv-affinity'},meta:{}}));
 await transport.request('/api/v4/tasks',{parse:'response',rememberTaskAffinity:true,taskAffinityKey:'owner-key'}).then(r=>r.json());
 let captured;
 global.fetch=async(url,options)=>{captured=options.headers;return new Response('{}')};
 await transport.request('/api/v4/tasks/task-affinity',{taskId:'task-affinity'});
 assert.equal(captured['X-Tofu-Affinity-Key'],'owner-key');
 global.fetch=async()=>source;
 for(const status of [401,429,503]){
  const problem={type:'about:blank',title:'Unavailable',status,data:{diagnostic:'keep'}};
  source=new Response(JSON.stringify(problem),{status,headers:{'Content-Type':'application/problem+json'}});
  const failed=await transport.request('/api/v4/test',{parse:'response'});
  assert.equal(failed.status,status); assert.deepEqual(await failed.json(),problem);
 }
 for(const status of [204,304]){
  source=new Response(null,{status});
  const empty=await transport.request('/api/v4/test',{parse:'response'});
  assert.equal(empty.status,status); assert.equal(await empty.text(),'');
 }
 let controller;
 source=new Response(new ReadableStream({start(c){controller=c}}),{headers:{'Content-Type':'text/event-stream'}});
 const stream=await transport.request('/api/v4/events',{parse:'response'});
 assert.equal(stream.body,source.body); assert.equal(stream.bodyUsed,false);
 controller.enqueue(new TextEncoder().encode('data: ready\n\n'));controller.close();
 assert.equal(await stream.text(),'data: ready\n\n');
}
main().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('BUILT', json.dumps(str(built)))
    result = subprocess.run(['node', '-e', harness], cwd=ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
