const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const ts=require('typescript');const {JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
function load(file,cache=new Map()){
 let full=path.resolve(source,file);if(!fs.existsSync(full)&&full.endsWith('.ts'))full=path.join(full.slice(0,-3),'index.ts');if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>/\.(png|webp)$/.test(name)?{default:'/office.png'}:name.endsWith('/i18n')?{t:key=>locale[key]||key}:load(path.relative(source,path.resolve(path.dirname(full),name+'.ts')),cache),exports);return exports;
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(overrides={},portOverrides={}){
 const d=new JSDOM('<div id="host"></div>',{url:'http://localhost/#/evolution',pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document,Element:d.window.Element,Option:d.window.Option});
 d.window.HTMLCanvasElement.prototype.getContext=()=>null;
 d.window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
 const api={getCoordination:async()=>({updates:[],delivery:{},revision:0,before_seq:0,has_earlier:false,coordinator:{status:'idle'}}),listRuns:async()=>({runs:[]}),listRunModels:async()=>({models:[]}),getSwarmStatus:async()=>({active:null,known:false,agents:[]}),...overrides};
 const root=document.getElementById('host');
 const mounted=load('studio.ts').mountStudio(root,{api,path:'evolution',navigate:()=>{},openTask:()=>{},...portOverrides});
 return {d,root,mounted};
}
test('landing enters the world without a setup modal, and scene focus never launches work',async()=>{
 let writes=0;const {d,root,mounted}=setup({createRun:async()=>{writes++;}});
 await tick();
 assert.equal(root.querySelector('wa-drawer[open]'),null);
 root.querySelector('.evo-object-library').click();
 assert.equal(root.querySelector('#evo-goal-panel').hidden,false);
 assert.match(root.querySelector('.evo-room-content').textContent,/source shelf/);
 root.querySelector('.evo-room-intro-actions button').click();
 assert.equal(root.querySelector('wa-drawer[open]'),null);
 const field=root.querySelector('#evo-objective');field.value='Keep my mission briefing';
 assert.ok(field.closest('.evo-mission-panel'));assert.equal(root.querySelector('.evo-briefing-settings').open,false);
 const focus=root.querySelector('[data-scene-focus-toggle]');focus.click();
 assert.equal(root.dataset.sceneFocus,'true');assert.equal(focus.getAttribute('aria-pressed'),'true');
 focus.click();assert.equal(root.dataset.sceneFocus,'false');
 root.querySelector('[data-studio-action=refresh]').click();await tick();
 assert.equal(root.querySelector('#evo-objective'),field);assert.equal(field.value,'Keep my mission briefing');
 assert.equal(writes,0);assert.equal(root.querySelectorAll('.evo-mission-route li').length,4);
 mounted.dispose();assert.equal(root.dataset.sceneFocus,undefined);d.window.close();
});
test('mission HUD projects actual stages and receipts, not simulated completion',async()=>{
 const run={run_id:'a',objective:'Real goal',status:'reviewing',task_id:'review-task',attempt:1,created_at:1,updated_at:1,
 actuals:{tokens:20,tool_calls:1},receipts:[{task_id:'execute-task',stage:'executing',status:'failed'}],result:'',review:null,error:''};
 const {d,root,mounted}=setup({getRun:async()=>({run,pending_questions:[]})},{path:'evolution/runs/a'});
 await tick();await tick();
 assert.equal(root.querySelector('[data-phase=reviewing]').getAttribute('aria-current'),'step');
 assert.equal(root.querySelector('[data-phase=executing]').dataset.state,'recorded');
 assert.equal(root.querySelector('[data-phase=completed]').dataset.state,'pending');
 assert.equal(root.querySelectorAll('.evo-mission-route [aria-current=step]').length,1);
 root.querySelector('[data-phase=executing] button').click();
 assert.equal(root.querySelector('#evo-room-drawer').hasAttribute('open'),true);
 assert.match(root.querySelector('.evo-work-inspector [data-task-id="execute-task"]').textContent,/Failed/);
 mounted.dispose();d.window.close();
});
test('one goal heading leads the page and progress stays with its task evidence',async()=>{
 const objective='A long goal should not be repeated as another panel heading';
 const run={run_id:'a',objective,status:'executing',task_id:'task-a',attempt:1,created_at:1,updated_at:1,
  actuals:{tokens:20,tool_calls:1},receipts:[],result:'',review:null,error:''};
 const {d,root,mounted}=setup({getRun:async()=>({run,pending_questions:[]})},{path:'evolution/runs/a'});
 await tick();await tick();
 const heading=root.querySelector('h1.evo-ledger-heading strong');
 assert.equal(heading.textContent,objective);
 assert.notEqual(root.querySelector('#evo-mission-title').textContent,objective);
 assert.equal(root.querySelector('.evo-mission-hud').closest('.evo-mission-panel'),root.querySelector('.evo-mission-panel'));
 assert.equal(root.querySelector('.evo-studio-grid .evo-mission-hud'),null);
 assert.equal(root.querySelector('.evo-mission-signal').hidden,true);
 mounted.updateRoute('evolution/start');await tick();await tick();
 assert.equal(root.querySelector('.evo-mission-hud').hidden,true);
 mounted.dispose();d.window.close();
});

test('drawer-to-briefing handoff waits for deferred trigger focus before accepting input',async()=>{
 const {d,root,mounted}=setup();await tick();
 const trigger=root.querySelector('[data-phase=executing] button');trigger.click();
 const drawer=root.querySelector('#evo-room-drawer');root.querySelector('.evo-room-intro-actions button').click();
 const field=root.querySelector('#evo-objective');assert.equal(field.disabled,true);
 drawer.open=false;drawer.removeAttribute('open');d.window.setTimeout(()=>trigger.focus(),0);
 drawer.dispatchEvent(new d.window.Event('wa-after-hide',{bubbles:true}));
 await new Promise(resolve=>d.window.setTimeout(resolve,10));
 assert.equal(field.disabled,false);assert.equal(d.window.document.activeElement,field);
 mounted.dispose();d.window.close();
});
test('ambiguous submission retry retains the same idempotency key and objective',async()=>{
 const calls=[];const {d,root,mounted}=setup({createRun:async(body,key)=>{calls.push({body,key});throw Error('connection lost');}});
 await tick();root.querySelector('textarea').value='Deliver a verified patch';
 const submit=()=>root.querySelector('form').dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));
 submit();await tick();submit();await tick();
 assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.ok(calls[0].key);
 assert.equal(root.querySelector('textarea').value,'Deliver a verified patch');
 root.querySelector('textarea').value='Different objective';submit();await tick();assert.notEqual(calls[2].key,calls[0].key);
 mounted.dispose();d.window.close();
});
test('one read flight survives repeated refresh and a late result cannot update a disposed page',async()=>{
 let finish,calls=0;const {d,root,mounted}=setup({listRuns:()=>{calls++;return new Promise(r=>finish=r);}});
 root.querySelector('[data-studio-action=refresh]').click();root.querySelector('[data-studio-action=refresh]').click();assert.equal(calls,1);
 mounted.dispose();const before=root.innerHTML;finish({runs:[]});await tick();assert.equal(root.innerHTML,before);d.window.close();
});

