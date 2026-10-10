const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const ts=require('typescript');
const {JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
function load(file, cache=new Map()){
 const full=path.resolve(source,file);if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>name.endsWith('/i18n')?{t:key=>locale[key]||key}:load(path.relative(source,path.resolve(path.dirname(full),name+'.ts')),cache),exports);return exports;
}
function api(overrides={}){return {
 listGoals:async()=>({goals:[]}),listPlans:async()=>({plans:[]}),listEpisodes:async()=>({episodes:[]}),listMutations:async()=>({mutations:[]}),listCapabilityGaps:async()=>({capability_gaps:[]}),listSkills:async()=>({skills:[]}),snapshot:async()=>({snapshot:{strategies:0,anti_patterns:0}}),...overrides};}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function dom(){const d=new JSDOM('<div id="host"></div>',{url:'http://localhost/#/evolution',pretendToBeVisual:true});Object.assign(global,{window:d.window,document:d.window.document,FormData:d.window.FormData,Element:d.window.Element});return d;}
test('refresh is single-flight, four-worker bounded, hidden-paused and disposed',async()=>{
 const {createEvolutionController}=load('controller.ts');let running=0,peak=0,calls=0,release;const gate=new Promise(r=>release=r);let visible=true,timers=0,updates=0;const signals=[];
 const a=api();for(const key of Object.keys(a)){const original=a[key];a[key]=async(...args)=>{signals.push(args.find(v=>v instanceof AbortSignal));calls++;running++;peak=Math.max(peak,running);await gate;running--;return original();};}
 const c=createEvolutionController(a,{update:()=>updates++,visible:()=>visible,schedule:()=>++timers,cancel:()=>timers--});
 const first=c.refresh();assert.equal(first,c.refresh());await tick();assert.equal(calls,4);assert.equal(peak,4);release();await first;assert.equal(calls,7);assert.equal(updates,1);assert.equal(timers,1);
 visible=false;c.visibilityChanged();assert.equal(timers,0);await c.refresh();assert.equal(calls,7);c.dispose();assert.ok(signals.filter(Boolean).every(s=>s.aborted));await c.refresh();assert.equal(calls,7);
});
test('partial failures preserve the last successful projection',async()=>{
 const {createEvolutionController}=load('controller.ts');let fail=false,last,errors;
 const a=api({listGoals:async()=>{if(fail)throw Error('offline');return{goals:[{goal_id:'one'}]};}});
 const c=createEvolutionController(a,{update:(s,e)=>{last=s;errors=e;},visible:()=>true,schedule:()=>1,cancel:()=>{}});
 await c.refresh();fail=true;await c.refresh();assert.equal(last.goals[0].goal_id,'one');assert.match(errors.join(),/offline/);c.dispose();
});
test('mount preserves form identity across refresh and stops late rendering on dispose',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');const page=mountEvolution(host,{api:api(),path:'evolution/new',navigate:()=>{},openTask:()=>{}});await tick();
 const input=host.querySelector('textarea');input.value='Keep this draft';input.focus();input.setSelectionRange(3,8);
 host.querySelector('[data-evo-action=refresh]').click();await tick();assert.equal(host.querySelector('textarea'),input);assert.equal(input.value,'Keep this draft');assert.equal(document.activeElement,input);assert.equal(input.selectionStart,3);
 page.dispose();assert.equal(host.childNodes.length,0);await tick();assert.equal(host.childNodes.length,0);d.window.close();
});
test('unmeasurable activation requires a second explicit click and keeps its receipt',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');const actions=[];let status='proposed';
 const a=api({listMutations:async()=>({mutations:[{mutation_id:'m',kind:'skill_candidate',status,version:1,payload:{name:'Skill'},evidence_episode_ids:[]}]}),mutationAction:async(id,action)=>{actions.push(action);status=action==='enable'?'confirmed':'active';return{mutation:{},final_status:action==='enable'?'needs_waiver':'active',receipt:{verdict:'unmeasurable'}};}});
 const page=mountEvolution(host,{api:a,path:'evolution/capabilities/m',navigate:()=>{},openTask:()=>{}});await tick();host.querySelector('[data-evo-action=enable]').click();await tick();await tick();assert.deepEqual(actions,['enable']);assert.match(host.textContent,/not active/);assert.ok(host.querySelector('[data-evo-action=waiver]'));
 host.querySelector('[data-evo-action=waiver]').click();await tick();await tick();assert.deepEqual(actions,['enable','enable_waiver']);assert.ok(!host.querySelector('[data-evo-action=enable]'));page.dispose();d.window.close();
});
test('failed plan creation retries only the admitted goal, preserving user text',async()=>{
 const d=dom();const {goalForm}=load('components/forms.ts');let creates=0,plans=0,saved='';const host=document.getElementById('host');
 goalForm(host,api({createGoal:async()=>{creates++;return{goal:{goal_id:'g'}};},proposePlan:async()=>{plans++;if(plans===1)throw Error('planner offline');}}),id=>saved=id);
 host.querySelector('textarea').value='A concrete goal';const form=host.querySelector('form');form.dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.equal(creates,1);assert.match(host.textContent,/Goal saved/);
 form.dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.equal(creates,1);assert.equal(plans,2);assert.equal(saved,'g');d.window.close();
});
test('running task never exposes verdict controls; terminal task permits evidence-based assessment',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');let task='running';
 const a=api({listGoals:async()=>({goals:[{goal_id:'g',statement:'Goal',status:'active',domain:'test'}]}),listEpisodes:async()=>({episodes:[{episode_id:'e',goal_id:'g',task_id:'t',status:'running',actuals:{tokens:0,wall_clock_seconds:0,tool_calls:0}}]}),taskStatus:async()=>({status:task})});
 const page=mountEvolution(host,{api:a,path:'evolution/goals/g',navigate:()=>{},openTask:()=>{}});await tick();host.querySelector('[data-evo-action=assess]').click();await tick();assert.equal(host.querySelector('select[name=outcome]'),null);
 task='done';host.querySelector('[data-evo-action=assess]').click();await tick();assert.ok(host.querySelector('select[name=outcome]'));assert.ok(host.querySelector('textarea[required]'));page.dispose();d.window.close();
});
test('workspace history events share one navigation and direct page switches dispose once',async()=>{
 const d=dom();document.body.innerHTML='<div class="chat-wrapper"><textarea>draft</textarea></div><div class="input-area"></div><div id="evolutionPageContainer"></div><div id="paperModeContainer"></div>';
 const shell=load('../../application-shell/page-host.ts');let loads=0,dispose=0,release;
 const gate=new Promise(r=>release=r);
 const stop=shell.installWorkspaceNavigation(async page=>{loads++;await gate;shell.showWorkspace(page);},()=>{});
 window.dispatchEvent(new d.window.Event('popstate'));window.dispatchEvent(new d.window.Event('hashchange'));assert.equal(loads,1);release();await tick();
 shell.mountWorkspacePage(()=>dispose++,()=>{});shell.showWorkspace('paper');assert.equal(dispose,1);assert.equal(document.querySelector('textarea').value,'draft');
 shell.releaseWorkspace('paper');assert.equal(document.querySelector('.chat-wrapper').style.display,'');stop();assert.equal(dispose,1);d.window.close();
});

