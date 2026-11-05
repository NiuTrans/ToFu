/* ===== migrated source: core/request_inspector.js ===== */
/* Request Inspector — the FLAT WIRE FEED (2026-09-22 owner directive).
 *
 * The drawer renders ONE flat message list for the whole conversation —
 * every message exactly once, from the server fold
 * (lib/tasks_pkg/request_inspector.get_conv_inspector_wire). Turn, agent
 * and rebuild sections render as dividers; the turn nav is a SCROLL
 * model (the conversation bar's turn-nav-label pattern): jumping never
 * switches or loads a view, it scrolls to the divider. The drill-down it
 * replaced (task list → round list → single-round detail) is gone:
 * retries collapse to the newest run server-side, and per-round
 * debugging lives in the INLINE tool panel (openToolDebugPanel) and the
 * per-turn Turn Trace, both kept below. */

/* Narrow ports into native lifecycle, detail and payload owners. Remove
 * these bridges when the retained markup migrates; no duplicate read state. */
const _riSession = createInspectorSession({
  conversation: () => DebugShellState.activeConversationId || null,
  content: () => _riEl('debugContent'),
  hidden: () => document.hidden || runtimeScope.nativeVisibility?.isHidden() === true,
  live: () => _riFeed.live,
  load: (convId) => _riFeed.load(convId, true),
  fingerprint: (convId) => _riStoreFingerprint(convId),
  subscribe: (convId, changed) => {
    const store = convId && runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(convId);
    return store && typeof store.subscribe === 'function' ? store.subscribe(changed) : null;
  },
  failed: (error) => debugLog('[ri] refresh failed: ' + String(error), 'warn'),
});
const _riNavigation = createInspectorNavigation({
  element: (id) => _riEl(id), scroller: () => _riFeedScroller(),
  feed: () => _riSession.isOpen && _riSession.paneKind === 'feed',
  rows: () => _riFeed.turns,
  orderedTurns: () => runtimeScope.ConversationTurnRead?.ordered?.(_riSession.conversationId) || [],
  translate: (key, params) => t(key, params),
  relativeTime: (time) => _riRelTime(time), absoluteTime: (time) => _riAbsTime(time),
  showTurn: (id) => _riScrollToTurn(id),
});
const _riFeed = createInspectorFeed(_riSession, {
  read: (convId) => (typeof Api !== 'undefined' && Api.tasks && typeof Api.tasks.convWire === 'function')
    ? Api.tasks.convWire(convId) : Promise.resolve(null),
  content: () => _riEl('debugContent'), title: () => _riEl('debugTitle'), scroller: () => _riFeedScroller(),
  navigation: _riNavigation, translate: (key, params) => t(key, params),
  reconcile: (node, messages, options) => reconcileDebugMessages(node, messages, options),
  ordinal: (turn) => _riNavigation.ordinal(turn),
  relativeTime: (time) => _riRelTime(time), absoluteTime: (time) => _riAbsTime(time),
  showTurn: (turn) => _riScrollToTurn(turn), trace: (task) => _riOpenTrace(task),
});
const _riPayloads = createInspectorPayloads({
  snapshots: (taskId) => DebugShellState.requests[taskId],
  live: (taskId) => {
    const row = _riFeed.task(taskId);
    return !!(row && (row.live || ['running', 'queued', 'pending'].includes(String(row.status || '').toLowerCase())));
  },
  read: (taskId, roundNum, turn, kind, fold) =>
    (typeof Api !== 'undefined' && Api.tasks)
      ? Api.tasks.getRequestPayload(taskId, roundNum, turn, kind, fold) : Promise.resolve(null),
  log: (key, message, level) => _riFetchLog(key, message, level),
});
const _riDetails = createInspectorDetails(_riSession, {
  trace: (taskId) => (typeof Api !== 'undefined' && Api.tasks)
    ? Api.tasks.getTrace(taskId) : Promise.resolve(null),
  round: (taskId, roundNum, turn) => _riPayloads.round(taskId, roundNum, turn),
  loading: (kind) => _riDetailLoading(kind),
  showTrace: (doc) => renderInspectorTrace(_riEl('debugContent'), _riEl('debugTitle'), doc, t),
  messages: (payload) => {
    if (typeof showMessagesInDebug !== 'function') return false;
    showMessagesInDebug(payload.messages, payload.label || '', false,
      DebugShellState.activeConversationId, payload.tools || undefined, false, undefined,
      { resetScroll: true, contextManifest: payload.contextManifest || [] });
    return true;
  },
  back: (pane) => _riShowFeedBackBar(pane),
  failed: (retry) => _riDetailFailed(retry),
  feed: () => _riFeed.render(),
  land: (taskId, roundNum) => openRequestInspectorForToolRound(taskId, roundNum),
  log: (error) => debugLog('[ri] detail read failed: ' + String(error), 'warn'),
});
const _riInline = createInspectorInline({
  round: (taskId, roundNum, turn) => _riPayloads.round(taskId, roundNum, turn),
  increment: (taskId, roundNum, kind, messages, turn) =>
    _riPayloads.increment(taskId, roundNum, kind, messages, turn),
  detached: (taskId, roundNum, turn) => {
    if (!_riSession.isOpen) openRequestInspector();
    return _riDetails.detached(taskId, roundNum, turn);
  },
  translate: (key) => t(key),
  render: (node, messages) => renderDebugBlocksInto(node, messages, null),
  characters: (message) => typeof _debugMsgChars === 'function' ? _debugMsgChars(message) : 0,
  expand: (node) => { if (typeof _debugOpenBlock === 'function') _debugOpenBlock(node); },
  failed: (error) => debugLog('[ri] inline read failed: ' + String(error), 'warn'),
});

