const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const ts=require('typescript'),{JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
const copy=key=>locale['evolution.studio.'+key]||key;
function load(file,cache=new Map()){
 const full=path.resolve(source,file+'.ts');if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>name.endsWith('/i18n')?{t:key=>locale[key]||key}:load(path.relative(source,path.resolve(path.dirname(full),name)),cache),exports);return exports;
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function dom(){const d=new JSDOM('<button id="opener">Read original</button><div id="host"></div>',{pretendToBeVisual:true});Object.assign(global,{window:d.window,document:d.window.document});return d;}
function page(content,offset=0,extra={}){
 const characters=Array.from(content),end=Math.min(offset+6000,characters.length);
 return {task_id:'executor',stage:'executing',status:'done',terminal_verdict:'completed',content:characters.slice(offset,end).join(''),
  offset,total_chars:characters.length,next_offset:end<characters.length?end:null,archive_state:'sealed',
  source_sha256:crypto.createHash('sha256').update(content).digest('hex'),
  actuals:{tokens:30,tool_calls:2,wall_clock_seconds:3},actuals_complete:false,...extra};
}
const run=(extra={})=>({run_id:'run-a',objective:'Check the mechanism',status:'reviewing',task_id:'reviewer',attempt:1,
 actuals:{tokens:30,tool_calls:2,wall_clock_seconds:3},receipts:[{task_id:'executor',stage:'executing',terminal_verdict:'completed',
  outcome_available:true,restored_from_outcome:true,actuals:{tokens:30,tool_calls:2,wall_clock_seconds:3}}],result:'Summary',error:'',review:null,...extra});

test('receipt-bound source identity rejects a different first-page digest or length',async()=>{
 const d=dom(),host=document.getElementById('host');let response=page('Different original');
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async()=>({outcome:response})});
 const expected=page('Expected original');reader.select('run-a','executor',false,document.getElementById('opener'),{references:{deliverable:{task_id:'executor',source_sha256:expected.source_sha256,total_chars:expected.total_chars}}});await tick();
 assert.equal(host.querySelector('pre').textContent,'');assert.equal(host.querySelector('[data-source-action=retry]').hidden,false);
 response={...expected,total_chars:expected.total_chars+1,next_offset:expected.total_chars};host.querySelector('[data-source-action=retry]').click();await tick();assert.equal(host.querySelector('pre').textContent,'');
 response=expected;host.querySelector('[data-source-action=retry]').click();await tick();assert.equal(host.querySelector('pre').textContent,'Expected original');reader.dispose();d.window.close();
});

test('a direct tool opener pins its own immutable reference and rejects a changed source',async()=>{
 const d=dom(),host=document.getElementById('host'),expected=page('Original tool evidence',0,{source_kind:'tool_evidence',tool_evidence_available:true,tool_evidence_scope:'terminal_retained_tool_messages'});let response={...expected,source_sha256:'d'.repeat(64)};const calls=[];
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async(id,task,offset,signal,kind)=>{calls.push(kind);return {outcome:response};}});
 const reference={task_id:'executor',source_sha256:expected.source_sha256,total_chars:expected.total_chars,source_kind:'tool_evidence'};
 reader.select('run-a','executor',false,document.getElementById('opener'),{kind:'tool_evidence',references:{tool_evidence:reference}});reference.source_sha256='d'.repeat(64);await tick();assert.deepEqual(calls,['tool_evidence']);assert.equal(host.querySelector('pre').textContent,'');
 response=expected;host.querySelector('[data-source-action=retry]').click();await tick();assert.equal(host.querySelector('pre').textContent,'Original tool evidence');reader.dispose();d.window.close();
});

