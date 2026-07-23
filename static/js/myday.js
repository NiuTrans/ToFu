/* ═══════════════════════════════════════════
   myday.js — My Day — Daily Task Report
   ═══════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════
   ★ My Day — daily task report with async progress & categories
   Clean task list as hero, mini calendar sidebar for date nav.
   Background generation keeps running when you switch dates.
   ═══════════════════════════════════════════════════════════ */

const _myday = {
  year: new Date().getFullYear(),
  month: new Date().getMonth(),
  selectedDay: new Date().getDate(),
  selectedDateStr: '',
  cache: {},           // { 'YYYY-MM-DD': { tasks, _full } }
  loading: false,
  _pollTimers: {},     // { 'YYYY-MM-DD': intervalId } — active poll loops
  _collapsedCats: {},  // { 'category-name': true } — collapsed state persists during session
  _convDays: {},       // { dayNum: convCount } — server-side conversation counts per day
  _costDays: {},       // { dayNum: {cost, conversations} } — server-side cost data per day
};

/* ═══════ Persistent per-day report cache (IndexedDB) ═══════
   The server is the SOURCE OF TRUTH; this is a local READ cache only, so a
   reopen / day-click paints INSTANTLY from cache and then revalidates against
   the server in the background.  Mirrors the idb-cache.js pattern (read-through
   + write-after-server-confirms).  Falls back gracefully when IDB is
   unavailable — callers just get null and hit the network as before. */
const _mydayIDB = (function () {
  const DB_NAME = 'tofu_myday_cache';
  const STORE = 'reports';
  const STORE_MONTHS = 'months';   // month overview (cost_days/conv_days) cache
  const VER = 2;
  let _dbp = null;
  function _open() {
    if (_dbp) return _dbp;
    _dbp = new Promise((resolve) => {
      try {
        if (typeof indexedDB === 'undefined') { resolve(null); return; }
        const rq = indexedDB.open(DB_NAME, VER);
        rq.onupgradeneeded = (e) => {
          const db = e.target.result;
          if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'date' });
          if (!db.objectStoreNames.contains(STORE_MONTHS)) db.createObjectStore(STORE_MONTHS, { keyPath: 'month' });
        };
        rq.onsuccess = (e) => resolve(e.target.result);
        rq.onerror = () => { console.warn('[MyDay] report cache open failed'); resolve(null); };
      } catch (e) { console.warn('[MyDay] report cache init error:', e && e.message); resolve(null); }
    });
    return _dbp;
  }
  function get(date) {
    return _open().then((db) => {
      if (!db) return null;
      return new Promise((resolve) => {
        try {
          const rq = db.transaction(STORE, 'readonly').objectStore(STORE).get(date);
          rq.onsuccess = () => resolve(rq.result ? rq.result.report : null);
          rq.onerror = () => resolve(null);
        } catch (e) { resolve(null); }
      });
    });
  }
  function put(date, report) {
    return _open().then((db) => {
      if (!db) return;
      return new Promise((resolve) => {
        try {
          const tx = db.transaction(STORE, 'readwrite');
          tx.objectStore(STORE).put({ date: date, report: report, cachedAt: Date.now() });
          tx.oncomplete = () => resolve();
          tx.onerror = () => resolve();
        } catch (e) { resolve(); }
      });
    });
  }
  // Month overview cache (cost_days + conv_days), keyed by 'YYYY-MM'. Lets the
  // calendar paint historical ¥ balances INSTANTLY on reopen/reload before the
  // (multi-second, cache-cold) calendar fetch returns; the fetch then reconciles.
  function getMonth(monthKey) {
    return _open().then((db) => {
      if (!db) return null;
      return new Promise((resolve) => {
        try {
          const rq = db.transaction(STORE_MONTHS, 'readonly').objectStore(STORE_MONTHS).get(monthKey);
          rq.onsuccess = () => resolve(rq.result ? rq.result.data : null);
          rq.onerror = () => resolve(null);
        } catch (e) { resolve(null); }
      });
    });
  }
  function putMonth(monthKey, data) {
    return _open().then((db) => {
      if (!db) return;
      return new Promise((resolve) => {
        try {
          const tx = db.transaction(STORE_MONTHS, 'readwrite');
          tx.objectStore(STORE_MONTHS).put({ month: monthKey, data: data, cachedAt: Date.now() });
          tx.oncomplete = () => resolve();
          tx.onerror = () => resolve();
        } catch (e) { resolve(); }
      });
    });
  }
  return { get: get, put: put, getMonth: getMonth, putMonth: putMonth };
})();

/* Set both the in-memory + persistent cache from an authoritative server
   report.  Every place that receives a fresh full report routes through here
   so the IDB copy stays in lockstep with what the server returned. */
function _mydaySetCache(dateStr, report) {
  if (!report) return;
  report._full = true;
  _myday.cache[dateStr] = report;
  try { _mydayIDB.put(dateStr, report); } catch (e) { /* cache is optional */ }
  // The pet's day-awareness rides on THIS single cache choke: every place a
  // fresh full report lands routes through here, so both opening My Day and the
  // boot digest fetch emit `tofu:day` from one path (no second source).
  if (dateStr === _mydayTodayStr()) _mydayEmitDay(report);
}

/* ═══════ Pet day-awareness: derive the digest + emit `tofu:day` ═══════
   The BACKEND report is the single source of truth. We do NOT recompute day
   logic — we read the counts the report already carries and hand the pet a
   compact digest {todos:{done,total}, streams:{done,blocked,total}, convCount}.
   The pet only maps that → expression/mood. A missing/empty report is a silent
   no-op, so a session that never opens My Day simply never fires it (until the
   boot fetch below succeeds).                                                */
function _mydayTodayStr() {
  const n = new Date();
  return _mydayDateStr(n.getFullYear(), n.getMonth(), n.getDate());
}
function _mydayBuildDigest(report) {
  if (!report || typeof report !== 'object') return null;
  const streams = Array.isArray(report.streams) ? report.streams : [];
  const todos = Array.isArray(report.today_todos) ? report.today_todos : [];
  return {
    streams: {
      total: streams.length,
      done: streams.filter(s => s && s.status === 'done').length,
      blocked: streams.filter(s => s && s.status === 'blocked').length,
    },
    todos: {
      total: todos.length,
      done: todos.filter(x => x && x.done).length,
    },
    convCount: ((report.stats || {}).totalConversations) || 0,
  };
}
function _mydayEmitDay(report) {
  try {
    const digest = _mydayBuildDigest(report);
    if (!digest) return;
    document.dispatchEvent(new CustomEvent('tofu:day', { detail: digest }));
  } catch (e) { /* the pet is decorative — a digest failure must never break it */ }
}