test('late goal A response cannot populate goal B workspace',async()=>{
 let finishA;const run=id=>({run_id:id,objective:'Objective '+id,status:'completed',task_id:'',attempt:1,created_at:id==='a'?2:1,updated_at:1,actuals:{tokens:0,tool_calls:0},receipts:[],result:'',review:null});
 const {d,root,mounted}=setup({listRuns:async()=>({runs:[run('a'),run('b')]}),getRun:async id=>id==='a'?new Promise(r=>finishA=r):{run:run('b'),pending_questions:[],workspace:{materials:[],stages:[],criteria:[]}}});
 await tick();mounted.updateRoute('evolution/runs/b');finishA({run:run('a'),pending_questions:[]});await tick();await tick();
 assert.equal(root.querySelector('#evo-goal').value,'b');assert.match(root.querySelector('.evo-room-goal-name').textContent,/Objective b/);
 mounted.updateRoute('evolution/start');await tick();await tick();assert.equal(root.querySelector('.evo-objective-form').hidden,false);assert.equal(root.querySelector('.evo-goal-panel').hidden,true);
 mounted.dispose();d.window.close();
});


test('late goal creation acknowledges the write without navigating away from another goal',async()=>{
 let finish;const navigations=[];
 const run=id=>({run_id:id,objective:'Objective '+id,status:'completed',task_id:'',attempt:1,created_at:1,updated_at:1,actuals:{tokens:0,tool_calls:0},receipts:[],result:'',review:null});
 const {d,root,mounted}=setup({createRun:()=>new Promise(r=>finish=r),getRun:async()=>({run:run('b'),pending_questions:[]})},{navigate:p=>navigations.push(p)});
 await tick();root.querySelector('#evo-objective').value='New objective';root.querySelector('.evo-objective-form').dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));
 mounted.updateRoute('evolution/runs/b');await tick();await tick();finish({run:run('a')});await tick();await tick();
 assert.deepEqual(navigations,[]);assert.equal(root.querySelector('#evo-goal').value,'b');mounted.dispose();d.window.close();
});