test('saved tool evidence is explicit, separately paged and rendered as inert original text',async()=>{
 const d=dom(),host=document.getElementById('host'),calls=[];
 const evidence=JSON.stringify({tool_call_id:'actual',content:'<script>inert</script>🧪'.repeat(900)})+'\n';
 const {mountOutcomeReader}=load('studio/outcome-reader');
 const reader=mountOutcomeReader(host,{copy,read:async(id,task,offset,signal,kind)=>{
  calls.push({id,task,offset,kind});return{outcome:page(kind==='tool_evidence'?evidence:'Original delivery',offset,
    {source_kind:kind||'deliverable',tool_evidence_available:true,tool_evidence_scope:'terminal_retained_tool_messages'})};}});
 reader.select('run-a','executor',true,document.getElementById('opener'));await tick();
 assert.equal(calls.length,1);assert.equal(host.querySelector('pre').textContent,'Original delivery');
 host.querySelector('[data-source-action="tool_evidence"]').click();await tick();
 assert.equal(calls[1].kind,'tool_evidence');assert.equal(calls[1].offset,0);
 assert.equal(host.querySelector('pre').textContent,Array.from(evidence).slice(0,6000).join(''));
 assert.equal(host.querySelectorAll('script').length,0);
 assert.ok(host.textContent.includes(copy('sourceToolEvidenceScope')));
 assert.ok(host.textContent.includes(copy('sourceRestored')));
 host.querySelector('[data-source-action="next"]').click();await tick();
 assert.equal(calls[2].kind,'tool_evidence');assert.equal(calls[2].offset,6000);
 host.querySelector('[data-source-action="deliverable"]').click();await tick();
 assert.equal(calls[3].kind,'deliverable');assert.equal(calls[3].offset,0);
 assert.equal(host.querySelector('pre').textContent,'Original delivery');
 reader.dispose();d.window.close();
});

test('cross-source responses cannot change saved task facts or replace a selected source kind',async()=>{
 const d=dom(),host=document.getElementById('host');let mismatch='kind';
 const {mountOutcomeReader}=load('studio/outcome-reader');
 const reader=mountOutcomeReader(host,{copy,read:async(id,task,offset,signal,kind)=>({outcome:page('Saved original',offset,
   {source_kind:kind==='tool_evidence'&&mismatch==='kind'?'deliverable':kind,
    tool_evidence_available:true,tool_evidence_scope:'terminal_retained_tool_messages',
    ...(kind==='tool_evidence'&&mismatch==='cost'?{actuals:{tokens:31,tool_calls:2,wall_clock_seconds:3}}:{})})})});
 reader.select('run-a','executor',false,document.getElementById('opener'));await tick();
 host.querySelector('[data-source-action="tool_evidence"]').click();await tick();
 assert.ok(host.textContent.includes(copy('sourceReadFailed')));assert.equal(host.querySelector('pre').textContent,'');
 mismatch='cost';host.querySelector('[data-source-action="retry"]').click();await tick();
 assert.ok(host.textContent.includes(copy('sourceReadFailed')));assert.equal(host.querySelector('pre').textContent,'');
 mismatch='valid';host.querySelector('[data-source-action="retry"]').click();await tick();
 assert.equal(host.querySelector('pre').textContent,'Saved original');
 reader.dispose();d.window.close();
});

test('original reads are explicit, preserve Unicode source, and retain just the current page',async()=>{
 const d=dom(),host=document.getElementById('host'),calls=[];
 const content=('<script>user text</script>🧪e\u0301\n').repeat(600);
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async(id,task,offset,signal)=>{calls.push({id,task,offset,signal});return{outcome:page(content,offset)};}});
 assert.equal(calls.length,0);
 reader.select('run-a','executor',true,document.getElementById('opener'));await tick();
 assert.deepEqual(calls.map(call=>call.offset),[0]);
 assert.equal(host.querySelector('pre').textContent,Array.from(content).slice(0,6000).join(''));
 assert.equal(host.querySelector('script'),null);assert.match(host.textContent,/Recovered from a saved outcome/);
 assert.match(host.textContent,/Recorded consumption of this delivery/);assert.match(host.textContent,/does not start a model or rerun the task/);
 assert.match(host.textContent,/≥ 30/);assert.match(host.textContent,/total consumption is incomplete/);
 assert.match(host.textContent,/acceptance depends on independent review/);
 const original=host.querySelector('pre');
 host.querySelector('[data-source-action=next]').click();await tick();
 assert.equal(host.querySelector('pre'),original);assert.equal(original.textContent,Array.from(content).slice(6000,12000).join(''));
 assert.equal(original.childNodes.length,1);assert.equal(host.querySelectorAll('pre').length,1);
 host.querySelector('[data-source-action=previous]').click();await tick();
 assert.deepEqual(calls.map(call=>call.offset),[0,6000,0]);
 host.querySelector('[data-source-action=close]').click();assert.equal(host.hidden,true);
 assert.equal(original.textContent,'');assert.equal(document.activeElement.id,'opener');
 reader.dispose();d.window.close();
});

