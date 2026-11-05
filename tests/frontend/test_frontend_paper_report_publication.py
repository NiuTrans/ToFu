"""Report observation adopts artifacts without writing stale library metadata."""
from pathlib import Path
import json
import subprocess

import pytest
from tests._jsdom import require_node_modules
from tests._paper_vite import compiled_typescript
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'frontend/src/features/paper/report-runtime.ts'

ENTRY = "\nexport * from './report-runtime';\nexport { saveActivePaperState } from './library';\nexport { createReportEventProjection } from './report-events';\nexport { featureRegistry } from '../../feature-registry';\n"

PROGRAM = r"""
const fs = require('fs');
const {JSDOM} = require(process.argv[1] + '/node_modules/jsdom');
const dom = new JSDOM('<!doctype html><body></body>', {url:'http://localhost/'});
global.window=global; global.document=dom.window.document;
global.localStorage=dom.window.localStorage;
global._activePaperId='paper-a'; global._paperFileName='Known paper';
global._paperTotalPages=4;
global._paperLibrary=[{id:'paper-a',title:'Known paper',createdAt:1,pageCount:4,
  _detailLoaded:true,_persisted:true}];
const timers=new Map();let timerId=0;
global.setTimeout=fn=>{timers.set(++timerId,fn);return timerId;};
global.clearTimeout=id=>timers.delete(id);
const stored={title:'Remote edit',folderId:'remote-folder'};
const calls={snapshots:0,venue:0,saves:0,upserts:[],paints:0,titles:0};
const saves=[];
const body='# Report\n\n'+'Verified content. '.repeat(1000);
let data={ok:true,status:'done',workerActive:true,report:body,
  meta:{model:'model-a',totalCostUsd:1},next_cursor:1,
  events:[{seq:0,type:'done',report:body,meta:{model:'model-a',totalCostUsd:1}}]};
global.Api={paper:{
  reportPoll:async()=>({ok:true,json:async()=>JSON.parse(JSON.stringify(data))}),
  libraryUpsert:async(id,body)=>{calls.upserts.push({id,body});Object.assign(stored,body);return {ok:true};}
}};
(0,eval)(fs.readFileSync(process.argv[2],'utf8'));
const R=global.TofuTestBundle;
const stream=R.makeReportStreamState('paper-a','en','task-a','review');
const view={kind:'review',stream,cache:'',meta:null,langKey:()=> 'review:venue-a:en'};
global._reportView=()=>view;
global._rememberReportSnapshot=(...args)=>{calls.snapshots++;R.rememberReportSnapshot(...args);};
global._persistGeneratedReviewVenue=()=>{calls.venue++;};
global._paintReportFromState=()=>{calls.paints++;};
global._applyResolvedTitle=()=>{calls.titles++;};
global._saveActivePaperState=(scope)=>{calls.saves++;const value=R.saveActivePaperState(scope);saves.push(value);return value;};
for(const name of ['_rememberReportSnapshot','_saveActivePaperState','_persistGeneratedReviewVenue','_paintReportFromState','_applyResolvedTitle']) R.featureRegistry[name]=global[name];
global._applyReportEventRaw=R.createReportEventProjection({
  isActivePaper:id=>id===global._activePaperId,viewFor:()=>view,
  setPaperHash(){},rememberSnapshot:global._rememberReportSnapshot,
  persistReviewVenue:global._persistGeneratedReviewVenue,
  applyResolvedTitle:global._applyResolvedTitle,
});
(async()=>{
  const started=performance.now();
  for(let page=0;page<30;page++){
    if(page===1)data={...data,events:[]};
    if(page===15)data={...data,meta:{model:'model-a',totalCostUsd:2},next_cursor:2,
      events:[{seq:1,type:'report_meta',meta:{model:'model-a',totalCostUsd:2}}]};
    if(page===16)data={...data,events:[]};
    if(page===29)data={...data,workerActive:false};
    await R.pollReportTask(view);
    await Promise.all(saves);
  }
  const durationMs=performance.now()-started;
  if(view.cache!==body||view.meta.totalCostUsd!==2||stream.status!=='done'||timers.size)
    throw new Error('publication/replay outcome changed');
  const reportUpserts=calls.upserts.length;
  const storedAfterReport={...stored};
  const reportBodies=[...new Set(calls.upserts.map(row=>JSON.stringify(row.body)))];
  global._paperFileName='Explicit local edit';
  global._paperLibrary[0].folderId='remote-folder';
  await R.saveActivePaperState('metadata');
  console.log(JSON.stringify({storedAfterReport,storedAfterEdit:stored,explicitEditUpserts:calls.upserts.length-reportUpserts,pages:30,bodyChars:body.length,finalCost:2,
    snapshots:calls.snapshots,venue:calls.venue,saves:calls.saves,
    libraryUpserts:reportUpserts,upsertBodies:reportBodies,
    paints:calls.paints,retainedTimers:timers.size,durationMs}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def probe_publication_effects():
    with compiled_typescript(SOURCE, contents=ENTRY) as bundle:
        result = subprocess.run(['node', '-e', PROGRAM, str(ROOT), bundle],
                                capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout.splitlines()[-1])


def test_report_observation_does_not_overwrite_remote_library_metadata():
    observed = probe_publication_effects()
    assert observed['libraryUpserts'] == 0
    assert observed['storedAfterReport'] == {
        'title': 'Remote edit', 'folderId': 'remote-folder',
    }
    assert observed['explicitEditUpserts'] == 1
    assert observed['storedAfterEdit']['title'] == 'Explicit local edit'
    assert observed['storedAfterEdit']['folderId'] == 'remote-folder'
    assert observed['finalCost'] == 2 and observed['retainedTimers'] == 0


_TITLE_PROGRAM = r"""
const fs=require('fs'),assert=require('assert/strict');
const {JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><span id="paperSidebarTitle"></span></body>',{url:'http://localhost/'});
global.window=global;global.runtimeScope=global;global.document=dom.window.document;
global.localStorage=dom.window.localStorage;
global.t=key=>key;
new Function('runtimeScope',fs.readFileSync(process.argv[2],'utf8'))(global);
global._activePaperId='active';
const active={id:'active',title:'arxiv:1234',folderId:'old-folder'};
const inactive={id:'inactive',title:'arxiv:5678'};
const custom={id:'custom',title:'User title'};
global._paperLibrary=[active,inactive,custom];
let saves=0,renders=0;
global._saveActivePaperState=()=>{saves++;};
global._renderPaperLibrary=()=>{renders++;};
_applyResolvedTitle('Server title','active');
assert.equal(active.title,'Server title');
assert.equal(document.getElementById('paperSidebarTitle').textContent,'Server title');
assert.equal(saves,0,'a server-confirmed title is a projection, not a library edit');
_applyResolvedTitle('Server title','active');
assert.equal(renders,1);
_applyResolvedTitle('Other server title','inactive');
assert.equal(inactive.title,'Other server title');
assert.equal(document.getElementById('paperSidebarTitle').textContent,'Server title');
_applyResolvedTitle('Do not replace user title','custom');
assert.equal(custom.title,'User title');
assert.equal(renders,2);
assert.equal(saves,0);
console.log('title adoption verified');
"""


def test_server_title_adoption_preserves_local_projection_without_writeback():
    require_node_modules('jsdom')
    section = runtime_section_path('paper-reader.js', scope_prelude=False)
    result = subprocess.run(['node', '-e', _TITLE_PROGRAM, str(ROOT), str(section)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