test('actor inspector waits for the selected goal and adopts its saved model on arrival',async()=>{
 let finish;
 const model={creator_id:'test',model_id:'saved'};
 const run={run_id:'a',objective:'Goal A',status:'executing',task_id:'t',attempt:1,created_at:1,updated_at:1,actuals:{tokens:0,tool_calls:0},receipts:[],result:'',review:null,role_models:{executor:model}};
 const {d,root,mounted}=setup({listRunModels:async()=>({models:[{model,label:'Saved model'}]}),getRun:()=>new Promise(r=>finish=r)},{path:'evolution/runs/a'});
 await tick();root.querySelector('.evo-person[data-actor="0"]').click();
 const drawer=root.querySelector('#evo-role-drawer');
 assert.equal(drawer.querySelector('button.ui-btn--primary').disabled,true);
 finish({run,pending_questions:[]});await tick();await tick();
 assert.equal(drawer.querySelector('select').value,'0');
 assert.equal(drawer.querySelector('button.ui-btn--primary').disabled,false);
 mounted.dispose();d.window.close();
});

test('failed mission prepares a new authorization, not a replay, and exposes final execution status',async()=>{
 const opened=[];let writes=0;
 const run={run_id:'a',objective:'Retry this exact goal',status:'failed',task_id:'last-task',attempt:1,created_at:1,updated_at:1,actuals:{tokens:10000,tool_calls:3,wall_clock_seconds:9},receipts:[{task_id:'last-task',stage:'executing',status:'failed'}],result:'',review:null,error:'budget_exceeded'};
 const {d,root,mounted}=setup({getRun:async()=>({run,pending_questions:[],workspace:{materials:[],stages:[{kind:'Verify',detail:'Run tests'}],criteria:[]}}),createRun:async()=>{writes++;return {run};},cancelRun:async()=>{writes++;return {run};}},{path:'evolution/runs/a',openTask:id=>opened.push(id)});
 await tick();await tick();
 const mission=root.querySelector('.evo-mission-panel');assert.match(mission.textContent,/No limit/);assert.match(mission.textContent,/Run tests/);
 assert.doesNotMatch(mission.textContent,/budget_exceeded/);mission.querySelector('[data-mission-action=execution]').click();assert.deepEqual(opened,['last-task']);
 mission.querySelector('[data-mission-action=retry]').click();assert.equal(writes,0);assert.equal(root.querySelector('#evo-objective').value,run.objective);
 assert.equal(root.querySelector('.evo-objective-form').hidden,false);assert.equal(root.querySelector('.evo-objective-form').closest('.evo-mission-panel'),mission);
 assert.equal(root.querySelector('wa-drawer[open]'),null);
 root.querySelector('[data-studio-action=refresh]').click();await tick();await tick();assert.equal(root.querySelector('.evo-objective-form').hidden,false);
 mounted.dispose();d.window.close();
});
test('pausing room animation never cancels execution; only the explicit stop control does',async()=>{
 let cancels=0;const run={run_id:'a',objective:'Active goal',status:'executing',task_id:'task-a',attempt:1,created_at:1,updated_at:1,actuals:{tokens:0,tool_calls:0},receipts:[],result:'',review:null,error:''};
 const {d,root,mounted}=setup({getRun:async()=>({run,pending_questions:[]}),cancelRun:async()=>{cancels++;return {run:{...run,status:'cancelled'}};}},{path:'evolution/runs/a'});
 await tick();await tick();root.querySelector('.evo-scene-pause').click();assert.equal(cancels,0);
 assert.match(root.querySelector('.evo-mission-notice').textContent,/only freezes the room/);
 root.querySelector('[data-mission-action=stop]').click();await tick();assert.equal(cancels,1);
 mounted.dispose();d.window.close();
});

