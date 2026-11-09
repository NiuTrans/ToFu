// @ts-check
/* Generated lazy retained runtime: myday-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { t } from '../i18n/index';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('myday-presenters runtime dependency is unavailable: Api');
/* ===== migrated source: myday.js ===== */
/* ═══════════════════════════════════════════
   myday.js — My Day — goals, items and progress
   ═══════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════
   Retained shell of My Day: modal open/close, date navigation and the mini
   calendar sidebar. The main column (goals → items → progress, and the
   read-only recap of a past day) is the typed goal board
   (frontend/src/features/myday/goal-board.ts), reached through the
   MyDayGoalBoard registry port; it reads this panel back through
   MyDayPanelPresentation. Deletion path: move the calendar into the typed
   feature and drop this section with its bundle.
   ═══════════════════════════════════════════════════════════ */

const _myday = {
  year: new Date().getFullYear(),
  month: new Date().getMonth(),
  selectedDay: new Date().getDate(),
  selectedDateStr: '',
  _convDays: {},       // { dayNum: convCount } — server-side conversation counts per day
  _costDays: {},       // { dayNum: {cost, conversations} } — server-side cost data per day
  _overviewLoads: {},  // { 'YYYY-M': Promise } — the one in-flight month load per month
  _overviewCache: null,
};

/* The owner-scoped, bounded IndexedDB cache is composed by the typed My Day
   feature before this panel can be invoked. Missing identity/storage degrades
   to a cache miss; the server remains authoritative. */
function _mydayReportRepository() {
  if (typeof runtimeScope === 'undefined') return null;
  const repository = runtimeScope.MyDayReportRepository;
  return repository && typeof repository === 'object' ? repository : null;
}

function _mydayGoalBoard() {
  if (typeof runtimeScope === 'undefined') return null;
  const board = runtimeScope.MyDayGoalBoard;
  return board && typeof board === 'object' ? board : null;
}

function openDailyReport() {
  document.getElementById('dailyReportModal').classList.add('open');
  _mydayShowToday();
}
function closeDailyReport() {
  document.getElementById('dailyReportModal').classList.remove('open');
}

function _mydayShowToday() {
  const now = new Date();
  _myday.year = now.getFullYear();
  _myday.month = now.getMonth();
  _myday.selectedDay = now.getDate();
  _mydayRenderCalendar();
  _mydayFetchMonthOverview(_myday.year, _myday.month);
  _mydaySelectDay(_myday.selectedDay);
}

/* Header ↻: re-read the goals (with their Worker evidence) and the month overview. */
function _mydayRefresh() {
  _myday._overviewCache = null;
  _mydayFetchMonthOverview(_myday.year, _myday.month);
  const board = _mydayGoalBoard();
  if (board) board.refresh();
}

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
  _mydayFetchMonthOverview(_myday.year, _myday.month);
}
function _mydayCalNext() {
  _myday.month++;
  if (_myday.month > 11) { _myday.month = 0; _myday.year++; }
  _mydayRenderCalendar();
  _mydayFetchMonthOverview(_myday.year, _myday.month);
}

/* Rendering never loads: month data arrives through _mydayFetchMonthOverview,
   whose completion renders again. A render that also fetched turned the
   IndexedDB instant paint into one more calendar request per cache read (the
   2026-11-08 request storm). */
