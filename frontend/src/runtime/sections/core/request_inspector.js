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

let _riOpen = false;
/* traceOpen: the pane shows a per-turn trace instead of the feed (a
 * background poll must not clobber it). detached: the tool-row fallback
 * rendered a one-off state view into the pane. */
const _riSel = { traceOpen: false, traceTaskId: null, detached: false };
let _riConvId = null;
let _riPollTimer = null;
/* Poll cadence: fast while any turn is live, slow when idle. */
const _RI_POLL_LIVE_MS = 3000;
const _RI_POLL_IDLE_MS = 15000;

/* The feed model for _riConvId: {messages, sections, turns}. */
let _riWire = null;

/* One request/last-good owner per open conversation; no cross-conversation
 * fallback, no stale response paint, no overlapping poll + retry fetch. */
const _riWireController = createDiagnosticsWireController((convId) =>
  (typeof Api !== 'undefined' && Api.tasks &&
    typeof Api.tasks.convWire === 'function')
    ? Api.tasks.convWire(convId) : Promise.resolve(null));
let _riWireReadError = false;
/* Anchor scroll requested before the wire landed. */
let _riPendingScrollTaskId = null;

/* Real-time drive: the poll alone made the drawer lag behind reality.
 * Subscribing to the conversation's TurnStore flips that: any
 * attempt/turn STATUS dispatch (not content deltas) triggers a throttled
 * silent refresh. The poll stays as the cross-process backstop. */
let _riStoreUnsub = null;
let _riStoreFp = '';
let _riStoreRefreshTimer = null;

function _riStoreFingerprint() {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !_riConvId || !read.state) return '';
    const state = read.state(_riConvId);
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

