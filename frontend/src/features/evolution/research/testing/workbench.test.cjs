const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');

test('bound Stop after launch and durable reopen cancels the exact Evolution run', async () => {
  const dom = new JSDOM('<body></body>');global.document=dom.window.document;
  global.window={setTimeout:()=>1,clearTimeout:()=>{}};
  const cancelled=[];let bareAborts=0;
  const registry={_paintResearch:()=>{},Api:{tasks:{get:async()=>({ok:true,status:'running'}),
    abort:async()=>{bareAborts++;return {ok:true};}},research:{record:async(taskId)=>({record:{task_id:taskId,
      direction:'same direction',lang:'en',status:'running',evolution_run_id:'run-restored'}})}}};
  registry._evolutionRunsApi={createRun:async()=>({run:{run_id:'run-created',task_id:'task-created',status:'executing'}}),
    cancelRun:async(runId)=>{cancelled.push(runId);return {run:{run_id:runId,status:'cancelled'}};}};
  const runtime=load('controllers/runtime.ts',{'../../../../feature-registry':{featureRegistry:registry},
    '../../../../i18n':{_i18nLang:'en'}});
  await runtime.startResearchJob('same direction');
  assert.equal(registry._researchStream.evolutionRunId,'run-created');
  await runtime.abortResearchJob();
  runtime.destroyResearchRuntime();
  await runtime.openResearchRecord('task-restored');
  assert.equal(registry._researchStream.evolutionRunId,'run-restored');
  await runtime.abortResearchJob();
  assert.deepEqual(cancelled,['run-created','run-restored']);assert.equal(bareAborts,0);
  runtime.destroyResearchRuntime();dom.window.close();
});

test('missing bound cancel capability fails visibly without falling back to bare abort', async () => {
  const registry={_paintResearch:()=>{},Api:{tasks:{abort:async()=>{throw new Error('unsafe fallback');}}}};
  global.window={setTimeout:()=>1,clearTimeout:()=>{}};
  const runtime=load('controllers/runtime.ts',{'../../../../feature-registry':{featureRegistry:registry},
    '../../../../i18n':{_i18nLang:'en'}});
  const stream=runtime.newResearchStream('bound');stream.taskId='bound-task';stream.evolutionRunId='bound-run';stream.status='running';
  registry._researchStream=stream;
  await runtime.abortResearchJob();
  assert.equal(stream.status,'running');assert.match(stream.commandError,/Evolution.*unavailable/);
  assert.equal(stream.cancelling,false);runtime.destroyResearchRuntime();
});

test('inflight task metadata restores binding and refuses a conflicting durable record', async () => {
  global.window={setTimeout:()=>1,clearTimeout:()=>{}};
  const registry={_paintResearch:()=>{},Api:{tasks:{list:async()=>({tasks:[{id:'task-resume',meta:{direction:'bound',evolution_run_id:'run-resume'}}]}),
    get:async()=>({ok:true,status:'running'}),abort:async()=>{throw new Error('bare abort');}},research:{record:async()=>({record:{task_id:'task-resume',direction:'bound',evolution_run_id:'run-conflicting'}})}}};
  const cancelled=[];registry._evolutionRunsApi={cancelRun:async(id)=>{cancelled.push(id);return {run:{run_id:id}};}};
  const runtime=load('controllers/runtime.ts',{'../../../../feature-registry':{featureRegistry:registry},
    '../../../../i18n':{_i18nLang:'en'}});
  assert.equal(await runtime.resumeInflightResearch(),true);
  await runtime.hydrateResearchFromStore(registry._researchStream);
  assert.equal(registry._researchStream.evolutionRunId,'run-resume');
  assert.match(registry._researchStream.commandError,/binding.*changed/);
  await runtime.abortResearchJob();assert.deepEqual(cancelled,['run-resume']);runtime.destroyResearchRuntime();
});