/* Fetch-once-on-boot of TODAY's digest so the pet is day-aware even when the
   user never opens My Day. Reuses the instant-paint IDB cache first (no cold
   blocking network on load) and the SAME Api.daily.status + _mydaySetCache path
   My Day uses — no second fetcher. Deduped: if My Day already loaded today
   (in-memory _full report) we emit from that and skip the network; the reminder
   already runs a lightweight conv-count probe, so this is the only added call
   and it degrades to a silent no-op when the report isn't ready. */
async function _mydayBootDayDigest() {
  if (_myday._bootDigestDone) return;
  _myday._bootDigestDone = true;
  const today = _mydayTodayStr();
  try {
    // 1) My Day already has today's full report cached in memory → emit, no fetch.
    const mem = _myday.cache[today];
    if (mem && mem._full) { _mydayEmitDay(mem); return; }
    // 2) Instant paint from the persistent IDB cache (survives reload) if present.
    let hadCache = false;
    try {
      const cached = await _mydayIDB.get(today);
      if (cached) { _mydayEmitDay(cached); hadCache = true; }
    } catch (e) { /* cache optional */ }
    // 3) Background revalidate through the shared status path. Only a ready
    //    report emits (via _mydaySetCache); idle/generating/error → no-op.
    const data = await Api.daily.status(today);
    if (data && data.status === 'done' && data.report) {
      _mydaySetCache(today, data.report);   // emits tofu:day through the choke
    }
    // idle / generating / error / not-ready → leave the pet on its cached or
    // generic behavior; never force a blank digest.
    void hadCache;
  } catch (e) {
    console.warn('[MyDay] boot day-digest failed (pet stays generic):', e && e.message);
  }
}

function openDailyReport() {
  const modal = document.getElementById('dailyReportModal');
  modal.classList.add('open');
  const now = new Date();
  _myday.year = now.getFullYear();
  _myday.month = now.getMonth();
  _myday.selectedDay = now.getDate();
  _mydayRenderCalendar();
  _mydaySelectDay(_myday.selectedDay);
}
function closeDailyReport() {
  document.getElementById('dailyReportModal').classList.remove('open');
  // DON'T stop polls — let background generation continue
}

/* Status: 3-state for streams */
const _STATUS_ICONS = {
  done:        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill="#34d399" opacity="0.18" stroke="#34d399" stroke-width="1.5"/><path d="M8 12l3 3 5-5" stroke="#34d399" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  in_progress: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill="#fbbf24" opacity="0.15" stroke="#fbbf24" stroke-width="1.5"/><path d="M12 7v5l3 3" stroke="#fbbf24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  blocked:     '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill="#f87171" opacity="0.15" stroke="#f87171" stroke-width="1.5"/><line x1="8" y1="8" x2="16" y2="16" stroke="#f87171" stroke-width="2" stroke-linecap="round"/><line x1="16" y1="8" x2="8" y2="16" stroke="#f87171" stroke-width="2" stroke-linecap="round"/></svg>',
  incomplete:  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.5" opacity="0.25"/></svg>',
};
function _getStatusLabels() {
  return {
    done: t('myday.statusDone'),
    in_progress: t('myday.statusInProgress'),
    blocked: t('myday.statusBlocked'),
    incomplete: t('myday.statusIncomplete'),
  };
}
/* The status cycle order (in_progress → done → blocked → …) is owned by the
 * backend (routes/api_v1/daily_report.py::_STATUS_CYCLE). We POST action:'cycle'
 * and render whatever status the server returns — no client-side next-status math. */

