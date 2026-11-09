const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript'),{JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
const copy=key=>locale['evolution.studio.'+key]||key;
function load(file,cache=new Map()){
 let full=path.resolve(source,file+'.ts');if(!fs.existsSync(full))full=path.join(full.slice(0,-3),'index.ts');if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>name.endsWith('/i18n')?{t:key=>locale[key]||key}:load(path.relative(source,path.resolve(path.dirname(full),name)),cache),exports);return exports;
}
const observation=load('goal-workbench');
const run=(extra={})=>({run_id:'a',objective:'Ship <safe> report',status:'executing',task_id:'task-a',attempt:1,actuals:{tokens:0,tool_calls:0,wall_clock_seconds:0},receipts:[],result:'',error:'',review:null,...extra});
const workspace={materials:[],stages:[{kind:'Investigate',detail:'Reproduce before making changes'}],criteria:[{criterion_id:'test',requirement:'Regression suite passes'}]};
test('unsubmitted void receipts never expose a completed verdict or an original-output action',()=>{
 const records=observation.taskRecords(run({status:'completed',task_id:'replacement',receipts:[{
  task_id:'old-carrier',stage:'executing',submission_voided:true,status:'done',
  terminal_verdict:'completed',outcome_available:true,restored_from_outcome:true,
  actuals:{tokens:0,tool_calls:0,wall_clock_seconds:12}
 }]}),copy);
 const old=records.find(item=>item.taskId==='old-carrier');
 assert.equal(old.status,'Voided before submission');
 assert.equal(old.outcomeAvailable,false);assert.equal(old.restoredFromOutcome,false);
 assert.equal(observation.failureReason('pending_stage_restoration_refused: changed checkpoint',copy),
  'This stage could not be safely restored. Inspect its execution records.');
});
test('missing, null and zero limits are unlimited telemetry, never fabricated progress',()=>{
 for(const budget of [undefined,null,{}, {max_tokens:0},{max_tokens:null}]){
  assert.equal(observation.runTelemetry(run({budget}),undefined,copy).tokenLimit,'No limit');
 }
 assert.equal(observation.runTelemetry(run(),{...workspace,budget:{max_tokens:200}},copy).tokenLimit,'200');
 assert.equal(observation.runTelemetry(run({actuals:{tokens:NaN}}),undefined,copy).tokens,'0');
});
test('a waiting run labels its current task by the wait, never by the parked turn archived verdict',()=>{
 const receipts=[{task_id:'task-a',stage:'executing',status:'done',terminal_verdict:'completed'},{task_id:'old',stage:'executing',terminal_verdict:'completed'}];
 const status=(value,id='task-a')=>observation.taskRecords(value,copy).find(record=>record.taskId===id).status;
 const wait={operation_id:'job-7',task_id:'task-a',parked:true,next_poll_at:0};
 assert.equal(status(run({compute_wait:wait,receipts})),copy('waitingCompute'));
 assert.equal(status(run({compute_wait:{...wait,last_error:'ModuleNotFoundError'},receipts})),copy('computeStalled'));
 assert.equal(status(run({compute_wait:wait,receipts}),'old'),copy('sourceCompletedShort'),'earlier turns keep their own verdict');
 assert.equal(status(run({background_wait:true,receipts})),copy('waitingBackground'));
 assert.equal(status(run({receipts})),copy('sourceCompletedShort'));
 assert.equal(observation.computeStallFacts(run({compute_wait:wait}),copy).length,0,'a readable wait has no stall facts');
});
test('terminal task labels use receipt status or terminal run fallback, never execution stage',()=>{
 const completed=run({status:'completed',receipts:[{task_id:'task-a',stage:'executing',status:'completed'}]});
 assert.equal(observation.taskRecords(completed,copy)[0].status,'Completed');
 assert.equal(observation.taskRecords(run({status:'failed',receipts:[{task_id:'task-a',stage:'executing',status:'running'}]}),copy)[0].status,'Failed');
 assert.equal(observation.taskRecords(run({status:'completed',task_id:'',receipts:[{task_id:'old',stage:'executing',status:'failed'}]}),copy)[0].status,'Failed');
});
test('legacy and unknown failures are localized while technical enums are not primary copy',()=>{
 assert.match(observation.failureReason('budget_exceeded: max_tokens',copy),/configured execution limit/);
 assert.match(observation.failureReason('execution_token_usage_unknown',copy),/usage receipt/);
 assert.doesNotMatch(observation.failureReason('execution_token_usage_unknown',copy),/configured execution limit/);
 assert.match(observation.failureReason('stage_handoff_interrupted',copy),/hand-off/);
 assert.doesNotMatch(observation.failureReason('secret_provider_enum',copy),/secret_provider_enum/);
});
test('stage admission waiting and refused recovery expose different next actions',()=>{
 assert.match(observation.failureReason('stage_admission_deferred: RuntimeError',copy),/will retry/);
 assert.match(observation.failureReason('stage_admission_deferred: RuntimeError',copy),/do not need to submit/);
 assert.match(observation.failureReason('stage_launch_recovery_refused: missing archive',copy),/could not be verified/);
 assert.match(observation.failureReason('stage_launch_recovery_refused: missing archive',copy),/Recorded work is preserved/);
});
test('inspector sections render only their own facts, escape content and never treat a compute park as a deliverable',()=>{
 const d=new JSDOM('<div id="work"></div><div id="review"></div><div id="delivery"></div>');Object.assign(global,{window:d.window,document:d.window.document});
 const opened=[];const {mountGoalRunInspector}=load('studio/goal-run-inspector');
 const host=document.getElementById('work');
 const work=mountGoalRunInspector(host,{copy,openTask:id=>opened.push(id),sections:['status','latest','telemetry','records']});
 const state={run:run(),workspace,coordination:{updates:[{seq:1,origin:'agent',content:'Verified <script>not markup</script>'},{seq:2,origin:'user',content:'Unacknowledged user claim'}]}};
 work.update(state);
 assert.match(host.textContent,/Verified <script>/);assert.equal(host.querySelector('script'),null);
 assert.doesNotMatch(host.textContent,/Unacknowledged user claim/);
 assert.doesNotMatch(host.textContent,/Reproduce before making changes/,'plan steps belong to the plan tab');
 assert.equal(host.querySelector('[role=progressbar], [role=meter]'),null);
 const task=host.querySelector('[data-task-id="task-a"][data-inspector-action=task]');assert.ok(task.classList.contains('ui-btn'));task.click();assert.deepEqual(opened,['task-a']);
 task.focus();work.update({...state,run:run({actuals:{tokens:5,tool_calls:1,wall_clock_seconds:1}})});assert.equal(document.activeElement,task);
 work.update({...state,run:run({status:'failed',error:'budget_exceeded',receipts:[{task_id:'task-a',stage:'executing',status:'failed'}]})});
 assert.match(host.querySelector('.evo-diagnosis p').textContent,/configured execution limit/);
 assert.match(host.querySelector('details.evo-technical').textContent,/budget_exceeded/);
 assert.match(host.querySelector('[data-task-id]').textContent,/Failed/);
 const parked=run({compute_wait:{operation_id:'job-7',task_id:'task-a',parked:true,next_poll_at:0,last_error:"ModuleNotFoundError: No module named 'tofu_mtsso_auth'",
  last_error_origin:'tofu_meituan_environments/__init__.py:23 in __call__',failed_since:1700000000},
  result:'Compute dependency pending; no completion or scientific acceptance.',receipts:[{task_id:'task-a',stage:'executing',terminal_verdict:'completed'}]});
 work.update({...state,run:parked});
 assert.match(host.textContent,/job-7/);assert.match(host.textContent,/ModuleNotFoundError/);assert.match(host.textContent,new RegExp(copy('computeStalled')));
 const status=host.querySelector('[data-inspection-section=status]');
 assert.equal(status.querySelector('[data-fact=at] dd').textContent,`${copy('stallReadingStatus')} · job-7`);
 assert.equal(status.querySelector('[data-fact=origin] dd').textContent,'tofu_meituan_environments/__init__.py:23 in __call__');
 assert.ok(status.querySelector('[data-fact=since] dd').textContent,'the status section says since when the read fails');
 const parkedRow=host.querySelector('[data-task-id="task-a"][data-inspector-action=task]').textContent;
 assert.match(parkedRow,new RegExp(copy('computeStalled')));
 assert.doesNotMatch(parkedRow,new RegExp(copy('sourceCompletedShort')),'a parked turn is not a finished delivery');
 const deliveryHost=document.getElementById('delivery');
 const delivery=mountGoalRunInspector(deliveryHost,{copy,openTask(){},sections:['deliverable']});
 delivery.update({run:parked});assert.equal(deliveryHost.querySelector('.evo-deliverable'),null);
 delivery.update({run:run({status:'completed',result:'Delivered <b>report</b>'})});
 assert.equal(deliveryHost.querySelector('.evo-deliverable').textContent,'Delivered <b>report</b>');assert.equal(deliveryHost.querySelector('b'),null);
 const reviewHost=document.getElementById('review');
 const review=mountGoalRunInspector(reviewHost,{copy,openTask(){},sections:['review']});
 review.update({...state,run:run({status:'completed',review:{outcome:'success',criterion_results:[{criterion_id:'test',outcome:'pass',evidence_ref:'12 tests passed'}]}})});
 assert.match(reviewHost.textContent,/Regression suite passes/);assert.match(reviewHost.textContent,/12 tests passed/);
 assert.equal(reviewHost.querySelector('.evo-diagnosis'),null);
 work.dispose();delivery.dispose();review.dispose();d.window.close();
});