test('a failed or changed source page retains the last verified page and retries the requested offset',async()=>{
 const d=dom(),host=document.getElementById('host'),content='x'.repeat(13000);let bad=false;const calls=[];
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async(id,task,offset)=>{
  calls.push(offset);return{outcome:page(content,offset,bad?{source_sha256:'f'.repeat(64)}:{})};
 }});
 reader.select('run-a','executor',false,document.getElementById('opener'));await tick();bad=true;
 host.querySelector('[data-source-action=next]').click();await tick();
 assert.equal(host.querySelector('pre').textContent,'x'.repeat(6000));assert.match(host.textContent,/could not be verified or read/);
 assert.equal(host.querySelector('[data-source-action=retry]').hidden,false);
 bad=false;host.querySelector('[data-source-action=retry]').click();await tick();
 assert.deepEqual(calls,[0,6000,6000]);assert.match(host.querySelector('.evo-outcome-range').textContent,/6001–12000/);
 reader.dispose();d.window.close();
});

test('late responses cannot repaint another task, a hidden view, or disposed DOM',async()=>{
 const d=dom(),host=document.getElementById('host'),pending=[];let visible=true;
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,visible:()=>visible,
  read:(id,task,offset,signal)=>new Promise(resolve=>pending.push({id,task,offset,signal,resolve}))});
 reader.select('run-a','executor',false,document.getElementById('opener'));
 reader.select('run-b','new-executor',false,document.getElementById('opener'));
 assert.equal(pending[0].signal.aborted,true);
 pending[1].resolve({outcome:page('Current source',0,{task_id:'new-executor'})});await tick();
 pending[0].resolve({outcome:page('Foreign stale source')});await tick();assert.equal(host.querySelector('pre').textContent,'Current source');
 reader.select('run-c','third-executor',false,document.getElementById('opener'));
 visible=false;reader.visibilityChanged();assert.equal(pending[2].signal.aborted,true);
 pending[2].resolve({outcome:page('Hidden source',0,{task_id:'third-executor'})});await tick();assert.equal(host.querySelector('pre').textContent,'');
 assert.match(host.textContent,/Reading paused/);visible=true;reader.visibilityChanged();assert.equal(pending.length,3);
 host.querySelector('[data-source-action=retry]').click();assert.equal(pending.length,4);
 reader.dispose();assert.equal(pending[3].signal.aborted,true);assert.equal(host.childNodes.length,0);
 pending[3].resolve({outcome:page('Disposed source',0,{task_id:'third-executor'})});await tick();assert.equal(host.childNodes.length,0);
 d.window.close();
});

test('wrong task identity or discontinuous offsets cannot replace the original',async()=>{
 for(const malformed of [{task_id:'foreign'},{offset:1},{next_offset:9000},{terminal_verdict:'unknown'}]){
  const d=dom(),host=document.getElementById('host');
  const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async()=>({outcome:page('Private foreign text',0,malformed)})});
  reader.select('run-a','executor',false,document.getElementById('opener'));await tick();
  assert.equal(host.querySelector('pre').textContent,'');assert.match(host.textContent,/could not be verified or read/);
  reader.dispose();d.window.close();
 }
});

test('empty archived text remains empty instead of adding placeholder text to the source',async()=>{
 const d=dom(),host=document.getElementById('host');
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async()=>({outcome:page('')})});
 reader.select('run-a','executor',false,document.getElementById('opener'));await tick();
 assert.equal(host.querySelector('pre').textContent,'');assert.match(host.textContent,/contains no text/);
 assert.equal(host.querySelector('[data-source-action=next]').disabled,true);
 reader.dispose();d.window.close();
});