/* ═══════ Date helpers ═══════ */
function _mydayDateStr(y, m, d) {
  return `${y}-${String(m + 1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
function _mydayWeekday(dateStr) {
  const days = t('myday.weekdays').split(',');
  const prefix = t('myday.weekdayPrefix');
  return prefix + days[new Date(dateStr + 'T00:00:00').getDay()];
}
function _mydayFormatDate(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  const now = new Date();
  const todayStr = _mydayDateStr(now.getFullYear(), now.getMonth(), now.getDate());
  const yest = new Date(now); yest.setDate(yest.getDate() - 1);
  const yestStr = _mydayDateStr(yest.getFullYear(), yest.getMonth(), yest.getDate());
  if (dateStr === todayStr) return t('myday.today');
  if (dateStr === yestStr) return t('myday.yesterday');
  return t('myday.monthDay', { m: d.getMonth() + 1, d: d.getDate() });
}

/* ═══════ Mini calendar sidebar ═══════ */
function _mydayCalPrev() {
  _myday.month--;
  if (_myday.month < 0) { _myday.month = 11; _myday.year--; }
  _mydayRenderCalendar();
}
function _mydayCalNext() {
  _myday.month++;
  if (_myday.month > 11) { _myday.month = 0; _myday.year++; }
  _mydayRenderCalendar();
}

function _mydayRenderCalendar() {
  const { year, month, selectedDay } = _myday;
  // mNames not needed — use i18n yearMonth
  const now = new Date();
  const isCurMonth = now.getFullYear() === year && now.getMonth() === month;
  const todayD = isCurMonth ? now.getDate() : -1;

  // Header
  const hdr = document.getElementById('mydayCalHeader');
  if (hdr) hdr.innerHTML =
    `<button class="mcal-nav" onclick="_mydayCalPrev()">‹</button>
     <span class="mcal-title">${t('myday.yearMonth', { y: year, m: month + 1 })}</span>
     <button class="mcal-nav" onclick="_mydayCalNext()">›</button>`;

  // Conversation counts per day — use server data (client-side conversations
  // are mostly _needsLoad shells with empty messages, so counting them gives 0).
  const dayCounts = _myday._convDays || {};

  // Cost data — use server-side calculated costs (accurate, covers all DB data)
  const costDaily = _myday._costDays || {};

  // Cached report data
  const cachedInfo = {};
  for (const [key, report] of Object.entries(_myday.cache)) {
    const [ry, rm, rd] = key.split('-').map(Number);
    const items = report.streams || report.tasks;
    if (ry === year && rm === month + 1 && items) {
      const done = items.filter(t => t.status === 'done').length;
      const open = items.length - done;
      cachedInfo[rd] = { done, open, total: items.length };
    }
  }

  // Build grid
  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  let html = '';
  const wk = t('myday.calWeek').split(',');
  for (const w of wk) html += `<span class="mcal-wk">${w}</span>`;

  // Padding
  for (let i = 0; i < firstDow; i++) html += '<span class="mcal-d empty"></span>';

  for (let d = 1; d <= daysInMonth; d++) {
    const isToday = d === todayD;
    const isSel = d === selectedDay && isCurMonth;
    const isFuture = isCurMonth && d > todayD;
    const hasConvs = !!dayCounts[d];
    const cost = costDaily[d] ? costDaily[d].cost : 0;
    const info = cachedInfo[d];

    let cls = 'mcal-d';
    if (isToday) cls += ' today';
    if (isSel) cls += ' sel';
    if (isFuture) cls += ' future';
    if (!hasConvs && !isFuture) cls += ' quiet';

    // Status dot: green = all done, orange = has incomplete
    let dotHtml = '';
    if (info) {
      const dotCls = info.open === 0 ? 'done' : 'open';
      dotHtml = `<span class="mcal-dot ${dotCls}"></span>`;
    } else if (hasConvs) {
      dotHtml = `<span class="mcal-dot unknown"></span>`;
    }

    // Spinning dot if generating for this date
    const genDateStr = _mydayDateStr(year, month, d);
    if (_myday._pollTimers[genDateStr]) {
      dotHtml = `<span class="mcal-dot generating"></span>`;
    }

    html += `<span class="${cls}" onclick="_mydaySelectDay(${d})" title="${cost > 0 ? '¥' + cost.toFixed(2) : ''}">
      ${d}${dotHtml}</span>`;
  }

  const grid = document.getElementById('mydayCalGrid');
  if (grid) grid.innerHTML = html;

  // Fetch month overview from API (for task status dots)
  _mydayFetchMonthOverview(year, month);
}

/* Fetch month overview from API for calendar dots (with 15s client-side TTL).
   Instant-paint: before the (possibly multi-second, cache-cold) network call,
   paint the calendar's ¥ balances + conv counts from the persistent IDB month
   cache so historical days show up immediately; the fetch then reconciles and
   rewrites the cache. Mirrors the per-day report instant-paint (_mydaySelectDay). */
async function _mydayFetchMonthOverview(year, month) {
  const cacheKey = `${year}-${month}`;
  const monthKey = `${year}-${String(month + 1).padStart(2, '0')}`;
  const now = Date.now();
  if (_myday._overviewCache && _myday._overviewCache.key === cacheKey &&
      now - _myday._overviewCache.ts < 15000) {
    return; // skip — data is fresh enough
  }

  // ── INSTANT PAINT from persistent month cache ──
  // Only when the in-memory cost/conv maps aren't already populated for this
  // month (a fresh reopen / reload). The server fetch below still runs and
  // reconciles, so a stale cache is only ever shown for a few hundred ms.
  try {
    const cachedMonth = await _mydayIDB.getMonth(monthKey);
    // Guard: user may have navigated to a different month while IDB read was in flight.
    if (cachedMonth && _myday.year === year && _myday.month === month) {
      if (cachedMonth.cost_days) _myday._costDays = cachedMonth.cost_days;
      if (cachedMonth.conv_days) _myday._convDays = cachedMonth.conv_days;
      _mydayRenderCalendar();
      if (_myday.selectedDateStr) _mydayRenderSidebarInfo(_myday.selectedDateStr);
    }
  } catch (e) { console.warn('[MyDay] month cache read failed:', e && e.message); }

  try {
    const data = await Api.daily.calendar(year, month + 1);
    if (!data) {
      console.warn('[MyDay] Calendar overview fetch failed');
      return;
    }
    if (!data.days) return;
    let changed = false;
    for (const [dateStr, info] of Object.entries(data.days)) {
      if (!_myday.cache[dateStr]) {
        const tasks = [];
        for (let i = 0; i < (info.done || 0); i++) tasks.push({ status: 'done' });
        for (let i = 0; i < (info.incomplete || 0); i++) tasks.push({ status: 'incomplete' });
        _myday.cache[dateStr] = { tasks };
        changed = true;
      }
    }
    // Store server-side conversation counts (reliable — not affected by _needsLoad shells)
    if (data.conv_days) {
      _myday._convDays = data.conv_days;
      changed = true;
    }
    // Store server-side cost data (accurate — covers all DB messages)
    if (data.cost_days) {
      _myday._costDays = data.cost_days;
      changed = true;
    }
    // Persist the authoritative month overview so the next reopen/reload paints
    // historical balances instantly (server stays the source of truth).
    try { _mydayIDB.putMonth(monthKey, { cost_days: data.cost_days || {}, conv_days: data.conv_days || {} }); } catch (e) { /* cache optional */ }
    _myday._overviewCache = { key: `${year}-${month}`, ts: Date.now() };
    if (changed) {
      _mydayRenderCalendar();
      // Re-render sidebar cost for currently selected day — the initial
      // _mydaySelectDay call runs before this async fetch returns, so cost
      // data wasn't available yet.  Without this, the sidebar stays empty
      // until the user manually clicks a day.
      if (_myday.selectedDateStr) _mydayRenderSidebarInfo(_myday.selectedDateStr);
    }
  } catch (e) {
    console.warn('[MyDay] Calendar overview error:', e);
  }
}

/* ═══════ Day selection ═══════ */
async function _mydaySelectDay(day) {
  _myday.selectedDay = day;
  const dateStr = _mydayDateStr(_myday.year, _myday.month, day);
  _myday.selectedDateStr = dateStr;

  // Update calendar selection
  document.querySelectorAll('.mcal-d.sel').forEach(el => el.classList.remove('sel'));
  const grid = document.getElementById('mydayCalGrid');
  if (grid) {
    grid.querySelectorAll('.mcal-d:not(.empty)').forEach(el => {
      if (parseInt(el.textContent) === day) el.classList.add('sel');
    });
  }

  // Update header
  _mydayUpdateHeader(dateStr);

  // Update sidebar cost
  _mydayRenderSidebarInfo(dateStr);

  // Check if generation is running for this date — show progress
  if (_myday._pollTimers[dateStr]) {
    // Poll is already running; just show current progress
    _mydayShowProgressUI(dateStr, null);
    return;
  }

  // ── INSTANT PAINT ──
  // 1) In-memory full report → render immediately, still revalidate below.
  // 2) Else IndexedDB (survives page reload) → paint from cache, then reconcile.
  // 3) Else skeleton. The first paint NEVER blocks on the network.
  let painted = false;
  if (_myday.cache[dateStr] && _myday.cache[dateStr]._full) {
    _mydayRenderTasks(_myday.cache[dateStr]);
    painted = true;
  } else {
    try {
      const cachedReport = await _mydayIDB.get(dateStr);
      // Guard: the user may have clicked another day while IDB read was in flight.
      if (cachedReport && _myday.selectedDateStr === dateStr) {
        cachedReport._full = true;
        _myday.cache[dateStr] = cachedReport;
        _mydayRenderTasks(cachedReport);
        painted = true;
      }
    } catch (e) { console.warn('[MyDay] report cache read failed:', e); }
  }
  if (!painted && _myday.selectedDateStr === dateStr) _mydayShowSkeleton();

  // ── BACKGROUND REVALIDATE ──
  // Reconcile the (possibly cached) view with the server's authoritative state.
  try {
    const data = await Api.daily.status(dateStr);
    if (_myday.selectedDateStr !== dateStr) return; // user navigated away
    if (data) {
      if (data.status === 'done' && data.report) {
        _mydaySetCache(dateStr, data.report);
        _mydayRenderTasks(data.report);
        return;
      }
      if (data.status === 'generating') {
        // Already running on server — start polling
        _mydayStartPolling(dateStr);
        _mydayShowProgressUI(dateStr, data.progress);
        return;
      }
    }
    // Server says idle/no-report. If we painted from cache, KEEP that view
    // (a stale cached report still beats a blank prompt); otherwise prompt.
    if (!painted) _mydayRenderWaiting(dateStr);
  } catch (e) {
    console.warn('[MyDay] Status check failed:', e);
    // Offline / error: keep the cached paint; only prompt if we had nothing.
    if (!painted && _myday.selectedDateStr === dateStr) _mydayRenderWaiting(dateStr);
  }
}

/* ═══════ Header update ═══════ */
function _mydayUpdateHeader(dateStr) {
  const titleEl = document.getElementById('mydayTitle');
  const subEl = document.getElementById('mydaySubtitle');
  const label = _mydayFormatDate(dateStr);
  if (titleEl) titleEl.textContent = label === t('myday.today') ? t('myday.title') : label;
  if (subEl) {
    const d = new Date(dateStr + 'T00:00:00');
    subEl.textContent = `${t('myday.dateFull', { y: d.getFullYear(), m: d.getMonth()+1, d: d.getDate() })} ${_mydayWeekday(dateStr)}`;
  }
}

/* ═══════ Sidebar cost info ═══════ */
function _mydayRenderSidebarInfo(dateStr) {
  const el = document.getElementById('mydayCalInfo');
  if (!el) return;
  const d = new Date(dateStr + 'T00:00:00');
  const costDaily = _myday._costDays || {};
  const dayData = costDaily[d.getDate()];
  if (!dayData || dayData.cost <= 0) { el.innerHTML = ''; return; }
  el.innerHTML = `<div class="mcal-info-label">\ud83d\udcb0 ¥${dayData.cost.toFixed(2)}</div>`;
}

/* ═══════ Skeleton ═══════ */
function _mydayShowSkeleton() {
  const container = document.getElementById('mydayTasks');
  if (!container) return;
  let html = '';
  for (let i = 0; i < 4; i++) {
    html += `<div class="myday-task-skel">
      <div class="skel-check"></div>
      <div class="skel-body"><div class="skel-line w75"></div><div class="skel-line w45"></div></div>
    </div>`;
  }
  container.innerHTML = html;
  const prog = document.getElementById('mydayProgress');
  if (prog) prog.innerHTML = '';
  const stats = document.getElementById('mydayStatsBar');
  if (stats) stats.innerHTML = '';
}

/* ═══════ Progress UI — shown during background generation ═══════ */
function _mydayShowProgressUI(dateStr, progressData) {
  const container = document.getElementById('mydayTasks');
  if (!container) return;

  const stage = (progressData && progressData.stage) || 'starting';
  // Use localized message based on stage (server sends Chinese progress text)
  let message;
  if (stage === 'extracting' && progressData && progressData.current != null) {
    message = t('myday.stageScanMsg', { c: progressData.current, t: progressData.total || '?' });
  } else if (stage === 'analyzing' && progressData && progressData.total) {
    message = t('myday.stageAnalyzeMsg', { n: progressData.total });
  } else if (stage === 'saving') {
    message = t('myday.stageSaveMsg');
  } else {
    message = t('myday.stageStarting');
  }

  const _mdIco = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px">${inner}</svg>`;
  const stageEmoji = {
    starting: _mdIco('<path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09"/><path d="M9 12a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.4 22.4 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 .05 5 .05"/>'),
    extracting: _mdIco('<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>'),
    analyzing: '✶',
    saving: _mdIco('<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>'),
  };
  const stageLabel = {
    starting: t('myday.stageStarting'),
    extracting: t('myday.stageExtracting'),
    analyzing: t('myday.stageAnalyzing'),
    saving: t('myday.stageSaving'),
  };
  const stageOrder = ['starting', 'extracting', 'analyzing', 'saving'];
  const activeIdx = stageOrder.indexOf(stage);

  let stepsHtml = '<div class="myday-gen-steps">';
  for (let i = 0; i < stageOrder.length; i++) {
    const s = stageOrder[i];
    let cls = 'myday-gen-step';
    if (i < activeIdx) cls += ' done';
    else if (i === activeIdx) cls += ' active';
    stepsHtml += `<div class="${cls}">
      <span class="myday-gen-step-dot">${i < activeIdx ? '✓' : (stageEmoji[s] || '○')}</span>
      <span class="myday-gen-step-label">${stageLabel[s]}</span>
    </div>`;
  }
  stepsHtml += '</div>';

  container.innerHTML = `
    <div class="myday-generating">
      <div class="myday-gen-spinner"></div>
      <div class="myday-gen-title">${t('myday.generating')}</div>
      <div class="myday-gen-message">${escapeHtml(message)}</div>
      ${stepsHtml}
      <div class="myday-gen-hint">${t('myday.genHint')}</div>
    </div>`;
  const prog = document.getElementById('mydayProgress');
  if (prog) prog.innerHTML = '';
  const stats = document.getElementById('mydayStatsBar');
  if (stats) stats.innerHTML = '';
}

