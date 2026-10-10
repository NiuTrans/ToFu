"""Current Orchestration HTTP lists and stable domain arrays have separate admission."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_list_admission_preserves_transport_failure_and_domain_facades():
    client = native_module_path(
        '.native/orchestration-list-client.js',
        ROOT / 'frontend/src/features/orchestration/api-client.ts',
    )
    reader = native_module_path(
        '.native/orchestration-list-reader.js',
        ROOT / 'frontend/src/features/orchestration/definition-read.ts',
    )
    script = r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');global.window=global;
eval(fs.readFileSync(CLIENT,'utf8'));
eval(fs.readFileSync(READER,'utf8'));
const responses=[];
const host={orchestrations:{},get:async()=>responses.shift()};
const api=installOrchestrationApiClient(host);
const response=(ok,status,body)=>({ok,status,json:async()=>body});
(async()=>{
  for(const body of [[],[{id:'retired'}],{},null,{items:null},
    {ok:false,items:[{id:'rejected'}]}]){
    responses.push(response(true,200,body));
    const result=await api.listResult();
    assert.equal(result.status,200);
    assert.equal(result.accepted,false);
    assert.deepEqual(result.items,[]);
    const read=normalizeOrchestrationDefinitionListRead({ok:true,status:200,data:body});
    assert.equal(read.ok,false);
    assert.deepEqual(read.items,[]);
  }
  for(const items of [[],[{id:'valid'}]]){
    responses.push(response(true,200,{items}));
    const result=await api.listResult();
    assert.equal(result.accepted,true);
    assert.deepEqual(result.items,items);
    const read=normalizeOrchestrationDefinitionListRead({ok:true,status:200,data:{items}});
    assert.equal(read.ok,true);
    responses.push(response(true,200,{items}));
    assert.deepEqual(await api.list(),items);
  }
  responses.push(response(false,503,{items:[]}));
  const failed=await api.listResult();
  assert.equal(failed.status,503);
  assert.equal(failed.accepted,false);
  assert.equal(normalizeOrchestrationDefinitionListRead({
    ok:false,status:503,data:{items:[]}}).reason,'server-failed');
  assert.equal(normalizeOrchestrationDefinitionListRead([]).ok,true);
  assert.equal(normalizeOrchestrationDefinitionListRead({ok:true,status:200,data:{
    format:'tofu.orchestration.definition-list/v2',items:[]}}).reason,'unsupported-format');
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('CLIENT', json.dumps(client)).replace('READER', json.dumps(reader))
    result = subprocess.run(
        ['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