test('inspector polls preserve the selected original, native disclosure, focus and execution record identity',async()=>{
 const d=dom(),host=document.getElementById('host'),calls=[];
 const inspector=load('studio/goal-run-inspector').mountGoalRunInspector(host,{copy,detailed:true,openTask(){},
  readOutcome:async(id,task,offset,signal)=>{calls.push({id,task,offset,signal});return{outcome:page('Original <b>hypothesis</b>')};}});
 inspector.update({run:run()});assert.equal(calls.length,0);
 const trigger=host.querySelector('[data-outcome-task-id=executor]');trigger.click();await tick();
 const original=host.querySelector('.evo-outcome-reader pre'),disclosure=host.querySelector('.evo-outcome-reader details');
 disclosure.open=true;original.focus();original.scrollTop=12;
 const selected=window.getSelection();selected.selectAllChildren(original);
 inspector.update({run:run({actuals:{tokens:40,tool_calls:3,wall_clock_seconds:4},updated_at:2})});
 assert.equal(calls.length,1);assert.equal(host.querySelector('[data-outcome-task-id=executor]'),trigger);
 assert.equal(host.querySelector('.evo-outcome-reader pre'),original);assert.equal(document.activeElement,original);
 assert.equal(original.scrollTop,12);assert.equal(disclosure.open,true);assert.match(selected.toString(),/Original <b>hypothesis<\/b>/);
 assert.match(host.querySelector('.evo-mission-records').textContent,/Delivery finished/);
 inspector.dispose();d.window.close();
});

test('long record histories keep a bounded DOM and remain pageable while a source stays open',async()=>{
 const d=dom(),host=document.getElementById('host'),opened=[];
 const receipts=Array.from({length:205},(_,i)=>({task_id:'task-'+i,stage:'executing',terminal_verdict:'completed',outcome_available:true,actuals:{tokens:1,tool_calls:0,wall_clock_seconds:1}}));
 const inspector=load('studio/goal-run-inspector').mountGoalRunInspector(host,{copy,detailed:true,openTask:id=>opened.push(id),
  readOutcome:async(id,task)=>({outcome:page('Paged history source',0,{task_id:task})})});
 inspector.update({run:run({task_id:'task-204',receipts})});
 assert.equal(host.querySelectorAll('.evo-mission-record').length,20);
 host.querySelector('[data-outcome-task-id="task-204"]').click();await tick();
 const original=host.querySelector('.evo-outcome-reader pre');
 host.querySelector('[data-record-action=older]').click();
 assert.equal(host.querySelectorAll('.evo-mission-record').length,20);assert.equal(original.textContent,'Paged history source');
 assert.equal(host.querySelector('[data-task-id="task-184"]').textContent.includes('Delivery finished'),true);
 for(let i=0;i<20&&!host.querySelector('[data-record-action=older]').disabled;i++)host.querySelector('[data-record-action=older]').click();
 assert.equal(host.querySelectorAll('.evo-mission-record').length,5);assert.ok(host.querySelector('[data-task-id="task-0"]'));
 assert.equal(host.querySelector('.evo-outcome-reader pre'),original);
 host.querySelector('[data-source-action=close]').click();
 assert.equal(document.activeElement,host.querySelector('.evo-run-records > h3'));
 inspector.dispose();d.window.close();
});