/* ═══════ Polling for background generation ═══════ */
function _mydayStartPolling(dateStr) {
  if (_myday._pollTimers[dateStr]) return; // already polling
  const INTERVAL = 1500; // poll every 1.5 seconds

  const pollFn = async () => {
    try {
      const data = await Api.daily.status(dateStr);
      if (!data) return;

      if (data.status === 'done') {
        _mydayStopPolling(dateStr);
        if (data.report) {
          _mydaySetCache(dateStr, data.report);
        }
        // Refresh display if user is viewing this date
        if (_myday.selectedDateStr === dateStr) {
          const report = _myday.cache[dateStr];
          if (report) _mydayRenderTasks(report);
          else _mydayRenderEmpty();
        }
        // Invalidate overview cache so calendar fetches fresh data
        _myday._overviewCache = null;
        _mydayRenderCalendar();
        // Remove spinning from refresh button
        const refreshBtn = document.getElementById('mydayRefreshBtn');
        if (refreshBtn) refreshBtn.classList.remove('spinning');
        return;
      }

      if (data.status === 'error') {
        _mydayStopPolling(dateStr);
        if (_myday.selectedDateStr === dateStr) {
          _mydayRenderEmpty(`${t('myday.genFailed')}: ${data.error || 'Unknown'}`);
        }
        const refreshBtn = document.getElementById('mydayRefreshBtn');
        if (refreshBtn) refreshBtn.classList.remove('spinning');
        return;
      }

      // Still generating — update progress UI if user is viewing this date
      if (_myday.selectedDateStr === dateStr) {
        _mydayShowProgressUI(dateStr, data.progress);
      }
    } catch (e) {
      console.warn('[MyDay] Poll error for', dateStr, e);
    }
  };

  // Run immediately, then every INTERVAL ms
  pollFn();
  _myday._pollTimers[dateStr] = setInterval(pollFn, INTERVAL);
}

