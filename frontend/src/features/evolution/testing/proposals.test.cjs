const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const ts=require('typescript');
const source=path.resolve(__dirname,'..');
const locale=JSON.parse(fs.readFileSync(path.resolve(source,'../../i18n/locales/en.json'),'utf8'));
function load(file, cache=new Map()){
 const full=path.resolve(source,file);if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>name.endsWith('/i18n')?{t:(key,params)=>{const raw=locale[key]||key;return params?raw.replace(/\{(\w+)\}/g,(_,k)=>String(params[k])):raw;}}:load(path.relative(source,path.resolve(path.dirname(full),name+'.ts')),cache),exports);return exports;
}

test('proposals panel renders each pending decision with gate actions and evidence count',()=>{
 const {proposalsPanelHtml}=load('proposals.ts');
 const html=proposalsPanelHtml({items:[
   {mutation_id:'m1',kind:'skill_candidate',status:'proposed',version:2,title:'引用校验技能',evidence_count:3,updated_at:20},
   {mutation_id:'m2',kind:'budget_policy',status:'confirmed',version:1,title:'预算收紧',evidence_count:1,updated_at:10},
 ],degraded:false});
 assert.equal((html.match(/evo-proposal-row/g)||[]).length,2);
 assert.ok(html.includes('data-studio-action="proposal-enable" data-id="m1"'));
 assert.ok(html.includes('data-studio-action="proposal-reject" data-id="m2"'));
 assert.ok(html.includes('3 evidence episodes'));
 assert.ok(html.includes('#/evolution/capabilities/m1'));
 assert.ok(!html.includes('No proposals waiting'));
});

test('receipts survive re-render and decided rows outlive the pending list',()=>{
 const {proposalsPanelHtml}=load('proposals.ts');
 const pending={items:[{mutation_id:'m1',kind:'skill_candidate',status:'proposed',version:1,title:'技能',evidence_count:1,updated_at:1}],degraded:false};
 const receipts=new Map([['m1',{result:{mutation:{},final_status:'needs_waiver',receipt:{verdict:'unmeasurable'}},title:'技能'}]]);
 const first=proposalsPanelHtml(pending,receipts);
 assert.ok(first.includes('data-status="needs_waiver"'));
 assert.ok(first.includes('data-studio-action="proposal-waiver" data-id="m1"'));
 // The mutation leaves the pending list after a decision: the row must stay
 // visible as a dismissible decided row instead of vanishing silently.
 const decided=proposalsPanelHtml({items:[],degraded:false},new Map([['m1',{result:{mutation:{},final_status:'active'},title:'技能'}]]));
 assert.ok(decided.includes('Recently decided'));
 assert.ok(decided.includes('data-studio-action="proposal-dismiss" data-id="m1"'));
 assert.ok(!decided.includes('No proposals waiting'));
});

test('empty and degraded panels are explicit states',()=>{
 const {proposalsPanelHtml}=load('proposals.ts');
 assert.ok(proposalsPanelHtml({items:[],degraded:false}).includes('No proposals waiting'));
 assert.ok(proposalsPanelHtml({items:[],degraded:true}).includes('temporarily unavailable'));
});

test('gate results keep receipts and unmeasurable requires a second explicit waiver click',()=>{
 const {gateResultHtml}=load('proposals.ts');
 const active=gateResultHtml('m1',{mutation:{},final_status:'active',receipt:{verdict:'pass'}});
 assert.ok(active.includes('data-status="active"'));assert.ok(!active.includes('proposal-waiver'));
 const failed=gateResultHtml('m1',{mutation:{},final_status:'regression_failed',receipt:{verdict:'fail',detail:'2 regressions'}});
 assert.ok(failed.includes('Regression failed'));assert.ok(failed.includes('2 regressions'));
 const waiver=gateResultHtml('m1',{mutation:{},final_status:'needs_waiver',receipt:{verdict:'unmeasurable'}});
 assert.ok(waiver.includes('not active'));
 assert.ok(waiver.includes('data-studio-action="proposal-waiver" data-id="m1"'));
});


test('admitted activation offers continuation without rejection or a stale waiver',()=>{
 const {proposalsPanelHtml}=load('proposals.ts');
 const item={mutation_id:'pending',kind:'budget_policy',status:'confirmed',version:2,title:'Policy',evidence_count:3,updated_at:2,activation_intent:{started_at:2,context_fingerprint:'0'.repeat(64)}};
 const receipts=new Map([['pending',{title:'Policy',result:{mutation:{},final_status:'needs_waiver'}}]]);
 const html=proposalsPanelHtml({items:[item],degraded:false},receipts);
 assert.match(html,/Activation in progress/);assert.match(html,/Continue activation/);
 assert.ok(!html.includes('proposal-reject'));assert.ok(!html.includes('proposal-waiver'));
});
