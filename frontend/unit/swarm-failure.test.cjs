const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../src');
function load(file) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file,'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,
    {exports, Set, Map, Date, require: name => load(path.resolve(path.dirname(file),name+'.ts'))});
  return exports;
}
const owner = load(root+'/conversation/presentation/swarm-failure-presentation.ts');
const background = load(root+'/background-activity-presentation.ts');
for (const language of ['en','zh']) test(language+': immediate bounded notices, replay suppression, new waves and truthful counts',()=>{
  const locale = JSON.parse(fs.readFileSync(root+'/i18n/locales/'+language+'.json'));
  const t = (key, vars={}) => (locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>vars[k]);
  const notices=[]; const notify=owner.createSwarmFailureNotices(t,n=>notices.push(n));
  const frame={type:'swarm_agent_complete',agentId:'a',status:'failed',error:'<img src=x onerror=alert(1)>'.repeat(80)};
  const agents=[{id:'a',status:'failed'},{id:'b',status:'running'},{id:'c',status:'pending'},{id:'d',status:'cancelled'},{id:'e',status:'done'}];
  notify('c','wave1',frame,agents); notify('c','wave1',frame,agents);
  assert.equal(notices.length,1); assert.equal(notices[0].detail.length,500);
  assert.ok(notices[0].hint.includes(t('swarm.failure.remaining',{running:1,waiting:1})));
  notify('c','wave2',frame,agents); assert.equal(notices.length,2);
  notify('c','hydrated',frame,agents,{status:'failed'}); assert.equal(notices.length,2);
  notify('c','cancelled',{...frame,status:'cancelled'},agents); assert.equal(notices.length,2);
  assert.deepEqual(JSON.parse(JSON.stringify(owner.swarmOutcomeCounts(agents))),{running:1,pending:1,succeeded:1,failed:1,cancelled:1,interrupted:0});
  const summary=background.backgroundActivitySummary({agents},(key,fallback,vars)=>t(key,vars));
  assert.ok(summary.includes(t('swarm.summary.failed',{count:1})));
  const dom=new JSDOM('');
  const row=background.renderBackgroundActivityAgent(dom.window.document,{id:'<img>',status:'failed',objective:'<script>bad()</script>'},(key,fallback)=>locale[key]||fallback);
  assert.equal(row.querySelector('script,img'),null);
  assert.ok(row.textContent.includes('<script>'));
});


test('restart interruption is distinct from Stop and may receive an authorized running replacement', () => {
  const presenter = load(root + '/conversation/presentation/swarm-agent-presentation.ts');
  const interrupted = { agentId: 'a', status: 'interrupted', finalAnswer: 'checkpoint' };
  assert.deepEqual(JSON.parse(JSON.stringify(owner.swarmOutcomeCounts([interrupted]))), {
    running: 0, pending: 0, succeeded: 0, failed: 0, cancelled: 0, interrupted: 1,
  });
  for (const language of ['en', 'zh']) {
    const locale = JSON.parse(fs.readFileSync(root + '/i18n/locales/' + language + '.json'));
    const tt = (key, fallback, vars = {}) => (locale[key] || fallback)
      .replace(/\{(\w+)\}/g, (_, key) => vars[key]);
    assert.equal(background.backgroundActivitySummary({agents: [interrupted, interrupted]}, tt),
      tt('swarm.summary.interrupted', '{count} interrupted', {count: 2}));
    const dom = new JSDOM('');
    const row = background.renderBackgroundActivityAgent(dom.window.document, interrupted, tt);
    assert.equal(row.dataset.status, 'interrupted');
    assert.ok(row.textContent.includes(tt('swarm.phase.interrupted', 'interrupted')));
  }
  assert.equal(presenter.mergeSwarmAgent(interrupted, { status: 'running' }).status, 'running');
  assert.equal(presenter.mergeSwarmAgent({ status: 'cancelled' }, { status: 'running' }).status, 'cancelled');
});