test('interrupted consumption is shown as a lower bound and recovery refusal has an explicit next action',()=>{
 const measured=observation.runTelemetry(run({actuals_complete:false,actuals:{tokens:30,tool_calls:1,wall_clock_seconds:2}}),undefined,copy);
 assert.equal(measured.tokens,'≥ 30');assert.equal(measured.calls,'≥ 1');assert.equal(measured.seconds,'≥ 2');
 assert.match(observation.failureReason('stage_loss_recovery_refused: unobserved_token_consumption',copy),/Work and evidence remain saved/);
 assert.match(observation.failureReason('stage_loss_recovery_refused: unobserved_token_consumption',copy),/executor has not been replayed/);
});
test('format failure remains visible alongside completed provider task status',()=>{
 const receipt={task_id:'old-review',stage:'reviewing',terminal_verdict:'completed',outcome_available:true,
  review_format_retry:{error:'invalid_json',admitted:true,intent_id:'frozen',refusal:''}};
 const records=observation.taskRecords(run({task_id:'new-review',status:'reviewing',receipts:[receipt]}),copy);
 assert.equal(records[1].reviewFormatNote,copy('reviewFormatRetryAdmitted'));
 assert.equal(records[1].outcomeAvailable,true);assert.equal(records[0].reviewFormatNote,'');
 const denied=observation.taskRecords(run({task_id:'',receipts:[{...receipt,review_format_retry:{...receipt.review_format_retry,admitted:false,refusal:'retry_budget'}}]}),copy);
 assert.equal(denied[0].reviewFormatNote,copy('reviewFormatRetryRefused'));
});