function _mydayStopPolling(dateStr) {
  if (_myday._pollTimers[dateStr]) {
    clearInterval(_myday._pollTimers[dateStr]);
    delete _myday._pollTimers[dateStr];
  }
}

/* ═══════ Waiting state — show generate prompt (for today/ungenerated dates) ═══════ */
async function _mydayRenderWaiting(dateStr) {
  const container = document.getElementById('mydayTasks');
  if (!container) return;

  const tofuSvg = `<svg class="myday-empty-tofu" width="56" height="56" viewBox="0 0 32 32" fill="none">
    <path d="M15.3 4.6 L6.4 9.6 L16.3 16 L26.2 10.5Z" fill="currentColor" opacity=".12"/>
    <path d="M6.4 9.6 L6.1 21.1 L17.2 27.2 L16.3 16Z" fill="currentColor" opacity=".08"/>
    <path d="M16.3 16 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" fill="currentColor" opacity=".05"/>
    <path d="M15.3 4.6 L6.4 9.6 L6.1 21.1 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" stroke="currentColor" stroke-width=".6" stroke-linejoin="round" fill="none"/>
    <rect x="7.8" y="14.2" width="2.6" height="3.3" rx=".3" fill="currentColor"/>
    <rect x="13.1" y="16.5" width="2.6" height="3.8" rx=".3" fill="currentColor"/>
    <path d="M10.1 20.1 Q12 21.6 13.9 20.1" stroke="currentColor" stroke-width=".5" fill="none" stroke-linecap="round"/>
  </svg>`;

  // Show initial waiting state immediately
  container.innerHTML = `
    <div class="myday-empty">
      ${tofuSvg}
      <div class="myday-empty-title">${t('myday.reportNotGenerated')}</div>
      <div class="myday-empty-hint">${t('myday.checkingConvs')}</div>
    </div>`;
  const prog = document.getElementById('mydayProgress');
  if (prog) prog.innerHTML = '';
  const stats = document.getElementById('mydayStatsBar');
  if (stats) stats.innerHTML = '';

  // Fetch conversation count from DB (authoritative source)
  let convCount = 0;
  try {
    const data = await Api.daily.convCount(dateStr);
    if (data) {
      convCount = data.count || 0;
    }
  } catch (e) { console.warn('[MyDay] conv-count fetch failed:', e); }

  // Check if user navigated away while we were fetching
  if (_myday.selectedDateStr !== dateStr) return;

  const hint = convCount > 0
    ? t('myday.hasConvsHint', { n: convCount })
    : t('myday.noConvsHint');

  container.innerHTML = `
    <div class="myday-empty">
      ${tofuSvg}
      <div class="myday-empty-title">${t('myday.reportNotGenerated')}</div>
      <div class="myday-empty-hint">${hint}</div>
      ${convCount > 0 ? `
        <button class="myday-generate-btn" id="mydayGenerateBtn" onclick="_mydayTriggerGenerate()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2L12 6M12 18L12 22M4.93 4.93L7.76 7.76M16.24 16.24L19.07 19.07M2 12H6M18 12H22M4.93 19.07L7.76 16.24M16.24 7.76L19.07 4.93"/>
          </svg>
          ${t('myday.generateBtn')}
        </button>` : ''}
    </div>
    <div class="myday-add-todo">
      <button class="myday-add-btn" onclick="document.getElementById('mydayTodoInput').focus()" title="${t('myday.addPlaceholder')}">＋</button>
      <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="${t('myday.addPlaceholder')}"
        onkeydown="if(event.key==='Enter'){event.preventDefault();_mydayAddTodo();}">
    </div>`;
}

/* ═══════ Trigger generation — async background + polling ═══════ */
async function _mydayTriggerGenerate() {
  const dateStr = _myday.selectedDateStr;
  if (!dateStr) return;

  // Already running?
  if (_myday._pollTimers[dateStr]) return;

  // Animate header refresh button
  const refreshBtn = document.getElementById('mydayRefreshBtn');
  if (refreshBtn) refreshBtn.classList.add('spinning');

  // Disable inline generate button if present
  const inlineBtn = document.getElementById('mydayGenerateBtn');
  if (inlineBtn) { inlineBtn.classList.add('loading'); inlineBtn.textContent = t('myday.analyzing'); }

  // Show progress immediately
  _mydayShowProgressUI(dateStr, { stage: 'starting' });

  try {
    const resp = await Api.daily.generate(dateStr, true);
    if (!resp || !resp.ok) throw new Error(`HTTP ${resp ? resp.status : 'no response'}`);
    const data = await resp.json();

    if (data.status === 'done' && data.report) {
      // Already cached — instant result
      _mydaySetCache(dateStr, data.report);
      if (_myday.selectedDateStr === dateStr) _mydayRenderTasks(data.report);
      if (refreshBtn) refreshBtn.classList.remove('spinning');
      _mydayRenderCalendar();
      return;
    }

    // Background job started → poll for progress
    _mydayStartPolling(dateStr);
    _mydayRenderCalendar();
  } catch (e) {
    console.warn('[MyDay] Generate failed:', e);
    if (_myday.selectedDateStr === dateStr) _mydayRenderEmpty(t('myday.genFailRetry'));
    if (refreshBtn) refreshBtn.classList.remove('spinning');
  }
}

/* ═══════ Past day backfill (now also async) ═══════ */
async function _mydayBackfillDay(dateStr) {
  // Use the same async mechanism
  return _mydayTriggerGenerateForDate(dateStr, false);
}

