const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
function owner() {
 const dom = new JSDOM('<main id="recovery"></main>'); global.document = dom.window.document;
 const compile = (file, require) => {
  const mod = {exports:{}};
  new Function('module','exports','require',ts.transpileModule(fs.readFileSync(file,'utf8'),
   {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(mod,mod.exports,require);
  return mod.exports;
 };
 const kit = {...compile(path.resolve(__dirname,'../../../../ui/button.ts')),
              ...compile(path.resolve(__dirname,'../../../../ui/field.ts'))};
 const {ResearchObservationRecovery} = compile(path.resolve(__dirname,'../components/observation-recovery.ts'),()=>kit);
 return {dom,host:document.getElementById('recovery'),recovery:new ResearchObservationRecovery()};
}
const tick = () => new Promise(resolve=>setImmediate(resolve));
const tr = (_key,fallback)=>fallback;
const page = (task='task',next='') => {
 const binding={task_id:task,user_id:1,revision:7,basis_digest:'a',direction_hash:'d',lang:'en'};
 return {found:true,binding,next_cursor:next,observations:[{ordinal:1,tool:'run_command',binding,
  receipt:{status:'done',result_excerpt:'preserved original <script>'}}]};
};
test('old page remains on failed next page and retry uses the same cursor',async()=>{
 const {host,recovery}=owner();let fail=true;const cursors=[];
 recovery.sync(host,'A',async(task,cursor)=>{cursors.push(cursor);if(cursor&&fail)throw Error('offline');return page(task,cursor?'':'next');},tr);
 recovery.suggestTask('task');host.querySelector('button').click();await tick();
 host.querySelectorAll('button')[1].click();await tick();
 assert.match(host.textContent,/preserved original/);assert.match(host.textContent,/offline/);
 fail=false;host.querySelectorAll('button')[2].click();await tick();assert(!host.textContent.includes('offline'));
 assert.deepEqual(cursors,['','next','next']);assert.equal(host.querySelectorAll('script').length,0);recovery.dispose();
});
test('editing task cancels flight and removes obsolete pagination; late result is fenced',async()=>{
 const {dom,host,recovery}=owner();let resolve,signal;
 recovery.sync(host,'A',(_task,_cursor,s)=>{signal=s;return new Promise(r=>resolve=r);},tr);
 recovery.suggestTask('task');host.querySelector('button').click();
 const input=host.querySelector('input');input.value='other';input.dispatchEvent(new dom.window.Event('input'));
 assert(signal.aborted);resolve(page());await tick();assert.equal(host.querySelectorAll('article').length,0);
 assert.equal(host.querySelector('input'),input);assert.equal(host.querySelectorAll('button').length,1);recovery.dispose();
});
test('workspace switch and disposal abort the request and do not paint foreign evidence',async()=>{
 const {host,recovery}=owner();let resolve,signal;
 recovery.sync(host,'A',(_task,_cursor,s)=>{signal=s;return new Promise(r=>resolve=r);},tr);
 recovery.suggestTask('task');host.querySelector('button').click();recovery.sync(host,'B',async()=>page('other'),tr);
 assert(signal.aborted);resolve(page());await tick();assert.equal(host.querySelectorAll('article').length,0);
 assert.equal(host.querySelector('input').value,'');recovery.dispose();
});
