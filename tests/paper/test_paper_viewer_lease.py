"""Reader reservation ownership, delayed acknowledgements and bounded renewal."""
import subprocess
from pathlib import Path

import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_viewer_lease_close_races_expiry_and_one_inflight_renewal(tmp_path):
    bundle = tmp_path / 'viewer.cjs'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    subprocess.run(['node', 'scripts/vite_test_bundle.mjs',
                    'frontend/src/features/paper/build-viewer-lease.ts', '--bundle',
                    '--format=cjs', '--platform=node', '--outfile=' + str(bundle)],
                   cwd=ROOT, check=True, capture_output=True)
    script = r"""
const assert=require('node:assert/strict');
const {BuildViewerLease}=require(process.argv[1]);
global.document=new EventTarget();document.visibilityState='visible';
global.window=new EventTarget();
global.crypto=require('node:crypto').webcrypto;
let now=0,sequence=0,lost=0,mode='ok',ack;
const timers=new Map(),calls=[];
global.performance={now:()=>now};
global.setTimeout=(fn,ms)=>{const id=++sequence;timers.set(id,{fn,at:now+ms});return id};
global.clearTimeout=id=>timers.delete(id);
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const tick=async ms=>{now+=ms;for(const [id,timer] of [...timers]){
  if(timer.at<=now){timers.delete(id);timer.fn()}}await settle()};
const success={retained:true,expires_in_seconds:240};
const api={viewer:async(d,b,request)=>{
  assert.equal(d,'d');assert.equal(b,'b');calls.push(request);
  if(request.operation==='release')return{retained:false,expires_in_seconds:0};
  if(mode==='hold')return new Promise(resolve=>{ack=resolve});
  if(mode==='offline')throw new Error('network unavailable');
  if(mode==='missing')return{retained:false,expires_in_seconds:0};
  return success;
}};
const create=()=>new BuildViewerLease(api,'d','b',()=>lost++);
(async()=>{
  // Closing before acquisition acknowledgement must release the late lease.
  mode='hold';let lease=create();const acquisition=lease.retain();lease.close();
  assert.deepEqual(calls.map(x=>x.operation),['retain']);ack(success);
  assert.equal(await acquisition,false);await settle();
  assert.deepEqual(calls.map(x=>x.operation),['retain','release']);assert.equal(timers.size,0);
  assert.equal(calls[0].viewer_id,calls[1].viewer_id);assert.equal(lost,0);
  // Visibility wakeups coalesce with an in-flight renewal, and closing waits for it.
  mode='ok';lease=create();assert.equal(await lease.retain(),true);assert.equal(timers.size,1);
  mode='hold';await tick(30000);const before=calls.length;
  for(let i=0;i<20;i++)document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(calls.length,before);lease.close();ack(success);await settle();
  assert.equal(calls.at(-1).operation,'release');assert.equal(timers.size,0);
  // A negative server acknowledgement is terminal, without silent reacquisition.
  mode='ok';lease=create();await lease.retain();mode='missing';await tick(30000);
  assert.equal(lost,1);assert.equal(timers.size,0);assert.equal(calls.at(-1).operation,'release');
  const released=calls.length;document.dispatchEvent(new Event('visibilitychange'));await settle();
  assert.equal(calls.length,released);
  // Temporary network failure retries within the last acknowledged lifetime only.
  mode='ok';lease=create();await lease.retain();mode='offline';await tick(30000);
  assert.equal(lost,1);assert.equal(timers.size,1);await tick(210000);
  assert.equal(lost,2);assert.equal(timers.size,0);
  // The bounded timer/metadata lifecycle also survives repeated opens and closes.
  mode='ok';for(let i=0;i<50;i++){lease=create();await lease.retain();lease.close()}
  await settle();assert.equal(timers.size,0);
  const retained=calls.filter(x=>x.operation==='retain').map(x=>x.viewer_id);
  assert.equal(new Set(retained).size,retained.length);
  assert.equal(calls.filter(x=>x.operation==='release').length,retained.length);
  window.dispatchEvent(new Event('pagehide'));assert.equal(lost,2);
  lease=create();await lease.retain();window.dispatchEvent(new Event('pagehide'));await settle();
  assert.equal(lost,3);assert.equal(timers.size,0);assert.equal(calls.at(-1).operation,'release');
  console.log('reader lease lifecycle OK');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script, str(bundle)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'reader lease lifecycle OK' in result.stdout
