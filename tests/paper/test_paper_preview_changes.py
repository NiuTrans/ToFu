"""Committed file receipts trigger builds independently of rendered tool history."""
from pathlib import Path
import shutil
import subprocess
import pytest
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_preview_observes_commits_once_and_fences_history_background_and_pending_changes():
    module = native_module_path('paper-preview-changes.js', ROOT / 'frontend/src/features/paper/preview-changes.ts')
    store_module = native_module_path('paper-preview-turn-store.js', ROOT / 'frontend/src/core/turn-state.ts')
    script = r"""
const assert=require('node:assert/strict');require(process.argv[1]);
require(process.argv[2]);
const received=[],activations=[];
const owner=createPaperPreviewChanges({changed:changes=>received.push(...changes),activated:id=>activations.push(id)});
const file=(path,id,extra={})=>({path,changeId:id,source:'local',...extra});
const turn=(id,ordinal,files,state='applied',commandId)=>({turnId:id,ordinal,projection:{fileChanges:{blockId:'file-changes',count:files.length,files,state,commandId}}});
const stores=new Map();let revision=0;
function store(id){
  if(!stores.has(id)){
    const value=createTurnStore(id);
    value.subscribe((state,update)=>owner.observe(id,state,update));
    stores.set(id,value);
  }
  return stores.get(id);
}
const observe=(id,turns)=>store(id).dispatch({type:'snapshot',snapshot:{
  turns:structuredClone(turns),conversationRevision:++revision}});
owner.activateConversation('conv');
owner.observe('conv',store('conv').getState());
store('conv').dispatch({type:'transport',status:'connecting'});
let current=turn('t',2,[file('main.tex','old')]);
observe('conv',[current]);observe('conv',[current]);
assert.equal(received.length,0,'initial history is a baseline');
current=turn('t',2,[file('main.tex','new')]);
observe('conv',[current]);observe('conv',[current]);
assert.equal(received.length,1);assert.equal(received[0].changeId,'new');
current=turn('t',2,[file('main.tex','new'),file('refs.bib','bib'),file('figures/a.png','image',{root:'extra'})]);
observe('conv',[current]);
assert.deepEqual(received.slice(-2).map(row=>row.path),['refs.bib','extra:figures/a.png']);
const pending=turn('next',3,[file('main.tex','pending',{pending:true})]);
observe('conv',[current,pending]);
assert.equal(received.length,3);
pending.projection.fileChanges.files[0].pending=false;
observe('conv',[current,pending]);
assert.equal(received.length,4,'settled pending receipt becomes eligible');
observe('background',[turn('foreign',1,[file('main.tex','foreign')])]);
observe('conv',[turn('history',1,[file('old.tex','backfill')]),current,pending]);
assert.equal(received.length,4,'backfill and background state do not compile');
const undo=turn('next',3,[file('main.tex','pending')],'undoing','undo');
observe('conv',[current,undo]);assert.equal(received.length,4);
undo.projection.fileChanges.state='undone';
observe('conv',[current,undo]);
assert.equal(received.at(-1).path,'.');assert.equal(received.at(-1).changeId,'undo');
observe('conv',[current,undo]);assert.equal(received.length,5);
owner.activateConversation('other');observe('other',[turn('history',1,[file('main.tex','foreign')])]);
assert.equal(received.length,5);assert.deepEqual(activations,['conv','other']);
observe('other',[turn('history',1,[file('main.tex','other-commit',{root:'obsolete-name',rootPath:'/owned/project'})])]);
assert.equal(received.at(-1).path,'/owned/project/main.tex');
const many=Array.from({length:20},(_,i)=>turn('many-'+i,i+4,Array.from({length:64},(_,j)=>file('image-'+j+'.png','receipt-'+i+'-'+j))));
observe('other',many);const after=received.length;
observe('other',many);assert.equal(received.length,after,'unchanged receipts do not replay');
observe('other',[turn('history',1,[file('refs.bib','older-running-edit')])]);
assert.equal(received.length,after+1,'a turn older than the most recent 16 is still observed');
assert.equal(received.at(-1).changeId,'older-running-edit');
observe('other',[turn('history',1,[file('refs.bib','older-running-edit')],'undoing','old-undo')]);
assert.equal(received.length,after+1);
observe('other',[turn('history',1,[file('refs.bib','older-running-edit')],'undone','old-undo')]);
assert.equal(received.at(-1).changeId,'old-undo');
observe('other',[turn('history',1,[file('refs.bib','older-running-edit')],'redoing','old-redo')]);
observe('other',[turn('history',1,[file('refs.bib','older-running-edit')],'applied','old-redo')]);
assert.equal(received.at(-1).changeId,'old-redo');
assert.equal(received.length,after+3);
owner.observe('other',store('other').getState());
assert.equal(received.length,after+3,'repainting is not a commit');
owner.activateConversation('empty');observe('empty',[]);
observe('empty',[turn('first',0,[file('main.tex','first-commit')])]);
assert.equal(received.at(-1).changeId,'first-commit','an initially empty conversation can build');
observe('empty',[turn('first',0,[file('main.tex','remote-commit',
  {root:'project',rootPath:'remote:device:project'})])]);
assert.equal(received.at(-1).path,'main.tex');
assert.equal(received.at(-1).projectRoot,'remote:device:project');
const largeBefore=received.length;
const completeFiles=Array.from({length:1101},(_,i)=>file('part-'+i+'.tex','wide-'+i));
observe('empty',[turn('first',0,completeFiles)]);
assert.equal(received.length-largeBefore,1101);
assert.equal(received.at(-1).changeId,'wide-1100');
console.log('ok');
"""
    result = subprocess.run(['node','-e',script,str(module),str(store_module)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node required')
def test_preview_discovery_uses_bounded_projection_hints_not_fake_receipts_or_prose():
    module = native_module_path('paper-preview-discovery.js', ROOT / 'frontend/src/features/paper/preview-changes.ts')
    store_module = native_module_path('paper-preview-discovery-store.js', ROOT / 'frontend/src/core/turn-state.ts')
    script = r"""
const assert=require('node:assert/strict');require(process.argv[1]);require(process.argv[2]);
const builds=[],discoveries=[];
const owner=createPaperPreviewChanges({changed:rows=>builds.push(rows),activated:()=>{},
  discover:(id,hint)=>discoveries.push({id,hint})});
const store=createTurnStore('c');let revision=0,lastUpdate;
store.subscribe((state,update)=>{lastUpdate=update;owner.observe('c',state,update)});
let turns=[];
const snapshot=()=>store.dispatch({type:'snapshot',snapshot:{turns:structuredClone(turns),conversationRevision:++revision}});
const turn=(id,ordinal,projection)=>({turnId:id,ordinal,projection});
const round=(id,name,args,extra={})=>({toolCallId:id,toolName:name,toolArgs:args,status:'done',...extra});
owner.activateConversation('c');
turns=[turn('old',0,{modifiedFileList:[{path:'old.tex'}]}),turn('t',1,{modifiedFileList:[
  {path:'main.tex',rootPath:'remote:device:project'},{path:'refs.bib'}]})];snapshot();
assert.equal(builds.length,0);assert.equal(discoveries.length,1);
assert.deepEqual(discoveries[0],{id:'c',hint:{path:'main.tex',projectRoot:'remote:device:project'}});
snapshot();owner.observe('c',store.getState(),lastUpdate);owner.observe('c',store.getState());
assert.equal(discoveries.length,1,'cold/repaint replay is not discovery');
turns[1].projection={fileChanges:{blockId:'f',state:'applied',files:[{path:'next.ltx',pending:true}]},
 content:'read_files main.tex; unzip paper.zip',toolRounds:[round('prose','other',{}, {toolContent:'main.tex unzip paper.zip'})]};snapshot();
assert.equal(discoveries.length,1);
turns[1].projection.fileChanges.files[0].pending=false;snapshot();
assert.equal(discoveries.length,2);assert.equal(builds.length,0,'legacy changes never fabricate writes');
turns[1].projection.toolRounds=[round('r','read_files',{reads:[{path:'refs.bib'},{path:'read.tex'}]},{status:'running'})];snapshot();
assert.equal(discoveries.length,2);
turns[1].projection.toolRounds[0].status='done';snapshot();
assert.equal(discoveries.at(-1).hint.path,'read.tex');assert.equal(discoveries.length,3);
snapshot();assert.equal(discoveries.length,3);
turns[1].projection.toolRounds.push(round('failed','read_files',{path:'failed.tex'},{status:'error'}),
 round('rejected','read_files',{path:'rejected.tex'},{rejection:{kind:'policy'}}),
 round('exit','run_command',{command:'unzip bad.zip'}, {results:[{exitCode:1}]}),
 round('list','run_command',{command:'tar -tf archive.tar'}),
 round('echo','run_command',{command:'echo "unzip archive.zip"'}),
 round('write','write_file',{path:'written.tex'}),
 round('oversize','read_files',{path:'a'.repeat(5000)+'.tex'}));snapshot();
assert.equal(discoveries.length,3,'failed, rejected, non-extract and oversized hints are ignored');
turns[1].projection.toolRounds.push(round('unzip','run_command',{command:'unzip paper.zip',working_dir:'/owned'}, {results:[{exitCode:0}]}),
 round('read2','read_files',JSON.stringify({path:'/owned/main.ltx'})));snapshot();
assert.equal(discoveries.length,4,'one discovery per observe');assert.equal(discoveries.at(-1).hint.path,'/owned/main.ltx');
turns[1].projection.toolRounds.push(round('tar','run_command',{command:'mkdir out && tar -xzf source.tgz -C out'}));snapshot();
assert.equal(discoveries.length,5);assert.equal(discoveries.at(-1).hint,undefined);
owner.observe('background',store.getState(),lastUpdate);assert.equal(discoveries.length,5);
turns.unshift(turn('backfill',-1,{modifiedFileList:[{path:'historical.tex'}]}));snapshot();assert.equal(discoveries.length,5);
turns[2].projection.fileChanges.files=[{path:'main.tex',changeId:'main'},{path:'refs.bib',changeId:'bib'},{path:'image.png',changeId:'image'}];snapshot();
assert.equal(builds.length,1);assert.equal(builds[0].at(-1).path,'main.tex');assert.equal(builds[0].length,3);
owner.activateConversation('other');owner.activateConversation('c');owner.observe('c',store.getState());
assert.equal(builds.length,1,'warm activation is also discovery-only');assert.equal(discoveries.length,6);
// Pending/error blocks do not qualify legacy fallback files.
for(const state of ['undoing','redoing','undone','error']) {
  turns.push(turn(state,turns.length+10,{fileChanges:{blockId:'f',state,files:[{path:state+'.tex'}]}}));snapshot();
}
assert.equal(discoveries.length,6);
owner.activateConversation('canonical');
owner.observe('canonical',{turnsById:{t:turn('t',0,{fileChanges:{blockId:'f',state:'applied',files:[{path:'cold.tex',changeId:'real'}]}})},conversationRevision:1});
assert.equal(builds.length,1);assert.equal(discoveries.at(-1).hint.path,'cold.tex');
console.log('ok');
"""
    result = subprocess.run(['node', '-e', script, str(module), str(store_module)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'