test('back to the original hashless URL restores the chat workspace',async()=>{
 const d=dom();const shell=load('../../application-shell/page-host.ts');const pages=[];
 shell.showWorkspace('evolution');const stop=shell.installWorkspaceNavigation(async page=>{pages.push(page);shell.showWorkspace(page);},()=>{});
 window.history.replaceState(null,'','/');const length=window.history.length;window.dispatchEvent(new d.window.Event('popstate'));await tick();
 assert.deepEqual(pages,['chat']);assert.equal(document.body.dataset.workspace,'chat');assert.equal(window.location.hash,'');assert.equal(window.history.length,length);stop();d.window.close();
});

test('a completed mutation refresh cannot reuse a pre-mutation read',async()=>{
 const {createEvolutionController}=load('controller.ts');let release,reads=0,last;const gate=new Promise(r=>release=r);
 const c=createEvolutionController(api({listGoals:async()=>{const current=++reads;if(current===1)await gate;return{goals:[{goal_id:String(current)}]};}}),{update:s=>last=s,visible:()=>true,schedule:()=>1,cancel:()=>{}});
 const old=c.refresh();await tick();const afterWrite=c.refreshAfterWrite();release();await old;await afterWrite;
 assert.equal(reads,2);assert.equal(last.goals[0].goal_id,'2');c.dispose();
});

test('activity projection folds receipts and request rows into a visible stage and rounds',()=>{
 const {projectActivity}=load('components/activity.ts');
 const running=projectActivity(
  [{stage:'explore',status:'started',detail:'reading sources'}],
  [{roundNum:1,turn:'model',toolNames:['web_search'],attempts:[{tokensIn:10,tokensOut:20}]}],
  true);
 assert.equal(running.stage,'explore');assert.equal(running.stageDone,false);assert.equal(running.hasRounds,true);
 assert.equal(running.rounds.length,1);assert.equal(running.rounds[0].toolNames[0],'web_search');assert.equal(running.rounds[0].tokens,30);
 const starting=projectActivity(undefined,undefined,true);
 assert.equal(starting.hasRounds,false);assert.ok(starting.stage);assert.equal(starting.rounds.length,0);
 const settled=projectActivity([{stage:'done',status:'ok'}],[],false);
 assert.equal(settled.stageDone,true);
});

test('live route renders the goal as a user message and the streaming stage chip',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');
 const a=api({
  listGoals:async()=>({goals:[{goal_id:'g',statement:'Improve recall',status:'active',domain:'test'}]}),
  listEpisodes:async()=>({episodes:[{episode_id:'e',goal_id:'g',task_id:'t',status:'running',stage_receipts:[{stage:'explore',status:'started'}],actuals:{tokens:0,wall_clock_seconds:0,tool_calls:0}}]}),
  taskRequests:async()=>({requests:[{roundNum:1,turn:'model',toolNames:['grep_search'],attempts:[{tokensIn:5,tokensOut:7}]}]})});
 const page=mountEvolution(host,{api:a,path:'evolution/live/e',navigate:()=>{},openTask:()=>{}});await tick();await tick();
 assert.match(host.textContent,/Improve recall/);assert.ok(host.querySelector('.evo-msg-user'));assert.ok(host.querySelector('.evo-stage-chip'));
 assert.ok(host.querySelector('.evo-act-round'));assert.match(host.textContent,/grep_search/);
 page.dispose();d.window.close();
});

