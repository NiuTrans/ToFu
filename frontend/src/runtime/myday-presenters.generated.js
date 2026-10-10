// @ts-check
/* Generated lazy retained runtime: myday-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';
import { MYDAY_PRESENTATION_ASSETS } from '../features/myday/presentation-assets';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('myday-presenters runtime dependency is unavailable: Api');
const _applyBrowserUI = runtimeScope._applyBrowserUI;
if (typeof _applyBrowserUI !== 'function') throw new Error('myday-presenters runtime dependency is unavailable: _applyBrowserUI');
const _applyCodeExecUI = runtimeScope._applyCodeExecUI;
if (typeof _applyCodeExecUI !== 'function') throw new Error('myday-presenters runtime dependency is unavailable: _applyCodeExecUI');
const _applyFetchEnabledUI = runtimeScope._applyFetchEnabledUI;
if (typeof _applyFetchEnabledUI !== 'function') throw new Error('myday-presenters runtime dependency is unavailable: _applyFetchEnabledUI');
const _applySearchModeUI = runtimeScope._applySearchModeUI;
if (typeof _applySearchModeUI !== 'function') throw new Error('myday-presenters runtime dependency is unavailable: _applySearchModeUI');
const newChat = runtimeScope.newChat;
if (typeof newChat !== 'function') throw new Error('myday-presenters runtime dependency is unavailable: newChat');
const updateSendButton = runtimeScope.updateSendButton;
if (typeof updateSendButton !== 'function') throw new Error('myday-presenters runtime dependency is unavailable: updateSendButton');
/* ===== migrated source: myday.js ===== */
/* ═══════════════════════════════════════════
   myday.js — My Day — Daily Task Report
   ═══════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════
   My Day — daily task report with async progress & categories
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
  _convDays: {},       // { dayNum: convCount } — server-side conversation counts per day
  _costDays: {},       // { dayNum: {cost, conversations} } — server-side cost data per day
};

/* The owner-scoped, bounded IndexedDB cache is composed by the typed My Day
   feature before this panel can be invoked. Missing identity/storage degrades
   to a cache miss; the server remains authoritative. */
function _mydayReportRepository() {
  if (typeof runtimeScope === 'undefined') return null;
  const repository = runtimeScope.MyDayReportRepository;
  return repository && typeof repository === 'object' ? repository : null;
}

function _mydayPersistReport(dateStr, report) {
  const repository = _mydayReportRepository();
  if (!repository || typeof repository.storeReport !== 'function') return;
  try {
    const pending = repository.storeReport(dateStr, report);
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
  } catch (_error) { /* reconstructible cache is optional */ }
}

/* Set both the in-memory + persistent cache from an authoritative server
   report.  Every place that receives a fresh full report routes through here
   so the IDB copy stays in lockstep with what the server returned. */
