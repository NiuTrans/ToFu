const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript'),{JSDOM}=require('jsdom');
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 let full=path.resolve(path.dirname(parent),name+'.ts');if(!fs.existsSync(full))full=path.join(full.slice(0,-3),'index.ts');const exports={};
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>name.endsWith('/i18n')?{t:k=>k}:load(name,full),exports);return exports;
}
const tick=()=>new Promise(r=>setImmediate(r));
const run=id=>({run_id:id,objective:'Goal '+id,status:'executing',task_id:'task-'+id,attempt:1,receipts:[],result:'',review:null,actuals:{tokens:0}});
function setup(api={}){
 const d=new JSDOM('<section id="panel"></section><div id="objects"></div><div id="shortcuts"></div>',{pretendToBeVisual:true});
 Object.assign(global,{window:d.window,document:d.window.document});
 const panel=document.getElementById('panel'),objects=document.getElementById('objects');let reads=0;
 const mod=load('goal-room'),room=mod.mountGoalRoom(panel,objects,document.getElementById('shortcuts'),{api,copy:k=>k,refresh:async()=>{reads++;},openTask:()=>{},readDelivery:()=>{}});
 return {d,panel,objects,room,mod,reads:()=>reads};
}
const material={material_id:'m',run_id:'a',kind:'paper',title:'Source <safe>',url:'https://example.org/paper',note:'Useful',origin:'agent',task_id:'task-a'};
test('shelf renders safe external links, accurate counts, and never leaks materials across goals',()=>{
 const s=setup();s.room.update(run('a'),{materials:[material],stages:[],criteria:[]});
 assert.equal(s.panel.querySelectorAll('.evo-material-list li').length,1);assert.equal(s.panel.querySelector('a').textContent.trim(),'Source <safe> ↗');
 assert.equal(s.panel.querySelector('a').rel,'noopener noreferrer');
 assert.match(s.objects.querySelector('[data-place=library]').textContent,/1/);
 s.room.update(run('b'),{materials:[],stages:[],criteria:[]});assert.equal(s.panel.querySelectorAll('.evo-material-list li').length,0);assert.ok(!s.panel.textContent.includes('Source <safe>'));
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
 s.panel.querySelector('form').dispatchEvent(new s.d.window.Event('submit',{bubbles:true,cancelable:true}));
 s.room.update(run('b'));s.panel.querySelector('[name=title]').value='Keep B';finish({});await tick();
 assert.equal(writes[0].id,'a');assert.equal(s.panel.querySelector('[name=title]').value,'Keep B');assert.equal(s.reads(),0);
 s.room.dispose();s.d.window.close();
});

test('work inspector shows terminal receipts, then releases its retained island on section switch',()=>{
 const s=setup();s.room.update({...run('a'),status:'completed',receipts:[{task_id:'old-task',stage:'executing',status:'completed'}],result:'Verified delivery'}, {materials:[material],stages:[],criteria:[]});
 s.objects.querySelector('[data-place=work]').click();
 assert.match(s.panel.querySelector('[data-task-id="old-task"]').textContent,/completed/);
 assert.doesNotMatch(s.panel.querySelector('[data-task-id="old-task"]').textContent,/executing/);
 assert.equal(s.panel.querySelectorAll('#evo-deliverable').length,0);
 s.objects.querySelector('[data-place=library]').click();assert.equal(s.panel.querySelector('.evo-work-inspector'),null);
 assert.match(s.panel.querySelector('.evo-material-list').textContent,/Source <safe>/);
 s.room.dispose();s.d.window.close();
});