test('material review objections are escaped and remain visible on unchanged polling',()=>{
 const d=new JSDOM('<div id="details"></div>');Object.assign(global,{window:d.window,document:d.window.document});
 const host=document.getElementById('details');
 const inspector=load('studio/goal-run-inspector').mountGoalRunInspector(host,{copy,openTask(){},sections:['review']});
 const evidence_audit={complete:false,objections:[{criterion_id:'evidence',classification:'contradicted',claim:'Four declarations, not <script>zero</script>.',evidence_ref:'Frozen audit: 4 <img src=x onerror=alert(1)> declarations.'},{criterion_id:'objective',classification:'unverified',claim:'The baseline is competent.',evidence_ref:'No qualification experiment was supplied.'}]};
 const state={loading:false,run:run({status:'needs_attention',learning:{complete:false,steps:{refetch:{result:{deferred:true}}}},review:{outcome:'failure',criterion_results:[],evidence_audit}}),workspace,coordination:{updates:[]}};
 inspector.update(state);assert.match(host.textContent,/Source contradicts this claim/);assert.match(host.textContent,/Evidence remains unverified/);assert.match(host.textContent,/Evidence audit incomplete/);assert.match(host.textContent,/no new sources have been gathered/);assert.match(host.textContent,/Four declarations, not <script>zero<\/script>/);assert.equal(host.querySelector('script, img'),null);
 const objections=Array.from(host.querySelectorAll('h4')).find(node=>node.textContent==='Review objections');assert.ok(objections);inspector.update(state);assert.equal(Array.from(host.querySelectorAll('h4')).find(node=>node.textContent==='Review objections'),objections);
 inspector.update({...state,run:run({run_id:'other',review:null})});assert.doesNotMatch(host.textContent,/Four declarations/);inspector.dispose();assert.equal(host.children.length,0);d.window.close();
});

test('original inspection retries are distinguished from unreadable JSON',()=>{
 for(const admitted of [true,false]){
  const value=run({task_id:'',receipts:[{task_id:'review',stage:'reviewing',terminal_verdict:'completed',
   review_format_retry:{error:'originals_uninspected',admitted,intent_id:'same-executor',refusal:admitted?'':'retry_budget'}}]});
  const record=observation.taskRecords(value,copy)[0];
  assert.equal(record.reviewFormatNote,copy(admitted?'reviewOriginalsRetryAdmitted':'reviewOriginalsRetryRefused'));
  assert.doesNotMatch(record.reviewFormatNote,/could not be read/);
 }
});
