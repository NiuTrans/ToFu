/** Project receipt outcome, trust and bounded presentation contracts. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-project-receipt-'));
const bundle = path.join(temp, 'receipt.cjs');
const owner = path.join(root, 'frontend/src/conversation/ui/project-receipt');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs',
  '--platform=node', '--loader=ts', '--sourcefile=project-tests.ts', `--outfile=${bundle}`], {
  cwd: root, input: ['index', 'time', 'policy'].map(name => `export * from ${JSON.stringify(path.join(owner, name+'.ts'))};`).join('\n'),
  stdio: ['pipe', 'pipe', 'pipe'],
});
const { renderProjectReceipt, relativeReceiptTime } = require(bundle);
test.after(() => fs.rmSync(temp, { recursive: true, force: true }));
const ports = { text: (_, fallback) => fallback, markdown: value => 'MARKDOWN:'+value,
  icon: name => `<svg data-icon="${name}"></svg>`, conversationTitle: id => `Title ${id}`,
  now: () => 1700001000000 };
function card(toolName, meta={}, extra={}, overrides={}) {
  return renderProjectReceipt({toolName, query:'Backend label', roundNum:7, results:[meta], ...extra}, '<svg></svg>', '', {...ports,...overrides});
}
function inspect(html, assertions) {
  const dom = new JSDOM(html); try { assertions(dom.window.document); } finally { dom.window.close(); }
}

test('routine reads fold with useful count chips, mutations and review decisions stay open', () => {
  for (const [tool, meta, chip] of [
    ['project_board_read', {boardSnapshot:{open:0}}, '0 open'],
    ['project_peer_status', {peerStatus:{peers:[]}}, '0 active'],
    ['project_feed_read', {feedActivity:{events:[]}}, '0 events'],
  ]) inspect(card(tool,meta), d => {
    assert.equal(d.querySelector('details').open, false);
    assert.equal(d.querySelector('.ptool-convmeta-count').textContent, chip);
    assert.notEqual(d.querySelector('.ptool-text').textContent, 'Backend label');
  });
  for (const tool of ['project_board_post','project_message','project_charter_propose','project_commit']) {
    inspect(card(tool), d => assert.equal(d.querySelector('details').open, true));
  }
});

test('structured metadata wins in declared order, fallback Markdown is called only when needed', () => {
  let reads=[]; const markdown=value=>{ reads.push(value); return 'MARKDOWN:'+value; };
  const html=card('project_board_read', {boardSnapshot:{},peerStatus:{peers:[{title:'Ignored peer'}]}},
    {toolContent:'Ignored prose'}, {markdown});
  assert.match(html,/ptool-board-mini/); assert.doesNotMatch(html,/Ignored/); assert.deepEqual(reads,[]);
  assert.match(card('project_charter_read', {snippet:'stream snippet'}, {toolContent:'finished charter'}, {markdown}), /MARKDOWN:finished charter/);
  assert.match(card('project_charter_read', {snippet:'stream snippet'}, {toolContent:'  '}, {markdown}), /MARKDOWN:stream snippet/);
  assert.match(card('project_charter_read',{}, {}, {markdown}), /No content returned/);
  assert.deepEqual(reads,['finished charter','stream snippet']);
});

test('failed board transition replaces success status, and backend title entities use the shared inline policy', () => {
  const failed=card('project_board_claim',{boardTransition:{verb:'claim',ok:false,status:'claimed',title:'**Plan** &lt;venue&gt;',error:'Already claimed'}});
  assert.match(failed,/ptool-board-transition-failed/); assert.match(failed,/Already claimed/);
  assert.doesNotMatch(failed, /ptool-board-tr-status/);
  let title;
  card('project_board_post',{boardTransition:{verb:'post',title:'**Plan** &lt;venue&gt;'}},{}, {inlineMarkup:value=>{title=value;return 'SAFE';}});
  assert.equal(title,'**Plan** <venue>');
});

test('scalar counts, round identity and translated peer captions cannot create HTML or handlers', () => {
  const malicious='<img src=x onerror=bad()>';
  for (const html of [
    card('project_board_read',{boardSnapshot:{open:malicious}}),
    card('project_peer_status',{peerStatus:{peers:[{round:malicious}]}}),
    card('project_peer_status',{peerStatus:{peers:[{round:1}]}},{}, {text:(key,fallback)=>key==='projectBrain.peerRound'?malicious:fallback}),
  ]) inspect(html,d=>{
    assert.equal(d.querySelectorAll('img,[onerror],script').length,0);
    assert.ok(d.body.textContent.includes(malicious));
  });
  inspect(card('project_board_read',{}, {roundNum:'7" onclick="bad()'}), d=>{
    assert.equal(d.querySelector('details').hasAttribute('onclick'),false);
    assert.equal(d.querySelector('details').dataset.rn,'7" onclick="bad()');
  });
  for (const tool of ['constructor','toString','__proto__']) {
    inspect(card(tool, {source:tool}),d=>{
      assert.equal(d.querySelector('.ptool-text').textContent,'Backend label');
      assert.equal(d.querySelector('.ptool-convmeta-src').textContent,tool);
    });
  }
});

test('peer delivery preserves title lookup, denied/advisory/hard outcomes, and pending charter review', () => {
  const denied=card('project_intervene',{peerDelivery:{tool:'project_intervene',toConv:'peer',outcome:'denied',text:'Overlap',hardAbort:false}});
  assert.match(denied,/Advisory intervention/); assert.match(denied,/Title peer/); assert.match(denied,/ptool-peermsg-denied/);
  assert.match(card('project_intervene',{peerDelivery:{tool:'project_intervene',toConv:'peer',hardAbort:true}}), /Hard intervention/);
  assert.match(card('project_charter_propose',{charterProposal:{proposal:'Do not auto-commit',title:'Decision'}}), /Awaiting human review/);
  assert.match(card('project_feed_read',{feedActivity:{events:[],hiddenSelf:2}}), /2 from this conversation hidden/);
});

test('commit plans, failures, held files and verification mismatches remain distinguishable', () => {
  const plan=card('project_commit',{source:'Board',commitResult:{mode:'plan',ok:true,clean:['owned.ts'],committed:['wrong.ts'],commitSha:'deadbeef'}});
  assert.match(plan,/plan only/); assert.match(plan,/owned.ts/); assert.doesNotMatch(plan,/wrong.ts|deadbeef/);
  inspect(plan,d=>assert.equal(d.querySelector('.ptool-convmeta-src').textContent,'Git'));
  const mismatch=card('project_commit',{commitResult:{ok:true,verified:false,committed:['owned.ts'],excluded:[{path:'shared.ts',reason:'Sibling changes',numstat:'2 1'}]}});
  assert.match(mismatch,/verify mismatch/); assert.match(mismatch,/Held back/); assert.match(mismatch,/Sibling changes/);
  assert.match(card('project_commit',{commitResult:{ok:false,error:'Authority check rejected'}}), /not committed/);
});

test('obsolete conversation digest metadata is never read by project coordination receipts', () => {
  const meta={boardSnapshot:{}};
  Object.defineProperty(meta,'convDigest',{get:()=>{throw Error('Stale read must never project');}});
  assert.match(card('project_board_read',meta),/ptool-board-mini/);
});

test('activity time uses only supplied clocks and retains absent/future/invalid outcomes', () => {
  assert.equal(relativeReceiptTime(1700000900000,ports),'1m ago');
  assert.equal(relativeReceiptTime(1700001001000,ports),'just now');
  assert.equal(relativeReceiptTime('invalid',ports),'');
});
