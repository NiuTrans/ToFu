/** Executable specifications for the composed studio game screen (studio.ts):
 * the HUD projects confirmed backend state, windows and hotkeys open the
 * journal, writes are explicit/idempotent/goal-fenced, and animation controls
 * never touch execution. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const ts=require('typescript');const {JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
const copy=key=>locale['evolution.studio.'+key];
function load(file,cache=new Map()){
 let full=path.resolve(source,file);if(!fs.existsSync(full)&&full.endsWith('.ts'))full=path.join(full.slice(0,-3),'index.ts');if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>/\.(png|webp)$/.test(name)?{default:'/art.webp'}:name.endsWith('/i18n')?{t:key=>locale[key]||key}:load(path.relative(source,path.resolve(path.dirname(full),name+'.ts')),cache),exports);return exports;
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const settle=async()=>{for(let i=0;i<6;i++)await tick();};
const run=(over={})=>({run_id:'a',objective:'Real goal',status:'executing',task_id:'task-a',attempt:1,created_at:1,updated_at:1,
 actuals:{tokens:20,tool_calls:1,wall_clock_seconds:3},receipts:[],result:'',review:null,error:'',...over});
function setup(overrides={},portOverrides={}){
 const d=new JSDOM('<div id="host"></div>',{url:'http://localhost/#/evolution',pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document,Element:d.window.Element,Option:d.window.Option,
  EventSource:class{constructor(){this.onmessage=null;this.onerror=null;}close(){}}});
 d.window.HTMLCanvasElement.prototype.getContext=()=>null;
 d.window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
 const api={getCoordination:async()=>({updates:[],delivery:{},revision:0,before_seq:0,has_earlier:false,coordinator:{status:'idle'}}),
  listRuns:async()=>({runs:[]}),listRunModels:async()=>({models:[]}),getSwarmStatus:async()=>({active:null,known:false,agents:[]}),
  listExecutorPlugins:async()=>({extensibility:{plugins:{available:{}}}}),...overrides};
 const root=document.getElementById('host');
 const mounted=load('studio.ts').mountStudio(root,{api,path:'evolution',navigate:()=>{},openTask:()=>{},...portOverrides});
 const key=(code,target=document.body)=>target.dispatchEvent(new d.window.KeyboardEvent('keydown',{code,key:code==='Escape'?'Escape':code,bubbles:true,cancelable:true}));
 return {d,root,mounted,key,q:selector=>root.querySelector(selector)};
}
const goal=(value,questions=[],workspace={materials:[],stages:[],criteria:[]})=>({getRun:async()=>({run:value,pending_questions:questions,workspace})});

test('with no goal the screen invites one, and briefing seeds never dispatch work',async()=>{
 let writes=0;const s=setup({createRun:async()=>{writes++;}});await settle();
 assert.equal(s.q('.evo-quest').dataset.hud,'create');assert.equal(s.q('.evo-primary-action').dataset.kind,'create');
 assert.ok([...s.root.querySelectorAll('.evo-slot[data-place]')].every(slot=>slot.disabled));
 assert.equal(s.q('.evo-window').hidden,true);assert.equal(s.root.dataset.window,undefined);
 s.key('KeyN');assert.equal(s.root.dataset.window,'briefing');assert.equal(document.activeElement,s.q('#evo-objective'));
 s.q('[data-example=research]').click();assert.equal(s.q('#evo-objective').value,copy('prompt_research'));
 assert.ok([...s.root.querySelectorAll('.evo-seed')].every(seed=>seed.disabled));
 assert.equal(writes,0);
 s.mounted.dispose();assert.equal(s.root.dataset.window,undefined);s.d.window.close();
});

test('ambiguous dispatch retries keep one idempotency key; a different objective gets a new one',async()=>{
 const calls=[];const s=setup({createRun:async(body,key)=>{calls.push({body,key});throw Error('connection lost');}});
 await settle();s.key('KeyN');
 const submit=()=>s.q('.evo-objective-form').dispatchEvent(new s.d.window.Event('submit',{bubbles:true,cancelable:true}));
 s.q('#evo-objective').value='Deliver a verified patch';submit();await tick();submit();await tick();
 assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.ok(calls[0].key);
 assert.equal(s.q('#evo-objective').value,'Deliver a verified patch');assert.match(s.q('.evo-error').textContent,/connection lost/);
 s.q('#evo-objective').value='Different objective';submit();await tick();assert.notEqual(calls[2].key,calls[0].key);
 s.mounted.dispose();s.d.window.close();
});

test('a compute-parked executor reads as a wait at the HUD, never as settlement or a delivery',async()=>{
 const parked=run({compute_wait:{operation_id:'job-9ec7',task_id:'task-a',parked:true,next_poll_at:0},
  result:'Compute dependency pending; no completion or scientific acceptance.',
  receipts:[{task_id:'task-a',stage:'executing',terminal_verdict:'completed',actuals:{tokens:20,tool_calls:1,wall_clock_seconds:3}}]});
 const s=setup(goal(parked),{path:'evolution/runs/a'});await settle();
 assert.equal(s.root.dataset.status,'waitingCompute');assert.equal(s.root.dataset.wait,'compute');
 assert.equal(s.q('.evo-quest-state').textContent,copy('waitingCompute'));assert.equal(s.q('.evo-quest-detail').textContent,'job-9ec7');
 assert.equal(s.q('.evo-track [data-stage=execute]').dataset.state,'waiting');
 assert.equal(s.q('.evo-track [data-stage=deliver]').dataset.state,'pending');
 assert.equal(s.q('.evo-primary-action').hidden,true);assert.equal(s.q('[data-place=delivery] .evo-slot-count').hidden,true);
 assert.doesNotMatch(s.q('[data-meter=tokens] b').textContent,/≥/,'nothing is running, so the recorded usage is exact');
 const executor=s.q('.evo-target[data-target="actor:executor"]');
 assert.equal(executor.getAttribute('aria-label'),`${copy('executor')} · ${copy('waitingCompute')}`);
 assert.equal(executor.dataset.emote,'compute');
 s.mounted.dispose();s.d.window.close();
});

test('hotbar slots and number keys open journal tabs; Escape closes and returns focus',async()=>{
 const s=setup(goal(run()),{path:'evolution/runs/a'});await settle();
 const slot=s.q('.evo-slot[data-place=work]');slot.focus();slot.click();
 assert.equal(s.root.dataset.window,'journal');assert.equal(s.q('[role=tab][data-place=work]').getAttribute('aria-selected'),'true');
 assert.equal(s.q('#evo-window-title').textContent,copy('place_work'));
 s.key('Escape',document.activeElement);assert.equal(s.q('.evo-window').hidden,true);assert.equal(document.activeElement,slot);
 s.key('Digit1');assert.equal(s.q('[role=tab][data-place=library]').getAttribute('aria-selected'),'true');
 s.key('Escape');s.key('KeyT');assert.equal(s.root.dataset.window,'chat');
 s.key('Escape');s.key('Escape');assert.equal(s.root.dataset.window,'menu','Escape with nothing open opens the menu');
 s.mounted.dispose();s.d.window.close();
});

test('pausing animation never cancels execution; stopping takes a second explicit confirmation',async()=>{
 let cancels=0;
 const s=setup({...goal(run()),cancelRun:async()=>{cancels++;return {run:run({status:'cancelled'})};}},{path:'evolution/runs/a'});await settle();
 s.q('[data-hud=menu]').click();s.q('[data-studio-action=motion]').click();
 assert.equal(cancels,0);assert.equal(s.q('.evo-stage').dataset.motion,'paused');
 assert.equal(s.root.dataset.motion,'paused','the pause stills HUD animations as well as the world');
 assert.equal(s.q('[data-studio-action=motion]').getAttribute('aria-pressed'),'true');
 s.q('.evo-slot[data-place=work]').click();
 s.q('[data-room-action=stop]').click();await tick();assert.equal(cancels,0);
 s.q('[data-room-action=stop]').click();await settle();assert.equal(cancels,1);
 s.mounted.dispose();s.d.window.close();
});

test('closing a route-opened briefing hands focus to the world, so hotkeys keep working',async()=>{
 const s=setup({},{path:'evolution/start'});await settle();
 assert.equal(s.root.dataset.window,'briefing');assert.equal(document.activeElement,s.q('#evo-objective'));
 s.key('Escape',s.q('#evo-objective'));
 assert.equal(s.q('.evo-window').hidden,true);assert.equal(document.activeElement,s.q('.evo-stage'),'focus never stays in the hidden window');
 s.key('KeyN',document.activeElement);assert.equal(s.root.dataset.window,'briefing');
 s.mounted.dispose();s.d.window.close();
});

test('a stalled status read says where it is stuck, and the dialogue hands the fault to the Watchdog',async()=>{
 const error="ModuleNotFoundError: No module named 'tofu_environments.capacity'";
 const origin='tofu_meituan_environments/hope_catalog.py:3 in <module>';
 let current=run({compute_wait:{operation_id:'job-9ec7',task_id:'task-a',parked:true,next_poll_at:0,last_error:error,
  last_error_origin:origin,failed_since:1700000000},result:'Compute dependency pending; no completion or scientific acceptance.'});
 let pending;const handoffs=[];
 const s=setup({getRun:async()=>({run:current,pending_questions:[],workspace:{materials:[],stages:[],criteria:[]}})},
  {path:'evolution/runs/a',openWatchdog:()=>{handoffs.push(1);return new Promise((resolve,reject)=>{pending={resolve,reject};});}});
 await settle();
 const box=s.q('.evo-dialogue'),facts=()=>Object.fromEntries([...box.querySelectorAll('.evo-stall-facts>div')]
  .map(div=>[div.dataset.fact,div.querySelector('dd').textContent]));
 assert.equal(box.dataset.mode,'stall');assert.equal(box.hidden,false);assert.equal(s.q('.evo-speaker').textContent,copy('executor'));
 assert.equal(s.q('.evo-primary-action').dataset.kind,'stall');
 assert.equal(s.q('.evo-line').textContent,copy('stallLine'));
 assert.deepEqual([...box.querySelectorAll('[data-fact=at] dd>*')].map(node=>node.textContent),[copy('stallReadingStatus'),'job-9ec7'],
  'the step and its job are separate elements, so a long job ID wraps as one unit');
 assert.equal(facts().error,error);assert.equal(facts().origin,origin);assert.ok(facts().since,'the streak start is shown');
 assert.equal(s.q('.evo-stall-route').textContent,copy('stallUnrouted'));
 const watchdog=()=>s.q('[data-dialogue-action=watchdog]');
 assert.equal(watchdog().textContent,copy('stallHandToWatchdog'));
 watchdog().click();watchdog().click();await tick();
 assert.equal(handoffs.length,1,'one hand-off at a time');assert.equal(watchdog().disabled,true);
 pending.reject(Error('home not writable'));await settle();
 assert.equal(box.querySelector('.evo-dialogue-actions [role=status]').textContent,'home not writable');
 assert.equal(watchdog().disabled,false);
 watchdog().click();pending.resolve();await settle();
 assert.equal(handoffs.length,2);assert.equal(box.querySelector('.evo-dialogue-actions [role=status]').textContent,'');
 s.q('[data-dialogue-action=record]').click();assert.equal(s.root.dataset.window,'journal');
 assert.equal(s.q('#evo-window-title').textContent,copy('place_work'));
 s.key('Escape');
 s.q('.evo-dialogue-close').click();assert.equal(box.hidden,true);
 s.q('.evo-primary-action').click();assert.equal(box.hidden,false,'the primary action reopens a dismissed stall');
 assert.equal(document.activeElement,watchdog(),'the primary hand-off takes focus');
 s.q('.evo-dialogue-close').click();
 s.q('.evo-target[data-target="actor:executor"]').click();
 const decide=[...s.root.querySelectorAll('.evo-role-models button')].find(button=>button.textContent===copy('primary_stall'));
 assert.equal(decide.hidden,false,'the stalled executor offers its stall from the actor sheet');
 decide.click();assert.equal(box.hidden,false);
 s.q('.evo-dialogue-close').click();assert.equal(box.hidden,true);
 current={...current,compute_wait:{...current.compute_wait,last_error:'successful_exit_unverified: exit_code=None',
  last_error_origin:undefined,watchdog_signal_id:'sig-2'}};
 s.q('[data-studio-action=refresh]').click();await settle();
 assert.equal(box.hidden,false,'a different failure is shown again');
 assert.equal(s.q('.evo-line').textContent,copy('stallLineExit'));
 assert.equal(box.querySelector('[data-fact=at] dd>span').textContent,copy('stallVerifyingExit'));assert.equal(facts().origin,undefined);
 assert.equal(s.q('.evo-stall-route').textContent,copy('stallRouted'));assert.equal(s.q('.evo-stall-route').dataset.routed,'true');
 assert.equal(watchdog().textContent,copy('stallOpenWatchdog'));
 current={...current,compute_wait:{operation_id:'job-9ec7',task_id:'task-a',parked:true,next_poll_at:0}};
 s.q('[data-studio-action=refresh]').click();await settle();
 assert.equal(box.hidden,true,'a successful read closes the stall');assert.equal(s.q('.evo-primary-action').hidden,true);
 s.mounted.dispose();s.d.window.close();
 const bare=setup(goal(run({compute_wait:{operation_id:'job-9ec7',task_id:'task-a',parked:true,next_poll_at:0,last_error:error}})),
  {path:'evolution/runs/a'});await settle();
 assert.equal(bare.q('.evo-dialogue').dataset.mode,'stall');assert.equal(bare.q('[data-dialogue-action=watchdog]'),null,
  'without the shell Watchdog entry there is no hand-off to offer');
 bare.mounted.dispose();bare.d.window.close();
});

test('a pending question is answered from the dialogue with its offered choices',async()=>{
 const answers=[];
 const question={guidance_id:'q1',question:'Which dataset?',response_type:'text',options:['Public','Internal'],questions:[]};
 const s=setup({...goal(run(),[question]),answerRun:async(...args)=>{answers.push(args);}},{path:'evolution/runs/a'});await settle();
 assert.equal(s.q('.evo-dialogue').dataset.mode,'question');assert.equal(s.q('.evo-dialogue').hidden,false);
 assert.equal(s.q('.evo-primary-action').dataset.kind,'answer');assert.equal(s.root.dataset.status,'askHuman');
 s.q('.evo-choice[data-value=Internal]').click();assert.equal(s.q('.evo-answer textarea').value,'Internal');
 s.q('.evo-answer').dispatchEvent(new s.d.window.Event('submit',{bubbles:true,cancelable:true}));await settle();
 assert.deepEqual(answers,[['a','q1','Internal']]);
 s.q('.evo-dialogue-close').click();assert.equal(s.q('.evo-dialogue').hidden,true);
 s.q('.evo-primary-action').click();assert.equal(s.q('.evo-dialogue').hidden,false,'the primary action reopens a collapsed question');
 s.mounted.dispose();s.d.window.close();
});

test('a failed goal prepares a new authorization in the briefing, never a replay',async()=>{
 let writes=0;
 const failed=run({status:'failed',error:'budget_exceeded: max_tokens',receipts:[{task_id:'task-a',stage:'executing',terminal_verdict:'failed'}]});
 const s=setup({...goal(failed),createRun:async()=>{writes++;}},{path:'evolution/runs/a'});await settle();
 assert.equal(s.q('.evo-quest-detail').textContent,copy('fail_limit'));
 assert.match(s.q('.evo-quest').dataset.tip,/configured execution limit/);
 assert.equal(s.q('.evo-track [data-stage=execute]').dataset.state,'failed');
 s.q('.evo-primary-action').click();
 assert.equal(s.root.dataset.window,'briefing');assert.equal(s.q('#evo-objective').value,'Real goal');assert.equal(writes,0);
 s.mounted.dispose();s.d.window.close();
});

test('a late response for goal A never populates goal B, and late creation does not navigate',async()=>{
 let finishA;const value=id=>run({run_id:id,objective:'Objective '+id,status:'completed'});
 const s=setup({listRuns:async()=>({runs:[value('a'),value('b')]}),getRun:async id=>id==='a'?new Promise(r=>finishA=r):{run:value('b'),pending_questions:[],workspace:{materials:[],stages:[],criteria:[]}}});
 await tick();s.mounted.updateRoute('evolution/runs/b');finishA({run:value('a'),pending_questions:[]});await settle();
 assert.equal(s.q('.evo-quest-title').textContent,'Objective b');
 assert.equal(s.q('.evo-goal-row[aria-current=true]').dataset.key,'b');
 s.mounted.dispose();s.d.window.close();
 let finish;const navigations=[];
 const t=setup({createRun:()=>new Promise(r=>finish=r),getRun:async()=>({run:value('b'),pending_questions:[]})},{navigate:p=>navigations.push(p)});
 await tick();t.key('KeyN');t.q('#evo-objective').value='New objective';
 t.q('.evo-objective-form').dispatchEvent(new t.d.window.Event('submit',{bubbles:true,cancelable:true}));
 t.mounted.updateRoute('evolution/runs/b');await settle();finish({run:value('a')});await settle();
 assert.deepEqual(navigations,[]);assert.equal(t.q('.evo-quest-title').textContent,'Objective b');
 t.mounted.dispose();t.d.window.close();
});

test('one read flight survives repeated refresh and a late result cannot update a disposed page',async()=>{
 let finish,calls=0;const s=setup({listRuns:()=>{calls++;return new Promise(r=>finish=r);}});
 s.q('[data-studio-action=refresh]').click();s.q('[data-studio-action=refresh]').click();assert.equal(calls,1);
 s.mounted.dispose();const before=s.root.innerHTML;finish({runs:[]});await tick();assert.equal(s.root.innerHTML,before);s.d.window.close();
});

test('clicking a resident opens its card, which waits for the goal and adopts its saved model',async()=>{
 let finish;const model={creator_id:'test',model_id:'saved'};
 const value=run({role_models:{executor:model}});
 const s=setup({listRunModels:async()=>({models:[{model,label:'Saved model'}]}),getRun:()=>new Promise(r=>finish=r)},{path:'evolution/runs/a'});
 await tick();s.q('.evo-target[data-target="actor:executor"]').click();
 assert.equal(s.root.dataset.window,'actor');
 const save=s.q('.evo-role-models button.ui-btn--primary');assert.equal(save.disabled,true);
 finish({run:value,pending_questions:[]});await settle();
 assert.equal(s.q('.evo-role-models select').value,'0');assert.equal(save.disabled,false);
 s.mounted.dispose();s.d.window.close();
});

test('opening chat reads the coordinator line once; a reply arriving in chat never pops the dialogue',async()=>{
 let updates=[{seq:1,origin:'coordinator',kind:'reply',content:'First line',references:[]}];
 const s=setup({...goal(run()),getCoordination:async()=>({updates,delivery:{},revision:updates.length,before_seq:1,has_earlier:false,coordinator:{status:'idle'}})},{path:'evolution/runs/a'});
 await settle();
 assert.equal(s.q('.evo-dialogue').dataset.mode,'line');assert.equal(s.q('.evo-slot[data-slot=chat]').dataset.attention,'true');
 s.key('KeyT');assert.equal(s.root.dataset.window,'chat');
 assert.equal(s.q('.evo-dialogue').hidden,true,'the chat log already shows the line');
 assert.equal(s.q('.evo-slot[data-slot=chat]').dataset.attention,'false');
 assert.equal(s.q('.evo-conversation-form label').classList.contains('evo-sr-only'),true);
 assert.equal(s.q('#evo-message').getAttribute('placeholder'),null,'the input carries no instructional copy');
 updates=[...updates,{seq:2,origin:'coordinator',kind:'reply',content:'Second line',references:[]}];
 s.q('[data-studio-action=refresh]').click();await settle();
 assert.match(s.q('.evo-conversation-log').textContent,/Second line/);assert.equal(s.q('.evo-dialogue').hidden,true);
 s.key('Escape');assert.equal(s.q('.evo-dialogue').hidden,true,'closing chat does not replay a read line');
 s.mounted.dispose();s.d.window.close();
});

test('the actor sheet shows live state and offers a decision only to the role that asked',async()=>{
 const question={guidance_id:'q1',question:'Which dataset?',response_type:'text',options:[],questions:[]};
 const s=setup(goal(run(),[question]),{path:'evolution/runs/a'});await settle();
 s.q('.evo-target[data-target="actor:executor"]').click();
 assert.equal(s.root.dataset.window,'actor');assert.equal(s.q('#evo-window-title').textContent,copy('executor'));
 assert.equal(s.q('.evo-actor-sheet h2'),null,'the window title alone names the actor');
 assert.equal(s.q('.evo-actor-state').textContent,copy('askHuman'));assert.equal(s.q('.evo-actor-state').dataset.mode,'waiting');
 const decide=[...s.root.querySelectorAll('.evo-role-models button')].find(button=>button.textContent===copy('primary_answer'));
 assert.equal(decide.hidden,false);
 s.q('.evo-target[data-target="actor:reviewer"]').click();
 assert.equal(s.q('#evo-window-title').textContent,copy('reviewer'));assert.equal(decide.hidden,true,'the reviewer did not ask');
 s.mounted.dispose();s.d.window.close();
});

test('execution environment choices are explicit at dispatch and never opted in silently',async()=>{
 const bodies=[];
 const s=setup({listExecutorPlugins:async()=>({extensibility:{plugins:{available:{environments:['environments']}}}}),createRun:async value=>{bodies.push(value);throw Error('test failure');}});
 await settle();s.key('KeyN');
 const submit=()=>s.q('.evo-objective-form').dispatchEvent(new s.d.window.Event('submit',{bubbles:true,cancelable:true}));
 s.q('#evo-objective').value='Optimize the system';submit();await tick();assert.equal('executor_plugins' in bodies[0],false);
 s.q('[name=project_path]').value='/workspace/experiment';s.q('[name=executor_plugins]').value='["environments"]';submit();await tick();
 assert.equal(bodies[1].project_path,'/workspace/experiment');assert.deepEqual(bodies[1].executor_plugins,['environments']);
 s.q('[name=executor_plugins]').value='[]';submit();await tick();assert.deepEqual(bodies[2].executor_plugins,[]);
 s.mounted.dispose();s.d.window.close();
 const failing=setup({listExecutorPlugins:async()=>{throw Error('offline');}});await settle();
 assert.equal(failing.q('[name=executor_plugins]').value,'');
 assert.ok([...failing.root.querySelectorAll('.evo-briefing-settings button')].some(b=>b.textContent==='Reload execution plugins'&&!b.hidden));
 failing.mounted.dispose();failing.d.window.close();
});
