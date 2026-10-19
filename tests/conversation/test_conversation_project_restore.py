"""Frontend project projection: drafts, late choices and isolated send roots."""
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
 const p=m.exports;
 let active='A', draft='unsent', state={}, serverWrites=0;
 const conv={id:'A',projectPath:'/A',projectPaths:['/extra','/A'],readOnlyPaths:['/A','/extra']};
 const other={id:'B',projectPath:'/B',projectPaths:['/B'],readOnlyPaths:[]};
 const original=JSON.stringify(conv);
 const c=p.createConversationProjectController({
   activeId:()=>active,exists:()=>true,apply:data=>{state=data;},
   // A forbidden navigation side effect: the projection must never call it.
   setPaths:()=>{serverWrites++;throw Error('global registry write');},
   hasDraft:()=>!!draft,
 });
 c.restore(conv);
 assert.equal(state.path,'/A');
 assert.deepStrictEqual(state.extraRoots,[{path:'/extra',readOnly:true}]);
 assert.equal(draft,'unsent'); assert.equal(serverWrites,0);
 const delayed=c.begin(); active='B'; c.restore(other);
 c.restore(conv,delayed); assert.equal(state.path,'/B');
 const staleChoice=c.beginMutation(); c.restore({...other,projectPath:'/chosen'});
 assert.equal(staleChoice(),false);
 active='A'; c.restore(conv);
 assert.equal(JSON.stringify(conv),original);
 const empty=p.captureConversationProject({...conv,projectPath:''});
 assert.deepStrictEqual(empty,{projectPath:'',projectPaths:[],readOnlyPaths:[]});
 const send=p.captureConversationProject(conv);
 conv.projectPaths.push('/later');
 assert.deepStrictEqual(send.projectPaths,['/A','/extra']);
 let finish;
 const pending=p.withProjectHydration(conv,async()=>{
   await new Promise(resolve=>finish=resolve);
   Object.assign(conv,p.protectHydratingProjectSettings(conv,other));
 });
 Object.assign(conv,{projectPath:'/chosen',projectPaths:['/chosen'],readOnlyPaths:['/chosen']});
 p.noteConversationProjectChoice(conv); finish(); await pending;
 assert.equal(conv.projectPath,'/chosen');
 assert.equal(p.protectHydratingProjectSettings(conv,other).projectPath,'/B');
 active=null; c.restore(null); assert.equal(state.active,false);
 c.chooseDraft({projectPath:'/draft'});
 assert.equal(c.captureDraft().projectPath,'/draft');
 c.restore(null); assert.equal(c.captureDraft().projectPath,'/draft');
 assert.equal(serverWrites,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
