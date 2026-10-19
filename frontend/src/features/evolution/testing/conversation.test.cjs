const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');const {JSDOM}=require('jsdom');
const source=path.resolve(__dirname,'..');
function load(file,cache=new Map()){
 let full=path.resolve(source,file);if(!fs.existsSync(full)&&full.endsWith('.ts'))full=path.join(full.slice(0,-3),'index.ts');if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>name.endsWith('/i18n')?{t:key=>key}:load(path.relative(source,path.resolve(path.dirname(full),name+'.ts')),cache),exports);return exports;
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const run=id=>({run_id:id,objective:'Goal '+id,status:'executing'});
const state=(updates=[],extra={})=>({updates,revision:updates.at(-1)?.seq||0,before_seq:updates[0]?.seq||0,has_earlier:false,delivery:{},coordinator:{status:'idle'},...extra});
const entry=(seq,content)=>({seq,content,kind:'message',origin:'user',references:[]});
function setup(api={}){
 const d=new JSDOM('<section></section>');Object.assign(global,{document:d.window.document,Element:d.window.Element});const host=document.querySelector('section'),navigations=[];
 const mounted=load('conversation.ts').mountGoalConversation(host,{api,copy:key=>key,refresh:async()=>{},navigate:p=>navigations.push(p)});
 mounted.update(run('a'),state());const input=host.querySelector('textarea');const submit=()=>host.querySelector('form').dispatchEvent(new d.window.Event('submit',{bubbles:true,cancelable:true}));
 return {d,host,mounted,input,submit,navigations,close:()=>{mounted.dispose();d.window.close();}};
}
test('message retry preserves key and never creates execution; drafts and replies are goal-scoped',async()=>{
 const calls=[];let success=false;const s=setup({sendGoalMessage:async(...args)=>{calls.push(args);if(!success)throw Error('offline');return {update:entry(1,args[1]),coordinator:{status:'pending'}};}});
 s.input.value='Why is this slow?';s.submit();await tick();assert.equal(s.input.value,'Why is this slow?');
 s.mounted.update(run('b'),state());s.input.value='B draft';s.mounted.update(run('a'),state());assert.equal(s.input.value,'Why is this slow?');
 success=true;s.host.querySelector('[data-dialogue=retry]').click();await tick();assert.deepEqual(calls[0],calls[1]);assert.equal(s.input.value,'');assert.match(s.host.textContent,/wakePending/);
 s.mounted.update(run('b'),state());assert.equal(s.input.value,'B draft');assert.doesNotMatch(s.host.querySelector('.evo-conversation-log').textContent,/Why is this slow/);assert.deepEqual(s.navigations,[]);s.close();
});
test('late send cannot append to another goal and text is escaped',async()=>{
 let resolve;const s=setup({sendGoalMessage:()=>new Promise(r=>resolve=r)});s.input.value='A request';s.submit();s.mounted.update(run('b'),state([entry(1,'<img src=x onerror=alert(1)>')]));
 resolve({update:entry(2,'A reply'),coordinator:{status:'idle'}});await tick();assert.doesNotMatch(s.host.textContent,/A reply/);assert.equal(s.host.querySelectorAll('img').length,0);s.close();
});
test('explicit follow-up creates linked work while a normal message stays conversation',async()=>{
 const calls=[];const s=setup({followupRun:async(...args)=>{calls.push(args);return {run:run('child')};}});s.mounted.update({...run('a'),status:'completed'},state());
 s.input.value='Add a Chinese version';s.host.querySelector('[data-dialogue=followup]').click();await tick();assert.equal(calls.length,1);assert.deepEqual(s.navigations,['evolution/runs/child']);s.close();
});
test('receipts update independently of event text and full content is safe',()=>{
 const s=setup();s.mounted.update(run('a'),state([entry(1,'Change direction')],{delivery:{'1':{status:'assigned',note:'queued'}}}));
 assert.equal(s.host.querySelector('.evo-message-receipt').dataset.status,'assigned');
 s.mounted.update(run('a'),state([entry(1,'Change direction')],{delivery:{'1':{status:'accepted',note:'will test'}}}));assert.equal(s.host.querySelector('.evo-message-receipt').dataset.status,'accepted');s.close();
});
