/** Product boundaries: durable writes, route races, stable drafts and evidence paging. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const crypto=require('node:crypto');
const {JSDOM}=require('jsdom');
const base=path.resolve(__dirname,'../../..');
const locale=JSON.parse(fs.readFileSync(path.join(base,'i18n/locales/en.json'),'utf8'));
function load(file,cache=new Map()){
 const full=path.resolve(base,file);if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>{
  if(name.endsWith('.css'))return {};
  if(name.endsWith('/i18n'))return {t:key=>locale[key]||key};
  if(name.endsWith('/ui')||name.endsWith('/ui/index'))return {...load('ui/button.ts',cache),...load('ui/field.ts',cache)};
  return load(path.relative(base,path.resolve(path.dirname(full),name+'.ts')),cache);
 },exports);return exports;
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const step={kind:'experiment',title:'Compare alternatives',instruction:'Measure with the actual protocol.',checks:['Reproduce the measurements.']};
const final={...step,kind:'final',title:'Audit original requirements'};
const program=(id='a',extra={})=>({schema_version:1,program_id:id,objective:'Original objective '+id,
 acceptance:['Restart keeps all evidence.'],status:'active',queue:[step,final],current:null,
 settled_through:0,actuals:{tokens:7,tool_calls:1,wall_clock_seconds:2},error:'',created_at:1,updated_at:1,...extra});
const receipt=sequence=>({schema_version:1,program_id:'a',sequence,step:sequence===25?final:step,run_id:'run-'+sequence,
 outcome:'completed',result:sequence===25?'Final delivered artifact':'Evidence '+sequence,review:null,
 actuals:{tokens:1,tool_calls:1,wall_clock_seconds:1},error:'',created_at:sequence});
const run={run_id:'r',status:'executing',result:'',task_id:'t'};
function api(overrides={}){
 return {listPrograms:async()=>({programs:[program()]}),listRunModels:async()=>({models:[]}),
 getProgramMemories:async()=>({memory:{entries:[],retained_limit:32}}),
 getProgram:async id=>({program:program(id)}),getProgramExperiments:async()=>({experiments:[],settled_through:0,next_cursor:null}),
 getRun:async()=>({run,pending_questions:[]}),...overrides};
}
function setup(overrides={},ports={}){
 const dom=new JSDOM('<div id="host"></div>',{url:'http://localhost/#/evolution',pretendToBeVisual:true});
 Object.assign(global,{window:dom.window,document:dom.window.document,Option:dom.window.Option,
 Element:dom.window.Element,HTMLTextAreaElement:dom.window.HTMLTextAreaElement,HTMLSelectElement:dom.window.HTMLSelectElement});
 const root=document.getElementById('host'),navigations=[];
 const mounted=load('features/evolution/programs/page.ts').mountProgramWorkbench(root,
 {api:api(overrides),path:'evolution',navigate:p=>navigations.push(p),openTask:()=>{},...ports});
 return {dom,root,mounted,navigations,close(){mounted.dispose();dom.window.close();}};
}
const submit=(env,selector)=>env.root.querySelector(selector).dispatchEvent(new env.dom.window.Event('submit',{bubbles:true,cancelable:true}));
const draftStorage=()=>{const values=new Map();return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};};
const edit=(env,name,value)=>{const field=env.root.querySelector(`[name="${name}"]`);field.value=value;field.dispatchEvent(new env.dom.window.Event('input',{bubbles:true}));};
const executorModel={creator_id:'fixture',model_id:'executor'},reviewerModel={creator_id:'fixture',model_id:'reviewer'};
const policyModels=[{model:executorModel,label:'Controlled executor'},{model:reviewerModel,label:'Controlled independent reviewer'}];

test('review allocation is frozen with the original creation and restored unsent draft',async t=>{
 const calls=[],storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};
 const overrides={createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');}};
 const env=setup(overrides,ports);await tick();edit(env,'objective','Complete the evidence and full review');
 edit(env,'program-token-limit','240000');edit(env,'program-tool-limit','40');edit(env,'program-hours','1');
 edit(env,'program-review-tokens','80000');edit(env,'program-review-tools','12');edit(env,'program-review-seconds','600');
 submit(env,'.exp-create-form');await tick();assert.deepEqual(calls[0].body.review_reserve,{tokens:80000,tool_calls:12,wall_clock_seconds:600});
 env.close();const restored=setup(overrides,ports);t.after(()=>restored.close());await tick();
 assert.equal(restored.root.querySelector('[name=program-review-tokens]').value,'80000');
 submit(restored,'.exp-create-form');await tick();assert.deepEqual(calls[1],calls[0]);
});

test('review reserve requires a larger finite corresponding cap before any creation',async t=>{
 let calls=0;const env=setup({createProgram:async()=>{calls++;throw Error('No creation');}});t.after(()=>env.close());await tick();
 edit(env,'objective','Preserve original authorization');edit(env,'program-review-tokens','1');submit(env,'.exp-create-form');await tick();assert.equal(calls,0);
 edit(env,'program-token-limit','1');submit(env,'.exp-create-form');await tick();assert.equal(calls,0);
 edit(env,'program-token-limit','2');submit(env,'.exp-create-form');await tick();assert.equal(calls,1);
});

test('pre-reserve eleven-field draft retains objective roles policy and ambiguous key',async t=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};
 const body={objective:'Original complete research objective',auto_replan:true,budget:{max_tokens:240000,max_tool_calls:40,max_wall_clock_seconds:3600},role_models:{executor:executorModel,reviewer:reviewerModel}};
 load('features/evolution/programs/drafts.ts').createProgramDraftStore({owner:()=>7,scope:'/app/',storage:()=>storage}).save(['create'],
  {fields:[body.objective,'','','','auto','240000','40','1','',JSON.stringify(executorModel),JSON.stringify(reviewerModel)],attempt:{signature:JSON.stringify(body),key:'original-pre-reserve-key'}});
 const calls=[],env=setup({listRunModels:async()=>({models:policyModels}),createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');}},ports);t.after(()=>env.close());await tick();
 assert.equal(env.root.querySelector('[name=objective]').value,body.objective);assert.equal(env.root.querySelector('[name=program-review-tokens]').value,'');
 submit(env,'.exp-create-form');await tick();assert.deepEqual(calls,[{body,key:'original-pre-reserve-key'}]);
});

test('saved review reserve presentation states scope and does not promise acceptance',()=>{
 const owner=load('features/evolution/programs/execution-options.ts');const text=owner.executionPolicyText(program('a',{execution_options:{budget:{max_tokens:240000},review_reserve:{tokens:80000,tool_calls:0,wall_clock_seconds:0}}}),[],key=>locale['evolution.program.'+key]||key);
 assert.ok(text.includes('Review token reserve per experiment: 80,000'));assert.ok(text.includes(locale['evolution.program.reviewReserveHint']));
});

test('explicit cumulative allowances, whole seconds and independent roles reach the original creation body',async t=>{
 const calls=[];const env=setup({listRunModels:async()=>({models:policyModels}),createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');}});
 t.after(()=>env.close());await tick();edit(env,'objective','Investigate an observed failure');
 edit(env,'program-token-limit','12345');edit(env,'program-tool-limit','47');edit(env,'program-hours','0.1');edit(env,'program-attempts','0');
 edit(env,'program-executor-model',JSON.stringify(executorModel));edit(env,'program-reviewer-model',JSON.stringify(reviewerModel));
 submit(env,'.exp-create-form');await tick();submit(env,'.exp-create-form');await tick();
 assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.deepEqual(calls[0].body,{objective:'Investigate an observed failure',auto_replan:true,
  budget:{max_tokens:12345,max_tool_calls:47,max_wall_clock_seconds:360},max_attempts:0,role_models:{executor:executorModel,reviewer:reviewerModel}});
});

test('empty policy preserves omitted defaults while explicit zero creates a different immutable request',async t=>{
 const calls=[];const env=setup({createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');}});
 t.after(()=>env.close());await tick();edit(env,'objective','Measure the real workaround');submit(env,'.exp-create-form');await tick();
 assert.deepEqual(calls[0].body,{objective:'Measure the real workaround',auto_replan:true});
 for(const name of ['program-token-limit','program-tool-limit','program-hours','program-attempts'])edit(env,name,'0');
 submit(env,'.exp-create-form');await tick();assert.deepEqual(calls[1].body.budget,{max_tokens:0,max_tool_calls:0,max_wall_clock_seconds:0});
 assert.equal(calls[1].body.max_attempts,0);assert.notEqual(calls[0].key,calls[1].key);
});

test('invalid allowances cannot launch a program or round fractional seconds',async t=>{
 let creates=0;const env=setup({createProgram:async()=>{creates++;throw Error('Must not write');}});t.after(()=>env.close());await tick();edit(env,'objective','Invalid cap');
 for(const [name,value] of [['program-token-limit','-1'],['program-tool-limit','1.5'],['program-attempts','9007199254740992'],['program-hours','0.0001']]){
  edit(env,name,value);submit(env,'.exp-create-form');await tick();assert.equal(creates,0);edit(env,name,'');
 }
});

test('legacy five-field drafts retain their exact ambiguous body and key',async t=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};
 const body={objective:'Original five-field objective',auto_replan:false,acceptance:['Original check'],project_path:'/original'};
 load('features/evolution/programs/drafts.ts').createProgramDraftStore({owner:()=>7,scope:'/app/',storage:()=>storage}).save(['create'],
  {fields:[body.objective,'Original check','/original','','manual'],attempt:{signature:JSON.stringify(body),key:'legacy-original-key'}});
 const calls=[];const env=setup({createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost original ACK');}},ports);t.after(()=>env.close());await tick();
 assert.equal(calls.length,0);assert.equal(env.root.querySelector('[name=program-token-limit]').value,'');submit(env,'.exp-create-form');await tick();
 assert.deepEqual(calls,[{body,key:'legacy-original-key'}]);
});

test('settings-only drafts survive remount without submitting or losing unavailable role identity',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};let creates=0;
 const first=setup({listRunModels:async()=>({models:policyModels})},ports);await tick();edit(first,'program-hours','2.5');edit(first,'program-reviewer-model',JSON.stringify(reviewerModel));first.close();
 const second=setup({createProgram:async()=>{creates++;throw Error('Must not write');}},ports);
 try {await tick();assert.equal(creates,0);assert.equal(second.root.querySelector('[name=program-hours]').value,'2.5');
  const select=second.root.querySelector('[name=program-reviewer-model]');assert.equal(select.value,JSON.stringify(reviewerModel));assert.equal(select.selectedOptions[0].disabled,true);
  edit(second,'objective','New objective');submit(second,'.exp-create-form');await tick();assert.equal(creates,0);assert.match(second.root.querySelector('.exp-form-status').textContent,/unavailable/i);
 }finally{second.close();}
});

test('a vanished role can resolve an ambiguous ACK with its original key but cannot admit a changed request',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'},calls=[];
 const createProgram=async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');};
 const first=setup({listRunModels:async()=>({models:policyModels}),createProgram},ports);await tick();edit(first,'objective','Original immutable objective');edit(first,'program-reviewer-model',JSON.stringify(reviewerModel));
 submit(first,'.exp-create-form');await tick();first.close();const second=setup({createProgram},ports);
 try{await tick();assert.equal(calls.length,1);submit(second,'.exp-create-form');await tick();assert.deepEqual(calls[1],calls[0]);
  edit(second,'program-token-limit','100');submit(second,'.exp-create-form');await tick();assert.equal(calls.length,2);assert.match(second.root.querySelector('.exp-form-status').textContent,/unavailable/i);
 }finally{second.close();}
});

test('role catalog refresh keeps focused controls, explicit selection and open settings',async t=>{
 let models=policyModels;const env=setup({listRunModels:async()=>({models})});t.after(()=>env.close());await tick();
 const select=env.root.querySelector('[name=program-reviewer-model]'),details=env.root.querySelector('.exp-create-form details');details.open=true;edit(env,'program-reviewer-model',JSON.stringify(reviewerModel));select.focus();
 const selected=select.selectedOptions[0];await env.mounted.refresh();assert.equal(select.selectedOptions[0],selected);assert.equal(document.activeElement,select);
 models=[policyModels[0]];await env.mounted.refresh();assert.equal(env.root.querySelector('[name=program-reviewer-model]'),select);assert.equal(document.activeElement,select);
 assert.equal(select.value,JSON.stringify(reviewerModel));assert.equal(select.selectedOptions[0].disabled,true);assert.equal(details.open,true);
 models=policyModels;await env.mounted.refresh();assert.equal(select.selectedOptions[0].disabled,false);assert.equal(select.value,JSON.stringify(reviewerModel));
});

test('model reference key order cannot invalidate restored roles or change their ambiguous request identity',async()=>{
 const original={provider_id:'fixture-provider',offering_id:'independent-reviewer'},reordered={offering_id:'independent-reviewer',provider_id:'fixture-provider'};
 let models=[{model:original,label:'Independent reviewer'}];const calls=[],storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};
 const createProgram=async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');};
 const first=setup({listRunModels:async()=>({models}),createProgram},ports);await tick();edit(first,'objective','Original provider route');edit(first,'program-reviewer-model',JSON.stringify(original));
 const select=first.root.querySelector('[name=program-reviewer-model]'),option=select.selectedOptions[0];select.focus();
 models=[{model:reordered,label:'Independent reviewer'}];await first.mounted.refresh();assert.equal(select.selectedOptions[0],option);assert.equal(document.activeElement,select);
 assert.equal(select.value,JSON.stringify(original));submit(first,'.exp-create-form');await tick();assert.equal(calls.length,1);first.close();
 const restored=setup({listRunModels:async()=>({models}),createProgram},ports);
 try{await tick();const saved=restored.root.querySelector('[name=program-reviewer-model]');assert.equal(saved.value,JSON.stringify(original));assert.equal(saved.selectedOptions[0].disabled,false);
  submit(restored,'.exp-create-form');await tick();assert.deepEqual(calls[1],calls[0]);
  edit(restored,'objective','A genuinely different request');submit(restored,'.exp-create-form');await tick();assert.equal(calls.length,3);assert.notEqual(calls[2].key,calls[0].key);
  assert.equal(JSON.stringify(calls[2].body.role_models.reviewer),JSON.stringify(original));
 }finally{restored.close();}
});

test('frozen provider references match catalog identity independent of serialization order',async t=>{
 const selected={offering_id:'reviewer',provider_id:'controlled'},catalog={provider_id:'controlled',offering_id:'reviewer'};
 const env=setup({listRunModels:async()=>({models:[{model:catalog,label:'Available independent reviewer'}]}),
 getProgram:async()=>({program:program('a',{execution_options:{role_models:{reviewer:selected}}})})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.match(env.root.querySelector('.exp-policy-summary').textContent,/Available independent reviewer/);assert.doesNotMatch(env.root.querySelector('.exp-policy-summary').textContent,/unavailable/i);
});

test('frozen allowances use settled consumption and expose pending or unknown balances without changing original caps',async t=>{
 let extra={execution_options:{budget:{max_tokens:100,max_tool_calls:9,max_wall_clock_seconds:360},role_models:{reviewer:reviewerModel},model:executorModel,max_attempts:0},auto_replan:true};
 const env=setup({getProgram:async id=>({program:program(id,extra)}),listRunModels:async()=>({models:policyModels})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const text=env.root.querySelector('.exp-policy-summary'),details=text.parentElement;details.open=true;
 assert.match(text.textContent,/100.*93/);assert.match(text.textContent,/9.*8/);assert.match(text.textContent,/0\.1/);assert.match(text.textContent,/Controlled independent reviewer/);assert.match(text.textContent,/Controlled executor/);
 extra={...extra,current:{run_id:'r',sequence:1,step}};await env.mounted.refresh();assert.match(text.textContent,/100.*pending/i);assert.equal(details.open,true);
 extra={...extra,actuals_complete:false};await env.mounted.refresh();assert.match(text.textContent,/100.*unknown/i);
 extra={auto_replan:true,execution_options:{budget:{max_wall_clock_seconds:1}}};await env.mounted.refresh();
 assert.match(text.textContent,/0\.000277777778/);assert.match(text.textContent,/Attempts per technique: 3/);
 extra={auto_replan:false};await env.mounted.refresh();assert.match(text.textContent,/Attempts per technique: Unlimited/);
 env.mounted.updateRoute('evolution/programs/b');await tick();assert.equal(details.open,false);
});

test('successful creation retains execution settings and newer edits survive a late creation ACK',async t=>{
 let finish;const bodies=[];const env=setup({listRunModels:async()=>({models:policyModels}),createProgram:body=>{bodies.push(body);return new Promise(done=>finish=done);},getProgram:async id=>({program:program(id)})});t.after(()=>env.close());await tick();
 edit(env,'objective','First goal');edit(env,'program-token-limit','99');edit(env,'program-reviewer-model',JSON.stringify(reviewerModel));submit(env,'.exp-create-form');await tick();finish({program:program('made')});await tick();await tick();
 assert.equal(env.root.querySelector('[name=objective]').value,'');assert.equal(env.root.querySelector('[name=program-token-limit]').value,'99');assert.equal(env.root.querySelector('[name=program-reviewer-model]').value,JSON.stringify(reviewerModel));
 edit(env,'objective','Second goal');submit(env,'.exp-create-form');await tick();edit(env,'objective','New unsubmitted goal');edit(env,'program-token-limit','101');finish({program:program('made-2')});await tick();await tick();
 assert.equal(bodies[1].budget.max_tokens,99);assert.equal(env.root.querySelector('[name=objective]').value,'New unsubmitted goal');assert.equal(env.root.querySelector('[name=program-token-limit]').value,'101');
});
const sourceText='🧪原始证据'.repeat(1600),toolText=JSON.stringify({tool:'measure',content:'<img src=x onerror=alert(1)>观察失败'})+'\n';
const sourceRef=(text,kind='deliverable')=>({task_id:'sealed-task',source_sha256:crypto.createHash('sha256').update(text).digest('hex'),total_chars:Array.from(text).length,source_kind:kind});
const sourcePage=(text,offset=0,kind='deliverable')=>{const chars=Array.from(text),end=Math.min(offset+6000,chars.length);return {task_id:'sealed-task',stage:'executing',status:'done',terminal_verdict:'completed',content:chars.slice(offset,end).join(''),offset,total_chars:chars.length,next_offset:end<chars.length?end:null,archive_state:'sealed',source_kind:kind,source_sha256:sourceRef(text).source_sha256,tool_evidence_available:true,tool_evidence_scope:'terminal_retained_tool_messages',actuals:{tokens:30,tool_calls:2,wall_clock_seconds:3},actuals_complete:false};};
const sourcedReceipt=()=>({...receipt(25),result:'Bounded preview',result_source:sourceRef(sourceText),tool_evidence_source:sourceRef(toolText,'tool_evidence')});
const sourceApi=overrides=>({getProgram:async()=>({program:program('a',{status:'completed',settled_through:25,queue:[]})}),getProgramExperiments:async()=>({experiments:[sourcedReceipt()],settled_through:25,next_cursor:null}),...overrides});

test('same experiment retries point to the new execution even when both execution reads fail',async t=>{
 let id='r1',sequence=1;const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:id,sequence,step}})}),getRun:async()=>{throw Error('Execution information unavailable');}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelector('.exp-current a').getAttribute('href'),'#/evolution/runs/r1');
 id='r2';sequence=2;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-current a').getAttribute('href'),'#/evolution/runs/r2');
 assert.match(env.root.querySelector('.exp-current').textContent,/temporarily unavailable/i);assert.doesNotMatch(env.root.querySelector('.exp-current').textContent,/waiting for execution/i);
});

test('wrong program responses are rejected before child reads or writes and preserve confirmed same-owner data',async t=>{
 let wrong=true,childReads=0;const env=setup({getProgram:async id=>({program:program(wrong?'other':id)}),getProgramExperiments:async()=>{childReads++;return {experiments:[],settled_through:0,next_cursor:null};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(childReads,0);assert.equal(env.root.querySelector('.exp-detail').hidden,true);assert.match(env.root.querySelector('.exp-error').textContent,/program_identity_mismatch/);
 wrong=false;await env.mounted.refresh();assert.equal(childReads,1);assert.match(env.root.querySelector('.exp-objective-text').textContent,/Original objective a/);
 wrong=true;await env.mounted.refresh();assert.equal(childReads,1);assert.match(env.root.querySelector('.exp-objective-text').textContent,/Original objective a/);assert.doesNotMatch(env.root.querySelector('.exp-title').textContent,/other/);
});

test('wrong execution responses never admit foreign questions or tasks and retain confirmed same-run drafts',async t=>{
 let wrong=true;const questions=[{guidance_id:'own-q',questions:[{question:'Own evidence?',options:[]}]}];
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step}})}),getRun:async()=>({run:{...run,run_id:wrong?'foreign':'r',task_id:wrong?'foreign-task':'own-task'},pending_questions:wrong?[{guidance_id:'foreign-q',questions:[{question:'Foreign evidence?',options:[]}]}]:questions})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelector('.exp-question'),null);assert.match(env.root.querySelector('.exp-error').textContent,/run_identity_mismatch/);assert.match(env.root.querySelector('.exp-current').textContent,/temporarily unavailable/i);
 wrong=false;await env.mounted.refresh();const answer=env.root.querySelector('.exp-question textarea');answer.value='Keep the original observed failure';answer.focus();
 wrong=true;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-question textarea'),answer);assert.equal(answer.value,'Keep the original observed failure');assert.equal(document.activeElement,answer);assert.doesNotMatch(env.root.textContent,/Foreign evidence/);assert.match(env.root.querySelector('.exp-current').textContent,/last confirmed/i);
});

test('execution inspection retains focus and hypothesis disclosure across status and read changes',async t=>{
 let status='executing',fail=false;const opened=[];
 const hypothesis={problem:'Observed unmet need',audience:'Researchers',baseline:'Manual workaround',scenario:'Future workflow',mechanism:'Competing mechanism',expected_value:'Measured benefit',falsifier:'No improvement',assumptions:[],evidence_refs:[]};
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step:{...step,hypothesis}}})}),getRun:async()=>{if(fail)throw Error('Read outage');return {run:{...run,status},pending_questions:[]};}},{path:'evolution/programs/a',openTask:id=>opened.push(id)});
 t.after(()=>env.close());await tick();const room=env.root.querySelector('.exp-current a'),inspect=env.root.querySelector('.exp-current button');
 env.root.querySelector('.exp-current .exp-hypothesis').open=true;room.focus();status='reviewing';await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-current a'),room);assert.equal(document.activeElement,room);assert.equal(env.root.querySelector('.exp-current .exp-hypothesis').open,true);
 inspect.focus();fail=true;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-current button'),inspect);assert.equal(document.activeElement,inspect);assert.match(env.root.querySelector('.exp-current').textContent,/last confirmed/i);
 inspect.click();assert.deepEqual(opened,['t']);env.close();inspect.click();assert.deepEqual(opened,['t']);
});

test('foreign experiment pages cannot replace the confirmed selected history',async t=>{
 let wrong=false;const env=setup({getProgramExperiments:async()=>({experiments:[{...receipt(wrong?2:1),program_id:wrong?'other':'a',result:wrong?'Other plan evidence':'Own evidence'}],settled_through:wrong?2:1,next_cursor:null})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();const row=env.root.querySelector('.exp-receipt');row.open=true;row.dispatchEvent(new env.dom.window.Event('toggle'));assert.match(row.textContent,/Own evidence/);
 wrong=true;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-receipt'),row);assert.match(row.textContent,/Own evidence/);assert.doesNotMatch(env.root.textContent,/Other plan evidence/);assert.match(env.root.querySelector('.exp-error').textContent,/history_identity_mismatch/);
});

test('notebook originals are explicit Unicode pages and tool evidence stays inert and separate',async t=>{
 const calls=[];const env=setup(sourceApi({getStageOutcome:async(id,task,offset,signal,kind)=>{calls.push({id,task,offset,kind});return {outcome:sourcePage(kind==='tool_evidence'?toolText:sourceText,offset,kind)};}}),{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(calls.length,0);await env.mounted.refresh();assert.equal(calls.length,0);
 const open=env.root.querySelector('.exp-output [data-program-source=deliverable]');assert.ok(open);open.click();await tick();
 const host=env.root.querySelector('[data-program-source-reader]');assert.equal(host.hidden,false);assert.equal(host.querySelector('pre').textContent,Array.from(sourceText).slice(0,6000).join(''));
 assert.deepEqual(calls[0],{id:'run-25',task:'sealed-task',offset:0,kind:'deliverable'});
 host.querySelector('[data-source-action=next]').click();await tick();assert.equal(calls[1].offset,6000);assert.equal(host.querySelector('pre').textContent,Array.from(sourceText).slice(6000).join(''));
 host.querySelector('[data-source-action=tool_evidence]').click();await tick();assert.equal(calls[2].kind,'tool_evidence');assert.equal(host.querySelector('pre').textContent,toolText);assert.equal(host.querySelector('img'),null);assert.match(host.textContent,/≥ 30/);
 const original=host.querySelector('pre');await env.mounted.refresh();assert.equal(host.querySelector('pre'),original);assert.equal(calls.length,3);
 host.querySelector('[data-source-action=close]').click();assert.equal(host.hidden,true);assert.equal(document.activeElement,open);
});

test('notebook source failure retains its admitted page and retry uses the same owner offset',async t=>{
 let fail=true;const calls=[];const env=setup(sourceApi({getStageOutcome:async(id,task,offset,signal,kind)=>{calls.push(offset);if(offset&&fail)throw Error('Source read outage');return {outcome:sourcePage(sourceText,offset,kind)};}}),{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();env.root.querySelector('.exp-output [data-program-source=deliverable]').click();await tick();
 const host=env.root.querySelector('[data-program-source-reader]'),original=host.querySelector('pre'),first=original.textContent;
 host.querySelector('[data-source-action=next]').click();await tick();assert.equal(original.textContent,first);assert.equal(host.querySelector('[data-source-action=retry]').hidden,false);
 fail=false;host.querySelector('[data-source-action=retry]').click();await tick();assert.deepEqual(calls,[0,6000,6000]);assert.equal(host.querySelector('pre'),original);assert.equal(original.textContent,Array.from(sourceText).slice(6000).join(''));
});

test('notebook owner change, visibility and disposal abort original reads and reject late pages',async t=>{
 let resolve,signal;const env=setup(sourceApi({getStageOutcome:async(id,task,offset,s)=>{signal=s;return new Promise(done=>resolve=done);}}),{path:'evolution/programs/a'});
 t.after(()=>env.close());
 await tick();env.root.querySelector('.exp-output [data-program-source=deliverable]').click();await tick();assert.equal(signal.aborted,false);
 env.mounted.updateRoute('evolution/programs/b');assert.equal(signal.aborted,true);resolve({outcome:sourcePage(sourceText)});await tick();const host=env.root.querySelector('[data-program-source-reader]');assert.equal(host.hidden,true);assert.equal(host.textContent.includes(sourceText.slice(0,10)),false);
 env.mounted.updateRoute('evolution/programs/a');await tick();env.root.querySelector('.exp-output [data-program-source=deliverable]').click();await tick();
 Object.defineProperty(env.dom.window.document,'hidden',{value:true,configurable:true});document.dispatchEvent(new env.dom.window.Event('visibilitychange'));assert.equal(signal.aborted,true);resolve({outcome:sourcePage(sourceText)});await tick();assert.equal(host.querySelector('pre').textContent,'');
 Object.defineProperty(env.dom.window.document,'hidden',{value:false,configurable:true});document.dispatchEvent(new env.dom.window.Event('visibilitychange'));host.querySelector('[data-source-action=retry]').click();await tick();const root=env.root;env.close();assert.equal(signal.aborted,true);resolve({outcome:sourcePage(sourceText)});await tick();assert.equal(root.childElementCount,0);
});

test('failed experiment evidence is directly inspectable without claiming acceptance or execution',async t=>{
 const failed={...sourcedReceipt(),sequence:1,step,outcome:'needs_attention'};const calls=[];
 const env=setup({getProgram:async()=>({program:program('a',{status:'needs_attention',settled_through:1})}),getProgramExperiments:async()=>({experiments:[failed],settled_through:1,next_cursor:null}),
 getStageOutcome:async(id,task,offset,signal,kind)=>{calls.push(kind);return {outcome:{...sourcePage(toolText,offset,kind),terminal_verdict:'interrupted'}};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();const row=env.root.querySelector('.exp-receipt');row.open=true;row.dispatchEvent(new env.dom.window.Event('toggle'));
 row.querySelector('[data-program-source=tool_evidence]').click();await tick();const host=env.root.querySelector('[data-program-source-reader]');assert.deepEqual(calls,['tool_evidence']);assert.equal(host.querySelector('pre').textContent,toolText);
 assert.match(host.textContent,/interrupted/i);assert.match(env.root.querySelector('.exp-status').textContent,/attention/i);assert.equal(env.root.querySelector('.exp-output pre'),null);
});

test('missing source capability explains the fallback without fetching or inventing task identity',async t=>{
 const env=setup(sourceApi({}),{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.equal(env.root.querySelector('[data-program-source]'),null);const fallback=env.root.querySelector('.exp-output .exp-source-actions');assert.match(fallback.textContent,/unavailable/i);assert.equal(fallback.querySelector('a').getAttribute('href'),'#/evolution/runs/run-25');
});

test('creation draft and ambiguous creation key survive a fresh mounted page without automatic writes',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};const calls=[];
 const overrides={createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost creation response');}};
 const first=setup(overrides,ports);await tick();
 edit(first,'objective','Investigate a real unmet problem');edit(first,'acceptance','Measure the failed workaround');
 first.root.querySelector('[name=auto-replan]').value='auto';
 first.root.querySelector('[name=auto-replan]').dispatchEvent(new first.dom.window.Event('change',{bubbles:true}));
 submit(first,'.exp-create-form');await tick();assert.equal(calls.length,1);first.close();
 const second=setup(overrides,ports);
 try {await tick();assert.equal(calls.length,1);assert.equal(second.root.querySelector('[name=objective]').value,'Investigate a real unmet problem');
  assert.equal(second.root.querySelector('[name=acceptance]').value,'Measure the failed workaround');assert.equal(second.root.querySelector('[name=auto-replan]').value,'auto');
  submit(second,'.exp-create-form');await tick();assert.deepEqual(calls[1],calls[0]);
 } finally {second.close();}
});

test('known created program resumes only its original start after reload',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};let creates=0;const starts=[];
 const overrides={createProgram:async()=>{creates++;return {program:program('saved')};},getProgram:async id=>({program:program(id,{status:'ready'})}),commandProgram:async(...args)=>{starts.push(args);throw Error('Lost start response');}};
 const first=setup(overrides,ports);await tick();edit(first,'objective','A complete original objective');submit(first,'.exp-create-form');await tick();assert.equal(creates,1);first.close();
 const second=setup(overrides,ports);
 try {await tick();assert.equal(starts.length,1);submit(second,'.exp-create-form');await tick();assert.equal(creates,1);assert.deepEqual(starts,[['saved','start'],['saved','start']]);}
 finally {second.close();}
});

test('expansion fields and ambiguous append key survive remount and confirmed acknowledgement clears them',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/',path:'evolution/programs/a'},calls=[];let lost=true;
 const overrides={appendProgramSteps:async(...args)=>{calls.push(args);if(lost)throw Error('Lost append response');}};
 const first=setup(overrides,ports);await tick();edit(first,'experiment-title','Competing mechanism');edit(first,'instruction','Observe what falsifies the mechanism');edit(first,'experiment-checks','Preserve negative evidence');
 submit(first,'.exp-expand-form');await tick();first.close();
 const second=setup(overrides,ports);
 try {await tick();assert.equal(second.root.querySelector('[name=instruction]').value,'Observe what falsifies the mechanism');assert.equal(calls.length,1);lost=false;submit(second,'.exp-expand-form');await tick();assert.deepEqual(calls[1],calls[0]);}
 finally {second.close();}
 const third=setup(overrides,ports);try {await tick();assert.equal(third.root.querySelector('[name=instruction]').value,'');}finally{third.close();}
});

test('answer drafts restore only for the same account, application, program, run and question',async()=>{
 const storage=draftStorage();let owner=7,runId='r';
 const questions=[{guidance_id:'q',questions:[{question:'Choose evidence',options:[]}]}];
 const ports={draftStorage:storage,draftOwner:()=>owner,draftScope:'/app/',path:'evolution/programs/a'};
 const overrides={getProgram:async()=>({program:program('a',{current:{run_id:runId,sequence:1,step}})}),getRun:async()=>({run:{...run,run_id:runId},pending_questions:questions})};
 const first=setup(overrides,ports);await tick();edit(first,'answer-q-0','Measure both alternatives');first.close();
 const same=setup(overrides,ports);try{await tick();assert.equal(same.root.querySelector('.exp-question textarea').value,'Measure both alternatives');}finally{same.close();}
 for(const change of [()=>owner=8,()=>{owner=7;runId='r2';}]){
  change();const other=setup(overrides,ports);try{await tick();assert.equal(other.root.querySelector('.exp-question textarea').value,'');}finally{other.close();}
 }
 runId='r';const otherApp=setup(overrides,{...ports,draftScope:'/other-app/'});try{await tick();assert.equal(otherApp.root.querySelector('.exp-question textarea').value,'');}finally{otherApp.close();}
});

test('late append acknowledgement retains input edited while the request was pending',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/',path:'evolution/programs/a'};let finish;
 const env=setup({appendProgramSteps:()=>new Promise(resolve=>finish=resolve)},ports);await tick();
 edit(env,'experiment-title','First mechanism');edit(env,'instruction','First protocol');edit(env,'experiment-checks','First check');submit(env,'.exp-expand-form');await tick();
 edit(env,'instruction','A newer unsubmitted protocol');finish();await tick();await tick();
 assert.equal(env.root.querySelector('[name=instruction]').value,'A newer unsubmitted protocol');env.close();
 const restored=setup({},ports);try{await tick();assert.equal(restored.root.querySelector('[name=instruction]').value,'A newer unsubmitted protocol');}finally{restored.close();}
});

test('an acknowledged answer is not saved again by page disposal',async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/',path:'evolution/programs/a'};
 const questions=[{guidance_id:'q',questions:[{question:'Choose evidence',options:[]}]}];
 const overrides={getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step}})}),getRun:async()=>({run,pending_questions:questions}),answerRun:async()=>{}};
 const first=setup(overrides,ports);await tick();edit(first,'answer-q-0','Confirmed answer');submit(first,'.exp-question');await tick();first.close();
 const second=setup(overrides,ports);try{await tick();assert.equal(second.root.querySelector('.exp-question textarea').value,'');}finally{second.close();}
});

test('draft recovery waits for owner resolution and never overwrites input already typed',async()=>{
 const storage=draftStorage();let owner=7;const ports={draftStorage:storage,draftOwner:()=>owner,draftScope:'/app/'};
 const first=setup({},ports);await tick();edit(first,'objective','Stored original objective');first.close();owner=null;
 const delayed=setup({},ports);try {await tick();owner=7;await delayed.mounted.refresh();assert.equal(delayed.root.querySelector('[name=objective]').value,'Stored original objective');}
 finally {delayed.close();}
 owner=null;const edited=setup({},ports);try {await tick();edit(edited,'objective','A new typed objective');owner=7;await edited.mounted.refresh();assert.equal(edited.root.querySelector('[name=objective]').value,'A new typed objective');}
 finally {edited.close();}
});

test('all catalog pages remain reachable while selected evidence and focused drafts survive',async t=>{
 const ids=Array.from({length:53},(_,i)=>'p'+String(i).padStart(3,'0')),queries=[];
 const env=setup({listPrograms:async(_signal,q={})=>{
  queries.push({...q});const n=q.limit||20;
  const start=q.after?ids.indexOf(q.after)+1:q.before?Math.max(0,ids.indexOf(q.before)-n):0;
  const part=ids.slice(start,start+n);
  return {programs:part.map(id=>program(id)),order:'program_id_asc',
   next_cursor:start+part.length<ids.length?part.at(-1):null,previous_cursor:start?part[0]:null};
 }},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const original=env.root.querySelector('.exp-objective'),draft=env.root.querySelector('[name=instruction]');
 original.open=true;draft.value='Preserve my unsubmitted experiment';draft.focus();
 const seen=[];
 for(let i=0;i<3;i++){
  seen.push(...[...env.root.querySelectorAll('.exp-catalog [data-program-id]')].map(el=>el.dataset.programId));
  assert.equal(env.root.querySelectorAll('.exp-catalog [data-program-id]').length<=20,true);
  assert.equal(env.root.querySelector('.exp-objective'),original);assert.equal(original.open,true);
  assert.equal(env.root.querySelector('[name=instruction]'),draft);assert.equal(draft.value,'Preserve my unsubmitted experiment');
  assert.equal(document.activeElement,draft);assert.match(env.root.querySelector('h1').textContent,/Original objective a/);
  if(i<2){env.root.querySelectorAll('.exp-catalog-pager button')[1].click();await tick();await tick();}
 }
 assert.deepEqual(seen,ids);assert.equal(env.root.querySelectorAll('.exp-catalog-pager button')[1].getAttribute('aria-disabled'),'true');
 env.root.querySelectorAll('.exp-catalog-pager button')[0].click();await tick();await tick();
 assert.equal(env.root.querySelector('.exp-catalog [data-program-id]').dataset.programId,'p020');
 await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-catalog [data-program-id]').dataset.programId,'p020');
 env.root.querySelectorAll('.exp-catalog-pager button')[2].click();await tick();await tick();
 assert.equal(env.root.querySelector('.exp-catalog [data-program-id]').dataset.programId,'p000');
 assert.ok(queries.every(q=>q.limit===20));
});

test('read navigation stays focused, coalesces keys and supersedes background polling',async t=>{
 let deferred=false,release;const queries=[];
 const first={programs:[program('a')],next_cursor:'a',previous_cursor:null};
 const env=setup({listPrograms:(_signal,q)=>{
  queries.push({...q});if(deferred&&!q.after)return new Promise(resolve=>release=resolve);
  return Promise.resolve(q.after?{programs:[program('b')],next_cursor:null,previous_cursor:'b'}:first);
 }},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const next=env.root.querySelectorAll('.exp-catalog-pager button')[1];next.focus();
 deferred=true;const old=env.mounted.refresh();await tick();
 assert.equal(document.activeElement,next);assert.equal(next.getAttribute('aria-disabled'),'false');
 next.click();next.click();assert.equal(next.getAttribute('aria-disabled'),'true');
 assert.equal(document.activeElement,next);assert.equal(queries.length,2);
 release({...first,programs:[program('stale')]});await old;await tick();await tick();
 assert.equal(queries.length,3);assert.equal(queries.at(-1).after,'a');
 assert.equal(env.root.querySelector('.exp-catalog [data-program-id]').dataset.programId,'b');
 assert.equal(document.activeElement,next);assert.equal(next.getAttribute('aria-disabled'),'true');
 next.click();await tick();assert.equal(queries.length,3);
 assert.match(env.root.querySelector('h1').textContent,/Original objective a/);
});

test('history windows preserve selected evidence and drafts until a fenced page arrives',async t=>{
 let deferred=false,release;const pages=[];
 const env=setup({getProgram:async()=>({program:program('a',{settled_through:41})}),
 getProgramExperiments:(_id,after)=>{
  pages.push(after);const value={experiments:[receipt(after+1)],settled_through:41,next_cursor:after+20<41?after+20:null};
  return deferred?new Promise(resolve=>release=()=>resolve(value)):Promise.resolve(value);
 }},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const older=env.root.querySelectorAll('.exp-pager button')[0],detail=env.root.querySelector('.exp-detail');
 const draft=env.root.querySelector('[name=instruction]');draft.value='Future mechanism';older.focus();
 deferred=true;older.click();older.click();await tick();
 assert.equal(detail.hidden,false);assert.equal(document.activeElement,older);
 assert.equal(older.getAttribute('aria-disabled'),'true');assert.deepEqual(pages,[21,1]);
 assert.equal(env.root.querySelector('.exp-receipt').dataset.sequence,'22');
 release();await tick();await tick();assert.equal(env.root.querySelector('.exp-receipt').dataset.sequence,'2');
 assert.equal(document.activeElement,older);assert.equal(env.root.querySelector('[name=instruction]'),draft);
 assert.equal(draft.value,'Future mechanism');deferred=false;older.click();await tick();await tick();
 assert.equal(env.root.querySelector('.exp-receipt').dataset.sequence,'1');
 assert.equal(document.activeElement,older);assert.equal(older.getAttribute('aria-disabled'),'true');
 older.click();await tick();assert.deepEqual(pages,[21,1,0]);
});

test('late catalog poll cannot overwrite a changed page even when transport ignores abort',async()=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');
 let defer=false,resolveOld;const states=[],queries=[];
 const first={programs:[program('a')],next_cursor:'a',previous_cursor:null,order:'program_id_asc'};
 const controller=createProgramController(api({listPrograms:(_signal,q)=>{
  queries.push({...q});if(defer&&!q.after)return new Promise(resolve=>resolveOld=resolve);
  return Promise.resolve(q.after?{...first,programs:[program('b')],next_cursor:null,previous_cursor:'b'}:first);
 }}),{update:s=>states.push(s),visible:()=>true,schedule:()=>1,cancel:()=>{}});
 controller.select('a');await tick();defer=true;const old=controller.refresh();await tick();
 controller.catalogPage('next');resolveOld({...first,programs:[program('stale')]});await old;await tick();await tick();
 assert.equal(states.at(-1).programs[0].program_id,'b');assert.equal(states.at(-1).program.program_id,'a');
 assert.equal(states.some(s=>s.programs.some(p=>p.program_id==='stale')),false);
 assert.ok(queries.some(q=>q.after==='a'));controller.dispose();
});

const memoryEntry=(key='memory-1')=>({key,query:'<script>Bandwidth bottleneck?</script>',created_at:1,model:'fixture',
 actuals:{tokens:31,tool_calls:0,wall_clock_seconds:1},actuals_complete:false,source_chars:1200,scanned_sources:1,
 source_chunks:[{candidate_id:'0',sequence:7,offset:0,end:1200,source_digest:'d'.repeat(64)}],
 matches:[{candidate_id:'0',relevance:.9,quote:'<img src=x onerror=alert(1)>',reason:'Measured failed transfer'}],
 task_id:'ranking',inspector_task_id:'ranking'});

test('full original goal stays safe and keyboard-readable across polls and resets on selection',async t=>{
 const longGoal=('<img src=x onerror=alert(1)>\n真实问题与新场景。'.repeat(120)).slice(0,4000);
 let status='active';
 const env=setup({getProgram:async id=>({program:program(id,{objective:id==='a'?longGoal:'Another full goal',status})})},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const detail=env.root.querySelector('.exp-objective'),text=detail.querySelector('[role=region]'),summary=detail.querySelector('summary');
 assert.equal(text.textContent,longGoal);assert.equal(env.root.querySelector('h1').textContent,longGoal);
 assert.equal(text.tabIndex,0);assert.ok(text.getAttribute('aria-label'));assert.equal(detail.querySelector('img,script'),null);
 detail.open=true;summary.focus();status='paused';await env.mounted.refresh();await tick();
 assert.equal(env.root.querySelector('.exp-objective'),detail);assert.equal(detail.open,true);assert.equal(document.activeElement,summary);
 text.focus();status='active';await env.mounted.refresh();await tick();
 assert.equal(document.activeElement,text);assert.equal(text.textContent,longGoal);
 env.mounted.updateRoute('evolution/programs/b');await tick();
 assert.equal(env.root.querySelector('.exp-objective'),detail);assert.equal(detail.open,false);
 assert.equal(text.textContent,'Another full goal');assert.match(env.root.querySelector('.exp-original').textContent,/Restart keeps all evidence/);
});

test('semantic history keeps exact quotes, lower-bound costs, focus and source reads across polls',async t=>{
 const pages=[];let entries=[memoryEntry()];
 const env=setup({getProgramMemories:async()=>({memory:{entries,retained_limit:32,evidence_status:'unverified'}}),
 getProgramExperiments:async(_id,after)=>{pages.push(after);return {experiments:[],settled_through:8,next_cursor:null};}},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const host=env.root.querySelector('[data-semantic-memory]'),detail=host.querySelector('details');
 assert.match(host.textContent,/relevance does not prove/i);assert.match(host.textContent,/≥ 31/);
 assert.equal(host.querySelector('img,script'),null);assert.match(host.textContent,/<img src=x onerror=alert\(1\)>/);
 detail.open=true;const source=detail.querySelector('button');source.focus();
 await env.mounted.refresh();await tick();
 assert.equal(host.querySelector('details'),detail);assert.equal(detail.open,true);assert.equal(document.activeElement,source);
 entries=[{...memoryEntry('memory-2'),query:'A later independent query'},...entries];
 await env.mounted.refresh();await tick();
 assert.equal(host.querySelectorAll('details')[1],detail);assert.equal(detail.open,true);assert.equal(document.activeElement,source);
 source.click();await tick();assert.equal(pages.at(-1),6);
 assert.match(host.textContent,/already included|original/i);
});

test('unavailable semantic history leaves current progress and receipts readable',async t=>{
 const env=setup({getProgramMemories:async()=>{throw new Error('Controlled memory outage');},
 getProgramExperiments:async()=>({experiments:[receipt(1)],settled_through:1,next_cursor:null})},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.match(env.root.querySelector('[data-semantic-memory]').textContent,/unavailable/i);
 assert.match(env.root.querySelector('.exp-current').textContent,/Exploration in progress/i);
 assert.equal(env.root.querySelectorAll('.exp-receipt').length,1);
 assert.equal(env.root.querySelector('.exp-error')?.hidden,true);
});

test('late memory results cannot cross program selection',async()=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');let release;const states=[];
 const controller=createProgramController(api({getProgramMemories:id=>id==='a'?new Promise(resolve=>release=resolve):Promise.resolve({memory:{entries:[{...memoryEntry(),query:'Program B evidence'}],retained_limit:32}})}),
 {update:s=>states.push(s),visible:()=>true,schedule:()=>1,cancel:()=>{}});
 controller.select('a');await tick();controller.select('b');release({memory:{entries:[memoryEntry()],retained_limit:32}});
 await tick();await tick();assert.equal(states.at(-1).program.program_id,'b');
 assert.equal(states.at(-1).memories.entries[0].query,'Program B evidence');
 assert.equal(states.some(s=>s.memories?.entries[0]?.query===memoryEntry().query),false);controller.dispose();
});

test('research hypotheses retain falsifiers and uncertainty across frontier, execution and history',async t=>{
 const hypothesis={problem:'Observed costly repeated failures',audience:'Researchers',baseline:'Journal reading',
 scenario:'Early warning before another experiment',mechanism:'<script>untrusted hypothesis</script>',
 expected_value:'Avoid repeated invalid trials',falsifier:'No measurable reduction',
 assumptions:['Counterexamples transfer'],evidence_refs:['experiment:1:result']};
 const proposed={...step,hypothesis};
 const active=program('a',{current:{run_id:'r',sequence:2,step:proposed},queue:[proposed,proposed,final],settled_through:1});
 const env=setup({getProgram:async()=>({program:active}),
 getProgramExperiments:async()=>({experiments:[{...receipt(1),step:proposed}],settled_through:1,next_cursor:null})},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 for(const selector of ['.exp-current','.exp-frontier']){
  const text=env.root.querySelector(selector).textContent;
  assert.match(text,/unverified/i);assert.match(text,/No measurable reduction/);
  assert.match(text,/Counterexamples transfer/);assert.match(text,/experiment:1:result/);
 }
 const record=env.root.querySelector('.exp-receipt');record.open=true;
 record.dispatchEvent(new env.dom.window.Event('toggle'));await tick();
 assert.match(record.textContent,/No measurable reduction/);assert.match(record.textContent,/unverified/i);
 assert.equal(env.root.querySelector('script'),null);
 assert.match(env.root.textContent,/<script>untrusted hypothesis<\/script>/);
});

test('create retries ambiguous replies with the same body/key, then starts the saved program',async t=>{
 const calls=[],commands=[];let lost=true;
 const env=setup({createProgram:async(body,key)=>{calls.push({body,key});if(lost)throw Error('lost reply');return {program:program('new')};},
 getProgram:async()=>({program:program('new',{status:'ready'})}),commandProgram:async(id,action)=>{commands.push({id,action});}});
 t.after(()=>env.close());await tick();
 env.root.querySelector('[name=objective]').value='Complete the real task';
 env.root.querySelector('[name=acceptance]').value='Restart survives\nWorks on mobile';
 env.root.querySelector('[name=project]').value='/server/repository';
 submit(env,'.exp-create-form');await tick();submit(env,'.exp-create-form');await tick();
 assert.deepEqual(calls[0],calls[1]);assert.ok(calls[0].key);
 assert.equal(calls[0].body.project_path,'/server/repository');assert.equal(commands.length,0);
 lost=false;submit(env,'.exp-create-form');await tick();
 assert.equal(calls[2].key,calls[0].key);assert.deepEqual(commands,[{id:'new',action:'start'}]);
 assert.deepEqual(env.navigations,['evolution/programs/new']);
});

test('lost start reply retries the saved program without creating it again',async t=>{
 let creates=0,starts=0;const env=setup({createProgram:async()=>{creates++;return {program:program('saved')};},
 getProgram:async()=>({program:program('saved',{status:'ready'})}),commandProgram:async()=>{starts++;if(starts===1)throw Error('lost start response');}});
 t.after(()=>env.close());await tick();env.root.querySelector('[name=objective]').value='Deliver';
 submit(env,'.exp-create-form');await tick();submit(env,'.exp-create-form');await tick();
 assert.equal(creates,1);assert.equal(starts,2);assert.deepEqual(env.navigations,['evolution/programs/saved']);
});

test('late creation can finish its authorized start but cannot navigate away from another program',async t=>{
 let finish;const commands=[];
 const env=setup({createProgram:()=>new Promise(resolve=>finish=resolve),getProgram:async id=>({program:program(id,{status:id==='new'?'ready':'active'})}),
 commandProgram:async(id,action)=>commands.push({id,action})});t.after(()=>env.close());
 await tick();env.root.querySelector('[name=objective]').value='Deliver';submit(env,'.exp-create-form');
 env.mounted.updateRoute('evolution/programs/b');await tick();finish({program:program('new')});await tick();await tick();
 assert.deepEqual(env.navigations,[]);assert.deepEqual(commands,[{id:'new',action:'start'}]);
 assert.equal(env.root.querySelector('h1').textContent,'Original objective b');
});

test('catalog reordering preserves selected model identity and text drafts',async t=>{
 const a={creator_id:'test',model_id:'a'},b={creator_id:'test',model_id:'b'};let models=[{model:a,label:'A'},{model:b,label:'B'}];
 const env=setup({listRunModels:async()=>({models})});t.after(()=>env.close());await tick();
 const select=env.root.querySelector('[name=model]'),draft=env.root.querySelector('[name=objective]');
 select.value=JSON.stringify(b);draft.value='Unsubmitted original goal';draft.focus();
 models=[models[1],models[0]];await env.mounted.refresh();
 assert.equal(select.value,JSON.stringify(b));assert.equal(draft.value,'Unsubmitted original goal');assert.equal(document.activeElement,draft);
 models=[{model:a,label:'A'}];await env.mounted.refresh();
 assert.equal(select.value,JSON.stringify(b));assert.match(select.selectedOptions[0].textContent,/unavailable/);
});

test('questions retain focused answers on refresh and submit the existing batch protocol',async t=>{
 const answered=[];const questions=[{guidance_id:'q',questions:[{question:'Select target',options:['A','B']},{question:'Explain checks',options:[]}]}];
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step}})}),
 getRun:async()=>({run,pending_questions:questions}),answerRun:async(...args)=>answered.push(args)},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();
 const controls=env.root.querySelectorAll('.exp-question textarea');controls[0].value='A';controls[1].value='Run the baseline';controls[1].focus();
 await env.mounted.refresh();assert.equal(env.root.querySelectorAll('.exp-question textarea')[1],controls[1]);assert.equal(document.activeElement,controls[1]);
 submit(env,'.exp-question');await tick();assert.deepEqual(answered,[['r','q',JSON.stringify([{index:0,answer:'A'},{index:1,answer:'Run the baseline'}])]]);
});

test('temporary run read failure preserves confirmed questions, focused drafts and recovery',async t=>{
 let outage=false;const answered=[];
 const questions=[{guidance_id:'q',questions:[{question:'What evidence should be collected?',options:[]}]}];
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step}})}),
 getRun:async()=>{if(outage)throw Error('Controlled run read outage');return {run,pending_questions:questions};},
 answerRun:async(...args)=>answered.push(args)},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();
 const form=env.root.querySelector('.exp-question'),answer=form.querySelector('textarea');
 answer.value='Measure the real failed workaround before changing the mechanism';answer.focus();
 outage=true;await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-error').hidden,false);
 assert.equal(env.root.querySelector('.exp-question'),form);
 assert.equal(document.activeElement,answer);
 assert.equal(answer.value,'Measure the real failed workaround before changing the mechanism');
 assert.deepEqual(answered,[]);
 outage=false;await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-error').hidden,true);
 assert.equal(env.root.querySelector('.exp-question'),form);assert.equal(document.activeElement,answer);
 submit(env,'.exp-question');await tick();
 assert.deepEqual(answered,[['r','q',JSON.stringify([{index:0,answer:'Measure the real failed workaround before changing the mechanism'}])]]);
});

test('run change cannot retain an older question or output after a failed detail read',async t=>{
 let id='r',outage=false;
 const questions=[{guidance_id:'q',questions:[{question:'Choose evidence',options:[]}]}];
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:id,sequence:1,step}})}),
 getRun:async()=>{if(outage)throw Error('New run is unavailable');return {run:{...run,run_id:id,result:'Output from '+id},pending_questions:questions};}},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const answer=env.root.querySelector('.exp-question textarea');answer.value='Answer for previous run';
 id='new-run';outage=true;await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-question'),null);
 assert.doesNotMatch(env.root.querySelector('.exp-output').textContent,/Output from r/);
 outage=false;await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-question textarea').value,'');
});

test('question form identity includes its run even when guidance identifiers recur',async t=>{
 let id='r';const answered=[];
 const questions=[{guidance_id:'q',questions:[{question:'Choose evidence',options:[]}]}];
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:id,sequence:1,step}})}),
 getRun:async()=>({run:{...run,run_id:id},pending_questions:questions}),
 answerRun:async(...args)=>answered.push(args)},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();
 const previous=env.root.querySelector('.exp-question textarea');previous.value='Old execution answer';
 id='new-run';await env.mounted.refresh();
 const current=env.root.querySelector('.exp-question textarea');assert.notEqual(current,previous);assert.equal(current.value,'');
 current.value='New execution answer';submit(env,'.exp-question');await tick();
 assert.equal(answered[0][0],'new-run');
});

test('successful empty question response removes confirmed old questions',async t=>{
 let questions=[{guidance_id:'q',questions:[{question:'Choose evidence',options:[]}]}];
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step}})}),
 getRun:async()=>({run,pending_questions:questions})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.ok(env.root.querySelector('.exp-question'));
 questions=[];await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-question'),null);
});

test('memory read outage preserves exact confirmed quotes, expansion and focus until authoritative recovery',async t=>{
 let outage=false,entries=[memoryEntry()];
 const env=setup({getProgramMemories:async()=>{if(outage)throw Error('Controlled memory read outage');return {memory:{entries,retained_limit:32}};}},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const host=env.root.querySelector('[data-semantic-memory]'),detail=host.querySelector('details'),source=detail.querySelector('button');
 detail.open=true;source.focus();outage=true;await env.mounted.refresh();
 assert.equal(host.querySelector('details'),detail);assert.equal(detail.open,true);assert.equal(document.activeElement,source);
 assert.match(host.textContent,/temporarily unavailable/i);assert.match(host.textContent,/<img src=x onerror=alert\(1\)>/);
 assert.equal(host.querySelector('img'),null);
 outage=false;await env.mounted.refresh();
 assert.equal(host.querySelector('details'),detail);assert.equal(document.activeElement,source);assert.doesNotMatch(host.textContent,/temporarily unavailable/i);
 entries=[];await env.mounted.refresh();assert.equal(host.querySelector('details'),null);
});

test('expansion draft and idempotency key survive ambiguous append and route visits',async t=>{
 const calls=[];const env=setup({appendProgramSteps:async(...args)=>{calls.push(args);throw Error('lost append response');}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();
 for(const [name,value] of [['experiment-title','Measure restart'],['instruction','Kill and restart the process'],['experiment-checks','Check exact receipts']])env.root.querySelector(`[name=${name}]`).value=value;
 submit(env,'.exp-expand-form');await tick();submit(env,'.exp-expand-form');await tick();assert.deepEqual(calls[0],calls[1]);
 env.mounted.updateRoute('evolution/programs/b');await tick();env.mounted.updateRoute('evolution/programs/a');await tick();
 assert.equal(env.root.querySelector('[name=instruction]').value,'Kill and restart the process');
 submit(env,'.exp-expand-form');await tick();assert.deepEqual(calls[0],calls[2]);
});

test('older receipts remain pageable while the final delivered artifact stays visible',async t=>{
 const cursors=[];const env=setup({getProgram:async()=>({program:program('a',{status:'completed',settled_through:25,queue:[]})}),
 getProgramExperiments:async(id,after,limit)=>{cursors.push([after,limit]);return {experiments:Array.from({length:Math.min(limit,25-after)},(_,i)=>receipt(after+i+1)),settled_through:25,next_cursor:after+limit<25?after+limit:null};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelectorAll('.exp-receipt').length,20);assert.equal(env.root.querySelector('.exp-receipt').dataset.sequence,'6');
 env.root.querySelector('.exp-pager button').click();await tick();await tick();
 assert.equal(env.root.querySelector('.exp-receipt').dataset.sequence,'1');assert.equal(env.root.querySelectorAll('.exp-receipt').length,20);
 assert.match(env.root.querySelector('.exp-output').textContent,/Final delivered artifact/);assert.ok(cursors.some(([a,l])=>a===24&&l===1));
});

test('final delivery retains its original DOM and text when its dedicated older-page read fails',async t=>{
 let outage=false;const cursors=[];
 const env=setup({getProgram:async()=>({program:program('a',{status:'completed',settled_through:25,queue:[]})}),
 getProgramExperiments:async(id,after,limit)=>{cursors.push([after,limit]);if(outage&&limit===1)throw Error('Final receipt transport interrupted');return {experiments:Array.from({length:Math.min(limit,25-after)},(_,i)=>receipt(after+i+1)),settled_through:25,next_cursor:after+limit<25?after+limit:null};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();const original=env.root.querySelector('.exp-output pre');
 outage=true;env.root.querySelector('.exp-pager button').click();await tick();await tick();
 assert.equal(env.root.querySelector('.exp-receipt').dataset.sequence,'1');
 assert.equal(env.root.querySelector('.exp-output pre'),original);assert.equal(original.textContent,'Final delivered artifact');
 const warning=env.root.querySelector('[data-delivery-warning]');assert.equal(warning.hidden,false);assert.match(warning.textContent,/last confirmed/i);
 assert.ok(cursors.some(([after,limit])=>after===24&&limit===1));
 outage=false;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-output pre'),original);assert.equal(warning.hidden,true);
});

test('successful empty or mismatched final receipt reads cannot retain or display a false delivery',async t=>{
 let target=receipt(25);const env=setup({getProgram:async()=>({program:program('a',{status:'completed',settled_through:25,queue:[]})}),
 getProgramExperiments:async(id,after,limit)=>({experiments:limit===1?(target?[target]:[]):Array.from({length:Math.min(limit,25-after)},(_,i)=>receipt(after+i+1)),settled_through:25,next_cursor:after+limit<25?after+limit:null})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();env.root.querySelector('.exp-pager button').click();await tick();await tick();
 for(const replacement of [{...receipt(25),program_id:'other',result:'Another owner delivery'},null,
  {...receipt(25),sequence:24,result:'Older final delivery'},{...receipt(25),step,result:'Non-final work'},
  {...receipt(25),outcome:'failed',result:'Rejected final work'}]){
  target=receipt(25);await env.mounted.refresh();assert.match(env.root.querySelector('.exp-output').textContent,/Final delivered artifact/);
  target=replacement;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-output pre'),null);
  assert.equal(env.root.querySelector('[data-delivery-warning]').hidden,false);
 }
});

test('failed tail reads retain confirmed delivery, while a cold failed read displays no invented result',async t=>{
 let outage=false;const overrides={getProgram:async()=>({program:program('a',{status:'completed',settled_through:25,queue:[]})}),
 getProgramExperiments:async(id,after,limit)=>{if(outage)throw Error('Tail transport interrupted');return {experiments:Array.from({length:Math.min(limit,25-after)},(_,i)=>receipt(after+i+1)),settled_through:25,next_cursor:null};}};
 const env=setup(overrides,{path:'evolution/programs/a'});await tick();const original=env.root.querySelector('.exp-output pre');outage=true;await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-output pre'),original);assert.equal(env.root.querySelector('[data-delivery-warning]').hidden,false);env.close();
 const cold=setup(overrides,{path:'evolution/programs/a'});t.after(()=>cold.close());await tick();
 assert.equal(cold.root.querySelector('.exp-output pre'),null);assert.equal(cold.root.querySelector('[data-delivery-warning]').hidden,false);
});

test('inline final candidates require the terminal page identity, kind and completed verdict',async t=>{
 let candidate=receipt(25),pageSequence=25;
 const env=setup({getProgram:async()=>({program:program('a',{status:'completed',settled_through:25,queue:[]})}),
 getProgramExperiments:async()=>({experiments:[candidate],settled_through:pageSequence,next_cursor:null})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.match(env.root.querySelector('.exp-output').textContent,/Final delivered artifact/);
 for(const invalid of [{...receipt(25),program_id:'b'},{...receipt(25),sequence:24},
  {...receipt(25),step},{...receipt(25),outcome:'needs_attention'}]){
  candidate=invalid;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-output pre'),null);
 }
 candidate=receipt(25);pageSequence=26;await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-output pre'),null);
 pageSequence=25;await env.mounted.refresh();assert.match(env.root.querySelector('.exp-output').textContent,/Final delivered artifact/);
});

test('nonterminal snapshots and new program owners cannot inherit a confirmed final delivery',async t=>{
 let status='completed';
 const env=setup({getProgram:async id=>({program:program(id,{status,settled_through:25,queue:[]})}),
 getProgramExperiments:async(id,after,limit)=>{if(id==='b')throw Error('Other program evidence unavailable');return {experiments:[receipt(25)],settled_through:25,next_cursor:null};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.match(env.root.querySelector('.exp-output').textContent,/Final delivered artifact/);
 status='needs_attention';await env.mounted.refresh();assert.equal(env.root.querySelector('.exp-output pre'),null);assert.equal(env.root.querySelector('[data-delivery-warning]').hidden,true);
 status='completed';await env.mounted.refresh();assert.match(env.root.querySelector('.exp-output').textContent,/Final delivered artifact/);
 env.mounted.updateRoute('evolution/programs/b');await tick();assert.equal(env.root.querySelector('.exp-output pre'),null);assert.equal(env.root.querySelector('[data-delivery-warning]').hidden,false);
});

test('a new terminal sequence clears the previous delivery until the matching receipt is available',async t=>{
 let settled=25,outage=false;
 const env=setup({getProgram:async()=>({program:program('a',{status:'completed',settled_through:settled,queue:[]})}),
 getProgramExperiments:async(id,after,limit)=>{if(limit===1&&outage)throw Error('New final temporarily unavailable');return {experiments:limit===1?[{...receipt(settled),step:final,result:'Final '+settled}]:Array.from({length:Math.min(limit,settled-after)},(_,i)=>receipt(after+i+1)),settled_through:settled,next_cursor:after+limit<settled?after+limit:null};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();env.root.querySelector('.exp-pager button').click();await tick();await tick();
 assert.match(env.root.querySelector('.exp-output').textContent,/Final 25/);settled=26;outage=true;await env.mounted.refresh();
 assert.equal(env.root.querySelector('.exp-output pre'),null);assert.equal(env.root.querySelector('[data-delivery-warning]').hidden,false);
 outage=false;await env.mounted.refresh();assert.match(env.root.querySelector('.exp-output').textContent,/Final 26/);
});

test('untrusted stored text is rendered literally; readonly refresh never issues commands',async t=>{
 let writes=0;const hostile='<img src=x onerror="window.evil=true">';
 const env=setup({getProgram:async()=>({program:program('a',{objective:hostile})}),
 getProgramExperiments:async()=>({experiments:[{...receipt(1),step:{...step,title:hostile},result:hostile}],settled_through:1,next_cursor:null}),commandProgram:async()=>{writes++;}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();await env.mounted.refresh();
 const details=env.root.querySelector('.exp-receipt');details.open=true;details.dispatchEvent(new env.dom.window.Event('toggle'));
 assert.match(env.root.textContent,/<img src=x/);assert.equal(env.root.querySelector('img'),null);assert.equal(writes,0);
});

test('paused/cancelled snapshots label actual state instead of promising work is preparing',async t=>{
 const env=setup({getProgram:async()=>({program:program('a',{status:'cancelled'})})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.match(env.root.querySelector('.exp-current').textContent,new RegExp(locale['evolution.program.state.cancelled']));assert.doesNotMatch(env.root.querySelector('.exp-current').textContent,/Preparing/);
});

test('one flight, route fencing, trailing refresh and teardown hold even when abort is ignored',async()=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');
 let finishA,calls=0,visible=true;const states=[],timers=new Map();let nextTimer=0;
 const controller=createProgramController(api({getProgram:id=>{calls++;return id==='a'?new Promise(resolve=>finishA=resolve):Promise.resolve({program:program(id)});}}),
 {update:s=>states.push(s),visible:()=>visible,schedule:(fn,delay)=>{timers.set(++nextTimer,{fn,delay});return nextTimer;},cancel:id=>timers.delete(id)});
 controller.select('a');await tick();const flight=controller.refresh();controller.refresh();assert.equal(calls,1);
 controller.select('b');finishA({program:program('a')});await flight;await tick();
 assert.equal(states.at(-1).program.program_id,'b');assert.equal(calls,2);assert.equal(timers.size,1);
 visible=false;controller.visibilityChanged();assert.equal(timers.size,0);
 visible=true;controller.visibilityChanged();await tick();assert.equal(calls,3);
 const updates=states.length;controller.dispose();assert.equal(timers.size,0);await controller.refresh();assert.equal(states.length,updates);
});

test('post-write refresh fences a successful stale pre-write read',async()=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');let finish,version=0;const states=[];
 const controller=createProgramController(api({getProgram:()=>version?Promise.resolve({program:program('a',{status:'paused'})}):new Promise(resolve=>finish=resolve)}),
 {update:s=>states.push(s),visible:()=>true,schedule:()=>1,cancel:()=>{}});
 controller.select('a');await tick();version=1;const settle=controller.afterWrite();finish({program:program('a')});await settle;await tick();
 assert.equal(states.at(-1).program.status,'paused');assert.equal(states.some(s=>s.program?.status==='active'),false);controller.dispose();
});

test('diagnosis exposes staged alternatives and retains submitted archive without claiming delivery',async t=>{
 const failure={...step,kind:'replan',recovery_of:1,title:'Diagnose experiment 1'};
 const plan={failed_sequence:1,source_run_id:'r',request_id:'tool-call',request_digest:'x'.repeat(64),
 reason:'<script>Inspect observed concurrency failure</script>',steps:[step]};
 const active=program('a',{auto_replan:true,current:{run_id:'r',sequence:2,step:failure},
 queue:[failure,step,final],replan_proposal:plan,settled_through:1});
 const env=setup({getProgram:async()=>({program:active}),getProgramExperiments:async()=>({
 experiments:[{...receipt(1),step:failure,replan:plan}],settled_through:1,next_cursor:null})},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.match(env.root.querySelector('.exp-current').textContent,/Diagnosing archived experiment: 1/);
 assert.match(env.root.querySelector('.exp-current').textContent,/independent diagnosis review before admission/);
 assert.match(env.root.querySelector('.exp-original').textContent,/Automatically diagnose/);
 const row=env.root.querySelector('.exp-receipt');row.open=true;row.dispatchEvent(new env.dom.window.Event('toggle'));await tick();
 assert.match(row.textContent,/Original failed checks/);assert.equal(env.root.querySelector('script'),null);
 assert.notEqual(env.root.querySelector('.exp-output h2').textContent,'Final delivery');
});

test('manual failure policy is explicit and included in ambiguous creation retry identity',async t=>{
 const calls=[];const env=setup({createProgram:async(body,key)=>{calls.push({body,key});throw Error('lost reply');}});
 t.after(()=>env.close());await tick();env.root.querySelector('[name=objective]').value='Explore safely';
 const policy=env.root.querySelector('[name=auto-replan]');assert.equal(policy.value,'auto');
 policy.value='manual';submit(env,'.exp-create-form');await tick();submit(env,'.exp-create-form');await tick();
 assert.equal(calls[0].body.auto_replan,false);assert.deepEqual(calls[0],calls[1]);
 policy.value='auto';submit(env,'.exp-create-form');await tick();
 assert.equal(calls[2].body.auto_replan,true);assert.notEqual(calls[2].key,calls[0].key);
});

test('incomplete episode and run consumption remain visibly unresolved after settlement',async t=>{
 const active=program('a',{actuals_complete:false,current:{run_id:'r',sequence:2,step},settled_through:1});
 const env=setup({getProgram:async()=>({program:active}),getRun:async()=>({run:{...run,actuals_complete:false},pending_questions:[]}),
 getProgramExperiments:async()=>({experiments:[{...receipt(1),actuals_complete:false}],settled_through:1,next_cursor:null})},
 {path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.match(env.root.querySelector('.exp-telemetry').textContent,/≥ 7/);
 assert.match(env.root.querySelector('.exp-current').textContent,/observed lower bounds/);
 const row=env.root.querySelector('.exp-receipt');row.open=true;row.dispatchEvent(new env.dom.window.Event('toggle'));await tick();
 assert.match(row.textContent,/not complete cost measurements/);
});


test('semantic tail matches open their exact original kind and Unicode offset without automatic reads',async t=>{
 for(const kind of ['deliverable','tool_evidence']){
  const text=sourceText+'TAIL counterexample';const ref=sourceRef(text,kind),calls=[];
  const entry={key:'matched-tail',query:'Recall old counterexample',source_kind:kind,scanned_sources:1,source_chars:6000,
   actuals:{tokens:31,tool_calls:0,wall_clock_seconds:1},actuals_complete:true,
   matches:[{candidate_id:'0',quote:'TAIL counterexample',reason:'Unverified relevance',relevance:.9}],
   source_chunks:[{candidate_id:'0',sequence:1,offset:6000,end:12000,source_digest:ref.source_sha256,source_kind:kind,run_id:'archived-run',source_ref:ref}]};
  const env=setup({getProgramMemories:async()=>({memory:{entries:[entry],retained_limit:32}}),
   getStageOutcome:async(id,task,offset,signal,k)=>{calls.push({id,task,offset,kind:k});return {outcome:sourcePage(text,offset,k)};}},{path:'evolution/programs/a'});
  try{
   await tick();assert.equal(calls.length,0);const lookup=env.root.querySelector('[data-semantic-memory] details');lookup.open=true;
   lookup.querySelector('[data-program-source]').click();await tick();assert.deepEqual(calls,[{id:'archived-run',task:'sealed-task',offset:6000,kind}]);
   assert.equal(env.root.querySelector('[data-program-source-reader] pre').textContent,Array.from(text).slice(6000,12000).join(''));
   await env.mounted.refresh();assert.equal(calls.length,1);assert.equal(env.root.querySelector('[data-semantic-memory] details'),lookup);
  }finally{env.close();}
 }
});

const comparisonHypothesis=(mechanism='Tested mechanism')=>({problem:'Observed failure affecting a real workflow',audience:'People handling the failed handoff',
 baseline:'Manual status reconciliation',scenario:'Future unattended multi-day investigations',mechanism,expected_value:'Lower measured reconciliation effort',
 assumptions:['Provider evidence is available','Demand has not been measured'],falsifier:'No improvement against the manual baseline',evidence_refs:['Observed incident, not proof of demand']});
const comparisonStep=(title,mechanism)=>({...step,title,hypothesis:comparisonHypothesis(mechanism)});
const chooseComparison=(env,index,key)=>{const control=env.root.querySelector(`[data-compare-slot="${index}"]`);control.value=key;control.dispatchEvent(new env.dom.window.Event('change',{bubbles:true}));return control;};
const comparisonPanel=(env,index)=>env.root.querySelector(`[data-comparison-slot="${index}"]`);

test('hypothesis comparison distinguishes planned mechanisms and future assumptions without reading or verifying sources',async t=>{
 let reads=0;const current=comparisonStep('Original carrier','Resume the same task'),queued=comparisonStep('Outcome resolution','Ask for provider send evidence');
 const env=setup({getProgram:async()=>({program:program('a',{current:{run_id:'r',sequence:1,step:current},queue:[current,queued,final]})}),getStageOutcome:async()=>{reads++;throw Error('Unrequested source read');}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();const host=env.root.querySelector('[data-hypothesis-comparison]');host.querySelector('details').open=true;
 chooseComparison(env,0,'run:r');const option=[...host.querySelector('[data-compare-slot="1"]').options].find(o=>o.textContent.includes('Outcome resolution'));chooseComparison(env,1,option.value);
 assert.match(comparisonPanel(env,0).textContent,/Resume the same task/);assert.match(comparisonPanel(env,1).textContent,/Ask for provider send evidence/);
 for(const panel of [comparisonPanel(env,0),comparisonPanel(env,1)]){
  for(const key of Object.keys(current.hypothesis))assert.ok(panel.querySelector(`[data-hypothesis-field="${key}"]`));
  assert.match(panel.textContent,/Demand has not been measured/);assert.match(panel.textContent,/No archived experiment review/);
 }
 assert.match(host.textContent,/does not establish demand, novelty or net value/);assert.equal(reads,0);
});

test('comparison retains failed hypotheses and exact independent checks without treating completion as value verification',async t=>{
 const first={...receipt(1),step:comparisonStep('Rejected direction','Already disproved mechanism'),outcome:'failed',review:{outcome:'failure',criterion_results:[],requirement_results:[{requirement_id:'requirement-1',outcome:'fail',evidence_ref:'Observed no improvement <img src=x onerror=alert(1)>'}]}};
 const second={...receipt(2),step:comparisonStep('Candidate direction','Technically feasible mechanism'),review:{outcome:'success',criterion_results:[{criterion_id:'delivery',outcome:'pass',evidence_ref:'Prototype runs; demand unknown'}],requirement_results:[{requirement_id:'requirement-1',outcome:'unknown',evidence_ref:'No consumer measurement'}]}};
 const env=setup({getProgramExperiments:async()=>({experiments:[first,second],settled_through:2,next_cursor:null})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 chooseComparison(env,0,'run:run-1');chooseComparison(env,1,'run:run-2');
 assert.match(comparisonPanel(env,0).textContent,/failed/i);assert.match(comparisonPanel(env,0).textContent,/Observed no improvement <img/);
 assert.equal(comparisonPanel(env,0).querySelector('img'),null);assert.match(comparisonPanel(env,1).textContent,/Prototype runs; demand unknown/);
 assert.match(comparisonPanel(env,1).textContent,/No consumer measurement/);assert.match(comparisonPanel(env,1).textContent,/assess the recorded experiment checks/);
});

test('two selected snapshots survive history paging and their explicit original opener keeps the frozen owner reference',async t=>{
 const first={...sourcedReceipt(),sequence:1,run_id:'run-1',step:comparisonStep('First measured direction','First mechanism')};
 const next={...receipt(21),step:comparisonStep('Older alternate','Second mechanism')};const reads=[];
 const env=setup({getProgram:async()=>({program:program('a',{settled_through:30})}),getProgramExperiments:async(id,after)=>({experiments:after>=20?[next]:[first],settled_through:30,next_cursor:after>=20?null:20}),getStageOutcome:async(id,task,offset,signal,kind)=>{reads.push({id,task,offset,kind});return {outcome:sourcePage(sourceText,offset,kind)};}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();chooseComparison(env,0,'run:run-1');const original=comparisonPanel(env,0).querySelector('[data-hypothesis-field=mechanism] p');
 env.root.querySelector('.exp-pager .ui-btn:nth-child(2)').click();await tick();chooseComparison(env,1,'run:run-21');
 assert.equal(comparisonPanel(env,0).querySelector('[data-hypothesis-field=mechanism] p'),original);assert.equal(original.textContent,'First mechanism');
 assert.match(comparisonPanel(env,0).textContent,/latest state has not been read here/);assert.match(comparisonPanel(env,1).textContent,/Second mechanism/);assert.equal(reads.length,0);
 comparisonPanel(env,0).querySelector('[data-program-source=deliverable]').click();await tick();assert.deepEqual(reads,[{id:'run-1',task:'sealed-task',offset:0,kind:'deliverable'}]);
});

test('comparison keeps selectors, disclosure, focus and selected evidence intact through unchanged polling and read failure',async t=>{
 let fail=false;const observed={...receipt(1),step:comparisonStep('Observed direction','Exact observed mechanism')};
 const env=setup({getProgramExperiments:async()=>{if(fail)throw Error('Controlled history outage');return {experiments:[observed],settled_through:1,next_cursor:null};}},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const select=chooseComparison(env,0,'run:run-1'),host=env.root.querySelector('[data-hypothesis-comparison]');host.querySelector('details').open=true;
 const panel=comparisonPanel(env,0),field=panel.querySelector('[data-hypothesis-field=mechanism] p');panel.querySelector('details').open=true;select.focus();
 const range=document.createRange();range.selectNodeContents(field);window.getSelection().removeAllRanges();window.getSelection().addRange(range);
 await env.mounted.refresh();fail=true;await env.mounted.refresh();
 assert.equal(env.root.querySelector('[data-compare-slot="0"]'),select);assert.equal(document.activeElement,select);
 assert.equal(comparisonPanel(env,0),panel);assert.equal(panel.querySelector('[data-hypothesis-field=mechanism] p'),field);assert.equal(window.getSelection().toString(),'Exact observed mechanism');
 assert.equal(host.querySelector('details').open,true);assert.equal(panel.querySelector('details').open,true);assert.match(env.root.querySelector('.exp-error').textContent,/Controlled history outage/);
});

test('current experiment selection follows its exact run into the archived review without a new hypothesis identity',async t=>{
 let archived=false;const observed=comparisonStep('Tracked direction','Preserved original mechanism');
 const env=setup({getProgram:async()=>({program:program('a',archived?{current:null,settled_through:1}:{current:{sequence:1,run_id:'r',step:observed},queue:[observed,final]})}),getProgramExperiments:async()=>({experiments:archived?[{...receipt(1),run_id:'r',step:observed,review:{outcome:'failure',criterion_results:[],requirement_results:[{requirement_id:'requirement-1',outcome:'fail',evidence_ref:'Original mechanism was falsified'}]}}]:[],settled_through:archived?1:0,next_cursor:null})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 chooseComparison(env,0,'run:r');const field=comparisonPanel(env,0).querySelector('[data-hypothesis-field=mechanism] p');archived=true;await env.mounted.refresh();
 assert.equal(env.root.querySelector('[data-compare-slot="0"]').value,'run:r');assert.equal(comparisonPanel(env,0).querySelector('[data-hypothesis-field=mechanism] p'),field);
 assert.match(comparisonPanel(env,0).textContent,/Archived experiment 1/);assert.match(comparisonPanel(env,0).textContent,/Original mechanism was falsified/);
});

test('comparison clears owner snapshots and stale controls cannot read originals after navigation or disposal',async t=>{
 const first={...sourcedReceipt(),step:comparisonStep('Private owner claim','Private mechanism')};let reads=0;
 const env=setup({getProgramExperiments:async(id)=>({experiments:id==='a'?[first]:[],settled_through:id==='a'?25:0,next_cursor:null}),getStageOutcome:async()=>{reads++;throw Error('Stale control read');}},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 chooseComparison(env,0,'run:run-25');const old=comparisonPanel(env,0).querySelector('[data-program-source=deliverable]');env.mounted.updateRoute('evolution/programs/b');old.click();await tick();
 assert.doesNotMatch(env.root.querySelector('[data-hypothesis-comparison]').textContent,/Private owner claim|Private mechanism/);assert.equal(env.root.querySelector('[data-compare-slot="0"]').value,'');assert.equal(reads,0);
 env.close();old.click();assert.equal(reads,0);
});

test('comparison marks duplicate directions and queued reorder does not silently change the selected hypothesis',async t=>{
 let reordered=false;const first=comparisonStep('First plan','First mechanism'),second=comparisonStep('Second plan','Second mechanism');
 const env=setup({getProgram:async()=>({program:program('a',{queue:reordered?[second,first,final]:[first,second,final]})})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 const key=[...env.root.querySelector('[data-compare-slot="0"]').options].find(o=>o.textContent.includes('First plan')).value;
 chooseComparison(env,0,key);chooseComparison(env,1,key);assert.match(env.root.querySelector('[data-hypothesis-comparison] [role=status]').textContent,/same record/);
 reordered=true;await env.mounted.refresh();assert.equal(env.root.querySelector('[data-compare-slot="0"]').value,key);assert.match(comparisonPanel(env,0).textContent,/First mechanism/);
});

test('comparison retains only two selected historical records while loading many later pages',async t=>{
 let page=0;const env=setup({getProgram:async()=>({program:program('a',{settled_through:1000})}),getProgramExperiments:async()=>({experiments:Array.from({length:20},(_,index)=>({...receipt(page*20+index+1),step:comparisonStep('Direction '+(page*20+index+1),'Mechanism '+(page*20+index+1))})),settled_through:1000,next_cursor:null})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 chooseComparison(env,0,'run:run-1');chooseComparison(env,1,'run:run-2');
 for(page=1;page<=12;page++){
  await env.mounted.refresh();assert.equal(env.root.querySelector('[data-compare-slot="0"]').options.length,23);
  assert.equal(env.root.querySelector('[data-compare-slot="1"]').options.length,23);
 }
 assert.equal(env.root.querySelectorAll('[data-comparison-slot]').length,2);assert.match(comparisonPanel(env,0).textContent,/Mechanism 1/);assert.match(comparisonPanel(env,1).textContent,/Mechanism 2/);
});

const hypothesisAssessment=(hypothesis,extra={})=>({hypothesis_sha256:crypto.createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.keys(hypothesis).sort().map(key=>[key,hypothesis[key]])))).digest('hex'),
 problem:{outcome:'unknown',evidence_ref:'No inspected consumer demand.'},feasibility:{outcome:'pass',evidence_ref:'Observed controlled prototype execution.'},
 novelty:{outcome:'unknown',evidence_ref:'Comparison sources missing.'},net_value:{outcome:'unknown',evidence_ref:'Adoption effort unmeasured.'},...extra});
const assessmentSettled=async env=>{for(let i=0;i<100;i++){await tick();if(!env.root.textContent.includes('Review results are being confirmed.'))return;}throw Error('Assessment remained pending');};
test('hypothesis comparison keeps feasible prototype separate from unknown demand novelty and net value',async t=>{
 const proposed=comparisonStep('Emerging workflow','Tested future mechanism');
 const item={...receipt(1),step:proposed,review:{outcome:'success',criterion_results:[],hypothesis_assessment:hypothesisAssessment(proposed.hypothesis)}};
 const env=setup({getProgram:async()=>({program:program('a',{settled_through:1})}),getProgramExperiments:async()=>({experiments:[item],settled_through:1,next_cursor:null})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();chooseComparison(env,0,'run:run-1');await assessmentSettled(env);
 const scope=comparisonPanel(env,0).querySelector('[data-hypothesis-assessment]');
 assert.match(scope.querySelector('[data-assessment-axis="feasibility"]').textContent,/Pass/);
 for(const key of ['problem','novelty','net_value'])assert.match(scope.querySelector(`[data-assessment-axis="${key}"]`).textContent,/unknown/i);
 assert.match(scope.textContent,/Technical success alone does not establish demand/);assert.match(scope.textContent,/No inspected consumer demand/);
});

test('foreign hypothesis assessment never displays its positive evidence',async t=>{
 const proposed=comparisonStep('Foreign review','Original mechanism');
 const item={...receipt(1),step:proposed,review:{outcome:'success',criterion_results:[],hypothesis_assessment:hypothesisAssessment(proposed.hypothesis,{hypothesis_sha256:'0'.repeat(64),problem:{outcome:'pass',evidence_ref:'Fabricated demand receipt'}})}};
 const env=setup({getProgram:async()=>({program:program('a',{settled_through:1})}),getProgramExperiments:async()=>({experiments:[item],settled_through:1,next_cursor:null})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();chooseComparison(env,0,'run:run-1');await assessmentSettled(env);
 const scope=comparisonPanel(env,0).querySelector('[data-hypothesis-assessment]');assert.match(scope.textContent,/does not match/);assert.doesNotMatch(scope.textContent,/Fabricated demand receipt/);
});

test('assessment text remains inert and legacy receipts retain unknown axes',async t=>{
 const proposed=comparisonStep('Safe evidence','Original mechanism');
 const item={...receipt(1),step:proposed,review:{outcome:'success',criterion_results:[],hypothesis_assessment:hypothesisAssessment(proposed.hypothesis,{problem:{outcome:'unknown',evidence_ref:'<img src=x onerror=alert(1)> source report'}})}};
 let legacy=false;const env=setup({getProgram:async()=>({program:program('a',{settled_through:1})}),getProgramExperiments:async()=>({experiments:[legacy?{...item,review:null}:item],settled_through:1,next_cursor:null})},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();chooseComparison(env,0,'run:run-1');await assessmentSettled(env);
 const scope=comparisonPanel(env,0).querySelector('[data-hypothesis-assessment]');assert.match(scope.textContent,/<img src=x/);assert.equal(scope.querySelector('img'),null);
 legacy=true;await env.mounted.refresh();await tick();assert.match(scope.textContent,/No confirmed assessment/);assert.doesNotMatch(scope.textContent,/source report/);
});

test('late assessment identity checks cannot repaint a changed selection or disposed owner',async t=>{
 const env=setup();t.after(()=>env.close());await tick();const host=document.createElement('div');env.root.append(host);
 const component=load('features/evolution/programs/hypothesis-assessment.ts').mountHypothesisAssessment(host,key=>locale['evolution.program.'+key]);
 const descriptor=Object.getOwnPropertyDescriptor(globalThis,'crypto'),pending=[];
 Object.defineProperty(globalThis,'crypto',{configurable:true,value:{subtle:{digest:(_algorithm,data)=>new Promise(resolve=>pending.push({resolve,data}))}}});
 t.after(()=>{component.dispose();if(descriptor)Object.defineProperty(globalThis,'crypto',descriptor);else delete globalThis.crypto;});
 const first=comparisonHypothesis('First mechanism'),second=comparisonHypothesis('Second mechanism');
 const old=component.update(first,hypothesisAssessment(first,{feasibility:{outcome:'pass',evidence_ref:'Old selected review'}}));
 const next=component.update(second,hypothesisAssessment(second,{feasibility:{outcome:'pass',evidence_ref:'Current selected review'}}));
 pending[0].resolve(crypto.createHash('sha256').update(pending[0].data).digest());await old;assert.doesNotMatch(host.textContent,/Old selected review/);
 pending[1].resolve(crypto.createHash('sha256').update(pending[1].data).digest());await next;assert.match(host.textContent,/Current selected review/);
 const late=component.update(first,hypothesisAssessment(first));component.dispose();pending[2].resolve(crypto.createHash('sha256').update(pending[2].data).digest());await late;assert.equal(host.textContent,'');
});


test('confirmed budget blockers disable retries with readable reasons and preserve cancellation',async t=>{
 const cases=[
  [{max_tokens:7},true,'limitTokens'],
  [{max_tokens:100},false,'limitUnknown'],
  [{max_tool_calls:1},true,'limitTools'],
  [{max_tool_calls:100},false,'policyRemainingUnknown'],
  [{max_wall_clock_seconds:10},true,'limitTime'],
 ];
 for(const [budget,complete,key] of cases){
  let calls=0;const env=setup({getProgram:async()=>({program:program('a',{status:'needs_attention',actuals_complete:complete,
   updated_at:11,execution_options:{budget}})}),commandProgram:async()=>{calls++;}},{path:'evolution/programs/a'});
  await tick();
  const retry=env.root.querySelector('[data-program-action="retry"]');
  assert.equal(retry.disabled,true);assert.equal(retry.hidden,false);
  assert.equal(retry.title,locale['evolution.program.'+key]);
  assert.equal(env.root.querySelector('.exp-attention [role="status"]').textContent,retry.title);
  assert.equal(env.root.querySelector('[data-program-action="cancel"]').disabled,false);
  retry.click();assert.equal(calls,0);env.close();
 }
});

test('unlimited and incomplete usage do not invent a retry blocker',async t=>{
 const env=setup({getProgram:async()=>({program:program('a',{status:'needs_attention',actuals_complete:false,
  execution_options:{budget:{max_tokens:0,max_tool_calls:0,max_wall_clock_seconds:0}}})})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelector('[data-program-action="retry"]').disabled,false);
});

test('confirmed clock limits disable start but a stale browser clock grants no authority',async t=>{
 const limits=load('features/evolution/programs/limits.ts');
 assert.equal(limits.programLimitHint(program('a',{execution_options:{budget:{max_wall_clock_seconds:10}},updated_at:9})),null);
 const env=setup({getProgram:async()=>({program:program('a',{status:'paused',updated_at:11,
  execution_options:{budget:{max_wall_clock_seconds:10}}})})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelector('[data-program-action="start"]').disabled,true);
 assert.equal(env.root.querySelector('.exp-attention').hidden,false);
});


test('resuming a committed current experiment preserves the frozen admission',async t=>{
 const env=setup({getProgram:async()=>({program:program('a',{status:'paused',actuals_complete:false,
  current:{run_id:'r',sequence:1,step},execution_options:{budget:{max_tokens:100}}})})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelector('[data-program-action="start"]').disabled,false);
});


for(const policy of ['auto','manual'])test('successful creation preserves the chosen failure policy for the next goal: '+policy,async t=>{
 const bodies=[];let count=0;
 const env=setup({createProgram:async body=>{bodies.push(body);return {program:program('created-'+(++count))};},
  getProgram:async id=>({program:program(id,{status:'ready'})}),commandProgram:async()=>({})});
 t.after(()=>env.close());await tick();
 env.root.querySelector('[name=auto-replan]').value=policy;
 edit(env,'objective','First controlled objective');submit(env,'.exp-create-form');await tick();await tick();
 assert.equal(env.navigations[0],'evolution/programs/created-1');
 env.mounted.updateRoute('evolution/programs/created-1');await tick();
 env.mounted.updateRoute('evolution/programs/new');await tick();
 assert.equal(env.root.querySelector('[name=objective]').value,'');
 assert.equal(env.root.querySelector('[name=auto-replan]').value,policy);
 edit(env,'objective','Second controlled objective');submit(env,'.exp-create-form');await tick();await tick();
 assert.equal(bodies.length,2);assert.deepEqual(bodies.map(body=>body.auto_replan),[policy==='auto',policy==='auto']);
});


for (const [dimension, cap, reserve, consumed] of [
 ['tokens',600000,25000,580915], ['tokens',100,20,80],
 ['tool_calls',100,20,80], ['wall_clock_seconds',100,20,80],
]) test('frozen review reserve blocks an unaffordable retry: '+dimension+'/'+cap,async t=>{
 let calls=0;
 const actuals={tokens:0,tool_calls:0,wall_clock_seconds:0};actuals[dimension]=consumed;
 const env=setup({getProgram:async()=>({program:program('a',{status:'needs_attention',actuals,
  updated_at:dimension==='wall_clock_seconds'?1+consumed:1,
  execution_options:{budget:{['max_'+dimension]:cap},review_reserve:{[dimension]:reserve}}})}),
  commandProgram:async()=>{calls++;}},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();
 const retry=env.root.querySelector('[data-program-action="retry"]');assert.equal(retry.disabled,true);
 assert.equal(retry.title,locale['evolution.program.limitReviewReserve']);
 assert.equal(env.root.querySelector('.exp-attention [role="status"]').textContent,retry.title);
 assert.equal(env.root.querySelector('[data-program-action="cancel"]').disabled,false);
 retry.click();assert.equal(calls,0);
});

test('frozen review reserve keeps one unit of execution available',()=>{
 const limits=load('features/evolution/programs/limits.ts');
 for (const dimension of ['tokens','tool_calls','wall_clock_seconds']) {
  const actuals={tokens:0,tool_calls:0,wall_clock_seconds:0};actuals[dimension]=79;
  assert.equal(limits.programLimitHint(program('a',{actuals,updated_at:dimension==='wall_clock_seconds'?80:1,
   execution_options:{budget:{['max_'+dimension]:100},review_reserve:{[dimension]:20}}})),null);
 }
});


test('frozen review reserve does not block resuming a committed current intent',async t=>{
 const env=setup({getProgram:async()=>({program:program('a',{status:'paused',actuals:{tokens:580915,tool_calls:19,wall_clock_seconds:240},
  current:{run_id:'r',sequence:1,step},execution_options:{budget:{max_tokens:600000},review_reserve:{tokens:25000}}})})},{path:'evolution/programs/a'});
 t.after(()=>env.close());await tick();assert.equal(env.root.querySelector('[data-program-action="start"]').disabled,false);
});


test('cancelled exploration keeps prompt refresh until consumption settles and tears down its timer',async()=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');
 const current={sequence:1,step,payload:{objective:'Preserve the original mission.'},context_seed:'',run_id:'r',deadline_at:null};
 let settled=false,next=0;const timers=new Map(),states=[];
 const controller=createProgramController(api({
  listPrograms:async()=>({programs:[program('a',{status:'cancelled'})]}),
  getProgram:async()=>({program:program('a',{status:'cancelled',current:settled?null:current,
   actuals:{tokens:settled?30:15,tool_calls:0,wall_clock_seconds:1},actuals_complete:settled})})
 }),{update:state=>states.push(state),visible:()=>true,
  schedule:(fn,delay)=>{timers.set(++next,{fn,delay});return next;},cancel:id=>timers.delete(id)});
 controller.select('a');await tick();await tick();
 assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,5000);
 assert.equal(states.at(-1).program.actuals.tokens,15);assert.equal(states.at(-1).program.actuals_complete,false);
 settled=true;const [id,pending]=[...timers.entries()][0];timers.delete(id);pending.fn();await tick();await tick();
 assert.equal(states.at(-1).program.current,null);assert.equal(states.at(-1).program.actuals.tokens,30);
 assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,30000);
 controller.dispose();assert.equal(timers.size,0);
});


test('unknown tool counts block only finite tool allowance while known tools tolerate unknown tokens',()=>{
 const limits=load('features/evolution/programs/limits.ts');
 const base={actuals_complete:false,execution_options:{budget:{max_tokens:0,max_tool_calls:100}}};
 assert.equal(limits.programLimitHint(program('a',{...base,tool_calls_complete:false})),'policyRemainingUnknown');
 assert.equal(limits.programLimitHint(program('a',{...base,tool_calls_complete:true})),null);
 assert.equal(limits.programLimitHint(program('a',base)),'policyRemainingUnknown');
 assert.equal(limits.programLimitHint(program('a',{...base,tool_calls_complete:false,execution_options:{budget:{max_tool_calls:0}}})),null);
});


test('saving a checkpoint plan persists the immutable request but never starts execution',async t=>{
 const calls=[];const env=setup({createProgram:async(body,key)=>{calls.push({body,key});return {program:program('prepared',{status:'ready',started_at:null})};},
  getProgram:async()=>({program:program('prepared',{status:'ready',started_at:null})}),commandProgram:async()=>assert.fail('Saving cannot start a run')});
 t.after(()=>env.close());await tick();edit(env,'objective','Prepare a multi-day experimental protocol');edit(env,'advance-mode','checkpoint');
 [...env.root.querySelectorAll('button')].find(button=>button.textContent===locale['evolution.program.savePlan']).click();await tick();await tick();
 assert.equal(calls.length,1);assert.equal(calls[0].body.advance_mode,'checkpoint');assert.deepEqual(env.navigations,['evolution/programs/prepared']);
});

test('checkpoint preparation survives ambiguous save and reload without automatic execution',async t=>{
 const calls=[],storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};
 const overrides={createProgram:async(body,key)=>{calls.push({body,key});throw Error('Lost ACK');},commandProgram:async()=>assert.fail('No start allowed')};
 const first=setup(overrides,ports);await tick();edit(first,'objective','Prepare without spending');edit(first,'advance-mode','checkpoint');
 [...first.root.querySelectorAll('button')].find(button=>button.textContent===locale['evolution.program.savePlan']).click();await tick();first.close();
 const second=setup(overrides,ports);t.after(()=>second.close());await tick();assert.equal(calls.length,1);assert.equal(second.root.querySelector('[name=advance-mode]').value,'checkpoint');
 [...second.root.querySelectorAll('button')].find(button=>button.textContent===locale['evolution.program.savePlan']).click();await tick();assert.deepEqual(calls[0],calls[1]);
});

test('pacing only changes explicit advancement permission and preserves protocol focus through polling',async t=>{
 let current=program('a',{status:'paused',advance_mode:'checkpoint'});const calls=[];
 const env=setup({getProgram:async()=>({program:current}),setProgramPacing:async(id,mode)=>{calls.push([id,mode]);current={...current,advance_mode:mode};},
  commandProgram:async()=>assert.fail('Pacing must not resume execution')},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();await tick();
 const protocol=env.root.querySelector('[data-protocol]');protocol.open=true;protocol.querySelector('summary').focus();
 assert.ok(protocol.textContent.includes(step.instruction));assert.ok(protocol.textContent.includes(step.checks[0]));
 env.root.querySelector('[data-program-pacing]').click();await tick();await tick();assert.deepEqual(calls,[['a','continuous']]);
 assert.equal(env.root.querySelector('[data-protocol]'),protocol);assert.equal(protocol.open,true);assert.equal(document.activeElement,protocol.querySelector('summary'));
 assert.ok(env.root.querySelector('[data-next-action]').textContent.includes('Paused at an experiment boundary'));
 assert.ok(env.root.querySelector('[data-pacing-status]').textContent.includes('Continuous exploration'));
});

test('unstarted preparation does not spend wall-clock allowance or review reserve',()=>{
 const limit=load('features/evolution/programs/limits.ts').programLimitHint;
 const prepared=program('a',{status:'ready',created_at:1,updated_at:100000,started_at:null,
  execution_options:{budget:{max_wall_clock_seconds:100},review_reserve:{wall_clock_seconds:99}}});
 assert.equal(limit(prepared),null);assert.equal(limit({...prepared,started_at:1}),'limitTime');
 const legacy={...prepared};delete legacy.started_at;assert.equal(limit(legacy),'limitTime');
});

for(const status of ['paused','needs_attention','completed','cancelled'])test(`lost initial start ACK restored after ${status} never grants another activation`,async()=>{
 const storage=draftStorage(),ports={draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'};
 let creates=0,starts=0,snapshot=program('saved',{status:'ready',started_at:null,advance_mode:'checkpoint'});
 const overrides={createProgram:async()=>{creates++;return {program:snapshot};},getProgram:async()=>({program:snapshot}),
  commandProgram:async()=>{starts++;snapshot={...snapshot,status:'active',started_at:10};throw Error('Lost initial start ACK');}};
 const first=setup(overrides,ports);await tick();edit(first,'objective','Review every experiment before continuing');
 first.root.querySelector('[name=advance-mode]').value='checkpoint';submit(first,'.exp-create-form');await tick();
 assert.equal(starts,1);first.close();snapshot={...snapshot,status,settled_through:1};
 const restored=setup(overrides,ports);try{await tick();submit(restored,'.exp-create-form');await tick();
  assert.equal(creates,1);assert.equal(starts,1);assert.deepEqual(restored.navigations,['evolution/programs/saved']);
 }finally{restored.close();}
});

for (const [name, snapshot, expected] of [
 ['prepared', {status:'ready',started_at:null}, 'start'],
 ['paused before activation', {status:'paused',started_at:null}, 'start'],
 ['checkpoint', {status:'paused',started_at:10,settled_through:1}, 'resume'],
 ['paused running intent', {status:'paused',started_at:10,current:{sequence:1,step,run_id:'r'}}, 'resume'],
 ['legacy paused intent', {status:'paused',current:{sequence:1,step,run_id:'r'}}, 'resume'],
 ['legacy checkpoint', {status:'paused',advance_mode:'checkpoint',settled_through:1}, 'resume'],
 ['legacy ambiguous pause', {status:'paused'}, 'resume'],
 ['legacy preparation', {status:'ready',settled_through:0,current:null}, 'start'],
]) test('explicit continuation uses the correct activation boundary: '+name,async t=>{
 const calls=[];let current=program('a',snapshot);
 const env=setup({getProgram:async()=>({program:current}),commandProgram:async(id,action)=>{
  calls.push([id,action]);current={...current,status:'active'};return {program:current};
 }},{path:'evolution/programs/a'});t.after(()=>env.close());await tick();
 assert.deepEqual(calls,[]);const control=env.root.querySelector('[data-program-action=start]');assert.equal(control.hidden,false);
 control.click();await tick();assert.deepEqual(calls,[['a',expected]]);
 assert.equal(env.root.querySelector('[data-program-action=pause]').hidden,false);
});

test('current experiment rejects prior run status usage and task identity', t=>{
 const dom=new JSDOM('<section id="current"></section>',{pretendToBeVisual:true});
 Object.assign(global,{window:dom.window,document:dom.window.document});
 const host=document.getElementById('current'),opened=[];
 const view=load('features/evolution/programs/current-experiment.ts').mountCurrentExperiment(host,
  {copy:key=>key,hypothesis:()=>null,replan:()=>document.createElement('details'),openTask:id=>opened.push(id)});
 t.after(()=>{view.dispose();dom.window.close();});
 const active=program('a',{current:{sequence:2,run_id:'new-run',step}});
 view.update(active,{run_id:'old-run',status:'completed',task_id:'old-task',actuals_complete:false});
 assert.match(host.textContent,/preparing/);assert.doesNotMatch(host.textContent,/run.completed|usageIncomplete/);
 assert.equal(host.querySelector('button').hidden,true);
 view.update(active,{run_id:'new-run',status:'reviewing',task_id:'review-task',actuals_complete:false});
 assert.match(host.textContent,/run.reviewing/);assert.match(host.textContent,/usageIncomplete/);
 host.querySelector('button').click();assert.deepEqual(opened,['review-task']);
 view.update(active,{run_id:'old-run',status:'completed',task_id:'old-task'},'Read failed');
 assert.match(host.textContent,/runUnavailable/);assert.doesNotMatch(host.textContent,/run.completed|runRetained/);
 assert.equal(host.querySelector('button').hidden,true);
});

test('current failure details update within one run and clear on another identity', t=>{
 const dom=new JSDOM('<section id="current"></section>',{pretendToBeVisual:true});
 Object.assign(global,{window:dom.window,document:dom.window.document});
 const host=document.getElementById('current');
 const view=load('features/evolution/programs/current-experiment.ts').mountCurrentExperiment(host,
  {copy:key=>key,hypothesis:()=>null,replan:()=>document.createElement('details'),openTask:()=>{}});
 t.after(()=>{view.dispose();dom.window.close();});
 const active=program('a',{current:{sequence:1,run_id:'r',step}});
 const run={run_id:'r',status:'needs_attention',task_id:'task',error:'First failure'};
 view.update(active,run);const detail=host.querySelector('.exp-current-error');detail.open=true;
 const summary=detail.querySelector('summary');summary.focus();
 view.update(active,{...run,error:'<img src=x onerror=alert(1)> changed failure'});
 assert.equal(host.querySelector('.exp-current-error').open,true);
 assert.equal(document.activeElement,summary);
 assert.match(host.querySelector('pre').textContent,/changed failure/);
 assert.equal(host.querySelector('img'),null);
 view.update(active,{...run,error:''});assert.equal(host.querySelector('.exp-current-error'),null);
 view.update(program('b',{current:{sequence:1,run_id:'next',step}}),run);
 assert.equal(host.querySelector('.exp-current-error'),null);
});

test('selected detail launches while catalogs are unresolved in the same flight', async t=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');
 let releaseCatalog;const details=[],states=[];
 const controller=createProgramController(api({listPrograms:()=>new Promise(resolve=>releaseCatalog=resolve),
  getProgram:async(id,signal)=>{details.push({id,signal});return {program:program(id)};}}),
  {update:s=>states.push(s),visible:()=>true,schedule:()=>1,cancel:()=>{}});
 t.after(()=>controller.dispose());
 controller.select('a');await tick();const flight=controller.refresh();
 assert.deepEqual(details.map(d=>d.id),['a']);assert.equal(states.at(-1).loading,true);
 controller.select('b');assert.equal(details[0].signal.aborted,true);
 releaseCatalog({programs:[program('stale')]});await flight;await tick();
 assert.deepEqual(details.map(d=>d.id),['a','b']);
 assert.equal(states.some(s=>s.program?.program_id==='a'),false);
 const final=controller.refresh();releaseCatalog({programs:[program('b')]});await final;
 assert.equal(states.at(-1).program.program_id,'b');
});

test('parallel detail synchronous failure retains confirmed state and reports the error',async t=>{
 const {createProgramController}=load('features/evolution/programs/controller.ts');let fails=false;const states=[];
 const controller=createProgramController(api({getProgram:id=>{if(fails)throw Error('Detail failed synchronously');return Promise.resolve({program:program(id)});}}),
  {update:s=>states.push(s),visible:()=>true,schedule:()=>1,cancel:()=>{}});
 t.after(()=>controller.dispose());controller.select('a');await controller.refresh();
 fails=true;await controller.refresh();assert.equal(states.at(-1).program.program_id,'a');
 assert.ok(states.at(-1).errors.includes('Detail failed synchronously'));
});


test('research intake restores an isolated source draft and includes its reference on save',async t=>{
 const storage=draftStorage(),drafts=load('features/evolution/programs/drafts.ts');
 const store=drafts.createProgramDraftStore({owner:()=>7,scope:'/app/',storage:()=>storage});
 store.save(['create'],{fields:['Existing unrelated mission','','','','auto']});
 const source={task_id:'original-record',idea_id:'selected-idea'},route=drafts.researchProgrammePath(source);
 store.save(drafts.programmeCreationDraftIdentity(route),{fields:[],researchSource:source,seedObjective:'Grounded research mission'});
 const calls=[];
 const env=setup({createProgram:async(body)=>{calls.push(body);return {program:program('new-source',{status:'ready'})};},
  getProgram:async()=>({program:program('new-source',{status:'ready',started_at:null})}),commandProgram:async()=>assert.fail('Saving a plan must not launch')},
  {path:route,draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'});t.after(()=>env.close());await tick();await tick();
 assert.equal(env.root.querySelector('[name=objective]').value,'Grounded research mission');
 assert.equal(env.root.querySelector('[data-research-source-note]').hidden,false);
 [...env.root.querySelectorAll('button')].find(button=>button.textContent===locale['evolution.program.savePlan']).click();await tick();await tick();
 assert.deepEqual(calls[0].research_source,source);
 assert.equal(store.read(['create']).fields[0],'Existing unrelated mission');
 env.mounted.updateRoute('evolution/programs/new');await tick();
 assert.equal(env.root.querySelector('[name=objective]').value,'Existing unrelated mission');
 assert.equal(env.root.querySelector('[data-research-source-note]').hidden,true);
});


test('source creation acknowledgment stays with submitted draft after intake route changes',async t=>{
 const storage=draftStorage(),drafts=load('features/evolution/programs/drafts.ts');
 const store=drafts.createProgramDraftStore({owner:()=>7,scope:'/app/',storage:()=>storage});
 const a={task_id:'source-a',idea_id:'idea-a'},b={task_id:'source-b',idea_id:'idea-b'};
 for(const [source,objective] of [[a,'Original A'],[b,'Original B']]) store.save(drafts.programmeCreationDraftIdentity(drafts.researchProgrammePath(source)),{fields:[],researchSource:source,seedObjective:objective});
 let resolve;const saving=new Promise(done=>resolve=done);
 const env=setup({createProgram:async()=>saving,getProgram:async()=>({program:program('created-a',{status:'ready',started_at:null})})},
  {path:drafts.researchProgrammePath(a),draftStorage:storage,draftOwner:()=>7,draftScope:'/app/'});t.after(()=>env.close());await tick();await tick();
 [...env.root.querySelectorAll('button')].find(button=>button.textContent===locale['evolution.program.savePlan']).click();await tick();
 env.mounted.updateRoute(drafts.researchProgrammePath(b));await tick();
 resolve({program:program('created-a',{status:'ready'})});await tick();await tick();
 assert.equal(env.root.querySelector('[name=objective]').value,'Original B');
 assert.equal(store.read(drafts.programmeCreationDraftIdentity(drafts.researchProgrammePath(a))).attempt.createdId,'created-a');
 assert.deepEqual(store.read(drafts.programmeCreationDraftIdentity(drafts.researchProgrammePath(b))).researchSource,b);
 assert.deepEqual(env.navigations,[]);
});