async function _mydayTriggerGenerateForDate(dateStr, force) {
  if (_myday._pollTimers[dateStr]) return; // already running

  _mydayShowProgressUI(dateStr, { stage: 'starting' });
  const refreshBtn = document.getElementById('mydayRefreshBtn');
  if (refreshBtn) refreshBtn.classList.add('spinning');

  try {
    const resp = await Api.daily.generate(dateStr, !!force);
    if (!resp || !resp.ok) throw new Error(`HTTP ${resp ? resp.status : 'no response'}`);
    const data = await resp.json();

    if (data.status === 'done' && data.report) {
      _mydaySetCache(dateStr, data.report);
      if (_myday.selectedDateStr === dateStr) _mydayRenderTasks(data.report);
      if (refreshBtn) refreshBtn.classList.remove('spinning');
      _mydayRenderCalendar();
      return;
    }

    _mydayStartPolling(dateStr);
    _mydayRenderCalendar();
  } catch (e) {
    console.warn('[MyDay] Generate failed for', dateStr, e);
    if (_myday.selectedDateStr === dateStr) _mydayRenderEmpty(t('myday.genFailRetry'));
    if (refreshBtn) refreshBtn.classList.remove('spinning');
  }
}

/* ═══════ RENDER STREAMS — Work stream summary view ═══════ */
function _mydayRenderTasks(report) {
  const container = document.getElementById('mydayTasks');
  if (!container) return;
  let streams = report.streams || [];

  // Legacy fallback
  if (streams.length === 0 && report.tasks && report.tasks.some(t => !t._todo)) {
    const legacyTasks = report.tasks.filter(t => !t._todo);
    const done = legacyTasks.filter(t => t.status === 'done').length;
    streams = [{
      id: 'legacy-summary', title: 'Legacy Report',
      summary: `${legacyTasks.length} conversations (${done} done) — click ↻ to regenerate`,
      status: 'in_progress', conv_ids: [], conv_count: legacyTasks.length,
    }];
  }

  const todayTodos = report.today_todos || [];
  const tomorrow = report.tomorrow || [];
  const isInherited = !!report._inherited;

  const unfinished = report.unfinished || [];
  if (streams.length === 0 && tomorrow.length === 0 && todayTodos.length === 0 && unfinished.length === 0) {
    _mydayRenderEmpty(); return;
  }

  // Stats & progress
  const doneCnt = streams.filter(s => s.status === 'done').length;
  _mydayRenderProgress(doneCnt, streams.length);
  _mydayRenderStreamStats(streams, report);

  // Sort: blocked → in_progress → done
  const statusOrder = { blocked: 0, in_progress: 1, done: 2 };
  const active = streams.filter(s => s.status !== 'done');
  const done = streams.filter(s => s.status === 'done');
  active.sort((a, b) => (statusOrder[a.status] ?? 1) - (statusOrder[b.status] ?? 1));

  let html = '';

  // Inherited-only: show generate prompt if there are conversations to analyze
  const convCount = (report.stats || {}).totalConversations || 0;
  if (isInherited && streams.length === 0 && convCount > 0) {
    html += `<div class="myday-inherited-prompt">
      <span>${t('myday.hasConvsToday', { n: convCount })}</span>
      <button class="myday-generate-btn" onclick="_mydayTriggerGenerate()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2L12 6M12 18L12 22M4.93 4.93L7.76 7.76M16.24 16.24L19.07 19.07M2 12H6M18 12H22M4.93 19.07L7.76 16.24M16.24 7.76L19.07 4.93"/>
        </svg>
        ${t('myday.generateDaily')}
      </button>
    </div>`;
  }

  // ── Section: Today's TODOs (inherited from yesterday's plan) ──
  if (todayTodos.length > 0) {
    const todayDoneCount = todayTodos.filter(t => t.done).length;
    const todayRatio = todayTodos.length > 0 ? Math.round(todayDoneCount / todayTodos.length * 100) : 0;
    html += `<div class="myday-section-label">
      ${t('myday.todayTodos')}
      <span class="myday-section-count">${todayDoneCount}/${todayTodos.length}</span>
      ${todayRatio > 0 ? `<span class="myday-accountability-bar"><span class="myday-accountability-fill" style="width:${todayRatio}%"></span></span>` : ''}
    </div>`;
    html += '<div class="myday-today-todos">';
    for (const item of todayTodos) html += _mydayInheritedTodoRow(item);
    html += '</div>';
  }

  // ── Section: Unfinished (yesterday's items not addressed today) ──
  if (unfinished.length > 0) {
    html += `<div class="myday-section-label" style="color:#f59e0b">
      ${t('myday.unfinishedSection')}
      <span class="myday-section-count">${unfinished.length}</span>
    </div>`;
    html += '<div class="myday-unfinished">';
    for (let ui = 0; ui < unfinished.length; ui++) html += _mydayUnfinishedRow(unfinished[ui], ui);
    html += '</div>';
  }

  // ── Section: Active work ──
  if (active.length > 0) {
    html += `<div class="myday-section-label">${t('myday.activeSection')} <span class="myday-section-count">${active.length}</span></div>`;
    for (const s of active) html += _mydayStreamRow(s);
  }

  // ── Section: Done ──
  if (done.length > 0) {
    html += `<div class="myday-section-label">${t('myday.doneSection')} <span class="myday-section-count">${done.length}</span></div>`;
    for (const s of done) html += _mydayStreamRow(s);
  }

  // ── Section: Tomorrow TODOs (LLM-generated plan for next day) ──
  if (tomorrow.length > 0) {
    const isToday = _myday.selectedDateStr === _mydayDateStr(new Date().getFullYear(), new Date().getMonth(), new Date().getDate());
    const todoLabel = isInherited ? t('myday.todoItems') : isToday ? t('myday.tomorrowPlan') : t('myday.nextDayPlan');
    html += `<div class="myday-section-label" style="margin-top:6px">${todoLabel} <span class="myday-section-count">${tomorrow.length}</span></div>`;
    html += '<div class="myday-tomorrow">';
    for (const item of tomorrow) html += _mydayTodoRow(item);
    html += '</div>';
  }

  // ── Manual add ──
  html += `<div class="myday-add-todo">
    <button class="myday-add-btn" onclick="document.getElementById('mydayTodoInput').focus()" title="${t('myday.addPlaceholder')}">＋</button>
    <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="${t('myday.addPlaceholder')}"
      onkeydown="if(event.key==='Enter'){event.preventDefault();_mydayAddTodo();}">
  </div>`;

  container.innerHTML = html;

  requestAnimationFrame(() => {
    container.querySelectorAll('.myday-stream, .myday-todo-item').forEach((el, i) => {
      el.style.animationDelay = `${i * 30}ms`;
      el.classList.add('enter');
    });
  });
}

