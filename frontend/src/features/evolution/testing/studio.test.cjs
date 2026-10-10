const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const ts=require('typescript');const {JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
function load(file,cache=new Map()){
 const full=path.resolve(source,file);if(cache.has(full))return cache.get(full);
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
 assert.equal(root.querySelector('.evo-objective-form').hidden,false);assert.ok(root.querySelector('#evo-create-drawer').hasAttribute('open'));
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