test('switching run scope aborts the pending original and legacy records remain usable',async()=>{
 const d=dom(),host=document.getElementById('host');let resolve,signal;const opened=[];
 const inspector=load('studio/goal-run-inspector').mountGoalRunInspector(host,{copy,detailed:true,openTask:id=>opened.push(id),
  readOutcome:(id,task,offset,abort)=>{signal=abort;return new Promise(done=>resolve=done);}});
 inspector.update({run:run()});host.querySelector('[data-outcome-task-id=executor]').click();
 inspector.update({run:run({run_id:'run-b',task_id:'legacy',receipts:[]})});assert.equal(signal.aborted,true);
 resolve({outcome:page('Stale old run')});await tick();assert.equal(host.querySelector('.evo-outcome-reader').hidden,true);
 assert.equal(host.querySelector('[data-outcome-task-id=legacy]').hidden,true);
 host.querySelector('[data-task-id=legacy][data-inspector-action=task]').click();assert.deepEqual(opened,['legacy']);
 inspector.dispose();assert.equal(host.childNodes.length,0);d.window.close();
});
test('invalid review output stays inspectable while its retry decision is visible',async()=>{
 const d=dom(),host=document.getElementById('host');
 const {mountGoalRunInspector}=load('studio/goal-run-inspector');
 const inspector=mountGoalRunInspector(host,{copy,openTask(){},detailed:true,readOutcome:async(id,task)=>({outcome:page('Preface then invalid JSON',0,{task_id:task,stage:'reviewing'})})});
 const receipt={task_id:'bad-review',stage:'reviewing',terminal_verdict:'completed',outcome_available:true,actuals:{tokens:30,tool_calls:0,wall_clock_seconds:3},
  review_format_retry:{error:'invalid_json',admitted:true,intent_id:'frozen',refusal:''}};
 inspector.update({run:run({receipts:[receipt]}),loading:false});
 const notice=host.querySelector('[data-review-format-retry="bad-review"]');
 assert.equal(notice.textContent,copy('reviewFormatRetryAdmitted'));assert.equal(notice.hidden,false);
 host.querySelector('[data-outcome-task-id="bad-review"]').click();await tick();
 assert.equal(host.querySelector('.evo-outcome-reader pre').textContent,'Preface then invalid JSON');
 inspector.update({run:run({status:'failed',task_id:'bad-review',receipts:[{...receipt,review_format_retry:{...receipt.review_format_retry,admitted:false,refusal:'retry_budget'}}]}),loading:false});
 assert.equal(notice.textContent,copy('reviewFormatRetryRefused'));
 assert.equal(host.querySelector('.evo-outcome-reader pre').textContent,'Preface then invalid JSON');
 inspector.dispose();d.window.close();
});


test('explicit evidence offsets reopen the same frozen source at the requested match and retry there',async()=>{
 const d=dom(),host=document.getElementById('host'),content='🧪观察'.repeat(7000),calls=[];let fail=false;
 const ref={task_id:'executor',source_sha256:page(content).source_sha256,total_chars:Array.from(content).length};
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async(id,task,offset)=>{calls.push(offset);if(fail)throw Error('Read unavailable');return {outcome:page(content,offset)};}});
 try{
  reader.select('run-a','executor',false,document.getElementById('opener'),{offset:12000,references:{deliverable:ref}});await tick();assert.equal(host.querySelector('pre').textContent,Array.from(content).slice(12000,18000).join(''));
  reader.select('run-a','executor',false,document.getElementById('opener'),{offset:6000,references:{deliverable:ref}});await tick();assert.deepEqual(calls,[12000,6000]);
  const original=host.querySelector('pre'),confirmed=original.textContent;
  fail=true;reader.select('run-a','executor',false,document.getElementById('opener'),{offset:18000,references:{deliverable:ref}});await tick();assert.equal(host.querySelector('pre'),original);assert.equal(original.textContent,confirmed);fail=false;host.querySelector('[data-source-action=retry]').click();await tick();assert.deepEqual(calls,[12000,6000,18000,18000]);
  for(const offset of [-1,1.5,Infinity,ref.total_chars+1])reader.select('run-a','executor',false,document.getElementById('opener'),{offset,references:{deliverable:ref}});
  assert.equal(calls.length,4);
 }finally{reader.dispose();d.window.close();}
});