function _mydayRenderCalendar() {
  const { year, month, selectedDay } = _myday;
  const now = new Date();
  const isCurMonth = now.getFullYear() === year && now.getMonth() === month;
  const todayD = isCurMonth ? now.getDate() : -1;

  const hdr = document.getElementById('mydayCalHeader');
  if (hdr) hdr.innerHTML =
    `<button class="mcal-nav" data-tofu-action="_mydayCalPrev()">‹</button>
     <span class="mcal-title">${t('myday.yearMonth', { y: year, m: month + 1 })}</span>
     <button class="mcal-nav" data-tofu-action="_mydayCalNext()">›</button>`;

  // Conversation counts and cost per day come from the server month overview.
  const dayCounts = _myday._convDays || {};
  const costDaily = _myday._costDays || {};
  // Days on which goal items were completed, from the typed goal board.
  const board = _mydayGoalBoard();
  const doneDays = board ? board.marks(year, month) : {};

  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  let html = '';
  const wk = t('myday.calWeek').split(',');
  for (const w of wk) html += `<span class="mcal-wk">${w}</span>`;
  for (let i = 0; i < firstDow; i++) html += '<span class="mcal-d empty"></span>';

  for (let d = 1; d <= daysInMonth; d++) {
    const isToday = d === todayD;
    const isSel = d === selectedDay && isCurMonth;
    const isFuture = isCurMonth && d > todayD;
    const hasConvs = !!dayCounts[d];
    const cost = costDaily[d] ? costDaily[d].cost : 0;

    let cls = 'mcal-d';
    if (isToday) cls += ' today';
    if (isSel) cls += ' sel';
    if (isFuture) cls += ' future';
    if (!hasConvs && !isFuture && !doneDays[d]) cls += ' quiet';

    let dotHtml = '';
    if (doneDays[d]) dotHtml = '<span class="mcal-dot done"></span>';
    else if (hasConvs) dotHtml = '<span class="mcal-dot unknown"></span>';

    html += `<span class="${cls}" data-tofu-action="_mydaySelectDay(${d})" title="${cost > 0 ? '¥' + cost.toFixed(2) : ''}">
      ${d}${dotHtml}</span>`;
  }

  const grid = document.getElementById('mydayCalGrid');
  if (grid) grid.innerHTML = html;
}

/* One in-flight month load per month (15s client freshness after it settles).
   Instant-paint: the persistent IDB month cache paints the calendar's ¥
   balances and conversation counts before the (possibly multi-second,
   cache-cold) network call, which then reconciles and rewrites the cache. */
function _mydayFetchMonthOverview(year, month) {
  const cacheKey = `${year}-${month}`;
  if (_myday._overviewCache && _myday._overviewCache.key === cacheKey &&
      Date.now() - _myday._overviewCache.ts < 15000) {
    return Promise.resolve();
  }
  const inFlight = _myday._overviewLoads[cacheKey];
  if (inFlight) return inFlight;
  const load = _mydayLoadMonthOverview(year, month).finally(() => {
    if (_myday._overviewLoads[cacheKey] === load) delete _myday._overviewLoads[cacheKey];
  });
  _myday._overviewLoads[cacheKey] = load;
  return load;
}

async function _mydayLoadMonthOverview(year, month) {
  const monthKey = `${year}-${String(month + 1).padStart(2, '0')}`;
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
    if (_myday.year !== year || _myday.month !== month) return;
    _myday._convDays = data.conv_days || {};
    _myday._costDays = data.cost_days || {};
    _mydayRenderCalendar();
    if (_myday.selectedDateStr) _mydayRenderSidebarInfo(_myday.selectedDateStr);
  } catch (e) {
    console.warn('[MyDay] Calendar overview error:', e);
  }
}

/* ═══════ Day selection ═══════ */
function _mydaySelectDay(day) {
  _myday.selectedDay = day;
  const dateStr = _mydayDateStr(_myday.year, _myday.month, day);
  _myday.selectedDateStr = dateStr;

  document.querySelectorAll('.mcal-d.sel').forEach(el => el.classList.remove('sel'));
  const grid = document.getElementById('mydayCalGrid');
  if (grid) {
    grid.querySelectorAll('.mcal-d:not(.empty)').forEach(el => {
      if (parseInt(el.textContent) === day) el.classList.add('sel');
    });
  }

  _mydayUpdateHeader(dateStr);
  _mydayRenderSidebarInfo(dateStr);
  const board = _mydayGoalBoard();
  if (board) board.show(dateStr);
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

// The typed goal board reads this panel back through one presentation port.
if (typeof runtimeScope !== 'undefined') {
  runtimeScope.MyDayPanelPresentation = Object.freeze({
    renderCalendar: _mydayRenderCalendar,
    showToday: _mydayShowToday,
  });
}

// BEGIN GENERATED LAZY RUNTIME PORTS — myday-presenters
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — myday-presenters
runtimeScope._mydayCalNext = _mydayCalNext;
runtimeScope._mydayCalPrev = _mydayCalPrev;
runtimeScope._mydayRefresh = _mydayRefresh;
runtimeScope._mydaySelectDay = _mydaySelectDay;
runtimeScope.closeDailyReport = closeDailyReport;
runtimeScope.openDailyReport = openDailyReport;
// END GENERATED LAZY RUNTIME ACTIONS