function _riStoreFingerprint(convId) {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !convId || !read.state) return '';
    const state = read.state(convId);
    if (!state) return '';
    const parts = [];
    const attempts = state.attemptsById || {};
    for (const id of Object.keys(attempts).sort()) {
      const a = attempts[id] || {};
      parts.push(id + ':' + (a.status || ''));
    }
    const turns = state.turnsById || {};
    let running = 0;
    for (const id of Object.keys(turns)) {
      if (turns[id] && turns[id].status === 'running') running += 1;
    }
    return parts.join(';') + '|r' + running;
  } catch (_) { return ''; }
}

function toggleRequestInspector() {
  if (_riSession.isOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riSession.open(DebugShellState.activeConversationId || null);
  _riNavigation.open();
  DebugShellState.visible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riFeed.select(_riSession.conversationId);
  void _riFeed.load(_riSession.conversationId);
}

function closeRequestInspector() {
  _riNavigation.close();
  _riSession.close();
  _riFeed.select(null);
  DebugShellState.visible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riSession.isOpen) openRequestInspector();
  /* Set AFTER open (open resets it): the pending scroll survives until
   * the wire for this conversation lands. */
  _riNavigation.land(String(taskId));
}

/* Turn-level landing for a tool-row anchor (the feed has no per-round
 * pane anymore): open the drawer scrolled to the producing turn. The
 * per-round view is the INLINE panel (openToolDebugPanel) — when the tool
 * row is in the DOM, that is where the round renders. */
async function openRequestInspectorForToolRound(taskId, roundNum) {
  return openRequestInspectorForTask(taskId);
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riSession.isOpen) return;
  _riSession.open(convId || null);
  _riNavigation.open();
  _riFeed.select(_riSession.conversationId);
  void _riFeed.load(_riSession.conversationId);
}

/* data-tofu-action target (header refresh button / inline retry). */
function riRefreshTasks() {
  if (!_riSession.isOpen) return undefined;
  return _riFeed.load(_riSession.conversationId);
}

function _riAbsTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(Number(ts));
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
      `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch (_) { return ''; }
}

/* Task-list primary timestamp: "3 分钟前" reads far better than a bare
 * HH:MM:SS when rows span a whole session; the absolute stamp stays on
 * the title tooltip. */
function _riRelTime(ts) {
  if (!ts) return '';
  const diff = Date.now() - Number(ts);
  if (diff < 0) return _riAbsTime(ts);
  const s = Math.floor(diff / 1000);
  if (s < 45) return t('ri.timeJustNow');
  const m = Math.floor(s / 60);
  if (m < 60) return t('ri.timeMinAgo', { n: Math.max(1, m) });
  const h = Math.floor(m / 60);
  if (h < 24) return t('ri.timeHoursAgo', { n: h });
  const d = Math.floor(h / 24);
  if (d < 7) return t('ri.timeDaysAgo', { n: d });
  return _riAbsTime(ts).slice(5, 16);
}

function _riScrollToTurn(turnId) {
  const node = (typeof _findRenderedNativeTurnNode === 'function')
    ? _findRenderedNativeTurnNode(turnId) : null;
  if (node && node.scrollIntoView) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function _riEl(id) { return document.getElementById(id); }

/* The drawer owns the pane whenever it is open: SSE evidence frames and
 * conversation-switch restores record into DebugShellState as before but
 * must not render over the feed. */
function _riFeedOwnsPane() { return _riSession.isOpen; }

function _riFeedScroller() {
  const content = _riEl('debugContent');
  if (!content || typeof content.closest !== 'function') return null;
  return content.closest('.ri-main') || content.parentElement;
}

/* ── Back-to-feed bar (trace / detached one-off views) ── */
function _riShowFeedBackBar(pane) {
  renderInspectorBack(_riEl('debugContent'), t, () => {
    if (pane && !pane.isCurrent()) return;
    _riSession.showFeed();
    _riFeed.render();
  });
}

/* ── Level 3: payload resolution — shared by the INLINE tool panel and
 * the drawer's no-slot fallback. Bounded payload cache; live SSE
 * snapshots win over server reads. */
function _riFetchLog(key, msg, level) {
  if (typeof debugLog === 'function') debugLog('[ri] payload ' + key + ' — ' + msg, level || 'info');
}
function _riDetailLoading(kind) {
  _riNavigation.suspend();
  const title = _riEl('debugTitle');
  if (title) title.textContent = t(kind === 'trace' ? 'ri.traceTitle' : 'ri.title');
  renderInspectorLoading(_riEl('debugContent'), t);
}
function _riDetailFailed(retry) {
  renderInspectorFailure(_riEl('debugContent'), t, retry);
}

/* Public tool entry delegates exact address, mount, read lifetime and markup
 * to the native inline owner. Remove this bridge with retained diagnostics. */
function openToolDebugPanel(taskId, roundNum, anchorEl) {
  if (typeof debugLog === 'function')
    debugLog('[ri] openToolDebugPanel ' + taskId + ' R' + roundNum, 'info');
  return _riInline.open(taskId, roundNum, anchorEl);
}

/* Trace fetching remains with the original-pane detail owner. Presentation
 * lives in inspector-trace-{projection,view}.ts; no retained duplicate renderer. */
function _riOpenTrace(taskId) { return _riDetails.trace(taskId); }
