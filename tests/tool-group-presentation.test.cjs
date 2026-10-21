/** Actual tool-group owners: ordering, provenance, cache lifetime, tail bounds and linear lookup. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {execFileSync}=require('node:child_process');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'tofu-tool-groups-'));
const bundle=path.join(temp,'owners.cjs');
execFileSync(process.execPath,[path.join(root,'scripts/vite_test_bundle.mjs'),'--bundle','--format=cjs','--platform=node','--loader=ts',
  '--sourcefile=owners.ts',`--outfile=${bundle}`],{cwd:root,input:[
  'frontend/src/conversation/ui/tool-groups/index.ts','frontend/src/conversation/ui/tool-groups/context.ts',
  'frontend/src/conversation/presentation/tool-sibling-titles.ts',
].map(p=>`export * from ${JSON.stringify(path.join(root,p))};`).join('\n'),stdio:['pipe','pipe','pipe']});
const {createToolGroupPresentation,createToolGroupContext,deriveSiblingTitleDiscriminators}=require(bundle);
test.after(()=>fs.rmSync(temp,{recursive:true,force:true}));
function ports(extra={}){return {text:(key,values={})=>key+' '+JSON.stringify(values),icon:name=>`<svg data-icon="${name}"></svg>`,
  markdown:text=>`<p>${text}</p>`,cleanTranslation:text=>text.replace(/<\/?nt>/g,''),
  renderLine:r=>`<span class="row">${r.query}</span>`,renderSwarm:()=>null,renderProgram:()=>'<div>program</div>',
  debugEnabled:()=>false,taskIdForRound:()=>'',debugLog:()=>{},...extra};}
function documentFor(html,fn){const dom=new JSDOM(html);try{return fn(dom.window.document);}finally{dom.window.close();}}
const round=(n,extra={})=>({roundNum:n,toolCallId:'call'+n,toolName:'read_files',llmRound:n,status:'done',query:'Read',...extra});

test('context prefers exact occurrence identity, falling back to IDs for cloned tail rows',()=>{
  const a=round(1,{toolCallId:'repeat'}),b=round(2,{toolCallId:'repeat'}),c=round(3);
  const context=createToolGroupContext([a,b,c]);
  assert.equal(context.groupFor({rounds:[b]}),context.groups[1]);
  assert.equal(context.groupFor({rounds:[{...b}]}),context.groups[0]); // Clones have only provider identity.
  assert.equal(context.groupFor({rounds:[{...c}]}),context.groups[2]);
  const unknown={rounds:[round(100)]};assert.equal(context.groupFor(unknown),unknown);
});

test('batch membership reads grow linearly for 1,000 timeline batches',()=>{
  const n=1000;let reads=0;
  const rounds=Array.from({length:n},(_,i)=>({...round(i),get toolCallId(){reads++;return 'call'+i;}}));
  const context=createToolGroupContext(rounds);
  for(const group of context.groups)assert.equal(context.groupFor(group),group);
  assert.ok(reads<=6*n,`expected bounded reads, got ${reads}`);
  const indexedReads=reads;
  // Mirror the former full-context first-match predicate as a measured baseline.
  let oldComparisons=0;
  for(const group of context.groups){const first=group.rounds[0];context.groups.find(candidate=>{
    oldComparisons++;return candidate.rounds.includes(first)||candidate.rounds.some(row=>row.toolCallId===first.toolCallId);
  });}
  assert.equal(oldComparisons,n*(n+1)/2);
  console.log(`membership benchmark: ${indexedReads} indexed identity reads; ${oldComparisons} former group probes`);
});

test('one render session reuses full-context grouping across 1,000 batch renders',()=>{
  const n=1000;let reads=0;
  const rows=Array.from({length:n},(_,i)=>({...round(i),get llmRound(){reads++;return i;}}));
  const session=createToolGroupPresentation(ports()).createSession(rows);
  for(const row of rows)assert.match(session.renderGroups([row]),new RegExp(`data-prn="${row.roundNum}"`));
  assert.ok(reads<30*n,`full context was repeatedly grouped: ${reads} round reads`);
  console.log(`complete batch rendering benchmark: ${reads} LLM-round reads for ${n} batches`);
});

test('render sessions see same-array title changes; immutable standalone projections still compute once',()=>{
  let argReads=0;
  const a=round(1,{llmRound:0,get toolArgs(){argReads++;return {path:'a'};}});
  const b=round(2,{llmRound:0,get toolArgs(){argReads++;return {path:'b'};}}),rounds=[a,b];
  const ui=createToolGroupPresentation(ports());
  assert.match(ui.renderSlot(a,rounds),/path=a/);assert.match(ui.renderSlot(b,rounds),/path=b/);
  const afterFirst=argReads;ui.renderSlot(a,rounds);assert.equal(argReads,afterFirst);
  b.query='Different resource';
  const next=ui.createSession(rounds).renderGroups(rounds);
  assert.doesNotMatch(next,/path=a/);assert.match(next,/Different resource/);
  rounds.push(round(3,{llmRound:0,query:'Read',toolArgs:{path:'c'}}));
  assert.match(ui.renderSlot(a,rounds),/path=a/); // Length-changing legacy append invalidates its weak entry.
});

test('tool name and title form a tuple, so unrelated tools cannot become false siblings',()=>{
  assert.equal(deriveSiblingTitleDiscriminators([{rounds:[
    round(1,{toolName:'mcp_a',query:'bc',toolArgs:{path:'one'}}),
    round(2,{toolName:'mcp_ab',query:'c',toolArgs:{path:'two'}}),
  ]}]).size,0);
});

test('narration precedes its tools; parent attribution and program/Swarm routing remain distinct',()=>{
  const rows=[round(1,{llmRound:0}),round(2,{llmRound:0,parentToolCallId:'parent'}),
    round(3,{llmRound:1,toolName:'spawn_agents',_swarm:true,_swarmAgents:[{id:'a'}]}),round(4,{llmRound:2,_programSynthetic:true})];
  let swarm=0,program=0;
  const ui=createToolGroupPresentation(ports({renderSwarm:()=>{swarm++;return '<b>swarm</b>';},renderProgram:()=>{program++;return '<i>program</i>';}}));
  const html=ui.renderPanel(rows,[{type:'text',llmRound:0,text:'Source',translatedText:'<nt>Translated</nt>'}]);
  documentFor(html,d=>{
    assert.equal(d.querySelector('.seg-narration').textContent,'Translated');
    assert.ok(html.indexOf('Translated')<html.indexOf('data-prn="1"'));
    assert.equal(d.querySelector('[data-prn="2"]').dataset.parentToolCallId,'parent');
    assert.equal(d.querySelector('[data-prn="3"]').dataset.prnKind,'swarm');
    assert.equal(d.querySelector('[data-prn="4"]').dataset.prnKind,'program');
    assert.equal(d.querySelectorAll('.ptool-turn-head').length,1);
  });assert.equal(swarm,1);assert.equal(program,1);
});

test('mixed program batches preserve chronology and attribute only execution children',()=>{
  const rows=[round(1,{llmRound:0,_programSynthetic:true,_programCallId:'program'}),
    round(2,{llmRound:0,parentToolCallId:'program'}),
    round(3,{llmRound:0,_artifactOrigin:{parentToolCallId:'program'}}),
    round(4,{llmRound:0,_programCallId:'program'}),round(5,{llmRound:0})];
  const before=JSON.stringify(rows);
  documentFor(createToolGroupPresentation(ports()).renderPanel(rows),d=>{
    assert.deepEqual([...d.querySelectorAll('[data-prn]')].map(n=>n.dataset.prn),['1','2','3','4','5']);
    assert.deepEqual([...d.querySelectorAll('.ptool-parent-child')].map(n=>n.dataset.prn),['2','4']);
    assert.equal(d.querySelector('[data-prn="1"]').hasAttribute('data-parent-tool-call-id'),false);
    assert.equal(d.querySelector('[data-prn="3"]').hasAttribute('data-parent-tool-call-id'),false);
    assert.equal(d.querySelector('.ptool-program-turn').hasAttribute('data-collapsible'),false);
    assert.equal(d.querySelector('.ptool-turn-head'),null);
  });
  assert.equal(JSON.stringify(rows),before);
});

test('durable launch evidence selects the Swarm slot without live-only flags',()=>{
  const receipt={swarm_run_id:'execution',agents:[{id:'child',role:'coder'}]};
  const fixtures=[
    {toolContent:JSON.stringify(receipt)},
    {toolContent:JSON.stringify({summary:'Launched',items:[receipt]})},
    {toolContent:JSON.stringify({contractVersion:'tofu.tool-result/v2',summary:'Launched',items:[receipt]})},
    {_swarmSnapshot:{swarm_id:'execution',settled:false,agents:receipt.agents}},
    {toolContent:'Swarm tool error: Launch admission failed'},
    {status:'error',toolContent:JSON.stringify({status:'error',summary:'Launch admission failed'})},
  ];
  const ui=createToolGroupPresentation(ports({renderSwarm:()=>'<b class="launch-panel">swarm</b>'}));
  for(const evidence of fixtures){
    const launch=round(1,{toolName:'spawn_agents',...evidence});
    const before=JSON.stringify(launch);
    documentFor(ui.renderSlot(launch,[launch]),d=>{
      assert.equal(d.querySelector('[data-prn]').dataset.prnKind,'swarm',JSON.stringify(evidence));
      assert.equal(d.querySelectorAll('.launch-panel').length,1);
    });
    assert.equal(JSON.stringify(launch),before,'classification never decorates durable state');
  }
  for(const evidence of [{}, {toolContent:'not a launch receipt'}, {toolArgs:{agents:receipt.agents}}]){
    const launch=round(1,{toolName:'spawn_agents',...evidence});
    documentFor(ui.renderSlot(launch,[launch]),d=>{
      assert.equal(d.querySelector('.launch-panel'),null,'requests or missing evidence do not prove admission');
    });
  }
  const wait=round(2,{toolName:'get_agent_result',toolContent:JSON.stringify(receipt)});
  documentFor(ui.renderSlot(wait,[wait]),d=>assert.equal(d.querySelector('.launch-panel'),null));
});

test('settled long panels render the bounded tail, active panels preserve every live row and synthetic counts',()=>{
  let rendered=[];const ui=createToolGroupPresentation(ports({renderLine:r=>{rendered.push(r.roundNum);return '<i>row</i>';}}));
  const rows=Array.from({length:200},(_,n)=>round(n));
  documentFor(ui.renderPanel(rows),d=>{
    assert.equal(d.querySelectorAll('[data-prn]').length,50);
    assert.equal(d.querySelector('.ptool-truncated').dataset.hiddenCount,'150');
    assert.equal(d.querySelector('.ptool-panel-body').dataset.fullCount,'200');
  });assert.deepEqual(rendered,rows.slice(-50).map(r=>r.roundNum));
  rendered=[];const active=[...rows,round(201,{status:'searching'}),round(202,{_peerInject:true})];
  documentFor(ui.renderPanel(active),d=>{
    assert.equal(d.querySelectorAll('[data-prn]').length,202);
    assert.equal(d.querySelector('.ptool-truncated'),null);
    assert.equal(d.querySelector('.ptool-panel-body').dataset.fullCount,'201');
  });
});

test('swarm debug anchor joins the panel header; no solo anchor row under the panel',()=>{
  /* The anchor used to render as `.ri-tool-anchor-row`, a right-aligned strip
   * UNDER the swarm panel. It now travels with the round into renderSwarm,
   * which places it inside .sw-header-right. */
  let receivedAnchor;
  const ui=createToolGroupPresentation(ports({
    debugEnabled:()=>true,
    renderSwarm:(round,allRounds,anchor)=>{receivedAnchor=anchor;return `<div class="sw-panel"><div class="sw-header"><div class="sw-header-right">${anchor||''}</div></div></div>`;},
  }));
  const launch=round(3,{toolName:'spawn_agents',_swarm:true,_swarmAgents:[{id:'a'}],_taskId:'task-x',llmRound:2});
  documentFor(ui.renderSlot(launch,[launch]),d=>{
    assert.equal(d.querySelector('.ri-tool-anchor-row'),null,'no solo anchor row under the panel');
    const anchor=d.querySelector('.sw-header-right .ri-tool-anchor');
    assert.ok(anchor,'anchor inside the swarm header-right cluster');
    assert.equal(anchor.dataset.riState,'task-x:3');
  });
  assert.ok(receivedAnchor&&receivedAnchor.includes('ri-tool-anchor'),'renderSwarm receives the anchor html');
});

test('inspector refs retain task/turn authority, escaped actions and bounded one-time diagnostics',()=>{
  let logs=0;const ui=createToolGroupPresentation(ports({debugEnabled:()=>true,debugLog:()=>logs++,taskIdForRound:()=> 'fallback'}));
  const r=round(1,{inspectorRef:{taskId:'task<unsafe>',roundNum:4,turn:'turn<unsafe>'}});
  documentFor(ui.renderInspector(r),d=>{
    const button=d.querySelector('.ri-tool-anchor');assert.equal(button.dataset.riState,'task<unsafe>:4');
    assert.equal(button.dataset.riTurn,'turn<unsafe>');assert.equal(d.querySelector('unsafe'),null);
  });ui.renderInspector(r);assert.equal(logs,1);
  assert.equal(ui.renderInspector({...r,_peerInject:true}),'');assert.equal(ui.renderInspector({...r,inspectorRef:null}),'');
  for(let i=0;i<600;i++)ui.renderInspector(round(i,{_taskId:'task'+i}));
  assert.equal(logs,601);
});
