/* tests/orch_nested_roundtrip_harness.js
 *
 * Headless (jsdom) exercise of the Orchestration Studio's nested-canvas
 * GROUP (subflow) state logic in static/js/orchestration.js. This is the
 * enter→edit→exit→serialize round-trip that silently rots: it lives purely
 * in module-global arrays (_orchNodes/_orchEdges/_orchStack) with no backend
 * seam, so neither the tsc ratchet nor the Python suites can see a regression
 * in it.
 *
 * Strategy
 * --------
 * orchestration.js is sloppy-mode vanilla JS sharing window scope (no
 * import/export). We eval it inside a jsdom window with minimal stubs for
 * the cross-file globals it reads (escapeHtml, t, BASE_PATH). The render
 * helpers each early-return when their DOM element is absent, so we drive
 * the state functions directly without building the DOM.
 *
 * On success: prints "ALL_OK" and a single line "RESULT_JSON=<json>" carrying
 * the final root definition, which the Python wrapper re-validates against the
 * real backend schema (lib.orchestration.validate_definition) — closing the
 * loop from JS state logic to backend contract.
 *
 * Any failed assertion throws → non-zero exit → the pytest wrapper fails.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static', 'js', 'orchestration.js'), 'utf8');

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
  runScripts: 'dangerously',
});
const W = dom.window;

// ── Cross-file global stubs (declared on the window before loading the file) ──
// escapeHtml: identity-ish (we only assert on state, not rendered HTML).
// t: key + {param} interpolation, mirroring i18n.js's t() shape closely
//    enough that t('orch.group.defaultLabel') is a stable string.
W.eval(`
  function escapeHtml(s){ return String(s == null ? '' : s); }
  function t(key, params){
    var s = key;
    if (params) { for (var k in params) { if (params.hasOwnProperty(k)) {
      s = s.replace(new RegExp('\\\\{' + k + '\\\\}', 'g'), params[k]);
    } } }
    return s;
  }
  var BASE_PATH = '';
`);

// Load the module under test into window scope.
W.eval(SRC);

// ── Tiny assert harness ──
let _checks = 0;
function assert(cond, msg) {
  _checks++;
  if (!cond) { throw new Error('ASSERT FAILED: ' + msg); }
}
function findGroup(nodes) { return nodes.filter(function (n) { return n.type === 'subflow'; })[0]; }
function byRole(nodes, role) { return nodes.filter(function (n) { return n.role === role; }); }

// ════════════════════════════════════════════════════════════════════
// Scenario 1 — create a Group, descend, edit, exit, verify serialization
// ════════════════════════════════════════════════════════════════════
W._orchStack = []; W._orchNodes = []; W._orchEdges = [];
W._orchSel = null; W._orchSeq = 0; W._orchName = 'Root';

W._orchAddNode({ ptype: 'control', kind: 'start' }, 100, 30);
W._orchAddNode({ ptype: 'subflow', role: 'general' }, 100, 180);
W._orchAddNode({ ptype: 'control', kind: 'stop' }, 100, 330);

const startId = W._orchNodes.filter(function (n) { return n.kind === 'start'; })[0].id;
const stopId = W._orchNodes.filter(function (n) { return n.kind === 'stop'; })[0].id;
const group = findGroup(W._orchNodes);
assert(group, 'group node created on drop');

// A freshly-dropped Group must be a valid black box out of the box.
assert(group.params.scope === 'isolated', 'new group defaults to isolated scope');
assert(group.params.definition && Array.isArray(group.params.definition.nodes),
  'new group carries an embedded child definition');
assert(group.params.definition.nodes.length === 3,
  'blank child = start + agent + stop (got ' + group.params.definition.nodes.length + ')');

// Wire the root so it is a runnable flow, and plant a STALE ref to prove
// exit drops it (an edited embedded child is authoritative).
W._orchEdges.push({ id: 'er1', from: startId, to: group.id });
W._orchEdges.push({ id: 'er2', from: group.id, to: stopId });
group.params.ref = 'stale-should-be-dropped';

// ── Descend into the group ──
W._orchEnterGroup(group.id);
assert(W._orchStack.length === 1, 'enter pushes one frame (got ' + W._orchStack.length + ')');
assert(W._orchStack[0].groupId === group.id, 'frame remembers which group we entered');
assert(W._orchNodes.length === 3, 'working canvas now shows the child (3 nodes)');
assert(byRole(W._orchNodes, 'general').length === 1, 'child has the seeded general agent');
const rootSnapshotName = W._orchStack[0].name;
assert(rootSnapshotName === 'Root', 'parent name preserved in the frame');

// ── Edit inside the box: change the agent objective + add a coder + wire it ──
const childAgent = byRole(W._orchNodes, 'general')[0];
W._orchSel = childAgent.id;
W._orchSetParam('objective', 'CHILD_EDIT_MARKER');
W._orchAddNode({ ptype: 'role', role: 'coder' }, 100, 300);
const coderId = W._orchSel;
W._orchEdges.push({ id: 'ec1', from: childAgent.id, to: coderId });

// ── Surface back to the parent ──
W._orchExitGroup();
assert(W._orchStack.length === 0, 'exit pops the frame (back at root)');
assert(W._orchNodes.length === 3, 'root canvas restored (start + group + stop)');
assert(W._orchName === 'Root', 'root name restored after exit');

const group2 = findGroup(W._orchNodes);
assert(group2.id === group.id, 'same group node identity after exit');
assert(group2.params.ref === undefined, 'stale ref dropped on commit');
const cd = group2.params.definition;
assert(cd, 'group still carries an embedded definition after exit');

// The edits made inside the box were committed into params.definition.
const editedAgent = cd.nodes.filter(function (n) { return n.id === childAgent.id; })[0];
assert(editedAgent && editedAgent.params.objective === 'CHILD_EDIT_MARKER',
  'objective edit persisted into the embedded child');
assert(cd.nodes.some(function (n) { return n.role === 'coder'; }),
  'newly-added child node persisted into the embedded child');
assert(cd.edges.some(function (e) { return e.from === childAgent.id && e.to === coderId; }),
  'newly-added child edge persisted (serialized as {from,to})');
// Serialization must shed the transient client-only edge id.
assert(cd.edges.every(function (e) { return !('id' in e); }),
  'serialized child edges carry no client-only id');

// ════════════════════════════════════════════════════════════════════
// Scenario 2 — depth-2 nesting: a group inside a group round-trips
// ════════════════════════════════════════════════════════════════════
W._orchEnterGroup(group2.id);                       // depth 1
W._orchAddNode({ ptype: 'subflow', role: 'general' }, 120, 200);  // nested group
const innerGroup = findGroup(W._orchNodes);
assert(innerGroup, 'nested group created at depth 1');
W._orchEnterGroup(innerGroup.id);                   // depth 2
assert(W._orchStack.length === 2, 'two frames at depth 2');
W._orchAddNode({ ptype: 'role', role: 'writer' }, 120, 260);
const writerId = W._orchSel;
W._orchExitGroup();                                 // back to depth 1
W._orchExitGroup();                                 // back to root
assert(W._orchStack.length === 0, 'fully surfaced after two exits');

const rootGroup = findGroup(W._orchNodes);
const lvl1 = rootGroup.params.definition;
const lvl1Group = lvl1.nodes.filter(function (n) { return n.type === 'subflow'; })[0];
assert(lvl1Group, 'depth-1 embedded child contains the nested group');
const lvl2 = lvl1Group.params.definition;
assert(lvl2 && lvl2.nodes.some(function (n) { return n.id === writerId && n.role === 'writer'; }),
  'depth-2 edit persisted through two levels of commit');

// ════════════════════════════════════════════════════════════════════
// Scenario 3 — _orchFlushToRoot collapses arbitrary depth before whole-flow ops
// ════════════════════════════════════════════════════════════════════
W._orchEnterGroup(rootGroup.id);
W._orchEnterGroup(findGroup(W._orchNodes).id);
assert(W._orchStack.length === 2, 'descended two levels again');
W._orchFlushToRoot();
assert(W._orchStack.length === 0, 'flushToRoot collapses all open frames');

// Final root definition for backend re-validation.
const finalDef = W._orchToDefinition();
assert(finalDef.nodes.some(function (n) { return n.type === 'subflow'; }),
  'final root definition still contains the group node');

// ════════════════════════════════════════════════════════════════════
// Scenario 4 — per-role STRUCTURED params (list / select / bool) round-trip
// The inspector edits these via _orchSetParam with a kind hint; they must
// serialize into params with the right SHAPE the backend validates:
//   list  → array of trimmed non-empty strings (NOT a newline blob)
//   select→ stored enum value, or KEY ABSENT when unset
//   bool  → true/false
// ════════════════════════════════════════════════════════════════════
W._orchStack = []; W._orchNodes = []; W._orchEdges = [];
W._orchSel = null; W._orchSeq = 0; W._orchName = 'Structured';

W._orchAddNode({ ptype: 'control', kind: 'start' }, 100, 30);
W._orchAddNode({ ptype: 'role', role: 'worker' }, 100, 150);
const workerId = W._orchSel;
W._orchAddNode({ ptype: 'role', role: 'critic' }, 100, 280);
const criticId = W._orchSel;
W._orchAddNode({ ptype: 'control', kind: 'stop' }, 100, 410);
const sId = W._orchNodes.filter(function (n) { return n.kind === 'start'; })[0].id;
const eId = W._orchNodes.filter(function (n) { return n.kind === 'stop'; })[0].id;
W._orchEdges.push({ id: 'sf1', from: sId, to: workerId });
W._orchEdges.push({ id: 'sf2', from: workerId, to: criticId });
W._orchEdges.push({ id: 'sf3', from: criticId, to: eId });

// ── Worker: objective (textarea) + must_do/must_not_do (list, kind hint) ──
W._orchSel = workerId;
W._orchSetParam('objective', 'Build the widget.');
// The list textarea sends a newline blob with blank lines + whitespace; the
// setter must trim, drop blanks, and store an ARRAY.
W._orchSetParam('must_do', '  ship it \n\n write tests \n', false, 'list');
W._orchSetParam('must_not_do', 'touch prod', false, 'list');

const worker = W._orchNodes.filter(function (n) { return n.id === workerId; })[0];
assert(Array.isArray(worker.params.must_do), 'must_do stored as an array');
assert(worker.params.must_do.length === 2,
  'blank lines dropped (got ' + JSON.stringify(worker.params.must_do) + ')');
assert(worker.params.must_do[0] === 'ship it' && worker.params.must_do[1] === 'write tests',
  'list items trimmed: ' + JSON.stringify(worker.params.must_do));
assert(worker.params.must_not_do.length === 1, 'must_not_do single item');

// Clearing a list field to all-blank must OMIT the key, not store [''].
W._orchSetParam('must_not_do', '   \n  ', false, 'list');
assert(!('must_not_do' in worker.params), 'emptied list key omitted');

// ── Critic: select (verdict_format) + bool (adversarial) ──
W._orchSel = criticId;
W._orchSetParam('objective', 'Check the widget.');
W._orchSetParam('verdict_format', 'pass_fail');   // select
W._orchSetParam('adversarial', true);             // checkbox → bool
W._orchSetParam('must_check', 'tests pass', false, 'list');

const critic = W._orchNodes.filter(function (n) { return n.id === criticId; })[0];
assert(critic.params.verdict_format === 'pass_fail', 'select value stored');
assert(critic.params.adversarial === true, 'bool stored as true');
assert(Array.isArray(critic.params.must_check), 'critic must_check is an array');

// Unset the select (choose the "" unset option) → key must be ABSENT, never ''.
W._orchSetParam('verdict_format', '');
assert(!('verdict_format' in critic.params),
  'unset select omits the key (not stored as empty string)');

// Unchecking the bool stores false (false is a meaningful value, kept).
W._orchSetParam('adversarial', false);
assert(critic.params.adversarial === false, 'unchecked bool stored as false');

const structuredDef = W._orchToDefinition();

console.log('CHECKS=' + _checks);
console.log('ALL_OK');
console.log('RESULT_JSON=' + JSON.stringify(finalDef));
console.log('RESULT_JSON2=' + JSON.stringify(structuredDef));