function _mydaySetCache(dateStr, report) {
  if (!report) return;
  report._full = true;
  _myday.cache[dateStr] = report;
  _mydayPersistReport(dateStr, report);
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
    `<button class="mcal-nav" data-tofu-action="_mydayCalPrev()">‹</button>
     <span class="mcal-title">${t('myday.yearMonth', { y: year, m: month + 1 })}</span>
     <button class="mcal-nav" data-tofu-action="_mydayCalNext()">›</button>`;

  // Conversation counts per day come from the server; catalog shells contain
  // no Turn bodies, so the browser cannot derive this aggregate locally.
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

    html += `<span class="${cls}" data-tofu-action="_mydaySelectDay(${d})" title="${cost > 0 ? '¥' + cost.toFixed(2) : ''}">
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
    const repository = _mydayReportRepository();
    const cachedMonth = repository && typeof repository.readMonth === 'function'
      ? await repository.readMonth(monthKey) : null;
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
    // Store server-side conversation counts (reliable — not affected by _turnSnapshotRequired shells)
    if (data.conv_days) {
      _myday._convDays = data.conv_days;
      changed = true;
    }
    // Store server-side cost data (accurate across all persisted Turns).
    if (data.cost_days) {
      _myday._costDays = data.cost_days;
      changed = true;
    }
    // Persist the authoritative month overview so the next reopen/reload paints
    // historical balances instantly (server stays the source of truth).
    try {
      const repository = _mydayReportRepository();
      if (repository && typeof repository.storeMonth === 'function') {
        const pending = repository.storeMonth(monthKey, {
          cost_days: data.cost_days || {}, conv_days: data.conv_days || {},
        });
        if (pending && typeof pending.catch === 'function') pending.catch(() => {});
      }
    } catch (_error) { /* reconstructible cache is optional */ }
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
      const repository = _mydayReportRepository();
      const cachedReport = repository && typeof repository.readReport === 'function'
        ? await repository.readReport(dateStr) : null;
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
  const FAIL_LIMIT = 8;  // consecutive failures → stop + clear spinner ()
  if (!_myday._pollFails) _myday._pollFails = {};

  /* Persistent failure (server stuck 'running', network down): the old code
   * spun the refresh button forever and leaked a 1.5s interval per date. */
  const _fail = (why) => {
    console.warn('[MyDay] Poll failure for', dateStr, why);
    _myday._pollFails[dateStr] = (_myday._pollFails[dateStr] || 0) + 1;
    if (_myday._pollFails[dateStr] >= FAIL_LIMIT) {
      _mydayStopPolling(dateStr);
      delete _myday._pollFails[dateStr];
      const refreshBtn = document.getElementById('mydayRefreshBtn');
      if (refreshBtn) refreshBtn.classList.remove('spinning');
      if (_myday.selectedDateStr === dateStr) {
        _mydayRenderEmpty(`${t('myday.genFailed')}: status check failed (network/server)`);
      }
    }
  };

  const pollFn = async () => {
    try {
      const data = await Api.daily.status(dateStr);
      if (!data) { _fail('empty response'); return; }
      _myday._pollFails[dateStr] = 0;

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
      _fail(e && e.message);
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

  // Show initial waiting state immediately
  container.innerHTML = `
    <div class="myday-empty">
      ${MYDAY_PRESENTATION_ASSETS.emptyIllustration}
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
      ${MYDAY_PRESENTATION_ASSETS.emptyIllustration}
      <div class="myday-empty-title">${t('myday.reportNotGenerated')}</div>
      <div class="myday-empty-hint">${hint}</div>
      ${convCount > 0 ? `
        <button class="myday-generate-btn" id="mydayGenerateBtn" data-tofu-action="_mydayTriggerGenerate()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2L12 6M12 18L12 22M4.93 4.93L7.76 7.76M16.24 16.24L19.07 19.07M2 12H6M18 12H22M4.93 19.07L7.76 16.24M16.24 7.76L19.07 4.93"/>
          </svg>
          ${t('myday.generateBtn')}
        </button>` : ''}
    </div>
    <div class="myday-add-todo">
      <button class="myday-add-btn" data-tofu-action="document.getElementById('mydayTodoInput').focus()" title="${t('myday.addPlaceholder')}">＋</button>
      <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="${t('myday.addPlaceholder')}"
        data-tofu-action-keydown="if(event.key==='Enter'){event.preventDefault();_mydayAddTodo();}">
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
      <button class="myday-generate-btn" data-tofu-action="_mydayTriggerGenerate()">
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
    <button class="myday-add-btn" data-tofu-action="document.getElementById('mydayTodoInput').focus()" title="${t('myday.addPlaceholder')}">＋</button>
    <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="${t('myday.addPlaceholder')}"
      data-tofu-action-keydown="if(event.key==='Enter'){event.preventDefault();_mydayAddTodo();}">
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
        data-tofu-action="_mydayToggleStreamStatus('${escapeHtml(stream.id)}')"
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
  const carriedBadge = isCarried ? `<span class="myday-inherited-badge" style="background:rgba(245,158,11,0.12);color:#f59e0b">${t('myday.badgeCarried')}</span>` : '';
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        data-tofu-action="event.stopPropagation();_mydayStartTodoConv('${escapeHtml(item.id)}')"
        title="${t('myday.startConv')}">${MYDAY_PRESENTATION_ASSETS.todoLaunchIcon}</button>` : '';
  return `
    <div class="myday-todo-item${isDone ? ' done' : ''}">
      <button class="myday-todo-check${isDone ? ' checked' : ''}"
        data-tofu-action="_mydayToggleTodo('${escapeHtml(item.id)}')"
        title="${isDone ? t('myday.markUndone') : t('myday.markDone')}">${MYDAY_PRESENTATION_ASSETS.todoCheckIcon}</button>
      <span class="myday-todo-text"${qa_prefill ? ` title="${escapeHtml(qa_prefill)}"` : ''}>${escapeHtml(item.text)}</span>
      ${carriedBadge}
      ${launchBtn}
      <button class="myday-todo-del"
        data-tofu-action="event.stopPropagation();_mydayDeleteTodo('${escapeHtml(item.id)}')"
        title="${t('myday.deleteTodo')}">${MYDAY_PRESENTATION_ASSETS.todoDeleteIcon}</button>
    </div>`;
}

/* ═══════ Inherited TODO row (from yesterday's plan) ═══════ */
function _mydayInheritedTodoRow(item) {
  const isDone = !!item.done;
  const originDate = item._origin_date || '';
  const hasAction = !!item.quick_action;
  const qa_prefill = hasAction ? (item.quick_action.prefill || '') : '';
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        data-tofu-action="event.stopPropagation();_mydayStartTodoConvInherited('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${t('myday.startConv')}">${MYDAY_PRESENTATION_ASSETS.todoLaunchIcon}</button>` : '';
  return `
    <div class="myday-todo-item inherited${isDone ? ' done' : ''}">
      <button class="myday-todo-check${isDone ? ' checked' : ''}"
        data-tofu-action="_mydayToggleInheritedTodo('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${isDone ? t('myday.markUndone') : t('myday.markDone')}">${MYDAY_PRESENTATION_ASSETS.todoCheckIcon}</button>
      <span class="myday-todo-text"${qa_prefill ? ` title="${escapeHtml(qa_prefill)}"` : ''}>${escapeHtml(item.text)}</span>
      <span class="myday-inherited-badge">${t('myday.badgeYesterday')}</span>
      ${launchBtn}
      <button class="myday-todo-del"
        data-tofu-action="event.stopPropagation();_mydayDeleteInheritedTodo('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${t('myday.deleteTodo')}">${MYDAY_PRESENTATION_ASSETS.todoDeleteIcon}</button>
    </div>`;
}

