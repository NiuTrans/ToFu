"""Typed conversation project restore: drafts, races and durable bindings."""
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_conversation_project_restore():
    script = r"""
const ts = require('typescript'), fs = require('fs'), assert = require('assert');
const m = {exports:{}};
new Function('module','exports', ts.transpileModule(fs.readFileSync(
 'frontend/src/conversation/application/conversation-project.ts','utf8'),
 {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText)(m,m.exports);
(async () => {
 let active='A', draft=false, state={}, calls=[], warnings=[], resolve;
 const conv={id:'A',projectPath:'/A',projectPaths:['/extra','/A'],readOnlyPaths:['/A','/extra']};
 const original=JSON.stringify(conv);
 let request=async () => ({ok:true,json:async()=>({path:'/A'})});
 const c=m.exports.createConversationProjectController({
 activeId:()=>active,exists:()=>true,hasDraft:()=>draft,selection:()=>state,
 clear:()=>{state={};},remote:(_,path)=>{state={path};},apply:data=>{state={...data,active:true};},
 setPaths:(...args)=>{calls.push(args);return request();},warn:x=>warnings.push(x)});
 await c.restore(conv);
 assert.deepStrictEqual(calls[0],[['/A','/extra'],['/A','/extra'],['/A']]);
 assert.equal(state.path,'/A');
 assert.deepStrictEqual(state.extraRoots,[{path:'/extra',readOnly:true}]);
 await c.restore(conv); assert.equal(calls.length,1); state={};
 request=async()=>({ok:true,json:async()=>({path:'/wrong'})});
 await c.restore(conv); assert.notEqual(state.path,'/wrong');
 assert.equal(JSON.stringify(conv),original); warnings=[]; calls=[];
 request=async()=>({ok:true,json:async()=>({path:'/A'})});
 await c.restore(conv);
 draft=true; state={path:'/draft'}; await c.restore(conv);
 assert.equal(state.path,'/draft'); assert.equal(calls.length,1);
 const blocked=c.begin(); draft=false; await c.restore(conv,blocked);
 assert.equal(calls.length,1);
 const cold=c.begin(); state={path:'/chosen'}; await c.restore(conv,cold);
 assert.equal(state.path,'/chosen');
 request=()=>new Promise(r=>resolve=r);
 let pending=c.restore(conv); draft=true;
 resolve({ok:true,json:async()=>({path:'/stale'})}); await pending;
 assert.notEqual(state.path,'/stale'); draft=false;
 pending=c.restore(conv); active='B'; c.begin(); active='A'; c.begin();
 resolve({ok:true,json:async()=>({path:'/stale'})}); await pending;
 assert.notEqual(state.path,'/stale');
 pending=c.restore(conv); state={path:'/user'};
 resolve({ok:true,json:async()=>({path:'/stale'})}); await pending;
 assert.equal(state.path,'/user');
 request=async()=>({ok:false,json:async()=>({})}); await c.restore(conv);
 assert.equal(warnings.length,1); assert.equal(JSON.stringify(conv),original);
 request=async()=>{throw Error('offline');}; await c.restore(conv);
 assert.equal(warnings.length,2); assert.equal(JSON.stringify(conv),original);
 await c.restore({id:'A',projectPath:'remote:device:/root'});
 assert.equal(state.path,'remote:device:/root');
 await c.restore({id:'A'}); assert.deepStrictEqual(state,{});
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