function _riOnStoreEvent() {
  if (!_riOpen) return;
  const fp = _riStoreFingerprint();
  if (fp === _riStoreFp) return;  // content delta — not task activity
  _riStoreFp = fp;
  if (_riStoreRefreshTimer) return;
  _riStoreRefreshTimer = setTimeout(async () => {
    if (typeof _riStoreRefreshTimer.unref === 'function') {
      _riStoreRefreshTimer.unref();
    }
    _riStoreRefreshTimer = null;
    if (!_riOpen) return;
    await _riLoadWire(_riConvId, { silent: true });
    _riSchedulePoll(_riFeedIsLive() ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
  }, 800);
}

function _riBindStore(convId) {
  _riUnbindStore();
  if (!convId) return;
  try {
    const rt = runtimeScope.ConversationTurnStore;
    const store = rt && rt.ensureRuntimeStore && rt.ensureRuntimeStore(convId);
    if (!store || typeof store.subscribe !== 'function') return;
    _riStoreFp = _riStoreFingerprint();
    _riStoreUnsub = store.subscribe(_riOnStoreEvent);
  } catch (_) { /* store unavailable — poll remains the drive */ }
}

function _riUnbindStore() {
  if (_riStoreUnsub) {
    try { _riStoreUnsub(); } catch (_) { /* already gone */ }
    _riStoreUnsub = null;
  }
  _riStoreFp = '';
  if (_riStoreRefreshTimer) {
    clearTimeout(_riStoreRefreshTimer);
    _riStoreRefreshTimer = null;
  }
}

function toggleRequestInspector() {
  if (_riOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riOpen = true;
  _riSel.traceOpen = false;
  _riSel.traceTaskId = null;
  _riSel.detached = false;
  DebugShellState.visible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riConvId = DebugShellState.activeConversationId || null;
  _riWire = null;
  _riWireReadError = false;
  _riWireController.select(_riConvId);
  _riCloseTurnJump();
  _riRenderFeedLoading();
  const convId = DebugShellState.activeConversationId;
  _riLoadWire(convId);
  _riBindStore(convId);
  _riSchedulePoll(_RI_POLL_IDLE_MS);
}

function closeRequestInspector() {
  _riOpen = false;

  _riWireController.select(null);
  _riConvId = null;
  _riWire = null;
  _riWireReadError = false;
  _riPendingScrollTaskId = null;
  _riStopPoll();

  _riUnbindStore();
  _riCloseTurnJump();
  DebugShellState.visible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riOpen) openRequestInspector();
  /* Set AFTER open (open resets it): the pending scroll survives until
   * the wire for this conversation lands. */
  _riPendingScrollTaskId = String(taskId);
  _riConsumePendingScroll();
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
  if (!_riOpen) return;

  _riConvId = convId || null;
  _riWire = null;
  _riWireReadError = false;
  _riWireController.select(_riConvId);
  _riFeedActiveIdx = -1;
  _riSel.traceOpen = false;
  _riSel.traceTaskId = null;
  _riSel.detached = false;
  _riPendingScrollTaskId = null;
  _riCloseTurnJump();
  _riRenderFeedLoading();
  _riLoadWire(convId);
  _riBindStore(convId);
}

function _riStopPoll() {
  if (_riPollTimer) { clearTimeout(_riPollTimer); _riPollTimer = null; }
}

function _riSchedulePoll(delayMs) {
  _riStopPoll();
  _riPollTimer = setTimeout(_riPollTick, delayMs);
  /* Node/jsdom harnesses: a pending poll must not keep the event loop
   * alive once the test body finished (browsers ignore unref). */
  if (_riPollTimer && typeof _riPollTimer.unref === 'function') {
    _riPollTimer.unref();
  }
}

async function _riPollTick() {
  _riPollTimer = null;
  if (!_riOpen) return;
  // The Android WebView reports visible while backgrounded; the shell's
  // nativeVisibility bridge is the only reliable pocket signal there.
  if (document.hidden || runtimeScope.nativeVisibility?.isHidden() === true) {
    _riSchedulePoll(_RI_POLL_IDLE_MS);
    return;
  }
  await _riLoadWire(_riConvId, { silent: true });
  _riSchedulePoll(_riFeedIsLive() ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
}

function _riFeedIsLive() {
  const turns = (_riWire && Array.isArray(_riWire.turns)) ? _riWire.turns : [];
  return turns.some((row) => row && row.live);
}

/* data-tofu-action target (header refresh button / inline retry). */
function riRefreshTasks() {
  if (!_riOpen) return undefined;
  return _riLoadWire(_riConvId, { silent: false });
}

function _riEsc(s) {
  return (typeof escapeHtml === 'function')
    ? escapeHtml(s == null ? '' : String(s)) : String(s == null ? '' : s);
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

/* Reply ordinal for a turn id (assistant replies only — matches what the
 * user reads as "回复 #N"). The TurnStore is the authority; unresolved
 * turns (older history, bucket rows) render as "更早". */
function _riTurnOrdinalForId(turnId) {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !_riConvId || !turnId) return null;
    const ordered = (read.ordered && read.ordered(_riConvId)) || [];
    let n = 0;
    for (const turn of ordered) {
      if (!turn || !turn.turnId) continue;
      if (turn.actor === 'assistant') n += 1;
      if (turn.turnId === turnId) return n > 0 ? n : null;
    }
    return null;
  } catch (_) { return null; }
}

function _riScrollToTurn(turnId) {
  const node = (typeof _findRenderedNativeTurnNode === 'function')
    ? _findRenderedNativeTurnNode(turnId) : null;
  if (node && node.scrollIntoView) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function _riEl(id) { return document.getElementById(id); }

function _riStatusInfo(row) {
  const raw = String((row && row.status) || '').toLowerCase();
  if ((row && row.live) || ['running', 'queued', 'pending'].includes(raw))
    return { tone: 'running', label: t('ri.statusRunning') };
  if (['done', 'completed', 'complete', 'success'].includes(raw))
    return { tone: 'done', label: t('ri.statusDone') };
  if (['error', 'failed', 'failure'].includes(raw))
    return { tone: 'error', label: t('ri.statusFailed') };
  if (['aborted', 'interrupted', 'cancelled', 'canceled', 'stopped'].includes(raw))
    return { tone: 'stopped', label: t('ri.statusStopped') };
  return { tone: 'neutral', label: raw || t('ri.statusUnknown') };
}

/* The drawer owns the pane whenever it is open: SSE evidence frames and
 * conversation-switch restores record into DebugShellState as before but
 * must not render over the feed. */
function _riFeedOwnsPane() { return _riOpen; }

/* ── Feed loading ── */
async function _riLoadWire(convId, opts) {
  const silent = !!(opts && opts.silent);
  if (!_riOpen || (convId || null) !== _riConvId) return;
  if (!silent && !_riWire) _riRenderFeedLoading();
  const view = await _riWireController.load();
  if (!view || !_riOpen || (convId || null) !== _riConvId) return;
  _riWireReadError = view.readError;
  if (!view.wire) {
    _riWire = null;
    _riRenderFeedError();
    return;
  }
  _riWire = view.wire;
  _riRenderWire({ keepScroll: true });
}

function _riRenderFeedLoading() {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.loading');
  if (content) {
    content.innerHTML = `<div class="ri-loading">` +
      `<span class="ri-spinner" aria-hidden="true"></span>` +
      `${_riEsc(t('ri.loading'))}</div>`;
  }
  _riPaintFeedNav();
}

function _riRenderFeedError() {
  const content = _riEl('debugContent');

  const title = _riEl('debugTitle');
  if (title) title.textContent = t('ri.loadFailed');
  if (content) {
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button>` +
      `</div>`;
  }
  if (content) {
    content._rawMessages = [];
    content._rawTools = null;
  }
  _riPaintFeedNav();
}

/* ── Feed rendering ─────────────────────────────────────────────────
 * One flat scroll: section dividers interleaved with the shared debug
 * message blocks (createBlock from debug_panel.js — ONE renderer). A
 * wire refresh preserves expanded blocks (by stable message identity),
 * focused headers and scroll position; a live append keeps the user pinned to the
 * bottom only when they were already there. */
function _riFeedScroller() {
  const content = _riEl('debugContent');
  if (!content || typeof content.closest !== 'function') return null;
  return content.closest('.ri-main') || content.parentElement;
}

function _riRenderWire(opts) {
  _riPaintFeedNav();
  if (_riSel.traceOpen || _riSel.detached) return;  // trace / one-off view owns the pane
  const content = _riEl('debugContent');
  if (!content) return;
  const keepScroll = !!(opts && opts.keepScroll);
  const openMids = new Set();
  const focusedMid = keepScroll ? captureInspectorFocus(content) : null;
  const scroller = _riFeedScroller();
  let prevScroll = 0;
  let stickBottom = false;
  if (keepScroll) {
    content.querySelectorAll('.debug-msg-block.open').forEach((b) => {
      if (b.dataset.mid) openMids.add(b.dataset.mid);
    });
    if (scroller) {
      prevScroll = scroller.scrollTop;
      stickBottom = scroller.scrollHeight > 0 &&
        scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 40;
    }
  }
  content.innerHTML = '';
  const wire = _riWire || { messages: [], sections: [], turns: [] };
  const messages = Array.isArray(wire.messages) ? wire.messages : [];
  const sections = Array.isArray(wire.sections) ? wire.sections : [];
  const turns = Array.isArray(wire.turns) ? wire.turns : [];
  const title = _riEl('debugTitle');
  if (title) {
    title.textContent = `Messages (${messages.length})` +
      (turns.length ? ` · ${t('ri.feedTurns', { n: turns.length })}` : '');
  }
  if (_riWireReadError) {
    /* A failed read leaves the last complete feed intact, or presents an
     * honest partial feed if this open epoch has no complete snapshot. */
    const warn = document.createElement('div');
    warn.className = 'ri-warn-line';
    warn.innerHTML = `${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button>`;
    content.appendChild(warn);
  }
  if (Array.isArray(wire.eventReadBounds) && wire.eventReadBounds.length) {
    const note = document.createElement('div');
    note.className = 'ri-feed-truncated';
    note.textContent = t('ri.eventWindowPartial');
    content.appendChild(note);
  }
  if (wire.olderTurnsDropped) {
    const note = document.createElement('div');
    note.className = 'ri-feed-truncated';
    note.textContent = t('ri.feedTruncated', { n: wire.olderTurnsDropped });
    content.appendChild(note);
  }
  if (!messages.length && !sections.length) {
    const empty = document.createElement('div');
    empty.className = 'ri-main-empty';
    empty.textContent = t('ri.empty');
    content.appendChild(empty);
    content._rawMessages = [];
    content._rawTools = null;
    _riConsumePendingScroll();
    return;
  }
  const byIndex = new Map();
  for (const s of sections) {
    const k = Math.max(0, Math.min(messages.length, Number(s.msgIndex) || 0));
    if (!byIndex.has(k)) byIndex.set(k, []);
    byIndex.get(k).push(s);
  }
  const appendSections = (idx) => {
    for (const s of (byIndex.get(idx) || [])) {
      content.appendChild(_riFeedDividerEl(s));
    }
  };
  if (typeof createBlock === 'function') {
    messages.forEach((msg, i) => {
      if (byIndex.has(i)) appendSections(i);
      const block = createBlock(msg, i);
      block.dataset.fp = (typeof msgFingerprint === 'function')
        ? msgFingerprint(msg) : '';
      block._msgRef = msg;
      content.appendChild(block);
    });
    if (byIndex.has(messages.length)) appendSections(messages.length);
  }
  content._rawMessages = messages;
  content._rawTools = null;
  if (openMids.size && typeof _debugOpenBlock === 'function') {
    content.querySelectorAll('.debug-msg-block').forEach((b) => {
      if (b.dataset.mid && openMids.has(b.dataset.mid)) _debugOpenBlock(b);
    });
  }
  if (keepScroll && scroller) {
    scroller.scrollTop = stickBottom ? scroller.scrollHeight : prevScroll;
  }
  restoreInspectorFocus(content, focusedMid);
  _riBindFeedScroll();
  /* Anchors that arrived before the feed rendered land here, once the
   * dividers actually exist. */
  _riConsumePendingScroll();
}

function _riFeedDividerEl(s) {
  if (s && s.type === 'rebuild') {
    const el = document.createElement('div');
    el.className = 'ri-feed-rebuild';
    el.textContent = t('ri.rebuildNote', { k: s.kept || 0, d: s.dropped || 0 });
    return el;
  }
  if (s && s.type === 'agent') {
    const el = document.createElement('div');
    el.className = 'ri-feed-agent';
    el.innerHTML =
      `<span class="ri-agent-badge">${_riEsc(s.agentId || 'agent')}</span>` +
      `<span class="ri-feed-agent-label">${_riEsc(t('ri.agentSection'))}</span>`;
    return el;
  }
  /* turn divider */
  const el = document.createElement('div');
  el.className = 'ri-feed-turn';
  el.dataset.turnId = (s && s.turnId) || '';
  el.dataset.taskId = (s && s.taskId) || '';
  const ordinal = _riTurnOrdinalForId(s && s.turnId);
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'ri-turn-chip' + (ordinal ? '' : ' ri-turn-chip-muted');
  chip.textContent = ordinal ? t('ri.turnChip', { n: ordinal }) : t('ri.earlierShort');
  chip.title = t('ri.turnChipTip');
  chip.onclick = () => { if (s.turnId) _riScrollToTurn(s.turnId); };
  el.appendChild(chip);
  const status = _riStatusInfo({ status: s.status, live: s.live });
  const dot = document.createElement('span');
  dot.className = `ri-task-status ri-tone-${_riEsc(status.tone)}`;
  dot.innerHTML = `<span class="ri-task-status-dot" aria-hidden="true"></span>` +
    `${_riEsc(status.label)}`;
  el.appendChild(dot);
  const preview = document.createElement('span');
  preview.className = 'ri-feed-turn-preview';
  preview.textContent = s.readError ? t('ri.loadFailed')
    : s.expired ? t('ri.expired') : (s.preview || '');
  preview.title = preview.textContent;
  el.appendChild(preview);
  const time = document.createElement('span');
  time.className = 'ri-feed-turn-time';
  time.textContent = _riRelTime(s.createdAt);
  time.title = _riAbsTime(s.createdAt);
  el.appendChild(time);
  /* Per-turn Turn Trace (耗时分析) — the ONE click that answers "where
   * did the time go" for this reply's latest run. */
  if (s.taskId) {
    const traceBtn = document.createElement('button');
    traceBtn.type = 'button';
    traceBtn.className = 'ri-feed-trace';
    traceBtn.textContent = t('ri.traceEntry');
    traceBtn.title = t('ri.traceEntryHint');
    traceBtn.onclick = () => _riOpenTrace(s.taskId);
    el.appendChild(traceBtn);
  }
  return el;
}

/* ── Turn navigation (SCROLL model) ──────────────────────────────────
 * Entries come from the wire's turn list; jumping scrolls the feed to
 * the divider, never switches content. The active entry tracks both the
 * explicit jumps and the user's own scrolling. */
let _riFeedActiveIdx = -1;

function _riFeedNavEntries() {
  const turns = (_riWire && Array.isArray(_riWire.turns)) ? _riWire.turns : [];
  return turns.map((row) => ({
    key: row.turnId || row.taskId,
    turnId: row.turnId || '',
    taskId: row.taskId || '',
    ordinal: _riTurnOrdinalForId(row.turnId),
    preview: row.preview || '',
    createdAt: row.createdAt || 0,
    live: !!row.live,
    status: row.status || '',
    expired: !!row.expired,
    readError: !!row.readError,
  }));
}

function _riPaintFeedNav() {
  const nav = _riEl('riTurnNav');
  if (!nav) return;  /* chrome absent (embedded harness) — nav is optional */
  const entries = _riFeedNavEntries();
  if (entries.length < 2) {
    nav.hidden = true;
    _riCloseTurnJump();
    return;
  }
  nav.hidden = false;
  if (_riFeedActiveIdx < 0 || _riFeedActiveIdx >= entries.length) {
    /* Default: the newest turn — reading starts at the bottom. */
    _riFeedActiveIdx = entries.length - 1;
  }
  const active = entries[_riFeedActiveIdx];
  const prev = _riEl('riTurnPrev');
  const next = _riEl('riTurnNext');
  const pos = _riEl('riTurnPos');
  const readout = _riEl('riTurnReadout');
  if (readout) {
    readout.textContent = active.ordinal
      ? t('ri.turnChip', { n: active.ordinal }) : t('ri.earlierShort');
  }
  if (pos) {
    pos.title = t('ri.jumpToTurn');
    pos.setAttribute('aria-label', t('ri.jumpToTurn'));
    pos.onclick = () => _riToggleTurnJump();
  }
  if (prev) {
    prev.disabled = _riFeedActiveIdx <= 0;
    prev.onclick = () => _riScrollFeedToIdx(_riFeedActiveIdx - 1);
  }
  if (next) {
    next.disabled = _riFeedActiveIdx >= entries.length - 1;
    next.onclick = () => _riScrollFeedToIdx(_riFeedActiveIdx + 1);
  }
  /* An open jump list rides live data — repaint it on refresh ticks. */
  const panel = _riEl('riTurnJump');
  if (panel && !panel.hidden) _riFillTurnJump(entries, active);
}

function _riFeedTurnDividerEl(entry) {
  const content = _riEl('debugContent');
  if (!content) return null;
  for (const el of content.querySelectorAll('.ri-feed-turn')) {
    if ((entry.turnId && el.dataset.turnId === entry.turnId) ||
        (!entry.turnId && el.dataset.taskId === entry.taskId)) return el;
  }
  return null;
}

function _riScrollFeedToIdx(idx) {
  const entries = _riFeedNavEntries();
  const entry = entries[idx];
  if (!entry) return;
  _riFeedActiveIdx = idx;
  _riPaintFeedNav();
  _riCloseTurnJump();
  const el = _riFeedTurnDividerEl(entry);
  if (!el) return;
  if (typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  el.classList.add('ri-flash');
  setTimeout(() => el.classList.remove('ri-flash'), 1600);
}

function _riConsumePendingScroll() {
  const taskId = _riPendingScrollTaskId;
  if (!taskId) return;
  const entries = _riFeedNavEntries();
  if (!entries.length) return;  // wire not here yet (or failed to load)
  const idx = entries.findIndex((e) => e.taskId === taskId);
  const target = entries[idx >= 0 ? idx : entries.length - 1];
  /* The pane may still show the loading spinner or a trace — keep the
   * pending scroll until the divider is actually rendered. */
  if (!_riFeedTurnDividerEl(target)) return;
  _riPendingScrollTaskId = null;
  /* An unknown/out-of-page task lands on the newest turn — the click
   * always lands somewhere meaningful. */
  _riScrollFeedToIdx(idx >= 0 ? idx : entries.length - 1);
}

/* The active nav entry also tracks the user's own scrolling (the same
 * 30%-of-viewport rule the conversation bar's turn nav uses). */
let _riFeedScrollTimer = null;
function _riBindFeedScroll() {
  const scroller = _riFeedScroller();
  if (!scroller || scroller._riFeedBound) return;
  scroller._riFeedBound = true;
  scroller.addEventListener('scroll', () => {
    if (_riFeedScrollTimer) return;
    _riFeedScrollTimer = setTimeout(() => {
      _riFeedScrollTimer = null;
      _riSyncActiveTurnFromScroll();
    }, 120);
  }, { passive: true });
}

function _riSyncActiveTurnFromScroll() {
  if (!_riOpen || _riSel.traceOpen || _riSel.detached) return;
  const entries = _riFeedNavEntries();
  if (entries.length < 2) return;
  const scroller = _riFeedScroller();
  if (!scroller) return;
  const boundary = scroller.scrollTop + scroller.clientHeight * 0.3;
  let idx = 0;
  entries.forEach((entry, i) => {
    const el = _riFeedTurnDividerEl(entry);
    if (el && el.offsetTop <= boundary) idx = i;
  });
  if (idx !== _riFeedActiveIdx) {
    _riFeedActiveIdx = idx;
    _riPaintFeedNav();
  }
}

let _riJumpBound = false;
function _riToggleTurnJump() {
  const panel = _riEl('riTurnJump');
  if (!panel) return;
  if (!panel.hidden) {
    _riCloseTurnJump();
    return;
  }
  const entries = _riFeedNavEntries();
  if (entries.length < 2) return;
  _riFillTurnJump(entries, entries[_riFeedActiveIdx]);
  panel.hidden = false;
  if (!_riJumpBound) {
    _riJumpBound = true;
    document.addEventListener('mousedown', _riJumpOutside, true);
  }
}

function _riJumpOutside(ev) {
  const panel = _riEl('riTurnJump');
  const pos = _riEl('riTurnPos');
  if (!panel || panel.hidden) return;
  const target = ev && ev.target;
  if ((panel.contains && panel.contains(target)) ||
      (pos && pos.contains && pos.contains(target))) return;
  _riCloseTurnJump();
}

function _riCloseTurnJump() {
  const panel = _riEl('riTurnJump');
  if (panel) panel.hidden = true;
  if (_riJumpBound) {
    _riJumpBound = false;
    document.removeEventListener('mousedown', _riJumpOutside, true);
  }
}

function _riFillTurnJump(entries, active) {
  const panel = _riEl('riTurnJump');
  if (!panel) return;
  panel.innerHTML = '';
  /* Newest first — the reply you were just reading lands on top. */
  const ordered = entries.map((e, i) => ({ e, i }))
    .sort((a, b) => b.e.createdAt - a.e.createdAt);
  for (const { e, i } of ordered) {
    const row = document.createElement('div');
    row.className = 'ri-turn-jump-row' + (e === active ? ' ri-sel' : '');
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    if (e.turnId) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ri-turn-chip' + (e.ordinal ? '' : ' ri-turn-chip-muted');
      chip.textContent = e.ordinal
        ? t('ri.turnChip', { n: e.ordinal }) : t('ri.earlierShort');
      chip.title = t('ri.turnChipTip');
      chip.onclick = (ev) => {
        ev.stopPropagation();
        _riCloseTurnJump();
        _riScrollToTurn(e.turnId);
      };
      row.appendChild(chip);
    } else {
      const tag = document.createElement('span');
      tag.className = 'ri-turn-chip ri-turn-chip-muted';
      tag.textContent = t('ri.earlierShort');
      row.appendChild(tag);
    }
    const preview = document.createElement('span');
    preview.className = 'ri-turn-jump-preview';
    preview.textContent = e.readError ? t('ri.loadFailed')
      : e.expired ? t('ri.expired') : (e.preview || '');
    preview.title = preview.textContent;
    row.appendChild(preview);
    const time = document.createElement('span');
    time.className = 'ri-turn-jump-time';
    time.textContent = _riRelTime(e.createdAt);
    time.title = _riAbsTime(e.createdAt);
    row.appendChild(time);
    row.onclick = () => _riScrollFeedToIdx(i);
    panel.appendChild(row);
  }
}

/* ── Back-to-feed bar (trace / detached one-off views) ── */
function _riShowFeedBackBar() {
  const content = _riEl('debugContent');
  if (!content) return;
  const bar = document.createElement('div');
  bar.className = 'ri-feed-back';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ri-retry';
  btn.textContent = t('ri.backToFeed');
  btn.onclick = () => {
    _riSel.traceOpen = false;
    _riSel.traceTaskId = null;
    _riSel.detached = false;
    _riRenderWire({ keepScroll: false });
  };
  bar.appendChild(btn);
  content.prepend(bar);
}

/* ── Level 3: payload resolution — shared by the INLINE tool panel and
 * the drawer's no-slot fallback. Bounded payload cache; live SSE
 * snapshots win over server reads. */
const _riPayloadCache = {};
const _RI_PAYLOAD_CACHE_MAX = 40;
function _riCachePayload(key, payload) {
  if (!_riPayloadCache[key]) {
    const ids = Object.keys(_riPayloadCache);
    if (ids.length >= _RI_PAYLOAD_CACHE_MAX) delete _riPayloadCache[ids[0]];
  }
  _riPayloadCache[key] = payload;
  return payload;
}
/* 2026-09-10 goal-mode probe: trace every payload resolution so one badge
 * click shows exactly where the round view dies (local snapshot / cache /
 * server). warn lines also relay to logs/app.log through the client-error
 * beacon, so the trace is visible in the background without devtools. */
function _riFetchLog(key, msg, level) {
  if (typeof debugLog === 'function')
    debugLog('[ri] payload ' + key + ' — ' + msg, level || 'info');
}
function _riRowIsLive(row) {
  return !!(row && (row.live ||
    ['running', 'queued', 'pending'].includes(
      String(row.status || '').toLowerCase())));
}
async function _riFetchPayload(taskId, roundNum, turn, kind, opts) {
  turn = turn || '';
  kind = kind || 'request';
  /* opts.fold: ask the SERVER to diff this round against the previous one
   * (same turn/kind) and return foldPrefix/diffBase — one payload download
   * per click instead of the old two full-context transfers. */
  const fold = !!(opts && opts.fold);
  const key = taskId + ':' + kind + ':' + turn + ':' + roundNum;
  /* Prefer live snapshots; Flow phases are keyed by turn|roundNum. */
  const _acc = DebugShellState.requests[taskId];
  if (kind === 'state') {
    const st = _acc && (_acc.states || []).filter((s) => s && s.messages &&
      String(s.roundNum) === String(roundNum) && (!turn || s.turn === turn)).pop();
    if (st) {
      const payload = { messages: st.messages, tools: st.tools,
        label: st.label, model: st.model, params: st.params, kind: 'state' };
      _riFetchLog(key, 'live state snapshot, msgs=' + payload.messages.length);
      return _riCachePayload(key, payload);
    }
  } else {
    const _accKey = turn ? turn + '|' + roundNum : String(roundNum);
    const acc = _acc && _acc.rounds[_accKey];
    if (acc && acc.messages) {
      const payload = { messages: acc.messages, tools: acc.tools,
        label: acc.label, model: acc.model, params: acc.params,
        turn: acc.turn || turn };
      _riFetchLog(key, 'live request snapshot, msgs=' + payload.messages.length);
      return _riCachePayload(key, payload);
    }
  }
  /* A live task keeps appending: a cached server read from an earlier
   * poll would freeze the round mid-flight. (Live SSE snapshots above
   * still win — they ARE the newest state.) */
  const live = _riRowIsLive(_riWireTaskRow(taskId));
  if (!live && _riPayloadCache[key]) {
    const cached = _riPayloadCache[key];
    const n = (cached.messages || []).length;
    _riFetchLog(key, 'cache serve, msgs=' + n, n ? 'info' : 'warn');
    return cached;
  }
  _riFetchLog(key, 'server fetch (live=' + live + ', localAcc=' +
    (_acc ? 'yes' : 'no') + ', cached=' + (_riPayloadCache[key] ? 'yes' : 'no') +
    ')');
  let data = null;
  try {
    data = (typeof Api !== 'undefined' && Api.tasks)
      ? await Api.tasks.getRequestPayload(taskId, roundNum, turn || undefined,
          kind === 'state' ? 'state' : undefined, fold || undefined) : null;
  } catch (error) {
    if (error && error.status === 404) return null;
    throw error;
  }
  if (data && data.messages) {
    _riFetchLog(key, 'server ok, msgs=' + data.messages.length,
      data.messages.length ? 'info' : 'warn');
    if (!_riRowIsLive(_riWireTaskRow(taskId))) {
      _riCachePayload(key, data);
    }
    return data;
  }
  _riFetchLog(key, data ? 'payload without messages' : 'null (404 or error)',
    'warn');
  return null;
}

/* Liveness for the payload cache: the wire's turn list is the drawer's
 * only liveness source now that the task list is gone. */
function _riWireTaskRow(taskId) {
  const turns = (_riWire && Array.isArray(_riWire.turns)) ? _riWire.turns : [];
  for (const row of turns) {
    if (row && row.taskId === taskId) return row;
  }
  return null;
}

/* Longest exact JSON prefix; divergence safely produces no fold. */
function _riSharedPrefix(prevMsgs, curMsgs) {
  const n = Math.min(prevMsgs.length, curMsgs.length);
  let k = 0;
  while (k < n && JSON.stringify(prevMsgs[k]) === JSON.stringify(curMsgs[k])) k++;
  return k;
}

/* Return only messages appended by this round; null selects the tail fallback. */
async function _riRoundScopedMessages(taskId, roundNum, kind, messages, turn = '') {
  const num = parseInt(roundNum, 10);
  if (!Number.isFinite(num) || num <= 1 || !Array.isArray(messages)) return null;
  let prev;
  try {
    prev = await _riFetchPayload(taskId, num - 1, turn,
      kind === 'state' ? 'state' : 'request');
  } catch (error) {
    /* The previous-round diff is optional. Keep the current round visible
     * when only the diff base fails to load. */
    _riFetchLog(taskId + ':' + roundNum, 'diff base unavailable', 'warn');
    return null;
  }
  if (!prev || !Array.isArray(prev.messages) || !prev.messages.length)
    return null;
  const k = _riSharedPrefix(prev.messages, messages);
  return k > 0 ? messages.slice(k) : null;
}

/* Tail fallback excludes system/history and keeps this round's tool exchange. */
function _riTailSlice(messages) {
  if (!Array.isArray(messages)) return [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === 'assistant' &&
        Array.isArray(m.tool_calls) && m.tool_calls.length)
      return messages.slice(i);
  }
  /* Final-answer round: use content after the last user message. */
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i] && messages[i].role === 'user') {
      const tail = messages.slice(i + 1);
      if (tail.length) return tail;
      break;
    }
  }
  /* Last resort: show non-system messages. */
  return messages.filter((m) => m && m.role !== 'system');
}

/* ── Tool-row debug panel (ONE view: the post-tool result state) ──────────
 * ONE entry per tool row opening ONE view. The former request | state tab
 * pair was redundant, not two questions: the state mirror for round N is
 * captured AFTER the tool results are appended to the SAME message list the
 * request was built from (lib/tasks_pkg/tool_dispatch/_pipeline.py — same
 * roundNum axis), so the request payload is a strict PREFIX of the mirror.
 * Showing both meant clicking twice to see the same messages minus the
 * results (owner, 2026-07-29: "we don't need both").
 *
 * The request axis survives as a FALLBACK, not a tab: swarm sub-agents
 * persist kind='request' snapshots only (lib/swarm/agent.py has no state
 * emission), so a state-only panel would render "mirror missing" on every
 * sub-agent tool row. `_riFetchRoundView` therefore tries the mirror first
 * and degrades to the request, telling the caller which one it got so the
 * chip can name the axis instead of silently mislabelling it.
 *
 * Mounts right after the tool round's [data-prn] slot and renders through
 * the SAME renderer as the drawer detail (renderDebugBlocksInto — no second
 * JSON renderer). When the tool row is not in the DOM (unloaded/old
 * conversation), degrades to the drawer so the click always lands somewhere
 * meaningful.
 *
 * ROUND-SCOPED (owner, 2026-07-28): renders ONLY what that round appended —
 * the increment over the previous round's same-kind payload — never the full
 * conversation-history dump ("records only for this round of tool calls are
 * sufficient"). The cross-round chip strip was removed with it: one click
 * answers one round; the drawer remains the place for cross-round
 * navigation. */
async function openToolDebugPanel(taskId, roundNum, anchorEl) {
  const turn = anchorEl?.dataset?.riTurn || '';
  if (!taskId || roundNum == null) return;
  if (typeof debugLog === 'function')
    debugLog('[ri] openToolDebugPanel ' + taskId + ' R' + roundNum, 'info');
  let slot = (anchorEl && typeof anchorEl.closest === 'function')
    ? anchorEl.closest('[data-prn]') : null;
  if (!slot) {
    const marker = document.querySelector(
      '[data-ri-state="' + String(taskId) + ':' + String(roundNum) + '"]');
    if (marker && typeof marker.closest === 'function')
      slot = marker.closest('[data-prn]');
  }
  if (!slot) {
    /* Tool row not in the DOM (unloaded / old conversation) — degrade to the
     * drawer instead of a dead click, showing the SAME view the inline panel
     * would have: the result state, or the request when no mirror exists.
     * The detached view owns the pane (polls must not clobber it) until the
     * user returns to the feed. */
    if (typeof debugLog === 'function')
      debugLog('[ri] no tool slot in DOM for ' + taskId + ' R' + roundNum +
        ' — degrading to drawer', 'info');
    if (!_riOpen) openRequestInspector();
    const convId = DebugShellState.activeConversationId;
    _riSel.detached = true;
    _riSel.traceOpen = false;
    let view;
    try {
      view = await _riFetchRoundView(taskId, roundNum, turn);
    } catch (error) {
      if (!_riOpen || DebugShellState.activeConversationId !== convId) return;
      const content = _riEl('debugContent');
      if (content) {
        content.innerHTML = '<div class="ri-empty">' +
          _riEsc(t('ri.loadFailed')) + '</div>';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'ri-retry';
        retry.textContent = t('ri.retry');
        retry.onclick = () => openToolDebugPanel(taskId, roundNum, anchorEl);
        content.appendChild(retry);
        _riShowFeedBackBar();
      }
      return;
    }
    if (!_riOpen || DebugShellState.activeConversationId !== convId) return;
    if (view && view.payload && view.payload.messages &&
        typeof showMessagesInDebug === 'function') {
      showMessagesInDebug(view.payload.messages, view.payload.label || '', false,
        DebugShellState.activeConversationId,
        view.payload.tools || undefined, false, undefined,
        { resetScroll: true,
          contextManifest: view.payload.contextManifest || [] });
      _riShowFeedBackBar();
      return;
    }
    _riSel.detached = false;
    _riRenderWire();
    if (!turn) await openRequestInspectorForToolRound(taskId, roundNum);
    return;
  }
  /* Re-clicking the entry for the round already open closes it (toggle). */
  const existing = document.querySelector('.ri-state-panel');
  if (existing && existing.dataset.riRound === String(roundNum) &&
      existing.dataset.riTask === String(taskId) &&
      existing.dataset.riTurn === turn) {
    existing.remove();
    return;
  }
  _riMountToolPanel(slot, taskId, roundNum, turn);
}

/* Resolve the ONE view a tool row's debug entry shows, and say which axis it
 * came from. The post-tool mirror is preferred because it is a superset of
 * the request; the request is the fallback for rounds that never emitted a
 * mirror (historical agents, an aborted round, an expired state row).
 * Returns {kind, payload} or null when neither axis has anything. */
async function _riFetchRoundView(taskId, roundNum, turn = '') {
  /* fold: the server diffs against the previous round from its cached rows,
   * so the round-scoped increment usually needs no second download. */
  return resolveInspectorView(
    (t, r, tu, kind) => _riFetchPayload(t, r, tu, kind, { fold: true }),
    taskId, roundNum, turn);
}

/* Mount the (single-instance) panel after a tool slot. Transient by
 * design — a chat re-render may drop it; re-click reopens. */
async function _riMountToolPanel(slot, taskId, roundNum, turn = '') {
  document.querySelectorAll('.ri-state-panel').forEach((p) => p.remove());
  const panel = document.createElement('div');
  panel.className = 'ri-state-panel';
  panel.dataset.riTask = String(taskId);
  panel.dataset.riTurn = turn;
  panel.dataset.riRound = String(roundNum);
  panel.innerHTML =
    '<div class="ri-state-panel-head">' +
      '<span class="ri-state-panel-kind"></span>' +
      '<span class="ri-state-panel-title"></span>' +
      '<span class="ri-state-panel-close" role="button" tabindex="0" title="' +
        _riEsc(t('ri.stateClose')) + '">' +
        (typeof Icon === 'function' ? Icon('x', 12) : '') + '</span>' +
    '</div>' +
    '<div class="ri-state-body"><div class="ri-empty">' +
      _riEsc(t('ri.loading')) + '</div></div>';
  panel.querySelector('.ri-state-panel-close').onclick = () => panel.remove();
  slot.insertAdjacentElement('afterend', panel);
  if (typeof panel.scrollIntoView === 'function')
    panel.scrollIntoView({ block: 'nearest' });
  await _riRenderToolPanel(panel, taskId, roundNum, turn);
}

/* Render the panel's ONE view: the message mirror captured right after this
 * round's tools ran, or the producing request when that round emitted no
 * mirror. Goes through the shared debug renderer, scoped to this round's
 * increment. The kind chip names the axis on screen, so a fallback render is
 * never mistaken for the mirror. */
async function _riRenderToolPanel(panel, taskId, roundNum, turn = '') {
  if (!panel.isConnected) return;  // closed while fetching
  const renderSeq = (panel._riRenderSeq || 0) + 1;
  panel._riRenderSeq = renderSeq;
  panel.dataset.riRound = String(roundNum);
  panel.dataset.riPanel = taskId + ':' + roundNum;
  const body = panel.querySelector('.ri-state-body');
  const titleEl = panel.querySelector('.ri-state-panel-title');
  const kindEl = panel.querySelector('.ri-state-panel-kind');
  let view;
  try {
    view = await _riFetchRoundView(taskId, roundNum, turn);
  } catch (error) {
    if (!panel.isConnected || panel._riRenderSeq !== renderSeq) return;
    panel.dataset.riKind = '';
    if (kindEl) kindEl.textContent = '';
    if (titleEl) titleEl.textContent = 'R' + roundNum;
    if (body) {
      body.innerHTML = '<div class="ri-empty">' +
        _riEsc(t('ri.loadFailed')) + '</div>';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'ri-retry';
      retry.textContent = t('ri.retry');
      retry.onclick = () => _riRenderToolPanel(panel, taskId, roundNum, turn);
      body.appendChild(retry);
    }
    return;
  }
  if (!panel.isConnected || panel._riRenderSeq !== renderSeq) return;
  if (!view) {
    panel.dataset.riKind = '';
    if (kindEl) kindEl.textContent = '';
    if (titleEl) titleEl.textContent = 'R' + roundNum;
    if (body) body.innerHTML = '<div class="ri-empty">' +
      _riEsc(t('ri.stateEmpty')) + '</div>';
    return;
  }
  const payload = view.payload;
  panel.dataset.riKind = view.kind;
  if (kindEl) {
    kindEl.textContent = t(view.kind === 'state'
      ? 'ri.tabState' : 'ri.tabRequest');
    kindEl.classList.toggle('ri-kind-fallback', view.kind !== 'state');
    kindEl.title = t(view.kind === 'state'
      ? 'ri.stateKindTip' : 'ri.requestKindTip');
  }
  /* Round-scoped: only what THIS round appended (see the section header).
   * When no exact increment exists, a state mirror degrades to its TAIL
   * slice (the tool call + its results) — never the full payload, whose
   * system prompt + history is precisely what this panel must not dump.
   * The request axis (the mirror-less fallback) keeps the full payload: it
   * has no post-tool tail to slice.
   * The server fold answers the increment from the payload itself (one
   * download); the client-side previous-round diff remains the fallback
   * for live SSE snapshots and payloads cached without the fold. */
  const scoped = (typeof payload.foldPrefix === 'number' &&
      payload.foldPrefix > 0)
    ? payload.messages.slice(payload.foldPrefix)
    : await _riRoundScopedMessages(taskId, roundNum, view.kind,
      payload.messages, turn);
  if (!panel.isConnected || panel._riRenderSeq !== renderSeq) return;
  const shown = (Array.isArray(scoped) && scoped.length)
    ? scoped
    : (view.kind === 'state'
      ? _riTailSlice(payload.messages) : payload.messages);
  if (titleEl) titleEl.textContent =
    (payload.label || ('R' + roundNum)) + ' · +' + shown.length + ' msgs';
  if (body) {
    renderDebugBlocksInto(body, shown, null);
    /* 2026-08-05 owner: NO tools-schema block here — it is identical on
     * every round and pure noise next to one round's increment (the drawer
     * detail keeps it: there it is part of the request payload). Small
     * increments auto-expand so the panel answers at a glance; large
     * payloads stay collapsed and render on click. */
    let total = 0;
    for (const m of shown)
      total += (typeof _debugMsgChars === 'function') ? _debugMsgChars(m) : 0;
    if (shown.length <= 6 && total <= 300 * 1024 &&
        typeof _debugOpenBlock === 'function') {
      body.querySelectorAll('.debug-msg-block').forEach(
        (b) => _debugOpenBlock(b));
    }
  }
}

/* ── Turn Trace (耗时分析) — the flame-graph view of ONE task ───────────
 * docs/TURN_TRACE_CONTRACT.md. The drawer renders ONLY what
 * /api/v4/tasks/<id>/trace folds SERVER-side spans from authoritative events
 * and returns the durable terminal snapshot when reconstructible rows expire.
 * The browser contributes only explicit received/painted/transport receipts;
 * it never rewrites server span clocks or lifecycle facts.
 * Layout: one row per span depth (turn / rounds & waits / llm & tools /
 * sub-segments) + a final gray row for the explicitly-unattributed gaps.
 */
function _trFmtMs(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return '…';
  ms = Math.max(0, Math.round(Number(ms)));
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : String(Math.round(s))) + 's';
  const m = Math.floor(s / 60);
  return m + 'm' + String(Math.round(s % 60)).padStart(2, '0') + 's';
}

const _TR_KIND_I18N = {
  turn: 'ri.trKindTurn', round: 'ri.trKindRound', llm: 'ri.trKindLlm',
  llm_ttft: 'ri.trKindLlmTtft', tool: 'ri.trKindTool',
  retry_wait: 'ri.trKindRetryWait', compaction: 'ri.trKindCompaction',
  approval_wait: 'ri.trKindApprovalWait', spawn_wait: 'ri.trKindSpawnWait',
  flow_step: 'ri.trKindFlowStep',
};

function _trKindLabel(kind) {
  const key = _TR_KIND_I18N[kind];
  const v = key ? t(key) : '';
  return (v && v !== key) ? v : String(kind || '');
}

function _trBarTitle(sp, elapsed) {
  const lines = [
    `${sp.name || sp.kind} · ${_trKindLabel(sp.kind)} · ${_trFmtMs(elapsed)}`,
  ];
  const a = sp.attrs || {};
  if (a.query) lines.push(a.query);
  if (a.model) lines.push(a.model);
  if (Array.isArray(a.attempts)) {
    for (const at of a.attempts) {
      lines.push(`${at.tag || at.model || 'attempt'}: ` +
        `${at.tokensIn}→${at.tokensOut} · ${_trFmtMs(at.streamElapsedMs)}`);
    }
  }
  if (a.attempt != null) lines.push(t('ri.trTipAttempt', { n: a.attempt }));
  if (a.verdict) lines.push(a.verdict);
  if (sp.budgetMs != null) {
    lines.push(sp.overBudget
      ? t('ri.trTipOverBudget', {
          budget: _trFmtMs(sp.budgetMs),
          over: _trFmtMs(Math.max(0, elapsed - sp.budgetMs)) })
      : t('ri.trTipBudget', { budget: _trFmtMs(sp.budgetMs) }));
  }
  if (sp.truncated) lines.push(t('ri.trTipTruncated'));
  if (sp.status === 'running') lines.push(t('ri.trTipRunning'));
  return lines.join('\n');
}

function _trStatusLabel(status) {
  const key = String((status && status.detailKey) || '');
  if (key) {
    const translated = t(key, (status && status.detailArgs) || {});
    if (translated && translated !== key) return translated;
  }
  return String((status && (status.detail || status.phase)) || '');
}

function _trObservationLabel(observation) {
  const o = observation || {};

  if ((o.kind === 'phase_painted' || o.kind === 'terminal_painted')
      && o.renderMs == null) return t('ri.tracePaintRestored');
  if (o.kind === 'phase_painted') {
    return t('ri.tracePhasePainted', {
      phase: o.phase || o.detailKey || '—',
      render: _trFmtMs(o.renderMs),
    });
  }
  if (o.kind === 'terminal_painted') {
    return t('ri.traceTerminalPainted', { render: _trFmtMs(o.renderMs) });
  }
  if (o.kind === 'transport_degraded') {
    return t('ri.traceTransportDegraded', {
      state: o.healthState || 'degraded', reason: o.reason || '—',
    });
  }
  if (o.kind === 'transport_recovered') {
    return t('ri.traceTransportRecovered', {
      duration: _trFmtMs(o.durationMs),
    });
  }
  return String(o.kind || '');
}

async function _riOpenTrace(taskId) {
  if (!taskId) return;
  _riSel.traceOpen = true;
  _riSel.traceTaskId = taskId;
  _riSel.detached = false;
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.traceTitle');
  if (content) {
    content.innerHTML = `<div class="ri-loading">` +
      `<span class="ri-spinner" aria-hidden="true"></span>` +
      `${_riEsc(t('ri.loading'))}</div>`;
    content._rawMessages = null;
  }
  const doc = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getTrace(taskId) : null;
  if (!_riOpen || !_riSel.traceOpen || _riSel.traceTaskId !== taskId) return;
  _riRenderTrace(doc);
  _riShowFeedBackBar();
}

function _riRenderTrace(doc) {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (!content) return;
  if (title) title.textContent = t('ri.traceTitle');
  if (!doc || !doc.eventsAvailable) {
    /* null = the fetch failed (retry via the entry); eventsAvailable:false
     * = the event log really is gone (30d retention). Say which one it is. */
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t(doc ? 'ri.expiredHint' : 'ri.loadFailed'))}</div>`;
    return;
  }
  const t0 = Number(doc.tStart) || 0;
  const total = Math.max(1, Number(doc.totalMs) || 1);
  const s = doc.summary || {};
  const observations = Array.isArray(doc.clientObservations)
    ? doc.clientObservations : [];
  const hasServerTimeline = doc.tStart != null && doc.totalMs != null &&
    doc.summary && typeof doc.summary === 'object';

  /* Summary chips — the disjoint bucket partition (sums to totalMs by
   * contract), each chip colored like its flame kind. */
  const chips = [];
  const pushChip = (cls, label, val, sub) => {
    if (val == null) return;
    chips.push(
      `<span class="tr-chip ${cls}">` +
      `<span class="tr-chip-dot" aria-hidden="true"></span>` +
      `${_riEsc(label)} <b>${_riEsc(_trFmtMs(val))}</b>` +
      (sub ? ` <i>${_riEsc(sub)}</i>` : '') + `</span>`);
  };
  pushChip('tr-c-total', t('ri.traceTotal'), doc.totalMs);
  pushChip('tr-c-llm', t('ri.trKindLlm'), s.llmMs,
    s.ttftMs ? t('ri.traceTtftSub', { v: _trFmtMs(s.ttftMs) }) : '');
  pushChip('tr-c-tool', t('ri.trKindTool'), s.toolMs);
  if (s.waitMs) pushChip('tr-c-wait', t('ri.trKindRetryWait'), s.waitMs);
  if (s.compactionMs) {
    pushChip('tr-c-compact', t('ri.trKindCompaction'), s.compactionMs);
  }
  if (s.approvalWaitMs) {
    pushChip('tr-c-approval', t('ri.trKindApprovalWait'), s.approvalWaitMs);
  }
  if (s.flowStepMs) {
    pushChip('tr-c-flowstep', t('ri.trKindFlowStep'), s.flowStepMs);
  }
  if (s.unattributedMs) {
    pushChip('tr-c-gap', t('ri.trKindGap'), s.unattributedMs);
  }

  let html = `<div class="tr-wrap"><div class="tr-chips">${chips.join('')}</div>`;

  /* Honesty notes (the Request Inspector disclosure precedent). */
  if (doc.running) {
    html += `<div class="tr-note">${_riEsc(t('ri.traceLive'))}</div>`;
  }
  if (doc.source === 'turn-snapshot') {
    html += `<div class="tr-note">${_riEsc(t('ri.traceDurable'))}</div>`;
  }
  if (doc.source === 'attempt-receipts' && !hasServerTimeline) {
    html += `<div class="tr-note tr-note-warn">` +
      `${_riEsc(t('ri.traceReceiptsOnly'))}</div>`;
  }
  if (doc.coverage === 'partial') {
    const key = doc.coverageReason === 'flow'
      ? 'ri.tracePartialFlow' : doc.coverageReason === 'no-round-markers'
        ? 'ri.tracePartialLegacy' : 'ri.tracePartialEvidence';
    html += `<div class="tr-note tr-note-warn">${_riEsc(
      t(key, { reason: doc.coverageReason || 'unknown' }),
    )}</div>`;
  }
  const over = Array.isArray(s.overBudget) ? s.overBudget : [];
  if (over.length) {
    const items = over.map((o) =>
      `${_riEsc(o.name)} ${_riEsc(_trFmtMs(o.elapsedMs))}` +
      ` / ${_riEsc(_trFmtMs(o.budgetMs))}`).join(' · ');
    html += `<div class="tr-note tr-note-over">` +
      `${_riEsc(t('ri.traceOverBudget', { n: over.length }))}: ${items}</div>`;
  }
  const compactedCount = ['droppedSpans', 'droppedGaps',
    'statusDroppedCount', 'clientObservationDroppedCount',
    'overBudgetDroppedCount']
    .reduce((sum, key) => sum + Math.max(0, Number(doc[key]) || 0), 0);
  if (doc.compacted || compactedCount) {
    html += `<div class="tr-note tr-note-warn">` +
      `${_riEsc(t('ri.traceCompacted', { n: compactedCount }))}</div>`;
  }
  const clientReportedDrops = observations.reduce((maximum, observation) =>
    Math.max(maximum, Math.max(0, Number(
      observation && observation.clientDroppedBefore) || 0)), 0);
  if (clientReportedDrops) {
    html += `<div class="tr-note tr-note-warn">` +
      `${_riEsc(t('ri.traceClientDropped', { n: clientReportedDrops }))}</div>`;
  }

  /* Flame rows require server clocks. A receipt-only durable document must
   * show its browser evidence without inventing a 0ms server timeline. */
  if (hasServerTimeline) {
    const spans = Array.isArray(doc.spans) ? doc.spans : [];
    const gaps = Array.isArray(doc.gaps) ? doc.gaps : [];
    let maxDepth = 0;
    for (const sp of spans) maxDepth = Math.max(maxDepth, sp.depth || 0);
    maxDepth = Math.min(maxDepth, 3);
    const rowLabelKeys = ['ri.trRowTurn', 'ri.trRowPhase', 'ri.trRowDetail',
      'ri.trRowSub'];
    const barHtml = (sp) => {
      const a = sp.tStart == null ? t0 : sp.tStart;
      const b = sp.tEnd == null ? (t0 + total) : sp.tEnd;
      const elapsed = Math.max(0, b - a);
      const left = Math.min(100, Math.max(0, (a - t0) / total * 100));
      const width = Math.min(100 - left,
        Math.max(0.15, (b - a) / total * 100));
      const cls = 'tr-bar tr-k-' + (sp.kind || 'unknown') +
        (sp.status === 'error' || sp.status === 'aborted' ? ' tr-err' : '') +
        (sp.overBudget ? ' tr-over' : '') +
        (sp.status === 'running' ? ' tr-live' : '') +
        (sp.truncated ? ' tr-trunc' : '');
      const inner = width > 7
        ? `<span class="tr-bar-txt">${_riEsc(sp.name || '')} ` +
          `${_riEsc(_trFmtMs(elapsed))}</span>` : '';
      return `<div class="${cls}" style="left:${left.toFixed(3)}%;` +
        `width:${width.toFixed(3)}%" title="${_riEsc(_trBarTitle(sp, elapsed))}">` +
        `${inner}</div>`;
    };
    html += '<div class="tr-flame">';
    for (let d = 0; d <= maxDepth; d++) {
      html += `<div class="tr-row"><span class="tr-row-label">` +
        `${_riEsc(t(rowLabelKeys[d]))}</span><div class="tr-track">`;
      for (const sp of spans) {
        if ((sp.depth || 0) === d) html += barHtml(sp);
      }
      html += '</div></div>';
    }
    /* The unattributed row — ALWAYS rendered when gaps exist, so a hole in
     * the accounting is visible, never silent (contract invariant #2). */
    if (gaps.length) {
      html += `<div class="tr-row"><span class="tr-row-label">` +
        `${_riEsc(t('ri.trKindGap'))}</span><div class="tr-track">`;
      for (const g of gaps) {
        const left = Math.min(100, Math.max(0, (g.tStart - t0) / total * 100));
        const width = Math.min(100 - left,
          Math.max(0.15, (g.tEnd - g.tStart) / total * 100));
        html += `<div class="tr-bar tr-k-gap" style="left:${left.toFixed(3)}%;` +
          `width:${width.toFixed(3)}%" title="` +
          `${_riEsc(t('ri.trKindGap') + ' · ' + _trFmtMs(g.tEnd - g.tStart))}">` +
          `</div>`;
      }
      html += '</div></div>';
    }
    /* Axis: 0 / mid / total. */
    html += `<div class="tr-axis"><span>0</span>` +
      `<span>${_riEsc(_trFmtMs(total / 2))}</span>` +
      `<span>${_riEsc(_trFmtMs(total))}</span></div>`;
    html += '</div>';
  }

  /* Exact user-visible phase prompts. Repeated heartbeats are coalesced by
   * the server, while count and lastObservedAt preserve the fact that the
   * same prompt remained active. */
  const statuses = Array.isArray(doc.statusHistory) ? doc.statusHistory : [];
  if (statuses.length) {
    const statusList = statuses.map((status) => {
      const end = status.tEnd == null
        ? (status.lastObservedAt == null ? t0 + total : status.lastObservedAt)
        : status.tEnd;
      const duration = Math.max(0, Number(end) - Number(status.tStart || end));
      const repeated = Number(status.count) > 1
        ? ` ${t('ri.traceStatusRepeated', { n: status.count })}` : '';
      return `${_trStatusLabel(status)} ${_trFmtMs(duration)}${repeated}`;
    });
    html += `<div class="tr-note"><b>${_riEsc(t('ri.traceStatusHistory'))}</b> ` +
      `${statusList.map(_riEsc).join(' → ')}</div>`;
    html += `<div class="tr-flame"><div class="tr-row">` +
      `<span class="tr-row-label">${_riEsc(t('ri.tracePromptRow'))}</span>` +
      `<div class="tr-track">`;
    for (const status of statuses) {
      const a = Number(status.tStart) || t0;
      const b = status.tEnd == null
        ? (Number(status.lastObservedAt) || t0 + total) : Number(status.tEnd);
      const elapsed = Math.max(0, b - a);
      const left = Math.min(100, Math.max(0, (a - t0) / total * 100));
      const width = Math.min(100 - left,
        Math.max(0.15, elapsed / total * 100));
      const cls = status.attention === 'stall'
        ? 'tr-bar tr-k-retry_wait tr-err'
        : status.attention === 'wait'
          ? 'tr-bar tr-k-retry_wait' : 'tr-bar tr-k-round';
      const label = _trStatusLabel(status);
      const inner = width > 7
        ? `<span class="tr-bar-txt">${_riEsc(label)}</span>` : '';
      html += `<div class="${cls}" style="left:${left.toFixed(3)}%;` +
        `width:${width.toFixed(3)}%" title="${_riEsc(label + ' · ' +
          _trFmtMs(elapsed))}">${inner}</div>`;
    }
    html += '</div></div></div>';
  }

  /* Browser evidence is intentionally content-free: it says when a known
   * phase/terminal/connection state was painted, never what the answer said. */
  if (observations.length) {
    const evidence = observations.map((observation) => {
      let label = _trObservationLabel(observation);
      if (observation.transportMs != null) {
        label += ` · ${t('ri.traceClientTransport', {
          duration: _trFmtMs(observation.transportMs),
        })}`;
      }
      return label;
    });
    html += `<div class="tr-note"><b>${_riEsc(t('ri.traceClientEvidence'))}</b> ` +
      `${evidence.map(_riEsc).join(' · ')}</div>`;
  }
  html += '</div>';
  content.innerHTML = html;
}