/* ═══════ Single work stream row (clean) ═══════ */
function _mydayStreamRow(stream) {
  const st = stream.status || 'in_progress';
  const convCount = stream.conv_count || stream.conv_ids?.length || 0;

  const summaryHtml = stream.summary
    ? `<div class="myday-stream-summary">${escapeHtml(stream.summary)}</div>` : '';

  const convsHtml = convCount > 1
    ? `<div class="myday-stream-convs">${t('myday.convCount', { n: convCount })}</div>` : '';

  return `
    <div class="myday-stream s-${st}" data-streamid="${escapeHtml(stream.id)}">
      <div class="myday-dot s-${st}"
        onclick="_mydayToggleStreamStatus('${escapeHtml(stream.id)}')"
        title="${t('myday.toggleStatus')}"></div>
      <div class="myday-stream-body">
        <div class="myday-stream-title">${escapeHtml(stream.title)}</div>
        ${summaryHtml}
        ${convsHtml}
      </div>
    </div>`;
}

/* ═══════ Tomorrow TODO row ═══════ */
function _mydayTodoRow(item) {
  const isDone = !!item.done;
  const isCarried = !!item._carried;
  const hasAction = !!item.quick_action;
  const qa_prefill = hasAction ? (item.quick_action.prefill || '') : '';
  const checkSvg = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L19 7"/></svg>`;
  const delSvg = `<svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square"><path d="M1 1l6 6M7 1l-6 6"/></svg>`;
  const launchSvg = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  const carriedBadge = isCarried ? `<span class="myday-inherited-badge" style="background:rgba(245,158,11,0.12);color:#f59e0b">${t('myday.badgeCarried')}</span>` : '';
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        onclick="event.stopPropagation();_mydayStartTodoConv('${escapeHtml(item.id)}')"
        title="${t('myday.startConv')}">${launchSvg}</button>` : '';
  return `
    <div class="myday-todo-item${isDone ? ' done' : ''}">
      <button class="myday-todo-check${isDone ? ' checked' : ''}"
        onclick="_mydayToggleTodo('${escapeHtml(item.id)}')"
        title="${isDone ? t('myday.markUndone') : t('myday.markDone')}">${checkSvg}</button>
      <span class="myday-todo-text"${qa_prefill ? ` title="${escapeHtml(qa_prefill)}"` : ''}>${escapeHtml(item.text)}</span>
      ${carriedBadge}
      ${launchBtn}
      <button class="myday-todo-del"
        onclick="event.stopPropagation();_mydayDeleteTodo('${escapeHtml(item.id)}')"
        title="${t('myday.deleteTodo')}">${delSvg}</button>
    </div>`;
}

/* ═══════ Inherited TODO row (from yesterday's plan) ═══════ */
function _mydayInheritedTodoRow(item) {
  const isDone = !!item.done;
  const originDate = item._origin_date || '';
  const hasAction = !!item.quick_action;
  const qa_prefill = hasAction ? (item.quick_action.prefill || '') : '';
  const checkSvg = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L19 7"/></svg>`;
  const delSvg = `<svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square"><path d="M1 1l6 6M7 1l-6 6"/></svg>`;
  const launchSvg = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        onclick="event.stopPropagation();_mydayStartTodoConvInherited('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${t('myday.startConv')}">${launchSvg}</button>` : '';
  return `
    <div class="myday-todo-item inherited${isDone ? ' done' : ''}">
      <button class="myday-todo-check${isDone ? ' checked' : ''}"
        onclick="_mydayToggleInheritedTodo('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${isDone ? t('myday.markUndone') : t('myday.markDone')}">${checkSvg}</button>
      <span class="myday-todo-text"${qa_prefill ? ` title="${escapeHtml(qa_prefill)}"` : ''}>${escapeHtml(item.text)}</span>
      <span class="myday-inherited-badge">${t('myday.badgeYesterday')}</span>
      ${launchBtn}
      <button class="myday-todo-del"
        onclick="event.stopPropagation();_mydayDeleteInheritedTodo('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${t('myday.deleteTodo')}">${delSvg}</button>
    </div>`;
}

/* ═══════ Unfinished TODO row (read-only, from yesterday's expired plan) ═══════ */
function _mydayUnfinishedRow(item, idx) {
  const hasAction = !!item.quick_action;
  const dashCircle = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="9" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4 3" fill="#f59e0b" fill-opacity="0.06"/>
  </svg>`;
  const launchSvg = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        onclick="event.stopPropagation();_mydayStartTodoConvUnfinished(${idx})"
        title="${t('myday.startConv')}">${launchSvg}</button>` : '';
  return `
    <div class="myday-todo-item unfinished" style="opacity:0.55">
      <span style="display:inline-flex;align-items:center;width:22px;justify-content:center;flex-shrink:0">${dashCircle}</span>
      <span class="myday-todo-text">${escapeHtml(item.text)}</span>
      ${launchBtn}
    </div>`;
}
/* ═══════ Progress ═══════ */
function _mydayRenderProgress(done, total) {
  const el = document.getElementById('mydayProgress');
  if (!el || total === 0) { if (el) el.innerHTML = ''; return; }
  const pct = Math.round((done / total) * 100);
  el.innerHTML = `
    <div class="myday-prog-track"><div class="myday-prog-fill" id="mydayProgFill"></div></div>
    <span class="myday-prog-label">${done}/${total}</span>`;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const fill = document.getElementById('mydayProgFill');
    if (fill) fill.style.width = pct + '%';
  }));
}

/* ═══════ Stats bar — clean ═══════ */
function _mydayRenderStreamStats(streams, report) {
  const el = document.getElementById('mydayStatsBar');
  if (!el) return;
  const stats = report.stats || {};
  const totalConvs = stats.totalConversations || streams.reduce((n, s) => n + (s.conv_count || 0), 0);

  const parts = [];
  if (totalConvs) parts.push(t('myday.convStat', { n: totalConvs }));
  parts.push(t('myday.streamStat', { n: streams.length }));
  const quote = report.quote;
  if (quote) parts.push(escapeHtml(quote));
  el.innerHTML = `<span class="myday-stat">${parts.join(' · ')}</span>`;
}
function _mydayRenderStats(tasks, report) { _mydayRenderStreamStats(report.streams || tasks, report); }

/* ═══════ Empty state ═══════ */
function _mydayRenderEmpty(msg) {
  const container = document.getElementById('mydayTasks');
  if (!container) return;

  const tofuSvg = `<svg class="myday-empty-tofu" width="56" height="56" viewBox="0 0 32 32" fill="none">
    <path d="M15.3 4.6 L6.4 9.6 L16.3 16 L26.2 10.5Z" fill="currentColor" opacity=".12"/>
    <path d="M6.4 9.6 L6.1 21.1 L17.2 27.2 L16.3 16Z" fill="currentColor" opacity=".08"/>
    <path d="M16.3 16 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" fill="currentColor" opacity=".05"/>
    <path d="M15.3 4.6 L6.4 9.6 L6.1 21.1 L17.2 27.2 L25.9 22.3 L26.2 10.5Z" stroke="currentColor" stroke-width=".6" stroke-linejoin="round" fill="none"/>
    <rect x="7.8" y="14.2" width="2.6" height="3.3" rx=".3" fill="currentColor"/>
    <rect x="13.1" y="16.5" width="2.6" height="3.8" rx=".3" fill="currentColor"/>
    <path d="M10.1 20.1 Q12 21.6 13.9 20.1" stroke="currentColor" stroke-width=".5" fill="none" stroke-linecap="round"/>
  </svg>`;

  // Check if there are inherited today_todos even for empty report
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  const todayTodos = cached && cached.today_todos ? cached.today_todos : [];
  let todayHtml = '';
  if (todayTodos.length > 0) {
    const todayDoneCount = todayTodos.filter(t => t.done).length;
    todayHtml += `<div class="myday-section-label">${t('myday.todayTodos')} <span class="myday-section-count">${todayDoneCount}/${todayTodos.length}</span></div>`;
    todayHtml += '<div class="myday-today-todos">';
    for (const item of todayTodos) todayHtml += _mydayInheritedTodoRow(item);
    todayHtml += '</div>';
  }

  container.innerHTML = `
    ${todayHtml}
    <div class="myday-empty">
      ${tofuSvg}
      <div class="myday-empty-title">${msg || t('myday.quietDay')}</div>
      <div class="myday-empty-hint">${t('myday.noConvsFound')}</div>
    </div>
    <div class="myday-add-todo">
      <button class="myday-add-btn" onclick="document.getElementById('mydayTodoInput').focus()" title="${t('myday.addPlaceholder')}">＋</button>
      <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="${t('myday.addPlaceholder')}"
        onkeydown="if(event.key==='Enter'){event.preventDefault();_mydayAddTodo();}">
    </div>`;
  const prog = document.getElementById('mydayProgress');
  if (prog) prog.innerHTML = '';
  const stats = document.getElementById('mydayStatsBar');
  if (stats) stats.innerHTML = '';
}

/* ═══════ Launch a TODO item as a new conversation ═══════ */
function _mydayStartTodoConv(todoId) {
  if (!todoId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached) return;
  // Search in tomorrow list
  const item = (cached.tomorrow || []).find(t => t.id === todoId);
  if (!item || !item.quick_action) return;
  _mydayLaunchConvFromAction(item);
}

function _mydayStartTodoConvInherited(todoId, originDate) {
  if (!todoId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached) return;
  // Search in today_todos (inherited) list
  const item = (cached.today_todos || []).find(t => t.id === todoId);
  if (!item || !item.quick_action) return;
  _mydayLaunchConvFromAction(item);
}

function _mydayStartTodoConvUnfinished(idx) {
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached) return;
  const unfinished = cached.unfinished || [];
  const item = unfinished[idx];
  if (!item || !item.quick_action) return;
  _mydayLaunchConvFromAction(item);
}

function _mydayLaunchConvFromAction(item) {
  const qa = item.quick_action;
  if (!qa) return;

  // 1) Close the daily report modal
  closeDailyReport();

  // 2) Create a new empty conversation
  newChat();

  // 3) Apply tool configuration from quick_action
  if (qa.searchMode && qa.searchMode !== 'off') {
    if (typeof _applySearchModeUI === 'function') _applySearchModeUI(qa.searchMode);
  } else {
    if (typeof _applySearchModeUI === 'function') _applySearchModeUI('off');
  }
  if (typeof _applyFetchEnabledUI === 'function')
    _applyFetchEnabledUI(!!qa.fetchEnabled);
  if (typeof _applyCodeExecUI === 'function')
    _applyCodeExecUI(!!qa.codeExecEnabled);
  if (typeof _applyBrowserUI === 'function')
    _applyBrowserUI(!!qa.browserEnabled);

  // Project: if the TODO suggests project mode AND we have an active project, enable it
  if (qa.projectEnabled && typeof projectState !== 'undefined' && projectState.path) {
    // Project is already active from the previous session — keep it
  }

  // 4) Pre-fill the user input with the detailed prompt
  const input = document.getElementById('userInput');
  if (input) {
    input.value = qa.prefill || item.text || '';
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
    input.focus();
  }

  // 5) Update send button state
  if (typeof updateSendButton === 'function') updateSendButton();
}


/* ═══════ Daily Reminder — gentle toast to check daily report ═══════ */

/**
 * _mydayScheduleReminder — schedule a daily toast reminder.
 *
 * Shows a toast once per day (afternoon, after enough conversations)
 * nudging the user to check their daily report.
 * Respects a localStorage flag so it only shows once per calendar day.
 */
function _mydayScheduleReminder() {
  const REMINDER_DELAY_MS = 3 * 60 * 60 * 1000; // 3 hours after page load
  const MIN_HOUR = 14; // Don't remind before 2 PM

  setTimeout(async () => {
    try {
      const now = new Date();
      if (now.getHours() < MIN_HOUR) return; // too early

      // Check if already reminded today
      const todayStr = _mydayDateStr(now.getFullYear(), now.getMonth(), now.getDate());
      const lastReminder = localStorage.getItem('tofu_myday_last_reminder');
      if (lastReminder === todayStr) return; // already shown today

      // Check if modal is already open
      const modal = document.getElementById('dailyReportModal');
      if (modal && modal.classList.contains('open')) return;

      // Check if there are conversations today (quick API check)
      let convCount = 0;
      try {
        const data = await Api.daily.convCount(todayStr);
        if (data) convCount = data.count || 0;
      } catch (e) { /* ignore */ }

      if (convCount < 3) return; // not enough activity to warrant a reminder

      // Mark as reminded
      localStorage.setItem('tofu_myday_last_reminder', todayStr);

      // Show a gentle toast
      if (typeof showToast === 'function') {
        const title = t('myday.reminderTitle');
        const body = convCount > 0
          ? t('myday.reminderBody', { n: convCount })
          : t('myday.reminderBodyGeneric');
        showToast('📋', title, body, 8000);
      }
    } catch (e) {
      console.warn('[MyDay] Reminder check failed:', e);
    }
  }, REMINDER_DELAY_MS);
}

// Auto-schedule reminder on page load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _mydayScheduleReminder);
} else {
  _mydayScheduleReminder();
}

// Fetch-once today's digest so the project-bar pet is day-aware without the
// user opening My Day. Deferred a beat so it never competes with first paint;
// it paints from the IDB cache first and degrades to a silent no-op when the
// report isn't ready. Runs once per session (guarded by _bootDigestDone).
function _mydayBootDayDigestSoon() { setTimeout(_mydayBootDayDigest, 2500); }
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _mydayBootDayDigestSoon);
} else {
  _mydayBootDayDigestSoon();
}
