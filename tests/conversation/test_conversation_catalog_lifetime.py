"""Catalog retry and cancellation remain bounded even with noncooperative ports."""
from pathlib import Path
import shutil
import subprocess
import pytest
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
OWNER = native_module_path('.native/catalog-lifetime.js', ROOT / 'frontend/src/conversation/application/conversation-catalog-loader.ts')


def run_harness(body):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    prelude = r'''
const fs=require('fs'),assert=require('assert/strict');
eval(fs.readFileSync(process.argv[1],'utf8'));
const tick=()=>new Promise(setImmediate);
const response=(status,retry=null)=>({status,ok:status===200,
 headers:{get:name=>name==='Retry-After'?retry:null},json:async()=>({items:[]})});
const ports={applyAuthoritativeRows:()=>[],hasEveryAppliedRow:()=>false,
 writeCache:async()=>{},wait:async()=>{},warn:()=>{}};
'''
    source = prelude + '\n(async()=>{' + body + "\nconsole.log('catalog-lifetime-completed');\n})().catch(e=>{console.error(e);process.exitCode=1});"
    result=subprocess.run(['node','-e',source,OWNER],cwd=ROOT,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'catalog-lifetime-completed' in result.stdout, 'harness exited with unfinished async work'


def test_retry_delay_and_request_count_are_bounded():
    run_harness(r'''
for(const hint of ['999999999','Infinity','-9','0','bad']){
 let calls=0;const waits=[];
 const loader=createConversationCatalogLoader({...ports,
 requestCatalog:async()=>{calls++;return response(503,hint)},
 wait:async delay=>waits.push(delay)});
 await loader.load();loader.destroy();
 assert.equal(calls,3);
 assert.equal(waits.length,2,'no pointless sleep after last allowed request');
 assert(waits.every(ms=>Number.isFinite(ms)&&ms>=0&&ms<=5000),JSON.stringify(waits));
}
''')


def test_destroy_settles_a_stuck_request_and_aborts_its_signal():
    run_harness(r'''
let signal,applied=0,settled=false;
const loader=createConversationCatalogLoader({...ports,
 applyAuthoritativeRows:()=>{applied++;return []},
 requestCatalog:request=>{signal=request.signal;return new Promise(()=>{})}});
const flight=loader.load().then(()=>{settled=true});
await tick();loader.destroy();await tick();
assert.equal(settled,true,'destroy must release single-flight even if a port ignores abort');
assert.equal(signal.aborted,true);assert.equal(applied,0);await flight;
''')


def test_destroy_during_backoff_never_dispatches_another_request():
    run_harness(r'''
let release,calls=0;
const loader=createConversationCatalogLoader({...ports,
 requestCatalog:async()=>{calls++;return response(503,'1')},
 wait:()=>new Promise(resolve=>{release=resolve})});
const flight=loader.load();await tick();loader.destroy();release();await flight;
assert.equal(calls,1);
''')


def test_timeout_covers_json_body_and_cleans_owned_timer():
    run_harness(r'''
const timers=new Map();let next=0,signal,applied=0;
const loader=createConversationCatalogLoader({...ports,
 setTimeout:(callback,delay)=>{timers.set(++next,{callback,delay});return next},
 clearTimeout:id=>timers.delete(id),
 applyAuthoritativeRows:()=>{applied++;return []},
 requestCatalog:async request=>{signal=request.signal;return {...response(200),json:()=>new Promise(()=>{})}}});
let settled=false;const flight=loader.load().then(()=>{settled=true});await tick();
assert.equal(timers.size,1,'one timeout owns the whole response read');
const timer=[...timers.values()][0];assert.equal(timer.delay,12000);timer.callback();await tick();
assert.equal(settled,true);assert.equal(signal.aborted,true);assert.equal(timers.size,0);
assert.equal(applied,0);loader.destroy();await flight;
''')


def test_unconditional_recovery_shares_original_request_budget():
    run_harness(r'''
let calls=0,applied=0;
const statuses=[503,304,503,503,200];
const loader=createConversationCatalogLoader({...ports,
 applyAuthoritativeRows:()=>{applied++;return []},
 requestCatalog:async()=>response(statuses[calls++])});
await loader.load();loader.destroy();assert.equal(calls,3);assert.equal(applied,0);
''')


def test_default_backoff_timer_is_released_on_destroy():
    run_harness(r'''
const timers=new Map();let next=0,calls=0;
const loader=createConversationCatalogLoader({...ports,wait:undefined,
 setTimeout:(callback,delay)=>{timers.set(++next,{callback,delay});return next},
 clearTimeout:id=>timers.delete(id),
 requestCatalog:async()=>{calls++;return response(503,'999999')}});
let settled=false;const flight=loader.load().then(()=>{settled=true});await tick();
assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,5000);
loader.destroy();await tick();assert.equal(timers.size,0);assert.equal(settled,true);
assert.equal(calls,1);await flight;
''')


def test_late_json_after_destroy_cannot_publish_catalog_or_cache():
    run_harness(r'''
let release,applied=0,writes=0;
const loader=createConversationCatalogLoader({...ports,
 applyAuthoritativeRows:()=>{applied++;return []},writeCache:async()=>{writes++},
 requestCatalog:async()=>({...response(200),json:()=>new Promise(resolve=>{release=resolve})})});
const flight=loader.load();await tick();loader.destroy();await flight;
release({items:[{id:'late'}]});await tick();assert.equal(applied,0);assert.equal(writes,0);
''')