test('execution environment selection preserves explicit opt-in and project path at dispatch',async()=>{
 let body;const {d,root,mounted}=setup({listExecutorPlugins:async()=>({extensibility:{plugins:{available:{environments:['environments']}}}}),createRun:async value=>{body=value;throw Error('test transport failure');}});
 await tick();root.querySelector('[name=project_path]').value='/workspace/experiment';
 root.querySelector('[name=executor_plugins]').value='["environments"]';
 root.querySelector('#evo-objective').value='Reproduce the baseline';
 root.querySelector('.evo-objective-form').dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));await tick();
 assert.equal(body.project_path,'/workspace/experiment');assert.deepEqual(body.executor_plugins,['environments']);
 assert.equal(root.querySelector('[name=executor_plugins]').value,'["environments"]');mounted.dispose();d.window.close();
});
test('failed environment catalog read exposes retry and does not opt in silently',async()=>{
 const {d,root,mounted}=setup({listExecutorPlugins:async()=>{throw Error('offline');}});await tick();
 assert.equal(root.querySelector('[name=executor_plugins]').value,'');
 assert.match(root.querySelector('.evo-briefing-settings').textContent,/unavailable/);
 assert.ok(Array.from(root.querySelectorAll('.evo-briefing-settings button')).some(b=>b.textContent==='Reload execution plugins'&&!b.hidden));mounted.dispose();d.window.close();
});

test('default environment is omitted and explicit no-plugin choice is retained',async()=>{
 const bodies=[];const {d,root,mounted}=setup({listExecutorPlugins:async()=>({extensibility:{plugins:{available:{}}}}),createRun:async value=>{bodies.push(value);throw Error('test failure');}});await tick();
 const submit=()=>root.querySelector('.evo-objective-form').dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));
 root.querySelector('#evo-objective').value='Optimize the system';submit();await tick();assert.equal('executor_plugins' in bodies[0],false);
 root.querySelector('[name=executor_plugins]').value='[]';submit();await tick();assert.deepEqual(bodies[1].executor_plugins,[]);mounted.dispose();d.window.close();
});

test('inspection disclosures retain reader focus across observations and reset for another goal',async()=>{
 const run={objective:'Compare measured results',status:'executing',task_id:'task-a',attempt:1,created_at:1,updated_at:1,
  actuals:{tokens:20,tool_calls:1},receipts:[],result:'',review:null,error:''};
 const {d,root,mounted}=setup({getRun:async id=>({run:{...run,run_id:id},pending_questions:[],workspace:{materials:[],stages:[{kind:'Measure',detail:'Record the observed baseline'}],criteria:[]}})},{path:'evolution/runs/a'});
 await tick();await tick();
 const selector='.evo-mission-panel [data-inspection-section=plannedSteps]';
 let fold=root.querySelector(selector);assert.equal(fold.open,false);
 fold.open=true;fold.querySelector('summary').focus();run.actuals.tokens=35;
 root.querySelector('[data-studio-action=refresh]').click();await tick();await tick();
 fold=root.querySelector(selector);assert.equal(fold.open,true);assert.equal(document.activeElement,fold.querySelector('summary'));
 assert.match(fold.textContent,/Record the observed baseline/);
 mounted.updateRoute('evolution/runs/b');await tick();await tick();
 assert.equal(root.querySelector(selector).open,false);
 mounted.dispose();d.window.close();
});
