/* ===== migrated source: core/request_inspector.js ===== */
/* Request Inspector adapter: task events are authoritative; debug-panel owns
 * the detail renderer. Opens by stable task/round identity. */

let _riOpen = false;
const _riSel = { taskId: null, fold: null };
const _riTaskRows = {};

let _riConvId = null;
let _riPollTimer = null;
/* Poll cadence: fast while any row is live, slow when idle. The drawer's
 * by-conv list is a point-in-time read — without a poll, a RUNNING row
 * (and its fold) freezes at whatever it was when the drawer opened. */
const _RI_POLL_LIVE_MS = 3000;
const _RI_POLL_IDLE_MS = 15000;

/* Accumulated level-1 rows (first page + user-paged earlier rows) and the
 * pagination cursor state. Silent polls MERGE the newest page into this
 * list instead of replacing it, so a user-expanded history never
 * collapses back on the next tick. */
let _riTaskList = [];
let _riHasMore = false;
let _riListConvId = null;
let _riLoadingEarlier = false;

/* Real-time drive: the poll alone made the drawer lag up to 3s (live) /
 * 15s (idle) behind reality, and a task that STARTED and FINISHED between
 * two ticks never showed its transitions at all. Subscribing to the
 * conversation's TurnStore flips that: any attempt/turn STATUS dispatch
 * (not content deltas — those fire per token) triggers a throttled silent
 * refresh. The poll stays as the cross-process backstop. */
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
    await _riRefreshTasks({ silent: true });
    const live = Object.keys(_riTaskRows).some(
      (id) => _riRowIsLive(_riTaskRows[id]));
    _riSchedulePoll(live ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
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
  _riSel.taskId = null;
  _riSel.fold = null;
  DebugShellState.visible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riResetDetail();
  const convId = DebugShellState.activeConversationId;
  _riLoadTasks(convId);
  _riBindStore(convId);
  _riSchedulePoll(_RI_POLL_IDLE_MS);
}

function closeRequestInspector() {
  _riOpen = false;
  _riStopPoll();

  _riUnbindStore();
  DebugShellState.visible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(String(taskId));
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riOpen) return;
  _riSel.taskId = null;
  _riSel.fold = null;
  _riResetDetail();
  _riLoadTasks(convId);
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
  if (document.hidden) { _riSchedulePoll(_RI_POLL_IDLE_MS); return; }
  await _riRefreshTasks({ silent: true });
  const live = Object.keys(_riTaskRows).some((id) => _riRowIsLive(_riTaskRows[id]));
  _riSchedulePoll(live ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
}

function _riRowIsLive(row) {
  return !!(row && (row.live ||
    ['running', 'queued', 'pending'].includes(
      String(row.status || '').toLowerCase())));
}

/* Refresh the task list; when the SELECTED task is still live, refresh its
 * level-2 fold as well — silently, so the detail pane the user is reading
 * (round payload / trace) is never reset by a background tick. */
async function _riRefreshTasks(opts) {
  const silent = !!(opts && opts.silent);
  await _riLoadTasks(_riConvId, { silent });
  if (_riSel.taskId && _riRowIsLive(_riTaskRows[_riSel.taskId])) {
    await _riSelectTask(_riSel.taskId, { silent: true });
  }
  if (!silent) _riSchedulePoll(_RI_POLL_IDLE_MS);
}

/* data-tofu-action targets (header refresh button / inline retry). */
function riRefreshTasks() {
  if (!_riOpen) return undefined;
  return _riRefreshTasks({ silent: false });
}

function riRetryTask() {
  if (!_riOpen) return undefined;
  if (_riSel.taskId) return _riSelectTask(_riSel.taskId);
  return _riRefreshTasks({ silent: false });
}

/* Turn badge label for a request row (P4): Flow node phases read through
 * i18n; swarm agents show their role; anything else falls back to the raw
 * tag so a future turn value still renders. */
function _riTurnLabel(row) {
  if (row.turn === 'swarm-agent') return row.agentRole || 'agent';
  const key = 'ri.turn' + String(row.turn || '').charAt(0).toUpperCase() +
    String(row.turn || '').slice(1);
  const v = t(key);
  return v === key ? String(row.turn) : v;
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

/* Map a task row to its reply bubble: live rows carry turnId from the
 * server; otherwise the conversation's attempt records know which task
 * produced which turn. The ordinal counts assistant replies only, so it
 * matches what the user reads as "reply #N". */
function _riTurnOrdinal(row) {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !_riConvId || !row) return null;
    let turnId = row.turnId || '';
    if (!turnId) {
      const state = read.state && read.state(_riConvId);
      const attempts = (state && state.attemptsById) || {};
      for (const att of Object.values(attempts)) {
        if (att && att.taskId === row.taskId && att.turnId) {
          turnId = att.turnId;
          break;
        }
      }
    }
    if (!turnId) return null;
    let n = 0;
    const ordered = (read.ordered && read.ordered(_riConvId)) || [];
    for (const turn of ordered) {
      if (!turn || !turn.turnId) continue;
      if (turn.actor === 'assistant') n += 1;
      if (turn.turnId === turnId) {
        return n > 0 ? { turnId, ordinal: n } : null;
      }
    }
    return null;
  } catch (_) { return null; }
}

