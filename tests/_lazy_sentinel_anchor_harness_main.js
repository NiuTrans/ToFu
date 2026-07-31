
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const NC = process.argv[6] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
global.IntersectionObserver = win.IntersectionObserver = function () {
  return { observe(){}, unobserve(){}, disconnect(){} };
};

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

// ── Idle conv: no live stream, so renderChat takes the static surgical path. ──
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c1';
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';
const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_stampFreshness','buildTurnNav','calcCostCny',
  '_forceScrollToBottom','scrollToBottom','isNearBottom','showStreamingUIForConv',
  '_captureScrollAnchor','_restoreScrollAnchor','_applyAutopilotRunFolds',
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; } }
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._prefetchConvCosts = global._prefetchConvCosts = () => ({ then: () => {} });
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => ({ then: () => {} });
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._openScrollConvId = global._openScrollConvId = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
// Never-equal fingerprint so Guard 2 never SKIPS the surgical re-render.
win._convRenderFingerprint = global._convRenderFingerprint =
  (c) => 'fp:' + (c ? c.messages.length : 0) + ':' + Math.random();

// jsdom has no layout engine — give the container the geometry
// _loadOlderMessages reads for its scroll compensation.
const _ct = win.document.getElementById('chatContainer');
Object.defineProperty(_ct, 'scrollHeight', { get: () => 5000, configurable: true });
_ct.scrollTop = 0;

let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
const FIXED = process.argv[7];
const LEGACY = process.argv[8];
if (NC === 'firstchild') {
  // NEUTER: restore the pre-fix anchor — fall back to `inner.firstChild`,
  // which is the SENTINEL when a lazy window is active.
  if (chatSrc.indexOf(FIXED) === -1) {
    console.log('FAIL neuter_not_applied (fixed anchor sentinel absent)');
    console.log(out.join('\n')); process.exit(0);
  }
  chatSrc = chatSrc.replace(FIXED, LEGACY);
}

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));
(0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));

// streaming_render.js + chat_render.js CONCATENATED into one script, exactly as
// lib/js_bundler.py emits them — see the module docstring for why.
const streamSrc = fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/streaming_render.js'), 'utf8');
const api = (0, eval)(
  streamSrc + '\n;\n' + chatSrc + '\n;({renderChat, _loadOlderMessages});');
const renderChat = api.renderChat;
const _loadOlderMessages = api._loadOlderMessages;
if (typeof renderChat !== 'function' || typeof _loadOlderMessages !== 'function') {
  console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('fns_exposed', true);

function mkMsg(id, role, text) {
  return { role: role || 'assistant', _msgId: id, content: text || ('body ' + id) };
}
function layout() {
  const inner = win.document.getElementById('chatInner');
  return Array.from(inner.children).map(el =>
    el.id === '_lazyLoadSentinel' ? 'SENTINEL'
      : el.id === '_lazyLoadSentinelBottom' ? 'SENT_BOT'
      : (el.getAttribute('data-msg-id') || el.id));
}

// 24 messages; _INITIAL_RENDER is 20 → the first paint renders m4..m23 plus the
// head sentinel standing in for the 4 older ones.
const msgs = [];
for (let i = 0; i < 24; i++) msgs.push(mkMsg('m' + i, i % 2 ? 'assistant' : 'user', 'body ' + i));
const conv = { id: 'c1', messages: msgs };
win.conversations = global.conversations = [conv];
win.getActiveConv = global.getActiveConv = () => conv;

// ── 1) Conversation open (full render). ──
renderChat(conv, true);
check('seed_sentinel_at_head', layout()[0] === 'SENTINEL', 'layout0=' + layout()[0]);

// ── 2) Background repaints — this is how cost / file-change / compaction data
//       lands on every conversation open. Each one runs the surgical path. ──
for (let k = 0; k < 3; k++) renderChat(conv, false);

// INVARIANT A: the sentinel is layout furniture pinned at the HEAD. If the
// reconcile treats it as a message node it gets pushed to the bottom here.
check('sentinel_stays_at_head', layout()[0] === 'SENTINEL',
  'layout=' + layout().join(',').slice(0, 200));

// ── 3) Reader scrolls up → the IntersectionObserver fires the REAL loader,
//       which splices the older batch in with `sentinel.after(frag)`. ──
_loadOlderMessages();

// INVARIANT B: DOM order == conv.messages order. This is the user-visible fact
// ("first-round user message renders at the bottom").
const L = layout().filter(x => x !== 'SENTINEL' && x !== 'SENT_BOT');
const want = conv.messages.map(m => m._msgId).filter(id => L.indexOf(id) !== -1);
check('dom_order_matches_messages', L.join(',') === want.join(','),
  'dom=' + L.join(',').slice(0, 200));

// Explicit oldest-vs-newest probe — states the reported symptom directly, so a
// failure names the bug rather than just "order differs".
const idxM0 = L.indexOf('m0');
const idxM23 = L.indexOf('m23');
check('oldest_renders_above_newest', idxM0 >= 0 && idxM0 < idxM23,
  'm0@' + idxM0 + ' m23@' + idxM23);

console.log(out.join('\n'));
process.exit(0);
