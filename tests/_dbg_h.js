
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="debugPanel">' +
  '<div id="debugTitle"></div>' +
  '<div id="debugContent"></div>' +
  '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
win.Icon = global.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}"></svg>`;
win.t = global.t = (k) => k;

win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1' }];
win.debugVisible = global.debugVisible = true;

/* eval the shipped file + append a getter INSIDE the eval scope so the
 * harness can inspect the const-scoped _debugRequests (const does not leak
 * out of a direct eval — function declarations do). */
const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src + '\n;win.__dumpReqs = function(){ return _debugRequests; };console.log("scope", typeof _debugRuntimeOwner, typeof DebugShellState, typeof _debugRequests);');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('fn_present', typeof showMessagesInDebug === 'function');
check('store_present', typeof win.__dumpReqs() === 'object');

const msgs1 = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hi' },
];
const msgs2 = msgs1.concat([
  { role: 'assistant', content: 'a1' },
  { role: 'user', content: 'u2' },
]);
const META = (round) => ({
  kind: 'request', model: 'm-x',
  params: { maxTokens: 1000, temperature: 1 },
  roundNum: round, taskId: 'task-A',
  contextManifest: [
    { id: 'project_rules', source: 'project.rules', placement: 'head',
      authority: 'project', injected: true, tokens: 42 },
  ],
});

/* ── Two request snapshots on the SAME task → BOTH rounds retained ── */
showMessagesInDebug(msgs1, 'Round 1 请求前 · 2条', true, 'conv-1', undefined, undefined, META(1));
showMessagesInDebug(msgs2, 'Round 2 请求前 · 4条', true, 'conv-1', undefined, undefined, META(2));
const tA = win.__dumpReqs()['task-A'];
check('two_rounds_retained', !!tA && tA.roundOrder.length === 2);
check('round1_not_overwritten',
  !!tA && tA.rounds['1'] && tA.rounds['1'].messageCount === 2);
check('round2_recorded',
  !!tA && tA.rounds['2'] && tA.rounds['2'].messageCount === 4);
check('rounds_ordered', !!tA &&
  tA.roundOrder[0] === '1' && tA.roundOrder[1] === '2');
check('model_params_stored', !!tA &&
  tA.rounds['1'].model === 'm-x' &&
  tA.rounds['1'].params && tA.rounds['1'].params.maxTokens === 1000);
check('messages_ref_kept', !!tA &&
  Array.isArray(tA.rounds['1'].messages) && tA.rounds['1'].messages.length === 2);
check('context_manifest_stored', !!tA &&
  tA.rounds['1'].contextManifest[0].id === 'project_rules');
check('context_block_rendered',
  !!document.querySelector('#debugContent .debug-context-block'));

/* ── kind='state' → .states, NOT the request rounds ── */
showMessagesInDebug(msgs2, '最终回复后 · 4条', true, 'conv-1', undefined, undefined,
  { kind: 'state', model: 'm-x', roundNum: 'final', taskId: 'task-A' });
check('state_not_in_rounds', !!tA && tA.roundOrder.length === 2);
check('state_routed_to_states', !!tA && tA.states.length === 1 &&
  tA.states[0].roundNum === 'final' && tA.states[0].kind === 'state');

/* ── Legacy event WITHOUT kind → treated as 'request' (back-compat) ── */
showMessagesInDebug(msgs1, 'legacy', true, 'conv-1', undefined, undefined,
  { roundNum: 1, taskId: 'task-B' });
const tB = win.__dumpReqs()['task-B'];
check('legacy_kind_defaults_request', !!tB &&
  tB.roundOrder.length === 1 && tB.rounds['1'].kind === 'request');

/* ── No meta (cold /debug-messages path) → nothing recorded ── */
const taskCountBefore = Object.keys(win.__dumpReqs()).length;
showMessagesInDebug(msgs1, '2 msgs (server)', false, 'conv-1', undefined, true);
check('no_meta_no_record',
  Object.keys(win.__dumpReqs()).length === taskCountBefore);

/* ── Legacy panel render still works (title summary on the latest) ── */
check('panel_still_renders',
  document.getElementById('debugTitle').innerHTML.indexOf('Messages') !== -1);

console.log(out.join('\n'));