/* ═══════ Unfinished TODO row (read-only, from yesterday's expired plan) ═══════ */
function _mydayUnfinishedRow(item, idx) {
  const hasAction = !!item.quick_action;
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        data-tofu-action="event.stopPropagation();_mydayStartTodoConvUnfinished(${idx})"
        title="${t('myday.startConv')}">${MYDAY_PRESENTATION_ASSETS.todoLaunchIcon}</button>` : '';
  return `
    <div class="myday-todo-item unfinished" style="opacity:0.55">
      <span style="display:inline-flex;align-items:center;width:22px;justify-content:center;flex-shrink:0">${MYDAY_PRESENTATION_ASSETS.unfinishedIcon}</span>
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

/* ═══════ Empty state ═══════ */
function _mydayRenderEmpty(msg) {
  const container = document.getElementById('mydayTasks');
  if (!container) return;

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
      ${MYDAY_PRESENTATION_ASSETS.emptyIllustration}
      <div class="myday-empty-title">${msg || t('myday.quietDay')}</div>
      <div class="myday-empty-hint">${t('myday.noConvsFound')}</div>
    </div>
    <div class="myday-add-todo">
      <button class="myday-add-btn" data-tofu-action="document.getElementById('mydayTodoInput').focus()" title="${t('myday.addPlaceholder')}">＋</button>
      <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="${t('myday.addPlaceholder')}"
        data-tofu-action-keydown="if(event.key==='Enter'){event.preventDefault();_mydayAddTodo();}">
    </div>`;
  const prog = document.getElementById('mydayProgress');
  if (prog) prog.innerHTML = '';
  const stats = document.getElementById('mydayStatsBar');
  if (stats) stats.innerHTML = '';
}

// Task mutation policy lives in the typed lazy My Day owner. This retained
// panel exposes only its selected report plus cache/render presentation ports.
if (typeof runtimeScope !== 'undefined') {
  runtimeScope.MyDayTaskPresentation = Object.freeze({
    selectedReport: function () {
      const date = _myday.selectedDateStr;
      return { date: date, report: date ? (_myday.cache[date] || null) : null };
    },
    acceptAuthoritativeReport: _mydaySetCache,
    persistReport: _mydayPersistReport,
    renderReport: _mydayRenderTasks,
    renderCalendar: _mydayRenderCalendar,
    taskInput: function () {
      return document.getElementById('mydayTodoInput');
    },
    composerInput: function () {
      return document.getElementById('userInput');
    },
    closeReport: closeDailyReport,
    createConversation: function () {
      if (typeof newChat === 'function') newChat();
    },
    applySearchMode: function (mode) {
      if (typeof _applySearchModeUI === 'function') _applySearchModeUI(mode);
    },
    applyFetchEnabled: function (enabled) {
      if (typeof _applyFetchEnabledUI === 'function') _applyFetchEnabledUI(enabled);
    },
    applyCodeExecEnabled: function (enabled) {
      if (typeof _applyCodeExecUI === 'function') _applyCodeExecUI(enabled);
    },
    applyBrowserEnabled: function (enabled) {
      if (typeof _applyBrowserUI === 'function') _applyBrowserUI(enabled);
    },
    updateSendButton: function () {
      if (typeof updateSendButton === 'function') updateSendButton();
    },
  });
}

// BEGIN GENERATED LAZY RUNTIME PORTS — myday-presenters
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — myday-presenters
runtimeScope._mydayCalNext = _mydayCalNext;
runtimeScope._mydayCalPrev = _mydayCalPrev;
runtimeScope._mydaySelectDay = _mydaySelectDay;
runtimeScope._mydayTriggerGenerate = _mydayTriggerGenerate;
runtimeScope.closeDailyReport = closeDailyReport;
runtimeScope.openDailyReport = openDailyReport;
// END GENERATED LAZY RUNTIME ACTIONS
