const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
function owner() {
  const dom = new JSDOM('<main id="history"></main>');
  global.document = dom.window.document;
  const buttonSource = fs.readFileSync(path.resolve(__dirname, '../../../../ui/button.ts'), 'utf8');
  const buttonModule = {exports:{}};
  new Function('module','exports',ts.transpileModule(buttonSource,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(buttonModule,buttonModule.exports);
  const module = {exports:{}};
  const source = fs.readFileSync(path.resolve(__dirname, '../components/history.ts'),'utf8');
  new Function('require','module','exports',ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(()=>buttonModule.exports,module,module.exports);
  return {host:dom.window.document.querySelector('main'), history:new module.exports.ResearchHistory()};
}
const snapshot=(direction='A', revision=1)=>({direction,lang:'en',revision,history:{head:'a'.repeat(64),count:24}});
const page=(id='negative',cursor=16)=>({history_page:{page:'b'.repeat(64),head:'a'.repeat(64),total:24,revision:1,next_cursor:cursor,runs:[{id,scientific_outcome:'refuted'}]}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const tr=(_key,fallback)=>fallback;
test('failed older page retains evidence and retry cursor; same snapshot retains open disclosure',async()=>{
 const {host,history}=owner(); const calls=[]; let fail=true;
 const read=async(_d,_l,cursor)=>{calls.push(cursor);if(cursor&&fail)throw Error('offline');return page(cursor?'older':'negative',cursor?null:16);};
 history.sync(host,snapshot(),read,tr);host.querySelector('button').click();await tick();
 host.querySelector('details').open=true; const detail=host.querySelector('details');
 history.sync(host,snapshot(),read,tr);assert.equal(host.querySelector('details'),detail);assert(detail.open);
 host.querySelectorAll('button')[1].click();await tick();assert.match(host.textContent,/negative/);assert.match(host.textContent,/offline/);
 fail=false;host.querySelectorAll('button')[2].click();await tick();assert.match(host.textContent,/older/);assert(!host.textContent.includes('negative'));
 assert.deepEqual(calls,[0,16,16]);history.dispose();
});
test('direction switch aborts old transport and fences late completion; disposal releases page',async()=>{
 const {host,history}=owner();let resolve;let signal;
 history.sync(host,snapshot(),(_d,_l,_c,s)=>{signal=s;return new Promise(r=>resolve=r);},tr);
 host.querySelector('button').click();history.sync(host,snapshot('B'),async()=>page('B'),tr);
 assert(signal.aborted);resolve(page('foreign'));await tick();assert(!host.textContent.includes('foreign'));
 history.dispose();history.sync(host,snapshot('C'),async()=>page('C'),tr);assert(!host.textContent.includes('foreign'));
});
test('different server revision and malformed records cannot replace a visible page',async()=>{
 const {host,history}=owner();let response=page();
 history.sync(host,snapshot(),async()=>response,tr);host.querySelector('button').click();await tick();
 response={history_page:{...page().history_page,revision:2,runs:[{id:'wrong'}]}};
 host.querySelector('button').click();await tick();assert.match(host.textContent,/negative/);assert(!host.textContent.includes('wrong'));
 response={history_page:{...page().history_page,runs:[null]}};
 host.querySelector('button').click();await tick();assert.match(host.textContent,/negative/);assert(host.querySelector('[role=alert]'));
});
