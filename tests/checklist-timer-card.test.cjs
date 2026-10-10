/** Receipt authority, escaping, bounded projections and shared countdown lifecycle. */
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..'), temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-timer-owner-'));
const bundle = path.join(temp, 'owners.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs',
  '--platform=node', '--loader=ts', '--sourcefile=owners.ts', `--outfile=${bundle}`], {
  cwd:root, input:[
    'frontend/src/conversation/ui/checklist-card/index.ts',
    'frontend/src/conversation/ui/timer-card/index.ts',
    'frontend/src/conversation/ui/timer-card/interactions.ts',
    'frontend/src/conversation/application/demand-scoped-presentation-ticker.ts',
    'frontend/src/conversation/ui/rich-card-reconciliation.ts',
  ].map(name=>`export * from ${JSON.stringify(path.join(root,name))};`).join('\n'),stdio:['pipe','pipe','pipe'],
});
const { renderChecklistCard, renderTimerCard, toggleTimerWatcher, toggleTimerInstruction, toggleTimerPoll,
  refreshTimerCountdowns, createDemandScopedPresentationTicker, reconcileRichCardHtml } = require(bundle);
test.after(()=>fs.rmSync(temp,{recursive:true,force:true}));
const text=(_,fallback)=>typeof fallback==='string'?fallback:`${fallback.n} updates`;
const ports={text,now:()=>10000,icon:name=>`<svg data-icon="${name}"></svg>`,markdown:value=>value,
  formatTime:value=>'at '+value,toolIcon:()=>undefined};
function timer(round={},polls=[],extra={}){return renderTimerCard({roundNum:1,status:'searching',...round},{polls,triggered:false},'',{...ports,...extra});}
function documentFor(html,fn){const dom=new JSDOM(html);try{return fn(dom.window.document,dom.window);}finally{dom.window.close();}}

test('checklist reads every status once, retains invalid slots in totals, and exposes distinct states',()=>{
  let reads=0;
  const todos=['completed','in_progress','blocked','pending'].map(status=>({get status(){reads++;return status;},content:'Step '+status}));
  const html=renderChecklistCard({roundNum:2,results:[{todos:[...todos,null]}]},'',()=>'',text);
  assert.equal(reads,4);
  documentFor(html,d=>{
    assert.equal(d.querySelector('.ptool-todo-count').textContent,'1/5');
    for(const state of ['completed','in_progress','blocked','pending'])assert.ok(d.querySelector('.ptool-todo-'+state));
    assert.equal(d.querySelector('details').open,false);
  });
  let controls=0;
  assert.equal(renderChecklistCard({},'',()=>{controls++;return '';},text),null);
  assert.equal(controls,0);
  assert.match(renderChecklistCard({todos:[]},'',()=>'',text),/Checklist cleared/);
});

test('checklist verdict owns rejection and noop; explanation, restored notice and accepted history stay in the body',()=>{
  const meta={todos:[{content:'Step',status:'pending'}],todoRejected:true,todoRejectReason:'Rejected <script>literal</script>',
    todoExplanation:'Why <b>literal</b>',todoDisplayUpdateCount:2,todoBreadcrumbs:[{label:'Parent'},{label:'Child'}],
    todoDisplayHistory:[{operation:'set',done:0,total:1},{operation:'complete',done:1,total:1,revision:2}]};
  documentFor(renderChecklistCard({roundNum:'1" onclick="bad()',results:[meta]},'',()=>'',text),d=>{
    assert.equal(d.querySelector('[onclick]'),null);assert.equal(d.querySelector('script'),null);
    assert.match(d.querySelector('summary').textContent,/Update rejected/);
    assert.equal(d.querySelector('summary .ptool-todo-explanation'),null);
    assert.equal(d.querySelector('.ptool-todo-explanation').textContent,'Why <b>literal</b>');
    assert.match(d.querySelector('.ptool-todo-rejection-detail').textContent,/Rejected/);
    assert.equal(d.querySelectorAll('.ptool-todo-history > div > span').length,2);
  });
  assert.match(renderChecklistCard({results:[{todos:[],todoNoop:true}]},'',()=>'',text),/Checklist unchanged/);
  assert.match(renderChecklistCard({results:[{todos:[],todoAutoPopped:['child']}]},'',()=>'',text),/returned to the parent/);
});

test('Timer projection counts authoritative polls, copies only five newest entries, and preserves parse-error evidence',()=>{
  let copied=0;
  const polls=Array.from({length:10000},(_,n)=>({decision:n===9999?'parse_error':'wait',pollNum:n,reason:'poll '+n,
    ...(n===9999?{rawContent:'not JSON <script>',cmdOutput:'exit 1',toolTrace:[{name:'run_command',isError:true,argsBrief:'build',elapsed:0}]}:{})}));
  polls.slice=function(start,end){const result=Array.prototype.slice.call(this,start,end);copied+=result.length;return result;};
  documentFor(timer({},polls),d=>{
    assert.equal(copied,5);assert.equal(d.querySelectorAll('.timer-poll-line').length,5);
    assert.equal(d.querySelectorAll('.timer-poll-line')[0].querySelector('.timer-poll-num').textContent,'#9999');
    assert.match(d.querySelector('.timer-watcher-label').textContent,/10000 polls.*last check errored/);
    assert.match(d.querySelector('.timer-poll-hidden').textContent,/9995/);
    assert.match(d.querySelector('.timer-poll-raw').textContent,/not JSON <script>/);
    assert.match(d.body.textContent,/exit 1/);assert.ok(d.querySelector('.sw-tl-failed'));
  });
});