test('foreign cancellation reply cannot settle a bound task; legacy bare Stop remains available', async () => {
  global.window={setTimeout:()=>1,clearTimeout:()=>{}};
  let bare=0;const registry={_paintResearch:()=>{},Api:{tasks:{get:async()=>({ok:true,status:'running'}),
    abort:async()=>{bare++;return {ok:true};}}},_evolutionRunsApi:{cancelRun:async()=>({run:{run_id:'foreign'}})}};
  const runtime=load('controllers/runtime.ts',{'../../../../feature-registry':{featureRegistry:registry},
    '../../../../i18n':{_i18nLang:'en'}});
  const bound=runtime.newResearchStream('bound');bound.taskId='task';bound.evolutionRunId='run-owned';bound.status='running';registry._researchStream=bound;
  await runtime.abortResearchJob();assert.equal(bare,0);assert.equal(bound.status,'running');
  assert.match(bound.commandError,/identity changed/);assert.equal(bound.cancelling,false);
  runtime.destroyResearchRuntime();
  const legacy=runtime.newResearchStream('legacy');legacy.taskId='old-bare-task';legacy.status='running';registry._researchStream=legacy;
  await runtime.abortResearchJob();assert.equal(bare,1);assert.equal(legacy.status,'running');runtime.destroyResearchRuntime();
});
function load(file, ports = {}, cache = new Map()) {
  const full = path.resolve(root, file);
  if (cache.has(full)) return cache.get(full);
  const exports = {};
  cache.set(full, exports);
  const js = ts.transpileModule(fs.readFileSync(full, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function('require', 'exports', js)((name) => {
    if (name in ports) return ports[name];
    return load(path.relative(root, path.resolve(path.dirname(full), name + '.ts')), ports, cache);
  }, exports);
  return exports;
}
test('telemetry retains editor identity, selection, disclosures and workspace', () => {
  const dom = new JSDOM('<div id="host"></div>');
  global.document = dom.window.document;
  global.Element = dom.window.Element;
  const { patchResearchDom } = load('components/stable-dom.ts');
  const host = document.getElementById('host');
  patchResearchDom(host, '<div><details><summary>Evidence</summary>old</details><textarea data-rsw-field="hypothesis">saved</textarea><section id="researchWorkspace">editor</section><span>1</span></div>');
  const editor = host.querySelector('textarea');
  editor.value = 'unsaved'; editor.focus(); editor.setSelectionRange(2, 4);
  host.querySelector('details').open = true;
  const workspace = host.querySelector('section');
  patchResearchDom(host, '<div><details><summary>Evidence</summary>new</details><textarea data-rsw-field="hypothesis">server</textarea><section id="researchWorkspace">loading</section><span>2</span></div>');
  assert.equal(host.querySelector('textarea'), editor);
  assert.equal(editor.value, 'unsaved');
  assert.equal(editor.selectionStart, 2);
  assert.equal(document.activeElement, editor);
  assert.equal(host.querySelector('details').open, true);
  assert.equal(host.querySelector('section'), workspace);
  assert.equal(workspace.textContent, 'editor');
  assert.equal(host.querySelector('span').textContent, '2');
  dom.window.close();
});
test('duplicate submit is gated and failed cancel never invents aborted state', async () => {
  const registry = { _paintResearch: () => {} };
  let resolveStart; let starts = 0; let reads = 0;
  registry.Api = { tasks: {
    get: async () => { reads++; return {ok: true, status: 'running'}; },
    abort: async () => ({ok: false, error: 'permission denied'}),
  }};
  registry._evolutionRunsApi = {
    createRun: () => { starts++; return new Promise((resolve) => { resolveStart = resolve; }); },
    cancelRun: async () => ({ok: false, error: 'permission denied'}),
  };
  global.window = { setTimeout: () => 1, clearTimeout: () => {} };
  const runtime = load('controllers/runtime.ts', {
    '../../../../feature-registry': { featureRegistry: registry },
    '../../../../i18n': { _i18nLang: 'en' },
    '../api/reader': { openResearchReader: async () => {} },
  });
  const launch = runtime.startResearchJob('test');
  assert.equal(registry._researchStream.status, 'submitting');
  await runtime.startResearchJob('duplicate');
  assert.equal(starts, 1);
  resolveStart({run: {run_id: 'run-1', task_id: 'task-1', status: 'executing'}});
  await launch;
  await runtime.abortResearchJob();
  assert.equal(registry._researchStream.status, 'running');
  assert.equal(registry._researchStream.commandError, 'permission denied');
  assert.equal(registry._researchStream.cancelling, false);
  const stream = registry._researchStream;
  const a = runtime.pollResearchOnce(stream); const b = runtime.pollResearchOnce(stream);
  assert.equal(a, b); await a;
  assert.equal(reads, 2);
  runtime.destroyResearchRuntime();
  assert.equal(registry._researchStream, null);
});

test('terminal push wins over late running snapshots and duplicate events', () => {
  const registry = {};
  const runtime = load('controllers/runtime.ts', {
    '../../../../feature-registry': { featureRegistry: registry },
    '../../../../i18n': { _i18nLang: 'en' },
  });
  const stream = runtime.newResearchStream('question');
  stream.status = 'running';
  runtime.researchIngestEvent(stream, {seq: 10, type: 'done', status: 'aborted'});
  assert.equal(stream.status, 'aborted');
  runtime.researchApplySnapshot(stream, {status: 'running', cursor: {next: 9}, result: {accepted: [{}]}});
  assert.equal(stream.status, 'aborted');
  assert.equal(stream.accepted, 0);
  assert.equal(runtime.researchIngestEvent(stream, {seq: 10, type: 'tool_start', toolCallId: 'late'}), false);
  assert.equal(stream.toolRounds.length, 0);
});

test('harvest receipt renders an escaped literature row and durable reopen retains exact run', async () => {
  const dom = new JSDOM('<main id="host"></main>');
  global.document = dom.window.document;
  global.window = dom.window;
  const record = { task_id:'saved-run',direction:'same direction',lang:'zh',status:'interrupted',created_at:Date.now(),updated_at:Date.now(),papers:[{arxivId:'2401.00001',title:'<img src=x onerror=alert(1)>',status:'parsed',textLength:123}],result:{survey_md:'this run only'} };
  const registry = {_paintResearch:()=>{},Api:{research:{record:async()=>({record})}}};
  const ports = {'../../../../feature-registry':{featureRegistry:registry},'../../../../i18n':{_i18nLang:'zh'}};
  const runtime = load('controllers/runtime.ts', ports);
  const stream = runtime.newResearchStream('live');
  stream.literatureView = 'list';
  runtime.researchApplyEvent(stream,{type:'paper_done',result:record.papers[0]});
  runtime.researchApplyEvent(stream,{type:'paper_done',result:record.papers[0]});
  assert.equal(stream.papers.length,1);
  const cards = load('components/live-library.ts',ports);
  const host = document.getElementById('host');
  host.innerHTML=cards.paperCards(stream);
  assert.equal(host.querySelectorAll('.rs-paper-table tbody tr').length,1);
  assert.equal(host.querySelector('img'),null);
  assert.ok(host.textContent.includes('<img src=x'));
  assert.ok(await runtime.openResearchRecord('saved-run'));
  assert.equal(registry._researchStream.status,'interrupted');
  assert.equal(registry._researchStream.papers[0].arxivId,'2401.00001');
  assert.equal(registry._researchStream.surveyMd,'this run only');
  runtime.destroyResearchRuntime(); dom.window.close();
});

// ── Landing stage (showResearchLanding) ──────────────────────────────
// The foundry landing is the entry point to auto-research: a composer with
// fill-in guidance plus a template gallery. Exploration records stay in the
// sidebar rail, so the landing carries no hero, pipeline journey or recent
// list. These pins drive the REAL view.ts through jsdom so a markup/behaviour
// regression fails here, not in a user session.
function landingPorts(registry, runtime) {
  return {
    '../../../../feature-registry': { featureRegistry: registry },
    '../../../../i18n': { _i18nLang: 'en' },
    '../../../../html-safety': { escapeHtml: (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;') },
    './live-library': { paperCards: () => '', bindPaperCards: () => {} },
    './stable-dom': { patchResearchDom: () => {} },
    '../../../paper/research-cycle': {
      researchNumber: (v) => { const n = Number(v); return Number.isFinite(n) ? n : null; },
      researchText: (v) => (v == null ? '' : String(v)),
    },
    '../controllers/runtime': runtime,
    '../controllers/workspace': { loadResearchWorkspace: () => {}, researchWorkspaceHtml: () => '', setResearchManuscriptVisible: () => {} },
  };
}

function landingDom() {
  const dom = new JSDOM('<div id="researchViewer"></div><div id="researchSidebarCurrent"></div>');
  global.document = dom.window.document;
  global.window = dom.window;
  global.Event = dom.window.Event;
  dom.window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  return dom;
}

test('landing renders the composer and template gallery only', () => {
  const dom = landingDom();
  const registry = { t: (k) => k, _researchStream: null };
  const view = load('components/view.ts', landingPorts(registry, {
    openResearchRecord: async () => true,
    resumeSavedResearch: async () => {},
    researchIsRunning: () => false,
  }));
  view.showResearchLanding();

  // Entry contract (the browser surface test drives the same two hooks).
  const input = document.getElementById('paperResearchInput');
  assert.ok(input, 'direction textarea missing');
  assert.equal(input.getAttribute('maxlength'), '2000');
  assert.ok(document.querySelector('[data-tofu-action*="_submitResearchDirection"]'),
    'start button lost its submit action');

  // Redundant blocks stay gone: hero, pipeline journey and the recent list
  // (records live in the sidebar rail, so the landing must not duplicate them).
  assert.equal(document.querySelector('.rs-hero'), null, 'landing must not resurrect the hero');
  assert.equal(document.querySelector('.rs-journey'), null,
    'pipeline phases belong to the console, not the landing');
  assert.equal(document.getElementById('researchLandingRecent'), null,
    'recent records must not duplicate the sidebar rail');

  // Template gallery fills the composer through the real action and the
  // autosize/counter wiring observes the injected value.
  const cards = [...document.querySelectorAll('.rs-template-card')];
  assert.equal(cards.length, 3);
  assert.ok(cards[0].getAttribute('data-tofu-action').includes('_fillResearchTemplate(0)'));
  view.fillResearchTemplate(0);
  assert.equal(input.value, 'evolution.research.template1');
  assert.equal(document.getElementById('researchCharCount').textContent,
    'evolution.research.template1'.length + ' / 2000');

  // No live stream: the sidebar current card stays empty.
  assert.equal(document.getElementById('researchSidebarCurrent').innerHTML, '');
  dom.window.close();
});

test('literature sorts both directions with missing last and zero valid; merges harvest metadata', () => {
  const { sortedPapers, mergeLiterature } = load('state/literature.ts');
  const papers = [{paperId:'a',year:2020,citationCount:0},{paperId:'b',year:null,citationCount:null},{paperId:'c',year:2024,citationCount:9}];
  for (const field of ['year','citations']) {
    assert.deepEqual(sortedPapers(papers,field+'-asc').map(p=>p.paperId),['a','c','b']);
    assert.deepEqual(sortedPapers(papers,field+'-desc').map(p=>p.paperId),['c','a','b']);
  }
  const stream = {papers:[],literatureSort:'citations-asc'};
  mergeLiterature(stream,{papers:[{paperId:'canonical',arxivId:'1',venue:'ICML',citationCount:0}]});
  mergeLiterature(stream,{papers:[{arxivId:'1',status:'parsed'}],error:'saved failure',failed_stage:'survey'});
  assert.equal(stream.papers.length,1); assert.equal(stream.papers[0].venue,'ICML');
  assert.equal(stream.papers[0].paperId,'canonical'); assert.equal(stream.papers[0].citationCount,0);
  assert.equal(stream.error,'saved failure'); assert.equal(stream.failedStage,'survey');
  assert.equal(stream.literatureSort,'citations-asc');
});
test('real citation edges are bounded and escaped; sorting control survives telemetry', () => {
  const dom = new JSDOM('<main id="host"></main>'); global.document=dom.window.document;
  const library = load('components/live-library.ts',{'../../../../feature-registry':{featureRegistry:{}}});
  const {patchResearchDom} = load('components/stable-dom.ts');
  const stream={literatureView:'list',papers:[{paperId:'a',title:'<img src=x>',year:2020},{paperId:'b',title:'B',year:2024}],citationEdges:[{from:'a',to:'b',relation:'cites'},{from:'b',to:'a',relation:'similar'}]};
  const host=document.querySelector('#host'); const paint=()=>{patchResearchDom(host,library.paperCards(stream));library.bindPaperCards(host,stream,paint);};
  paint(); assert.equal(host.querySelector('img'),null); assert.equal(host.querySelectorAll('.rs-lineage li').length,1);
  const select=host.querySelector('select');select.focus();select.value='year-asc';select.onchange();
  stream.papers[0].citationCount=0;paint();
  assert.equal(host.querySelector('select'),select);assert.equal(document.activeElement,select);assert.equal(select.value,'year-asc');
  assert.equal(host.querySelector('tbody tr').id,'rs-paper-a');
  assert.match(library.citationLineage({papers:[]},key=>key),/noEdges/);
  dom.window.close();
});


test('sparse parser receipts retain bibliometrics and canonical citation identity', () => {
  const {mergeLiterature} = load('state/literature.ts');
  const stream = {papers: []};
  mergeLiterature(stream, {papers:[{paperId:'doi:one',doi:'one',arxivId:'1',venue:'ICML',citationCount:0,status:'discovered'}]});
  mergeLiterature(stream, {papers:[{paperId:'parser-hash',arxivId:'1',venue:'',citationCount:null,status:'parsed'}]});
  assert.equal(stream.papers[0].paperId, 'doi:one');
  assert.equal(stream.papers[0].citationCount, 0);
  assert.equal(stream.papers[0].venue, 'ICML');
  mergeLiterature(stream, {papers:[{paperId:'doi:one',doi:'one',status:'discovered'}],citation_edges:[]});
  assert.equal(stream.papers[0].status, 'parsed');
  const old = {papers:[{paperId:'parser-hash',arxivId:'1',status:'parsed'}]};
  mergeLiterature(old, {papers:[{paperId:'doi:one',arxivId:'1',status:'discovered'}],citation_edges:[]});
  assert.equal(old.papers[0].paperId,'doi:one');
  assert.equal(old.papers[0].status,'parsed');
});

test('lineage SVG is a bounded real-edge projection, including same-year and unknown-year papers', () => {
  const dom = new JSDOM('<main id="host"></main>'); global.document=dom.window.document;
  const {citationLineage} = load('components/live-library.ts');
  const papers=Array.from({length:200},(_,i)=>({paperId:String(i),title:i===0?'<img src=x onerror=alert(1)>':'Paper '+i,year:i<4?2024:null}));
  const citationEdges=Array.from({length:199},(_,i)=>({from:String(i),to:String(i+1),relation:'cites'}));
  citationEdges.unshift(citationEdges[0],{from:'0',to:'0',relation:'cites'},{from:'0',to:'2',relation:'similar'},{from:'0',to:'outside',relation:'cites'});
  document.querySelector('#host').innerHTML=citationLineage({papers,citationEdges},key=>key);
  assert.equal(document.querySelectorAll('.rs-lineage-edge').length,40);
  assert.equal(document.querySelectorAll('.rs-lineage-node').length,41);
  assert.equal(document.querySelectorAll('.rs-lineage li').length,40);
  assert.equal(document.querySelector('img'),null);
  assert.ok(document.querySelector('svg').textContent.includes('<img src=x'));
  assert.ok(document.querySelector('svg').textContent.includes('unknownYear'));
  for(const path of document.querySelectorAll('.rs-lineage-edge')) assert.ok(!/NaN|undefined/.test(path.getAttribute('d')));
  dom.window.close();
});

test('record hydration is single-flight and never falls back to another run of the same direction', async () => {
  let resolveRecord; let records=0; let lookups=0;
  const registry={Api:{research:{
    record:()=>{records++;return new Promise(resolve=>{resolveRecord=resolve;});},
    lookup:async()=>{lookups++;return {accepted:[{title:'other run'}]};},
  }}};
  const runtime=load('controllers/runtime.ts',{'../../../../feature-registry':{featureRegistry:registry},'../../../../i18n':{_i18nLang:'en'}});
  const stream=runtime.newResearchStream('same direction');stream.status='error';stream.taskId='this-run';registry._researchStream=stream;
  const a=runtime.hydrateResearchFromStore(stream);const b=runtime.hydrateResearchFromStore(stream);
  assert.equal(a,b);assert.equal(records,1);
  resolveRecord({ok:true,record:null});await a;
  assert.equal(lookups,0);assert.equal(stream.accepted,0);assert.equal(stream.verdictAvailable,undefined);
  const saved=runtime.hydrateResearchFromStore(stream);
  resolveRecord({ok:true,record:{error:'actual failure',failed_stage:'survey',papers:[{paperId:'p',citationCount:0}]}});await saved;
  assert.equal(stream.error,'actual failure');assert.equal(stream.failedStage,'survey');assert.equal(stream.hydrated,true);
  assert.equal(stream.papers[0].citationCount,0);assert.equal(lookups,0);
});

test('submission gates preserve the complete server decision and escape new gate labels', () => {
  const { submissionReadiness, submissionGatesHtml } = load('components/submission-gates.ts');
  const gates = ['protocol','run_evidence','claims','manuscript','compile','baseline_fairness',
    'uncertainty','seeds','validation_split','claim_traceability','new_<check>'].map(id => ({id,ok:true}));
  const decision = submissionReadiness({ready:false,gates});
  const html = submissionGatesHtml(decision, (_key, fallback) => fallback);
  assert.equal((html.match(/data-gate-id=/g)||[]).length, 11);
  assert.match(html, /Submission gate blocked/);
  assert.match(html, /new &lt;check&gt;/);
  assert.equal(submissionReadiness({ready:true,gates:[]}), null);
  assert.match(submissionGatesHtml(null, (_key, fallback) => fallback), /Save changes/);
});

test('workspace edits invalidate server gates while reader nodes survive CAS refresh and retire on hide', async () => {
  const dom = new JSDOM('<section id="researchWorkspace"><div class="rsw-loading"></div></section>');
  global.window = dom.window; global.document = dom.window.document; global.Element = dom.window.Element;
  let created=0, destroyed=0, resolveSave;
  const workspace = {revision:1,direction:'reader',lang:'en',stage:'writing',selected_idea_id:'',hypothesis:'',
    protocol:{},capability_bindings:[],runs:[],claims:[],manuscript:{},source_files:[],figures:[],tables:[],
    compilation:{status:'passing',mode:'paper_build',build:{document_id:'d'.repeat(64),build_id:'b'.repeat(32)}},publication:{}};
  const readiness = {ready:true,gates:[{id:'claim_traceability',ok:true}]};
  const registry = {_researchStream:{direction:'reader',lang:'en',acceptedIdeas:[]},Api:{research:{
    workspace:async()=>({workspace,readiness}),capabilities:async()=>({catalog:{capabilities:[],tools:[]}}),
    saveWorkspace:body=>new Promise(resolve=>{resolveSave=()=>resolve({workspace:{...body.workspace,revision:body.expected_revision+1},readiness})}),
  }}};
  class Preview {
    constructor(host){this.host=host;this.element=document.createElement('canvas');host.append(this.element);created++}
    show(){} view(){return null} destroy(){destroyed++;this.element.remove()}
  }
  class Manuscript {
    show(host){if(this.preview?.host!==host){this.hide();this.preview=new Preview(host)}}
    hide(){this.preview?.destroy();this.preview=null}
    destroy(){this.hide()}
  }
  const controller=load('controllers/workspace.ts',{
    '../../../../feature-registry':{featureRegistry:registry},
    './manuscript':{ResearchManuscript:Manuscript},
  });
  try {
    await controller.loadResearchWorkspace('reader');
    assert.equal(created,0,'hidden research pages do not allocate a reader');
    controller.setResearchManuscriptVisible(true);
    assert.equal(created,1);
    const canvas=document.querySelector('canvas');
    const editor=document.querySelector('[data-rsw-field="hypothesis"]');
    const gates=document.querySelector('.rsw-output .rsw-gates');
    assert.ok(gates.classList.contains('is-ready'));
    editor.value='unsaved';editor.dispatchEvent(new window.Event('input',{bubbles:true}));
    assert.equal(gates.classList.contains('is-ready'),false);
    const saving=controller.saveResearchWorkspace();
    // A server reply for an earlier edit must not mark a newer draft ready.
    editor.value='newer edit';editor.dispatchEvent(new window.Event('input',{bubbles:true}));
    resolveSave();await saving;
    assert.equal(document.querySelector('.rsw-output .rsw-gates').classList.contains('is-ready'),false);
    assert.equal(document.querySelector('canvas'),canvas);
    assert.equal(editor.value,'newer edit');
    const saveAgain=controller.saveResearchWorkspace();resolveSave();await saveAgain;
    assert.ok(document.querySelector('.rsw-output .rsw-gates').classList.contains('is-ready'));
    assert.equal(document.querySelector('canvas'),canvas);assert.equal(created,1);
    controller.setResearchManuscriptVisible(false);
    assert.equal(destroyed,1);assert.equal(document.querySelector('canvas'),null);
    controller.setResearchManuscriptVisible(true);assert.equal(created,2);
  } finally {controller.destroyResearchWorkspace();dom.window.close()}
  assert.equal(destroyed,2);
});

test('field native adapters preserve explicit selection and numeric serialization', () => {
  const dom=new JSDOM('<main></main>');global.document=dom.window.document;
  const {createField}=load('../../../ui/field.ts');
  const direction=createField({label:'Metric direction',controlId:'direction',value:'',choices:[
    {value:'',label:'Choose explicitly'},{value:'higher',label:'Higher is better'}]});
  const threshold=createField({label:'Effect threshold',controlId:'threshold',inputType:'number',value:'-0.05'});
  const host=document.querySelector('main');host.innerHTML=direction.el.outerHTML+threshold.el.outerHTML;
  assert.equal(host.querySelector('select').value,'');
  assert.equal(host.querySelector('input').value,'-0.05');
  assert.equal(host.querySelector('label').htmlFor,host.querySelector('select').id);
  assert.equal(host.querySelector('.ui-select > select'),host.querySelector('select'));
  dom.window.close();
});

for (const action of ['analyze', 'compile']) {
  test(`workspace ${action} preserves invocation model through asynchronous save`, async () => {
    const dom = new JSDOM('<section id="researchWorkspace"></section>');
    global.window=dom.window;global.document=dom.window.document;global.Element=dom.window.Element;
    window.confirm=()=>true;
    const workspace={revision:1,direction:'model-choice',lang:'en',stage:'evidence',selected_idea_id:'',
      hypothesis:'',protocol:{},capability_bindings:[],runs:[],claims:[],manuscript:{},source_files:[],
      figures:[],tables:[],compilation:{status:'not_run'},publication:{}};
    let resolveSave;const starts=[];
    const registry={config:{model:' chosen-model '},_researchStream:{direction:'model-choice',lang:'en',acceptedIdeas:[]},
      Api:{research:{workspace:async()=>({workspace}),capabilities:async()=>({catalog:{capabilities:[],tools:[]}}),
        saveWorkspace:body=>new Promise(resolve=>{resolveSave=()=>resolve({workspace:{...body.workspace,revision:2}});})},
        tasks:{start:async(kind,body)=>{starts.push({kind,body});return {taskId:'controlled-task'};},
          get:async()=>({status:'done',result:{workspace}})}}};
    const controller=load('controllers/workspace.ts',{'../../../../feature-registry':{featureRegistry:registry},
      './manuscript':{ResearchManuscript:class {show(){} hide(){} destroy(){}}}});
    try {
      await controller.loadResearchWorkspace('model-choice');
      const pending=controller.startResearchAction(action);
      assert.equal(typeof resolveSave,'function');
      registry.config.model='later-model';resolveSave();await pending;
      assert.equal(starts.length,1);assert.equal(starts[0].kind,'research-action');
      assert.equal(starts[0].body.expected_revision,2);
      assert.equal(starts[0].body.model,action==='compile'?undefined:'chosen-model');
    } finally {controller.destroyResearchWorkspace();dom.window.close();}
  });
}
