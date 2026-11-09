const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
function load(file){
 const js=ts.transpileModule(fs.readFileSync(path.resolve(__dirname,'..',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const exports={};new Function('require','exports',js)(()=>({}),exports);return exports;
}
const {projectStudio,studioEvents,statusKey,actorStatusKey,failureCode}=load('studio/projection.ts');
const run=(over={})=>({run_id:'r',objective:'Goal',status:'executing',task_id:'t-exec',attempt:1,created_at:1,updated_at:1,
 actuals:{tokens:100,tool_calls:2,wall_clock_seconds:9},receipts:[],result:'',review:null,error:'',...over});
const workspace={materials:[{material_id:'m1'}],stages:[{kind:'a',detail:'b'}],criteria:[{criterion_id:'c',requirement:'r'}],budget:{max_tokens:1000,max_tool_calls:0,max_wall_clock_seconds:null}};

test('no goal invites a new one; a pending selection is loading, not empty',()=>{
 const none=projectStudio({});
 assert.equal(none.phase,'none');assert.equal(none.primary,'create');assert.equal(none.meters,undefined);
 assert.deepEqual(Object.values(none.stages),['pending','pending','pending','pending']);
 const loading=projectStudio({loading:true});assert.equal(loading.phase,'loading');assert.equal(loading.primary,'');
});

test('a compute-parked executor is a visible wait, and its placeholder result is not a deliverable',()=>{
 const parked=run({compute_wait:{operation_id:'job-9ec7',task_id:'t-exec',parked:true,next_poll_at:5},
  result:'Compute dependency pending; no completion or scientific acceptance.',
  receipts:[{task_id:'t-exec',stage:'executing',terminal_verdict:'completed',actuals:{tokens:100,tool_calls:2,wall_clock_seconds:9}}]});
 const view=projectStudio({run:parked,workspace});
 assert.equal(view.phase,'executing');
 assert.deepEqual(view.wait,{kind:'compute',operation:'job-9ec7',error:'',origin:'',since:null,routed:false});
 assert.equal(view.stages.execute,'waiting');assert.equal(view.stages.plan,'done');assert.equal(view.stages.review,'pending');
 assert.deepEqual(view.actors.executor,{role:'executor',mode:'waiting',emote:'compute',taskId:'t-exec'});
 assert.equal(view.deliverable,false);assert.equal(view.primary,'');
 // Nothing is running: the recorded consumption is exact, not a lower bound.
 assert.equal(view.meters.tokens.lowerBound,false);assert.equal(view.meters.tokens.limit,1000);
 assert.equal(view.meters.calls.limit,null);assert.equal(statusKey(view),'waitingCompute');
 assert.equal(actorStatusKey(view,'executor'),'waitingCompute');
 const stalled=projectStudio({run:{...parked,compute_wait:{...parked.compute_wait,last_error:'ModuleNotFoundError'}},workspace});
 assert.equal(stalled.actors.executor.emote,'alert');assert.equal(statusKey(stalled),'computeStalled');
 assert.equal(stalled.primary,'stall','a failing status read is the one thing to show, not a silent label');
 assert.deepEqual(studioEvents(view,stalled),[{kind:'computeError'}]);
 assert.deepEqual(studioEvents(stalled,stalled),[]);
 const error="ModuleNotFoundError: No module named 'tofu_environments.capacity'";
 const routed=projectStudio({run:{...parked,compute_wait:{...parked.compute_wait,last_error:error,
  last_error_origin:'tofu_yourprovider_environments/hope_catalog.py:3 in <module>',failed_since:1700000000,watchdog_signal_id:'sig-1'}},workspace});
 assert.deepEqual(routed.wait,{kind:'compute',operation:'job-9ec7',error,
  origin:'tofu_yourprovider_environments/hope_catalog.py:3 in <module>',since:1700000000,routed:true});
 const asked=projectStudio({run:{...parked,compute_wait:{...parked.compute_wait,last_error:error}},workspace,questions:1});
 assert.equal(asked.primary,'answer','a pending human question still comes first');
});

test('a running stage reports its usage as a lower bound and works at its desk',()=>{
 const view=projectStudio({run:run(),workspace});
 assert.equal(view.actors.executor.mode,'working');assert.equal(view.actors.executor.emote,'work');
 assert.equal(view.stages.execute,'active');assert.equal(view.meters.tokens.lowerBound,true);
 assert.equal(statusKey(view),'state_executing');assert.equal(view.counts.records,1);
 assert.deepEqual(view.counts,{materials:1,steps:1,criteria:1,records:1});
 const background=projectStudio({run:run({background_wait:true}),workspace});
 assert.equal(background.actors.executor.emote,'hourglass');assert.equal(statusKey(background),'waitingBackground');
 assert.equal(background.meters.tokens.lowerBound,false);
});

test('a human question pauses the active role and becomes the primary action',()=>{
 const view=projectStudio({run:run({status:'reviewing',task_id:'t-rev',receipts:[{task_id:'t-exec',stage:'executing',terminal_verdict:'completed',actuals:{}}]}),questions:2});
 assert.deepEqual(view.wait,{kind:'human',count:2});assert.equal(view.primary,'answer');
 assert.deepEqual(view.actors.reviewer,{role:'reviewer',mode:'waiting',emote:'ask',taskId:'t-rev'});
 assert.equal(view.actors.executor.mode,'done');assert.equal(view.actors.executor.taskId,'t-exec');
 assert.equal(view.stages.review,'waiting');assert.equal(view.stages.execute,'done');
 assert.equal(statusKey(view),'askHuman');assert.equal(actorStatusKey(view,'reviewer'),'askHuman');
});

test('completion closes every stage with the independent verdict; a deliverable becomes readable',()=>{
 const done=projectStudio({run:run({status:'completed',result:'Report',review:{outcome:'success',criterion_results:[]},
  receipts:[{task_id:'t-exec',stage:'executing',terminal_verdict:'completed',actuals:{}},{task_id:'t-rev',stage:'reviewing',actuals:{}}]})});
 assert.equal(done.terminal,true);assert.equal(done.verdict,'pass');assert.equal(done.primary,'read');
 assert.deepEqual(Object.values(done.stages),['done','done','done','done']);assert.equal(done.wait,undefined);
 assert.equal(statusKey(done),'state_completed');
 const partial=projectStudio({run:run({status:'completed',result:'Report',review:{outcome:'partial',criterion_results:[]}})});
 assert.equal(statusKey(partial),'verdict_partial');
});

test('failures and stops mark exactly where the pipeline ended',()=>{
 const limit=projectStudio({run:run({status:'failed',error:'budget_exceeded: max_tokens',
  receipts:[{task_id:'t-exec',stage:'executing',terminal_verdict:'failed',actuals:{}}]})});
 assert.equal(limit.failure,'limit');assert.equal(limit.primary,'retry');
 assert.deepEqual(limit.stages,{plan:'done',execute:'failed',review:'pending',deliver:'pending'});
 assert.deepEqual(limit.actors.executor,{role:'executor',mode:'failed',emote:'fail',taskId:'t-exec'});
 const stopped=projectStudio({run:run({status:'cancelled',receipts:[{task_id:'t-exec',stage:'executing',terminal_verdict:'completed',actuals:{}}]})});
 assert.deepEqual(stopped.stages,{plan:'done',execute:'done',review:'stopped',deliver:'pending'});
 assert.equal(stopped.failure,'stopped');
 const attention=projectStudio({run:run({status:'needs_attention',error:'stage_handoff_interrupted'})});
 assert.equal(attention.failure,'interrupted');assert.equal(attention.stages.execute,'failed');
 assert.equal(failureCode('weird_provider_code: x'),'unknown');assert.equal(failureCode(''),'');
});

test('coordinator and swarm state come only from their own snapshots',()=>{
 const coordination=status=>({coordinator:{status,task_id:'c-task'},updates:[]});
 assert.equal(projectStudio({run:run(),coordination:coordination('running')}).actors.coordinator.emote,'think');
 const blocked=projectStudio({run:run(),coordination:coordination('needs_attention')});
 assert.equal(blocked.coordinator,'attention');assert.equal(actorStatusKey(blocked,'coordinator'),'coordinatorBlocked');
 const crowd=projectStudio({run:run(),agents:[{id:'a',status:'running'},{id:'b',status:'done'},{id:'c',status:'pending'}],agentsKnown:true});
 assert.deepEqual(crowd.agents,{known:true,live:2});
 assert.equal(projectStudio({run:run(),agentsKnown:false}).agents.known,false);
});

test('events are transitions within one goal, never page loads or goal switches',()=>{
 const executing=projectStudio({run:run(),workspace});
 const reviewing=projectStudio({run:run({status:'reviewing',task_id:'t-rev'}),workspace:{...workspace,materials:[{material_id:'m1'},{material_id:'m2'},{material_id:'m3'}]},questions:1});
 assert.deepEqual(studioEvents(undefined,executing),[]);
 assert.deepEqual(studioEvents(executing,reviewing),[{kind:'phase',phase:'reviewing'},{kind:'question',count:1},{kind:'materials',added:2}]);
 assert.deepEqual(studioEvents(executing,projectStudio({run:run({run_id:'other',status:'completed'})})),[]);
 const judged=projectStudio({run:run({status:'completed',review:{outcome:'failure',criterion_results:[]}})});
 assert.deepEqual(studioEvents(executing,judged),[{kind:'phase',phase:'completed'},{kind:'verdict',verdict:'fail'}]);
 const waiting=projectStudio({run:run({compute_wait:{operation_id:'j',task_id:'t-exec',parked:false,next_poll_at:0}})});
 assert.deepEqual(studioEvents(executing,waiting),[{kind:'wait',wait:'compute'}]);
});