test('promoted command tier has no LLM decider row; readiness, orphaned and interrupted states use explicit inputs',()=>{
  const html=timer({_timerConditionKind:'code',_timerConditionCommand:'test -f ready',_timerPollInterval:0},[{decision:'wait',pollNum:1,reason:'predicate no match (exit=1)'}]);
  assert.match(html,/command-based/);assert.match(html,/Shell command exit code/);assert.doesNotMatch(html,/Cheap LLM/);
  assert.match(html,/Not met yet \(command exit 1\)/);
  assert.match(timer({_timerOrphaned:true}),/task interrupted/);
  assert.match(timer({status:'cancelled'}),/cancelled/);
  assert.match(renderTimerCard({status:'searching'},{polls:[],triggered:true},'',ports),/triggered after 0 polls/);
});

test('Timer identity, poll fields and translated text remain data, and malformed deadlines never demand a clock',()=>{
  const literal='<img src=x onerror=bad()>';let demanded=0;
  for(const next of ['bad',Infinity,-1,0]){
    const html=timer({_timerNextPollTs:next},[],{demandCountdown:()=>demanded++});
    assert.doesNotMatch(html,/data-timer-next/);
  }
  assert.equal(demanded,0);
  documentFor(timer({roundNum:'1" onclick="bad()',_timerNextPollTs:15000},[{decision:'wait',pollNum:literal,tokensUsed:literal}],
    {text:()=>literal,demandCountdown:()=>demanded++,formatTime:()=>literal}),d=>{
    assert.equal(d.querySelectorAll('img,script,[onclick],[onerror]').length,0);
    assert.ok(d.body.textContent.includes(literal));
    assert.equal(d.querySelector('.timer-watcher-body').id,'tmr-r1" onclick="bad()-wrap');
  });
  assert.equal(demanded,1);
});

test('duplicate round IDs cannot redirect instruction disclosure; keyboard focus and aria stay on the activated card',()=>{
  const html=timer({_timerCheckInstruction:'long instruction '.repeat(20)},[{decision:'error',pollNum:1,cmdOutput:'evidence'}]);
  documentFor(html+html,(d,win)=>{
    const rows=d.querySelectorAll('.timer-meta-row-instr');rows[1].focus();
    toggleTimerInstruction(rows[1],new win.KeyboardEvent('keydown',{key:'Enter'}));
    assert.equal(rows[0].getAttribute('aria-expanded'),'false');assert.equal(rows[1].getAttribute('aria-expanded'),'true');
    assert.equal(rows[0].querySelector('.timer-meta-val').classList.contains('expanded'),false);
    assert.equal(d.activeElement,rows[1]);
    toggleTimerInstruction(rows[1],new win.KeyboardEvent('keydown',{key:'Escape'}));
    assert.equal(rows[1].getAttribute('aria-expanded'),'true');
    const poll=d.querySelectorAll('.timer-poll-line')[1];toggleTimerPoll(poll,new win.KeyboardEvent('keydown',{key:' '}));
    assert.equal(poll.getAttribute('aria-expanded'),'true');assert.ok(poll.nextElementSibling.classList.contains('expanded'));
  });
});

test('countdown paint changes only valid visible spans and does not replace selection or DOM identity',()=>{
  const html=timer({_timerNextPollTs:15000},[{decision:'wait',reason:'waiting'}]);
  documentFor(html,(d,win)=>{
    const span=d.querySelector('.timer-next-poll-txt'),row=d.querySelector('.timer-poll-line');let now=11000;
    const range=d.createRange();range.selectNodeContents(row);win.getSelection().addRange(range);
    assert.equal(refreshTimerCountdowns(d,{text,now:()=>now}),true);
    assert.equal(span.textContent,'Next check in ~4s…');assert.equal(d.querySelector('.timer-next-poll-txt'),span);
    assert.equal(d.querySelector('.timer-poll-line'),row);assert.ok(win.getSelection().toString().includes('waiting'));
    toggleTimerWatcher(d.querySelector('.timer-watcher-header'));
    assert.equal(refreshTimerCountdowns(d,{text,now:()=>now}),false);
  });
});

