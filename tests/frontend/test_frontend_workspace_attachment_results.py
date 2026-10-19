"""Optional project attachment and failed/delayed picker results preserve bindings."""
import json
from pathlib import Path
import subprocess

import pytest
from tests._runtime_sections import shipped_source_text

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == '{': depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0: return source[start:index + 1]
    raise AssertionError('unbalanced retained adapter')


@pytest.mark.parametrize('succeeds,still_current', [(False, True), (True, True), (False, False), (True, False)])
def test_picker_result_preserves_current_binding_and_complete_readonly_rollback(succeeds, still_current):
    function = _function(shipped_source_text('frontend/src/runtime/sections/project.js'), 'async function mpApplyFolders() {')
    script = r'''
const assert=require('assert');
let state={path:'/old',active:true,readOnly:true,extraRoots:[{path:'/reference',readOnly:true}]};
const previous=JSON.stringify(state), saved=[], requests=[];
const ProjectPresentationShellState={activeConversationId:'c',projectState:state,
 beginProjectMutation:()=>()=>__CURRENT__,
 captureProjectStatus:data=>({projectPath:data.path||'',projectPaths:[data.path,...(data.extraRoots||[]).map(r=>r.path)],
 readOnlyPaths:[...(data.readOnly?[data.path]:[]),...(data.extraRoots||[]).filter(r=>r.readOnly).map(r=>r.path)]})};
let _mpFolders=['/new','/new-ref'],_mpReadOnly=new Set(['/new-ref']);
const _saveConvProjectPath=(path,extraRoots,readonly)=>saved.push({path,extraRoots,readonly});
const closeProjectModal=()=>{},onProjectAttached=()=>{},onProjectCleared=()=>{},
 debugLog=()=>{},_applyProjectData=()=>{},_updateProjectUI=()=>{},escapeHtml=x=>x;
const document={getElementById:()=>({classList:{add(){}},innerHTML:''})};
const Api={project:{setPaths:async(...args)=>{requests.push(args);return {
 ok:__SUCCESS__,json:async()=>({path:'/new',extraRoots:[{path:'/new-ref',readOnly:true}],error:'declined'})};}}};
__FUNCTION__
(async()=>{
 await mpApplyFolders();
 assert.deepStrictEqual(requests,[[['/new','/new-ref'],['/new-ref'],['/new','/new-ref']]]);
 assert.equal(JSON.stringify(state),previous);
 assert.deepStrictEqual(saved[0],{path:'/new',extraRoots:['/new-ref'],readonly:['/new-ref']});
 if(!__CURRENT__) assert.equal(saved.length,1); // Stale successes and errors are inert.
 else if(!__SUCCESS__) assert.deepStrictEqual(saved[1],{path:'/old',extraRoots:['/reference'],readonly:['/old','/reference']});
 else assert.equal(saved[1].path,'/new');
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('__FUNCTION__', function).replace('__SUCCESS__', json.dumps(succeeds)).replace('__CURRENT__', json.dumps(still_current))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