test('live route for a settled episode shows the results affordance, not the stream',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');
 const a=api({
  listGoals:async()=>({goals:[{goal_id:'g',statement:'Improve recall',status:'active',domain:'test'}]}),
  listEpisodes:async()=>({episodes:[{episode_id:'e',goal_id:'g',task_id:'t',status:'success',actuals:{tokens:0,wall_clock_seconds:0,tool_calls:0}}]})});
 const page=mountEvolution(host,{api:a,path:'evolution/live/e',navigate:()=>{},openTask:()=>{}});await tick();
 assert.equal(host.querySelector('.evo-live-stream'),null);assert.match(host.textContent,/View results|查看结果/);page.dispose();d.window.close();
});

test('floor route renders one workstation per running agent with a live bubble',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');const navigated=[];
 const a=api({
  listGoals:async()=>({goals:[{goal_id:'g1',statement:'Agent one',status:'active',domain:'t'},{goal_id:'g2',statement:'Agent two',status:'active',domain:'t'}]}),
  listEpisodes:async()=>({episodes:[
   {episode_id:'e1',goal_id:'g1',task_id:'t1',status:'running',stage_receipts:[{stage:'explore',status:'started'}],actuals:{tokens:0,wall_clock_seconds:0,tool_calls:0}},
   {episode_id:'e2',goal_id:'g2',task_id:'t2',status:'running',stage_receipts:[{stage:'synthesize',status:'started'}],actuals:{tokens:0,wall_clock_seconds:0,tool_calls:0}}]}),
  taskRequests:async()=>({requests:[]})});
 const page=mountEvolution(host,{api:a,path:'evolution/floor',navigate:p=>navigated.push(p),openTask:()=>{}});await tick();await tick();
 const stations=host.querySelectorAll('.evo-station');assert.equal(stations.length,2);
 assert.match(host.textContent,/explore/);assert.match(host.textContent,/synthesize/);
 stations[0].click();await tick();assert.deepEqual(navigated,['evolution/live/e1']);page.dispose();d.window.close();
});

test('floor route with no running agents shows the empty floor',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');
 const page=mountEvolution(host,{api:api(),path:'evolution/floor',navigate:()=>{},openTask:()=>{}});await tick();
 assert.equal(host.querySelectorAll('.evo-station').length,0);assert.ok(host.querySelector('.evo-floor-empty'));page.dispose();d.window.close();
});


test('live activity renders a nonempty round with escaped tool names',()=>{
 const d=dom();
 try {
  const {projectActivity,liveConversationHtml}=load('components/activity.ts');
  const state=projectActivity([], [{roundNum:3,toolNames:['<script>bad()</script>'],attempts:[{tokensIn:5,tokensOut:2}]}],true);
  const host=document.getElementById('host');host.innerHTML=liveConversationHtml('Goal',state);
  assert.equal(host.querySelectorAll('.evo-act-round').length,1);
  assert.equal(host.querySelectorAll('script').length,0);
  assert.match(host.textContent,/7 tok/);
  assert.match(host.textContent,/<script>bad\(\)<\/script>/);
 } finally {d.window.close();}
});


test('failed enable refreshes a durable intent and continuation avoids rejected or stale waiver controls',async()=>{
 const d=dom();const {mountEvolution}=load('view.ts');const host=document.getElementById('host');const calls=[];
 const mutation={mutation_id:'pending',kind:'budget_policy',status:'confirmed',version:1,payload:{policy:'{}'},evidence_episode_ids:[]};
 const a=api({listMutations:async()=>({mutations:[{...mutation}]}),mutationAction:async(id,action)=>{
  calls.push(action);if(calls.length===1){mutation.activation_intent={started_at:2,context_fingerprint:'0'.repeat(64)};throw Error('Activation interrupted');}
  mutation.status='active';delete mutation.activation_intent;return{mutation:{...mutation},final_status:'active'};
 }});
 const page=mountEvolution(host,{api:a,path:'evolution/capabilities/pending',navigate:()=>{},openTask:()=>{}});await tick();
 host.querySelector('[data-evo-action=enable]').click();await tick();await tick();
 assert.match(host.textContent,/Activation in progress/);assert.match(host.textContent,/Activation interrupted/);
 assert.equal(host.querySelector('[data-evo-action=rejectMutation]'),null);assert.equal(host.querySelector('[data-evo-action=waiver]'),null);
 assert.equal(host.querySelector('[data-evo-action=enable]').textContent,'Continue activation');
 host.querySelector('[data-evo-action=enable]').click();await tick();await tick();
 assert.deepEqual(calls,['enable','enable']);assert.ok(host.querySelector('[data-evo-action=rollback]'));
 page.dispose();d.window.close();
});