test('shared ticker keeps one scheduled callback and releases listeners for collapse, invalid hints, removal and teardown',()=>{
  documentFor(timer({_timerNextPollTs:15000}),(d)=>{
    const callbacks=new Map(),listeners=new Set();let sequence=0,peak=0;
    const ticker=createDemandScopedPresentationTicker({
      schedule:{setTimeout:callback=>{const id=sequence++;callbacks.set(id,callback);peak=Math.max(peak,callbacks.size);return id;},clearTimeout:id=>callbacks.delete(id)},
      visibility:{isHidden:()=>false,subscribe:callback=>{listeners.add(callback);return()=>listeners.delete(callback);}},
      tick:()=>refreshTimerCountdowns(d,ports),
    });
    const run=()=>{const [id,callback]=callbacks.entries().next().value;callbacks.delete(id);callback();};
    ticker.demand();ticker.demand();assert.equal(callbacks.size,1);run();assert.equal(callbacks.size,1);
    toggleTimerWatcher(d.querySelector('.timer-watcher-header'));run();assert.equal(callbacks.size,0);assert.equal(listeners.size,0);
    toggleTimerWatcher(d.querySelector('.timer-watcher-header'),undefined,()=>ticker.demand());assert.equal(callbacks.size,1);
    d.querySelector('.timer-next-poll').setAttribute('data-timer-next','Infinity');run();assert.equal(listeners.size,0);
    d.querySelector('.timer-next-poll').setAttribute('data-timer-next','15000');ticker.demand();d.body.replaceChildren();run();
    assert.equal(callbacks.size,0);assert.equal(listeners.size,0);assert.equal(peak,1);ticker.stop();
  });
});


test('new checks cannot inherit disclosure or replace the focused evidence of an older check',()=>{
  const older={decision:'error',pollNum:1,reason:'Failed check',rawContent:'unparseable',cmdOutput:'Evidence preserved'};
  documentFor('<main>'+timer({},[older])+'</main>',(d,win)=>{
    const root=d.querySelector('main'),row=root.querySelector('.timer-poll-line');
    row.focus();toggleTimerPoll(row,new win.KeyboardEvent('keydown',{key:'Enter'}));
    const evidence=root.querySelector('.timer-poll-detail-output code');
    const range=d.createRange();range.selectNodeContents(evidence);d.getSelection().removeAllRanges();d.getSelection().addRange(range);
    const newer={decision:'error',pollNum:2,reason:'Another check',cmdOutput:'Different evidence'};
    reconcileRichCardHtml(root,timer({},[older,newer]));
    assert.equal(root.querySelectorAll('.timer-poll-line').length,2);
    const newest=root.querySelector('[data-prn="number:2"]');
    assert.equal(newest.getAttribute('aria-expanded'),'false');
    assert.equal(root.querySelector('[data-prn="number:1"]'),row);
    assert.equal(row.getAttribute('aria-expanded'),'true');assert.equal(d.activeElement,row);
    assert.equal(d.getSelection().toString(),'Evidence preserved');
    assert.equal(root.querySelectorAll('.timer-poll-detail-output code')[1],evidence);
  });
});


test('nested copy controls and Markdown links keep native activation instead of folding their parent',()=>{
  documentFor(timer({_timerTimerId:'tmr_copy',_timerCheckInstruction:'long instruction '.repeat(20)}),(d,win)=>{
    const header=d.querySelector('.timer-watcher-header'),chip=d.querySelector('.timer-id-chip');
    header.addEventListener('click',event=>toggleTimerWatcher(header,event));
    header.addEventListener('keydown',event=>toggleTimerWatcher(header,event));
    for(const event of [new win.MouseEvent('click',{bubbles:true,cancelable:true}),
      new win.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}),
      new win.KeyboardEvent('keydown',{key:' ',bubbles:true,cancelable:true})]){
      chip.dispatchEvent(event);assert.equal(event.defaultPrevented,false);
      assert.equal(header.getAttribute('aria-expanded'),'true');
    }
    const row=d.querySelector('.timer-meta-row-instr'),link=d.createElement('a');
    link.href='#evidence';link.textContent='Evidence';row.querySelector('.timer-meta-val').append(link);
    row.addEventListener('click',event=>toggleTimerInstruction(row,event));
    const click=new win.MouseEvent('click',{bubbles:true,cancelable:true});link.dispatchEvent(click);
    assert.equal(click.defaultPrevented,false);assert.equal(row.getAttribute('aria-expanded'),'false');
  });
});

test('settled background receipt describes handoff without reporting task completion or demanding a timer',()=>{
  let demanded=0;
  const receipt={timerId:'tmr_receipt',background:true,checkInstruction:'',conditionCommand:'test -f ready',
    checkCommand:'',conditionKind:'code',pollInterval:60,maxPolls:120};
  const html=renderTimerCard({status:'done',results:[{timerReceipt:receipt}]},{polls:[],triggered:false},'',
    {...ports,demandCountdown:()=>demanded++});
  documentFor(html,d=>{
    assert.match(d.querySelector('.timer-watcher-label').textContent,/background watcher created/);
    assert.equal(d.querySelector('.timer-id-chip').getAttribute('data-timer-id'),'tmr_receipt');
    assert.match(d.body.textContent,/test -f ready/);
    assert.equal(d.querySelectorAll('.ptool-spinner,.timer-next-poll').length,0);
  });
  assert.equal(demanded,0);
});
