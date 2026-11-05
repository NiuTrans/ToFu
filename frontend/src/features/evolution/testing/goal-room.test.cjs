/** Executable specifications for the goal journal (goal-room.ts): five
 * furniture tabs, goal-fenced writes, persistent drafts and a two-step stop. */
const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript'),{JSDOM}=require('jsdom');
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 let full=path.resolve(path.dirname(parent),name+'.ts');if(!fs.existsSync(full))full=path.join(full.slice(0,-3),'index.ts');const exports={};
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>/\.(png|webp)$/.test(name)?{default:'/art.webp'}:name.endsWith('/i18n')?{t:k=>k}:load(name,full),exports);return exports;
}
const tick=()=>new Promise(r=>setImmediate(r));
const run=(id,over={})=>({run_id:id,objective:'Goal '+id,status:'executing',task_id:'task-'+id,attempt:1,receipts:[],result:'',review:null,error:'',actuals:{tokens:0,tool_calls:0,wall_clock_seconds:0},...over});
function setup(api={},ports={}){
 const d=new JSDOM('<section id="panel"></section>',{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 const panel=document.getElementById('panel');let reads=0,stops=0;
 const mod=load('goal-room');
 const room=mod.mountGoalRoom(panel,{api,copy:k=>k,refresh:async()=>{reads++;},openTask:()=>{},stop:async()=>{stops++;},reconcile:async()=>{},
  download(){},busy:()=>false,visible:()=>true,...ports});
 return {d,panel,room,mod,reads:()=>reads,stops:()=>stops};
}
const material={material_id:'m',run_id:'a',kind:'paper',title:'Source <safe>',url:'https://example.org/paper',note:'Useful',origin:'agent',task_id:'task-a'};
const tab=(s,place)=>s.panel.querySelector(`[role=tab][data-place=${place}]`);
test('the shelf renders safe external links and counts, and never leaks materials across goals',()=>{
 const s=setup();s.room.update(run('a'),{materials:[material],stages:[],criteria:[]});
 assert.equal(s.panel.querySelectorAll('.evo-materials li').length,1);
 const link=s.panel.querySelector('.evo-materials a');assert.equal(link.textContent,'Source <safe>');assert.equal(link.rel,'noopener noreferrer');
 assert.equal(tab(s,'library').querySelector('b').textContent,'1');assert.equal(tab(s,'library').getAttribute('aria-selected'),'true');
 s.room.update(run('b'),{materials:[],stages:[],criteria:[]});
 assert.equal(s.panel.querySelectorAll('.evo-materials li').length,0);assert.ok(!s.panel.textContent.includes('Source <safe>'));
 for(const url of ['javascript:alert(1)','data:text/html,bad','https://u:p@example.org/'])assert.equal(s.mod.safeExternalUrl(url),'');
 s.room.dispose();s.d.window.close();
});
test('refresh retains an add-material draft but switching goals clears it',()=>{
 const s=setup();s.room.update(run('a'));const field=s.panel.querySelector('[name=title]');field.value='Draft';
 s.room.update(run('a'));assert.equal(s.panel.querySelector('[name=title]'),field);assert.equal(field.value,'Draft');
 s.room.update(run('b'));assert.equal(s.panel.querySelector('[name=title]').value,'');s.room.dispose();s.d.window.close();
});
test('an in-flight material write stays bound to its original goal',async()=>{
 let finish;const writes=[];const s=setup({saveMaterial:async(id,body)=>{writes.push({id,body});return new Promise(r=>finish=r);}});
 s.room.update(run('a'));s.panel.querySelector('[name=title]').value='Original';s.panel.querySelector('[name=url]').value='https://example.org/a';
 s.panel.querySelector('.evo-material-add form').dispatchEvent(new s.d.window.Event('submit',{bubbles:true,cancelable:true}));
 s.room.update(run('b'));s.panel.querySelector('[name=title]').value='Keep B';finish({});await tick();
 assert.equal(writes[0].id,'a');assert.equal(s.panel.querySelector('[name=title]').value,'Keep B');assert.equal(s.reads(),0);
 s.room.dispose();s.d.window.close();
});
test('execution, review and delivery tabs show only their own records; arrow keys move between tabs',()=>{
 const s=setup();
 s.room.update(run('a',{status:'completed',receipts:[{task_id:'old-task',stage:'executing',status:'completed'}],result:'Verified delivery',
  review:{outcome:'success',criterion_results:[{criterion_id:'c',outcome:'pass',evidence_ref:'checked'}]}}),{materials:[material],stages:[{kind:'Step',detail:'Do it'}],criteria:[{criterion_id:'c',requirement:'Works'}]});
 s.room.open('work');
 assert.match(s.panel.querySelector('[data-task-id="old-task"][data-inspector-action=task]').textContent,/completed/);
 assert.equal(s.panel.querySelector('.evo-tab-inspector[data-tab=work]').hidden,false);
 assert.equal(s.panel.querySelector('.evo-tab-static').hidden,true);
 assert.equal(s.panel.querySelector('[data-room-action=stop]'),null,'a terminal goal cannot be stopped');
 tab(s,'work').dispatchEvent(new s.d.window.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
 assert.equal(s.room.selected(),'review');assert.match(s.panel.querySelector('.evo-tab-inspector[data-tab=review]').textContent,/Works/);
 s.room.open('delivery');assert.equal(s.panel.querySelector('#evo-deliverable').textContent,'Verified delivery');
 assert.ok(s.panel.querySelector('[data-room-action=download]'));
 s.room.open('plan');assert.match(s.panel.querySelector('.evo-tab-static').textContent,/Do it/);
 s.room.dispose();s.d.window.close();
});
test('stopping a live goal takes an explicit second confirmation that resets when you leave the tab',async()=>{
 const s=setup();s.room.update(run('a'));s.room.open('work');
 const stop=()=>s.panel.querySelector('[data-room-action=stop]');
 stop().click();await tick();assert.equal(s.stops(),0);assert.equal(stop().dataset.armed,'true');
 s.room.open('library');s.room.open('work');assert.equal(stop().dataset.armed,'false');
 stop().click();await tick();stop().click();await tick();assert.equal(s.stops(),1);
 s.room.dispose();s.d.window.close();
});
test('a compute-parked placeholder is never offered as the deliverable',()=>{
 const s=setup();
 s.room.update(run('a',{compute_wait:{operation_id:'job',task_id:'task-a',parked:true,next_poll_at:0},result:'Compute dependency pending; no completion or scientific acceptance.'}));
 s.room.open('delivery');
 assert.equal(s.panel.querySelector('#evo-deliverable'),null);assert.equal(s.panel.querySelector('[data-room-action=download]'),null);
 assert.equal(tab(s,'delivery').dataset.ready,'false');
 s.room.dispose();s.d.window.close();
});