test('declared artifacts page exact immutable generations and mark incomplete capture',async()=>{
 const d=dom(),host=document.getElementById('host');const artifact=JSON.stringify({relative_path:'result.json',content:'<img src=x onerror=alert(1)>🧪'.repeat(600)})+'\n';let complete=false;const calls=[];
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async(id,task,offset,signal,kind)=>{
  calls.push({kind,offset});return{outcome:page(kind==='artifact_evidence'?artifact:'Summary',offset,
   {source_kind:kind,artifact_evidence_available:true,artifact_evidence_scope:'declared_project_artifact_snapshots',artifact_evidence_complete:complete})};}});
 reader.select('run-a','executor',false,document.getElementById('opener'));await tick();
 const choice=host.querySelector('[data-source-action=artifact_evidence]');assert.equal(choice.hidden,false);choice.click();await tick();
 assert.deepEqual(calls[1],{kind:'artifact_evidence',offset:0});assert.equal(host.querySelector('pre').textContent,Array.from(artifact).slice(0,6000).join(''));assert.equal(host.querySelector('img'),null);assert.ok(host.textContent.includes(copy('sourceArtifactsIncomplete')));
 complete=true;host.querySelector('[data-source-action=next]').click();await tick();assert.equal(host.querySelector('[data-source-action=retry]').hidden,false);assert.equal(host.querySelector('pre').textContent,Array.from(artifact).slice(0,6000).join(''));
 reader.dispose();d.window.close();
});

test('artifact openers pin completeness and scope as well as generation identity',async()=>{
 const d=dom(),host=document.getElementById('host');const expected=page('Declared body',0,{source_kind:'artifact_evidence',artifact_evidence_available:true,artifact_evidence_scope:'declared_project_artifact_snapshots',artifact_evidence_complete:false});let response={...expected,artifact_evidence_complete:true};
 const reference={task_id:'executor',source_kind:'artifact_evidence',source_sha256:expected.source_sha256,total_chars:expected.total_chars,complete:false};
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async()=>({outcome:response})});reader.select('run-a','executor',false,document.getElementById('opener'),{kind:'artifact_evidence',references:{artifact_evidence:reference}});await tick();assert.equal(host.querySelector('pre').textContent,'');
 response={...expected,artifact_evidence_scope:'live_files'};host.querySelector('[data-source-action=retry]').click();await tick();assert.equal(host.querySelector('pre').textContent,'');
 response=expected;host.querySelector('[data-source-action=retry]').click();await tick();assert.equal(host.querySelector('pre').textContent,'Declared body');reader.dispose();d.window.close();
});

test('background completion originals are readable while unknown scopes fail closed',async()=>{
 const d=dom(),host=document.getElementById('host');
 let response=page('Actual background measurement 🧪',0,{source_kind:'tool_evidence',tool_evidence_available:true,tool_evidence_scope:'unknown_future_scope'});
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async()=>({outcome:response})});
 reader.select('run-a','executor',false,document.getElementById('opener'),{kind:'tool_evidence'});await tick();
 assert.equal(host.querySelector('pre').textContent,'');
 assert.equal(host.querySelector('[data-source-action=retry]').hidden,false);
 response={...response,tool_evidence_scope:'terminal_retained_tool_messages_and_background_completions'};
 host.querySelector('[data-source-action=retry]').click();await tick();
 assert.equal(host.querySelector('pre').textContent,'Actual background measurement 🧪');
 reader.dispose();d.window.close();
});

test('late background originals use a distinct immutable source kind',async()=>{
 const d=dom(),host=document.getElementById('host'),calls=[];
 const expected=page('Completed background original 🧪',0,{source_kind:'background_evidence',background_evidence_available:true,background_evidence_scope:'owned_background_command_completions'});
 const reader=load('studio/outcome-reader').mountOutcomeReader(host,{copy,read:async(id,task,offset,signal,kind)=>{calls.push(kind);return {outcome:expected};}});
 reader.select('run-a','executor',false,document.getElementById('opener'),{kind:'background_evidence',references:{background_evidence:{task_id:'executor',source_kind:'background_evidence',source_sha256:expected.source_sha256,total_chars:expected.total_chars}}});await tick();
 assert.deepEqual(calls,['background_evidence']);assert.equal(host.querySelector('pre').textContent,'Completed background original 🧪');
 assert.equal(host.querySelector('[data-source-action=background_evidence]').hidden,false);
 reader.dispose();d.window.close();
});