function _riTurnChip(row) {
  const ref = _riTurnOrdinal(row);
  if (!ref) return null;
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'ri-turn-chip';
  chip.dataset.turnId = ref.turnId;
  chip.textContent = t('ri.turnChip', { n: ref.ordinal });
  chip.title = t('ri.turnChipTip');
  return chip;
}

function _riScrollToTurn(turnId) {
  const node = (typeof _findRenderedNativeTurnNode === 'function')
    ? _findRenderedNativeTurnNode(turnId) : null;
  if (node && node.scrollIntoView) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function _riEl(id) { return document.getElementById(id); }

function _riSetDetailActive(active) {
  const drawer = _riEl('riDrawer');
  if (drawer) drawer.classList.toggle('ri-detail-active', !!active);
}

function _riResetDetail() {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.detailTitle');
  if (content) content.innerHTML = `<div class="ri-main-empty">` +
    `${_riEsc(t('ri.selectRound'))}</div>`;
  _riSetDetailActive(false);
}

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

function _riRevealTechnical() {
  const details = _riEl('riRoundList') &&
    _riEl('riRoundList').querySelector('.ri-technical');
  if (details) details.open = true;
}

/* ── Level 1: task rows for the active conversation ── */
async function _riLoadTasks(convId, opts) {
  const silent = !!(opts && opts.silent);
  const list = _riEl('riTaskList');
  const rounds = _riEl('riRoundList');
  if (!list) return;
  _riConvId = convId || null;
  if (!silent && rounds) rounds.innerHTML = '';
  if (_riListConvId !== _riConvId || !silent) {
    /* A fresh explicit load (open / conv switch / manual refresh) resets
     * the accumulated pagination; silent polls merge into it. */
    _riTaskList = [];
    _riHasMore = false;
  }
  _riListConvId = _riConvId;
  if (!_riConvId) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  /* Silent polls keep the current DOM (and its scroll position) while the
   * fetch is in flight — no loading-flash every few seconds. */
  if (!silent) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  }
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.byConv(convId) : null;
  if (!_riOpen) return;  // drawer closed while fetching
  if (convId !== _riConvId) return;  // conversation switched mid-flight
  if (!data) {
    /* byConv resolves null on ANY failure (network error, 404, 500).
     * Never present that as "no tasks" — say it failed and offer retry. */
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button></div>`;
    return;
  }
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  _riHasMore = !!data.hasMore;
  _riMergeFirstPage(tasks);
  if (!_riTaskList.length) {
    /* readError = the STORAGE read failed: an empty list is only honest
     * when the read succeeded. Failure gets the retry affordance, never
     * the "no tasks recorded" empty state. */
    list.innerHTML = data.readError
      ? `<div class="ri-empty">${_riEsc(t('ri.loadFailed'))} ` +
        `<button type="button" class="ri-retry" ` +
        `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button></div>`
      : `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  if (!silent && rounds && !_riSel.taskId) {
    rounds.innerHTML = `<div class="ri-empty ri-select-task">` +
      `${_riEsc(t('ri.selectTask'))}</div>`;
  }
  _riRenderTaskList({ keepScroll: silent });
  if (data.readError) {
    /* Partial failure: live rows made it, the persisted read did not —
     * warn on top instead of silently presenting a truncated history. */
    const warn = document.createElement('div');
    warn.className = 'ri-warn-line';
    warn.innerHTML = `${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button>`;
    list.prepend(warn);
  }
}

/* Merge the newest page into the accumulated list. Persisted rows are
 * immutable and stay; a LIVE row missing from the newest page vanished
 * from the registry (finished → its persisted twin is in this page, or
 * evicted) and must not linger as forever-running. */
function _riMergeFirstPage(rows) {
  const fresh = {};
  for (const r of rows) fresh[r.taskId] = true;
  const kept = _riTaskList.filter((r) => fresh[r.taskId] === undefined && !r.live);
  _riTaskList = kept.concat(rows)
    .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  for (const key of Object.keys(_riTaskRows)) delete _riTaskRows[key];
  for (const r of _riTaskList) _riTaskRows[r.taskId] = r;
}

/* Group task rows by the reply they produced. The old flat newest-first
 * list mixed retries, swarm agents and unrelated replies into one strip
 * ("任务顺序看不懂"). Grouped: one header per reply (chip + question
 * preview + latest time), runs inside a group in CHRONOLOGICAL order
 * (run 1, run 2…), swarm children nested right after their parent. Rows
 * whose turn is not loaded (old pages, pre-TurnStore history) cluster in
 * one trailing "earlier" group; when NOTHING resolves the list renders
 * headerless, exactly like before. */
function _riGroupTaskRows(tasks) {
  const childrenByParent = {};
  const parents = [];
  const parentIds = {};
  for (const row of tasks) {
    if (row.parentTaskId) {
      (childrenByParent[row.parentTaskId] =
        childrenByParent[row.parentTaskId] || []).push(row);
    } else {
      parents.push(row);
      parentIds[row.taskId] = true;
    }
  }
  /* Orphaned swarm children (parent paged out) render standalone rather
   * than vanishing. */
  for (const pid of Object.keys(childrenByParent)) {
    if (parentIds[pid]) continue;
    for (const child of childrenByParent[pid]) parents.push(child);
    delete childrenByParent[pid];
  }
  const groups = [];
  const byKey = {};
  for (const row of parents) {
    const ref = _riTurnOrdinal(row);
    const key = ref ? ref.turnId : '__none__';
    let g = byKey[key];
    if (!g) {
      g = { ref, key, rows: [], latest: 0, preview: '' };
      byKey[key] = g;
      groups.push(g);
    }
    g.rows.push(row);
    if ((row.createdAt || 0) > g.latest) g.latest = row.createdAt || 0;
    if (!g.preview && row.userPreview) g.preview = row.userPreview;
  }
  groups.sort((a, b) => b.latest - a.latest);
  for (const g of groups) {
    /* A resolved group holds retries of ONE reply: chronological reads as
     * run 1, run 2…. The unresolved bucket mixes unrelated tasks — keep it
     * newest-first like the pre-grouping flat list. */
    g.rows.sort(g.ref
      ? (a, b) => (a.createdAt || 0) - (b.createdAt || 0)
      : (a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    g.childrenByParent = childrenByParent;
  }
  return groups;
}

function _riGroupHeaderEl(group) {
  const head = document.createElement('div');
  head.className = 'ri-group-head';
  const label = document.createElement('span');
  label.className = 'ri-group-preview';
  if (group.ref) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ri-turn-chip';
    chip.textContent = t('ri.turnChip', { n: group.ref.ordinal });
    chip.title = t('ri.turnChipTip');
    chip.onclick = (ev) => {
      ev.stopPropagation();
      _riScrollToTurn(group.ref.turnId);
    };
    head.appendChild(chip);
    label.textContent = group.preview || '';
    label.title = group.preview || '';
  } else {
    label.textContent = t('ri.groupOlder');
  }
  head.appendChild(label);
  const time = document.createElement('span');
  time.className = 'ri-group-time';
  time.textContent = _riRelTime(group.latest);
  time.title = _riAbsTime(group.latest);
  head.appendChild(time);
  return head;
}

function _riTaskRowEl(row, opts) {
  const runIndex = (opts && opts.runIndex) || 0;
  const showTurnChip = !!(opts && opts.showTurnChip);
  const el = document.createElement('div');
  el.className = 'ri-task' + (_riSel.taskId === row.taskId ? ' ri-sel' : '') +
    (row.isSwarmAgent ? ' ri-task-agent' : '') +
    (row.parentTaskId ? ' ri-task-child' : '');
  el.dataset.taskId = row.taskId;
  const status = _riStatusInfo(row);
  const expired = !row.hasEvents && !row.live;
  const agentBadge = row.isSwarmAgent
    ? `<span class="ri-agent-badge">${_riEsc(row.agentId || 'agent')}</span> · ` : '';
  const runBadge = runIndex
    ? `<span class="ri-run-badge">${_riEsc(t('ri.runIndex', { n: runIndex }))}</span>`
    : '';
  const preview = row.userPreview
    ? `<div class="ri-task-preview">${_riEsc(row.userPreview)}</div>` : '';
  el.innerHTML =
    `<div class="ri-task-top">` +
    `<span class="ri-task-status ri-tone-${_riEsc(status.tone)}">` +
    `<span class="ri-task-status-dot" aria-hidden="true"></span>` +
    `${_riEsc(status.label)}</span>` +
    runBadge +
    `<span class="ri-task-time" title="${_riEsc(_riAbsTime(row.createdAt))}">` +
    `${_riEsc(_riRelTime(row.createdAt))}</span>` +
    `</div>` +
    preview +
    `<div class="ri-task-sub">` +
    agentBadge +
    (expired
      ? `<span class="ri-expired">${_riEsc(t('ri.expired'))}</span>`
      : `<span>${_riEsc(t('ri.viewProcess'))}</span>`) +
    `<span class="ri-task-id">${_riEsc(t('ri.taskLabel', {
      id: String(row.taskId).slice(0, 8) }))}</span>` +
    `</div>`;
  /* Flat fallback (no turn resolved anywhere): keep the per-row anchor
   * chip. Grouped mode carries the anchor on the group header instead. */
  if (showTurnChip) {
    const chip = _riTurnChip(row);
    if (chip) {
      chip.onclick = (ev) => {
        ev.stopPropagation();
        _riScrollToTurn(chip.dataset.turnId);
      };
      el.querySelector('.ri-task-sub').prepend(chip);
    }
  }
  el.onclick = () => _riSelectTask(row.taskId);
  return el;
}

function _riRenderTaskList(opts) {
  const list = _riEl('riTaskList');
  if (!list) return;
  const keepScroll = !!(opts && opts.keepScroll);
  const scrollTop = keepScroll ? list.scrollTop : 0;
  list.innerHTML = '';
  const groups = _riGroupTaskRows(_riTaskList);
  const anyResolved = groups.some((g) => !!g.ref);
  for (const g of groups) {
    if (anyResolved) list.appendChild(_riGroupHeaderEl(g));
    /* run badges only make sense on true retry runs (same reply); the
     * unresolved bucket's rows are unrelated tasks, not numbered runs. */
    const multi = !!g.ref && g.rows.length > 1;
    g.rows.forEach((row, i) => {
      list.appendChild(_riTaskRowEl(row, {
        runIndex: multi ? i + 1 : 0,
        showTurnChip: !anyResolved,
      }));
      const children = (g.childrenByParent[row.taskId] || [])
        .slice()
        .sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
      for (const child of children) {
        list.appendChild(_riTaskRowEl(child, { showTurnChip: false }));
      }
    });
  }
  if (_riHasMore) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'ri-load-earlier';
    more.disabled = _riLoadingEarlier;
    more.textContent = t(_riLoadingEarlier ? 'ri.loading' : 'ri.loadEarlier');
    more.onclick = () => _riLoadEarlierTasks();
    list.appendChild(more);
  }
  if (keepScroll) list.scrollTop = scrollTop;
}

/* Page OLDER persisted rows in (cursor = oldest accumulated createdAt).
 * Live rows never participate: they are always first-page citizens. */
async function _riLoadEarlierTasks() {
  if (!_riOpen || !_riConvId || !_riHasMore || _riLoadingEarlier) return;
  const persisted = _riTaskList.filter((r) => !r.live);
  const cursor = persisted.length
    ? persisted[persisted.length - 1].createdAt || 0 : 0;
  if (!cursor) { _riHasMore = false; _riRenderTaskList(); return; }
  _riLoadingEarlier = true;
  _riRenderTaskList({ keepScroll: true });
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.byConv(_riConvId, { before: cursor }) : null;
  _riLoadingEarlier = false;
  if (!_riOpen) return;
  if (!data) { _riRenderTaskList({ keepScroll: true }); return; }
  _riHasMore = !!data.hasMore;
  const known = {};
  for (const r of _riTaskList) known[r.taskId] = true;
  const older = (Array.isArray(data.tasks) ? data.tasks : [])
    .filter((r) => r && r.taskId && !known[r.taskId]);
  if (older.length) {
    _riTaskList = _riTaskList.concat(older)
      .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    for (const r of older) _riTaskRows[r.taskId] = r;
  }
  _riRenderTaskList({ keepScroll: true });
}

/* ── Level 2: request rows (metadata) for the selected task ── */
async function _riSelectTask(taskId, opts) {
  const silent = !!(opts && opts.silent);
  _riSel.taskId = taskId;
  if (!silent) {
    _riSel.fold = null;
    _riSel.traceOpen = false;
    /* A silent background refresh must NOT reset the detail pane — the
     * user may be reading a round payload or the trace right now. */
    _riResetDetail();
  }
  /* Re-mark the selected task row. */
  const list = _riEl('riTaskList');
  if (list) {
    list.querySelectorAll('.ri-task').forEach((el) => {
      el.classList.toggle('ri-sel', el.dataset.taskId === taskId);
    });
  }
  const rounds = _riEl('riRoundList');
  if (!rounds) return;
  const scrollTop = silent ? rounds.scrollTop : 0;
  if (!silent) {
    rounds.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  }
  const fold = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequests(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId) return;  // stale response
  _riSel.fold = fold;
  rounds.innerHTML = '';
  if (!fold || !fold.eventsAvailable) {
    const note = document.createElement('div');
    note.className = 'ri-empty';
    if (!fold) {
      /* getRequests resolves null on ANY failure (network, 404, 500) —
       * a load error is NOT "records cleaned up"; offer a retry. */
      note.innerHTML = `<span>${_riEsc(t('ri.loadFailed'))}</span> ` +
        `<button type="button" class="ri-retry" ` +
        `data-tofu-action="riRetryTask()">${_riEsc(t('ri.retry'))}</button>`;
    } else if (fold.readError) {
      /* Storage read FAILED — the honest "expired" empty state would be a
       * lie; say so and offer a retry. */
      note.innerHTML = `<span>${_riEsc(t('ri.loadFailed'))}</span> ` +
        `<button type="button" class="ri-retry" ` +
        `data-tofu-action="riRetryTask()">${_riEsc(t('ri.retry'))}</button>`;
    } else if (_riRowIsLive(_riTaskRows[taskId])) {
      /* Live task before its first persisted snapshot: honest "starting" —
       * the poll and the TurnStore subscription fill rows in as they land. */
      note.textContent = t('ri.starting');
    } else {
      note.textContent = t('ri.expiredHint');
    }
    rounds.appendChild(note);
    return;
  }
  /* Turn Trace entry (耗时分析): the drawer's top-level plain-language
   * entry — the ONE click that answers "where did the time go" for this
   * task, folded server-side (docs/TURN_TRACE_CONTRACT.md). */
  const traceEntry = document.createElement('div');
  traceEntry.className = 'ri-trace-entry';
  traceEntry.setAttribute('role', 'button');
  traceEntry.innerHTML =
    `<span class="ri-trace-label">${_riEsc(t('ri.traceEntry'))}</span>` +
    `<span class="ri-trace-hint">${_riEsc(t('ri.traceEntryHint'))}</span>`;
  traceEntry.onclick = () => _riOpenTrace(taskId);
  rounds.appendChild(traceEntry);
  const technical = document.createElement('details');
  technical.className = 'ri-technical';
  /* The round list IS the drawer's payload — open by default; collapsing
   * is the opt-out for very long tasks, not the other way round. */
  technical.open = true;
  const technicalHead = document.createElement('summary');
  technicalHead.innerHTML = `<span>${_riEsc(t('ri.technicalDetails'))}</span>` +
    `<span class="ri-technical-count">${_riEsc(t('ri.roundTotal', {
      n: fold.requestCount || 0 }))}</span>`;
  technical.appendChild(technicalHead);
  const technicalBody = document.createElement('div');
  technicalBody.className = 'ri-technical-body';
  technical.appendChild(technicalBody);
  rounds.appendChild(technical);
  /* Coverage chip — honest disclosure (design §7). 'flow-untagged':
   * a Flow log whose planner/worker/critic rounds exist but
   * share numbers with no phase tag (ambiguous, NOT uncovered). */
  if (fold.coverage === 'partial') {
    const reasonKey = fold.coverageReason === 'flow-untagged'
      ? 'ri.coverageAmbiguous' : 'ri.coveragePartial';
    const chip = document.createElement('div');
    chip.className = 'ri-coverage-chip';
    chip.innerHTML =
      (typeof Icon === 'function' ? Icon('alertTriangle', 12) : '') +
      ` <span>${_riEsc(t(reasonKey))}</span>`;
    technicalBody.appendChild(chip);
  }
  const reqs = Array.isArray(fold.requests) ? fold.requests : [];
  if (!reqs.length) {
    const emp = document.createElement('div');
    emp.className = 'ri-empty';
    emp.textContent = t('ri.empty');
    technicalBody.appendChild(emp);
  }
  for (const row of reqs) {
    const el = document.createElement('div');
    el.className = 'ri-round';
    el.dataset.round = String(row.roundNum);
    el.dataset.turn = row.turn || '';
    const attempts = Array.isArray(row.attempts) ? row.attempts : [];
    const attemptBits = attempts.map((a) => {
      const el2 = (a.streamElapsedMs / 1000).toFixed(1) + 's';
      const fb = /FALLBACK|REACTIVE|DISCARDED/.test(a.tag || '') ? ' ⚠' : '';
      return `<span class="ri-attempt" title="${_riEsc(a.traceId || '')}">` +
        `${_riEsc(a.tag || a.model)} ${a.tokensIn}→${a.tokensOut} · ${el2}${fb}</span>`;
    }).join('');
    /* Tool-name chips — the SAME glanceability contract as the chat
     * timeline's turn blocks: which tools this round invoked, nothing
     * else. Counts (messages/tokens/schema) stay inside the round's
     * detail pane. */
    const toolNames = Array.isArray(row.toolNames) ? row.toolNames : [];
    const toolChips = toolNames.map((name) =>
      `<span class="ri-tool-chip">${_riEsc(name)}</span>`).join('');
    const turnBadge = row.turn
      ? `<span class="ri-turn-badge">${_riEsc(_riTurnLabel(row))}</span>` : '';
    el.innerHTML =
      `<div class="ri-round-top">` +
      turnBadge +
      `<span class="ri-round-n">${_riEsc(t('ri.roundNumber', {
        n: row.roundNum }))}</span>` +
      `</div>` +
      (toolChips ? `<div class="ri-round-tools">${toolChips}</div>` : '') +
      (attemptBits ? `<div class="ri-round-attempts">${attemptBits}</div>` : '');
    el.onclick = () => _riSelectRound(taskId, row.roundNum, el, row.turn || '');
    technicalBody.appendChild(el);
  }
  if (silent) rounds.scrollTop = scrollTop;
}

function _riShowWireProjection(projection) {
  if (!projection || !Array.isArray(projection.toolNames)) return;
  const content = _riEl('debugContent');
  if (!content) return;
  const details = document.createElement('details');
  details.className = 'ri-coverage-chip ri-wire-projection';
  const summary = document.createElement('summary');
  const bits = [
    t('ri.availableTools', { n: projection.toolCount || 0 }),
    t('ri.wireSchemaTokens', { n: projection.schemaTokens || 0 }),
  ];
  if (projection.backend) bits.push(projection.backend);
  if (projection.schemaBudgetTokens) {
    bits.push(t('ri.wireBudget', { n: projection.schemaBudgetTokens }));
  }
  if (Array.isArray(projection.budgetDroppedNames)
      && projection.budgetDroppedNames.length) {
    bits.push(t('ri.wireDropped', {
      n: projection.budgetDroppedNames.length,
    }));
  }
  summary.textContent = bits.join(' · ');
  const names = document.createElement('pre');
  names.textContent = projection.toolNames.join('\n');
  details.append(summary, names);
  content.prepend(details);
}

function _riRawArchiveText(dataBase64) {
  try {
    const binary = atob(String(dataBase64 || ''));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  } catch (_) {
    return t('ri.rawUnavailable');
  }
}

function _riShowRawArchives(taskId, archives) {
  if (!Array.isArray(archives) || !archives.length) return;
  const content = _riEl('debugContent');
  if (!content) return;
  const owner = document.createElement('details');
  owner.className = 'ri-coverage-chip ri-raw-archives';
  const summary = document.createElement('summary');
  summary.textContent = t('ri.rawArchive', { n: archives.length });
  owner.appendChild(summary);
  for (const archive of archives) {
    const item = document.createElement('section');
    item.className = 'ri-raw-archive';
    const meta = document.createElement('div');
    meta.className = 'ri-raw-archive-meta';
    const partial = archive.integrity === 'partial'
      ? ' · ' + t('ri.rawPartial', {
        reason: archive.truncationReason || 'partial' }) : '';
    meta.textContent = `#${archive.transportAttempt || 0} · ` +
      `${archive.storedBytes || 0}/${archive.byteCount || 0} B${partial}`;
    item.appendChild(meta);
    for (const part of ['request', 'response']) {
      const row = document.createElement('div');
      row.className = 'ri-raw-part';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ri-raw-load';
      button.textContent = t(part === 'request'
        ? 'ri.rawRequest' : 'ri.rawResponse');
      const body = document.createElement('pre');
      body.hidden = true;
      let offset = 0;
      button.onclick = async () => {
        button.disabled = true;
        const chunk = (typeof Api !== 'undefined' && Api.tasks)
          ? await Api.tasks.getRawArchiveChunk(
            taskId, archive.archiveId, part, offset) : null;
        button.disabled = false;
        if (!chunk) {
          body.hidden = false;
          body.textContent = t('ri.rawUnavailable');
          return;
        }
        // Replace, never append: browser residency stays one 256 KiB window
        // even when the durable archive is multi-MiB.
        body.hidden = false;
        body.textContent = _riRawArchiveText(chunk.dataBase64);
        offset = chunk.nextOffset || 0;
        button.textContent = chunk.hasMore
          ? t('ri.rawNext')
          : t(part === 'request' ? 'ri.rawRequest' : 'ri.rawResponse');
        button.disabled = !chunk.hasMore && offset > 0;
      };
      row.append(button, body);
      item.appendChild(row);
    }
    owner.appendChild(item);
  }
  content.prepend(owner);
}

/* ── Level 3: detail — REUSES showMessagesInDebug (no second renderer) ── */

/* Bounded payload cache; live SSE snapshots win over server reads. */
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
async function _riFetchPayload(taskId, roundNum, turn, kind) {
  turn = turn || '';
  kind = kind || 'request';
  const key = taskId + ':' + kind + ':' + turn + ':' + roundNum;
  /* Prefer live snapshots; Flow phases are keyed by turn|roundNum. */
  const _acc = DebugShellState.requests[taskId];
  if (kind === 'state') {
    const st = _acc && (_acc.states || []).filter((s) => s && s.messages &&
      String(s.roundNum) === String(roundNum)).pop();
    if (st) {
      const payload = { messages: st.messages, tools: st.tools,
        label: st.label, model: st.model, params: st.params, kind: 'state' };
      return _riCachePayload(key, payload);
    }
  } else {
    const _accKey = turn ? turn + '|' + roundNum : String(roundNum);
    const acc = _acc && _acc.rounds[_accKey];
    if (acc && acc.messages) {
      const payload = { messages: acc.messages, tools: acc.tools,
        label: acc.label, model: acc.model, params: acc.params,
        turn: acc.turn || turn };
      return _riCachePayload(key, payload);
    }
  }
  /* A live task keeps appending: a cached server read from an earlier
   * poll would freeze the round mid-flight. (Live SSE snapshots above
   * still win — they ARE the newest state.) */
  if (!_riRowIsLive(_riTaskRows[taskId]) && _riPayloadCache[key]) {
    return _riPayloadCache[key];
  }
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequestPayload(taskId, roundNum, turn || undefined,
        kind === 'state' ? 'state' : undefined) : null;
  if (data && data.messages) {
    if (!_riRowIsLive(_riTaskRows[taskId])) {
      _riCachePayload(key, data);
    }
    return data;
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
async function _riRoundScopedMessages(taskId, roundNum, kind, messages) {
  const num = parseInt(roundNum, 10);
  if (!Number.isFinite(num) || num <= 1 || !Array.isArray(messages)) return null;
  const prev = await _riFetchPayload(taskId, num - 1, '',
    kind === 'state' ? 'state' : 'request');
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

async function _riSelectRound(taskId, roundNum, el, turn) {
  turn = turn || '';
  _riSel.traceOpen = false;
  _riRevealTechnical();
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.toggle('ri-sel', r === el));
    rounds.querySelectorAll('.ri-trace-entry').forEach((r) =>
      r.classList.remove('ri-sel'));
  }
  const payload = await _riFetchPayload(taskId, roundNum, turn);
  // Stale: the user moved on — including into the Turn Trace view, which a
  // late round-payload resolve must not clobber (the reverse race is
  // already guarded in _riOpenTrace).
  if (!_riOpen || _riSel.taskId !== taskId || _riSel.traceOpen) return;
  if (!payload || !payload.messages) return;
  /* Fold against the previous round in the same Flow phase. */
  let opts = { resetScroll: true };
  const num = parseInt(roundNum, 10);
  if (Number.isFinite(num) && num > 1) {
    const prev = await _riFetchPayload(taskId, num - 1, turn);
    if (!_riOpen || _riSel.taskId !== taskId || _riSel.traceOpen) return;  // stale
    if (prev && prev.messages) {
      const k = _riSharedPrefix(prev.messages, payload.messages);
      if (k > 0) { opts.foldPrefix = k; opts.diffBase = 'R' + (num - 1); }
    }
  }
  if (typeof showMessagesInDebug === 'function') {
    _riSetDetailActive(true);
    showMessagesInDebug(payload.messages, payload.label || '', false,
      DebugShellState.activeConversationId,
      payload.tools || undefined, false, undefined,
      Object.assign(opts, { contextManifest: payload.contextManifest || [] }));
    _riShowWireProjection(payload.wireProjection);
    _riShowRawArchives(taskId, payload.rawArchives);
  }
}

/* Open the request that produced a tool call. */
async function openRequestInspectorForToolRound(taskId, roundNum) {
  if (!taskId || roundNum == null) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(taskId);
  _riRevealTechnical();
  const fold = _riSel.fold;
  const reqs = (fold && Array.isArray(fold.requests)) ? fold.requests : [];
  /* Same-numbered Flow phases prefer the worker request. */
  const exact = reqs.filter((r) => String(r.roundNum) === String(roundNum));
  const pick = exact.find((r) => r.turn === 'working') || exact[0] ||
    reqs[reqs.length - 1];
  if (!pick) return;
  const targetTurn = pick.turn || '';
  const el = document.querySelector(
    '#riRoundList .ri-round[data-round="' + String(pick.roundNum) +
    '"][data-turn="' + targetTurn + '"]') ||
    document.querySelector(
      '#riRoundList .ri-round[data-round="' + String(pick.roundNum) + '"]');
  if (el) {
    if (typeof el.scrollIntoView === 'function')
      el.scrollIntoView({ block: 'nearest' });
    el.classList.add('ri-flash');
    setTimeout(() => el.classList.remove('ri-flash'), 1600);
  }
  await _riSelectRound(taskId, pick.roundNum, el, targetTurn);
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
  if (!taskId || roundNum == null) return;
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
     * would have: the result state, or the request when no mirror exists. */
    if (!_riOpen) openRequestInspector();
    await _riSelectTask(taskId);
    const view = await _riFetchRoundView(taskId, roundNum);
    if (view && view.payload && view.payload.messages &&
        typeof showMessagesInDebug === 'function') {
      _riSetDetailActive(true);
      showMessagesInDebug(view.payload.messages, view.payload.label || '', false,
        DebugShellState.activeConversationId,
        view.payload.tools || undefined, false, undefined,
        { resetScroll: true,
          contextManifest: view.payload.contextManifest || [] });
      return;
    }
    await openRequestInspectorForToolRound(taskId, roundNum);
    return;
  }
  /* Re-clicking the entry for the round already open closes it (toggle). */
  const existing = document.querySelector('.ri-state-panel');
  if (existing && existing.dataset.riRound === String(roundNum) &&
      existing.dataset.riTask === String(taskId)) {
    existing.remove();
    return;
  }
  _riMountToolPanel(slot, taskId, roundNum);
}

/* Resolve the ONE view a tool row's debug entry shows, and say which axis it
 * came from. The post-tool mirror is preferred because it is a superset of
 * the request; the request is the fallback for rounds that never emitted a
 * mirror (swarm sub-agents, an aborted round, an expired state row).
 * Returns {kind, payload} or null when neither axis has anything. */
async function _riFetchRoundView(taskId, roundNum) {
  const state = await _riFetchPayload(taskId, roundNum, '', 'state');
  if (state && state.messages && state.messages.length)
    return { kind: 'state', payload: state };
  const req = await _riFetchPayload(taskId, roundNum, '', 'request');
  if (req && req.messages && req.messages.length)
    return { kind: 'request', payload: req };
  return null;
}

/* Mount the (single-instance) panel after a tool slot. Transient by
 * design — a chat re-render may drop it; re-click reopens. */
async function _riMountToolPanel(slot, taskId, roundNum) {
  document.querySelectorAll('.ri-state-panel').forEach((p) => p.remove());
  const panel = document.createElement('div');
  panel.className = 'ri-state-panel';
  panel.dataset.riTask = String(taskId);
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
  await _riRenderToolPanel(panel, taskId, roundNum);
}

/* Render the panel's ONE view: the message mirror captured right after this
 * round's tools ran, or the producing request when that round emitted no
 * mirror. Goes through the shared debug renderer, scoped to this round's
 * increment. The kind chip names the axis on screen, so a fallback render is
 * never mistaken for the mirror. */
async function _riRenderToolPanel(panel, taskId, roundNum) {
  if (!panel.isConnected) return;  // closed while fetching
  panel.dataset.riRound = String(roundNum);
  panel.dataset.riPanel = taskId + ':' + roundNum;
  const body = panel.querySelector('.ri-state-body');
  const titleEl = panel.querySelector('.ri-state-panel-title');
  const kindEl = panel.querySelector('.ri-state-panel-kind');
  const view = await _riFetchRoundView(taskId, roundNum);
  if (!panel.isConnected) return;
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
   * has no post-tool tail to slice. */
  const scoped = await _riRoundScopedMessages(taskId, roundNum, view.kind,
    payload.messages);
  if (!panel.isConnected) return;
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
 * /api/v1/tasks/<id>/trace folds SERVER-side spans from authoritative events
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
  _riSel.traceOpen = true;
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.remove('ri-sel'));
    rounds.querySelectorAll('.ri-trace-entry').forEach((r) =>
      r.classList.add('ri-sel'));
  }
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.traceTitle');
  if (content) {
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t('ri.loading'))}</div>`;
  }
  _riSetDetailActive(true);
  const doc = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getTrace(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId || !_riSel.traceOpen) return;
  _riRenderTrace(doc);
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
      ? 'ri.tracePartialFlow' : 'ri.tracePartialLegacy';
    html += `<div class="tr-note tr-note-warn">${_riEsc(t(key))}</div>`;
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
