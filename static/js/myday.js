/* ═══════════════════════════════════════════
   myday.js — My Day — Daily Task Report
   ═══════════════════════════════════════════ */

/* ── Cost Dashboard — aliases for backward compatibility ── */
function openCostDashboard() { openDailyReport(); }
function closeCostDashboard() { closeDailyReport(); }

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
const _STATUS_LABELS = { done: '✓ 完成', in_progress: '⏳ 进行中', blocked: '⛔ 受阻', incomplete: '进行中' };
const _STATUS_CYCLE = ['in_progress', 'done', 'blocked']; // toggle order

/* ═══════ Date helpers ═══════ */
function _mydayDateStr(y, m, d) {
  return `${y}-${String(m + 1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
function _mydayWeekday(dateStr) {
  return ['周日','周一','周二','周三','周四','周五','周六'][new Date(dateStr + 'T00:00:00').getDay()];
}
function _mydayFormatDate(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  const months = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
  const now = new Date();
  const todayStr = _mydayDateStr(now.getFullYear(), now.getMonth(), now.getDate());
  const yest = new Date(now); yest.setDate(yest.getDate() - 1);
  const yestStr = _mydayDateStr(yest.getFullYear(), yest.getMonth(), yest.getDate());
  if (dateStr === todayStr) return '今天';
  if (dateStr === yestStr) return '昨天';
  return `${months[d.getMonth()]}${d.getDate()}日`;
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
  const mNames = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
  const now = new Date();
  const isCurMonth = now.getFullYear() === year && now.getMonth() === month;
  const todayD = isCurMonth ? now.getDate() : -1;

  // Header
  const hdr = document.getElementById('mydayCalHeader');
  if (hdr) hdr.innerHTML =
    `<button class="mcal-nav" onclick="_mydayCalPrev()">‹</button>
     <span class="mcal-title">${year}年${mNames[month]}</span>
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
  const wk = ['日','一','二','三','四','五','六'];
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

/* Fetch month overview from API for calendar dots (with 15s client-side TTL) */
async function _mydayFetchMonthOverview(year, month) {
  const cacheKey = `${year}-${month}`;
  const now = Date.now();
  if (_myday._overviewCache && _myday._overviewCache.key === cacheKey &&
      now - _myday._overviewCache.ts < 15000) {
    return; // skip — data is fresh enough
  }
  try {
    const resp = await fetch(apiUrl(`/api/daily-report/calendar/${year}/${month + 1}`));
    if (!resp.ok) {
      console.warn('[MyDay] Calendar overview fetch failed: HTTP', resp.status);
      return;
    }
    const data = await resp.json();
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

  // Check cache first — if we have a full report, show it
  if (_myday.cache[dateStr] && _myday.cache[dateStr]._full) {
    _mydayRenderTasks(_myday.cache[dateStr]);
    return;
  }

  // Check server for existing report or running job
  _mydayShowSkeleton();
  try {
    const resp = await fetch(apiUrl(`/api/daily-report/status/${dateStr}`));
    if (resp.ok) {
      const data = await resp.json();
      if (data.status === 'done' && data.report) {
        data.report._full = true;
        _myday.cache[dateStr] = data.report;
        if (_myday.selectedDateStr === dateStr) _mydayRenderTasks(data.report);
        return;
      }
      if (data.status === 'generating') {
        // Already running on server — start polling
        _mydayStartPolling(dateStr);
        if (_myday.selectedDateStr === dateStr)
          _mydayShowProgressUI(dateStr, data.progress);
        return;
      }
    }
  } catch (e) {
    console.warn('[MyDay] Status check failed:', e);
  }

  // Nothing cached, nothing running → show waiting/generate prompt
  if (_myday.selectedDateStr === dateStr) _mydayRenderWaiting(dateStr);
}

/* ═══════ Header update ═══════ */
function _mydayUpdateHeader(dateStr) {
  const titleEl = document.getElementById('mydayTitle');
  const subEl = document.getElementById('mydaySubtitle');
  const label = _mydayFormatDate(dateStr);
  if (titleEl) titleEl.textContent = label === '今天' ? '我的一天' : label;
  if (subEl) {
    const d = new Date(dateStr + 'T00:00:00');
    subEl.textContent = `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${_mydayWeekday(dateStr)}`;
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
  el.innerHTML = `<div class="mcal-info-label">💰 ¥${dayData.cost.toFixed(2)}</div>`;
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
  const message = (progressData && progressData.message) || '正在启动…';

  const stageEmoji = { starting: '🚀', extracting: '🔍', analyzing: '✶', saving: '💾' };
  const stageLabel = { starting: '启动中', extracting: '扫描对话', analyzing: 'AI 分析', saving: '保存报告' };
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
      <div class="myday-gen-title">正在生成报告</div>
      <div class="myday-gen-message">${escapeHtml(message)}</div>
      ${stepsHtml}
      <div class="myday-gen-hint">你可以切换到其他日期，生成不会中断</div>
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
      const resp = await fetch(apiUrl(`/api/daily-report/status/${dateStr}`));
      if (!resp.ok) return;
      const data = await resp.json();

      if (data.status === 'done') {
        _mydayStopPolling(dateStr);
        if (data.report) {
          data.report._full = true;
          _myday.cache[dateStr] = data.report;
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
          _mydayRenderEmpty(`生成失败: ${data.error || '未知错误'}`);
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
      <div class="myday-empty-title">报告尚未生成</div>
      <div class="myday-empty-hint">正在查询对话数量…</div>
    </div>`;
  const prog = document.getElementById('mydayProgress');
  if (prog) prog.innerHTML = '';
  const stats = document.getElementById('mydayStatsBar');
  if (stats) stats.innerHTML = '';

  // Fetch conversation count from DB (authoritative source)
  let convCount = 0;
  try {
    const resp = await fetch(apiUrl(`/api/daily-report/conv-count/${dateStr}`));
    if (resp.ok) {
      const data = await resp.json();
      convCount = data.count || 0;
    }
  } catch (e) { console.warn('[MyDay] conv-count fetch failed:', e); }

  // Check if user navigated away while we were fetching
  if (_myday.selectedDateStr !== dateStr) return;

  const hint = convCount > 0
    ? `有 ${convCount} 个对话，点击上方刷新按钮或下方按钮生成报告`
    : '还没有对话记录，开始聊天后可以生成报告';

  container.innerHTML = `
    <div class="myday-empty">
      ${tofuSvg}
      <div class="myday-empty-title">报告尚未生成</div>
      <div class="myday-empty-hint">${hint}</div>
      ${convCount > 0 ? `
        <button class="myday-generate-btn" id="mydayGenerateBtn" onclick="_mydayTriggerGenerate()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2L12 6M12 18L12 22M4.93 4.93L7.76 7.76M16.24 16.24L19.07 19.07M2 12H6M18 12H22M4.93 19.07L7.76 16.24M16.24 7.76L19.07 4.93"/>
          </svg>
          生成报告
        </button>` : ''}
    </div>
    <div class="myday-add-todo">
      <button class="myday-add-btn" onclick="document.getElementById('mydayTodoInput').focus()" title="添加">＋</button>
      <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="添加待办…"
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
  if (inlineBtn) { inlineBtn.classList.add('loading'); inlineBtn.textContent = '分析中…'; }

  // Show progress immediately
  _mydayShowProgressUI(dateStr, { stage: 'starting', message: '正在启动…' });

  try {
    const resp = await fetch(apiUrl('/api/daily-report/generate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, force: true }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (data.status === 'done' && data.report) {
      // Already cached — instant result
      data.report._full = true;
      _myday.cache[dateStr] = data.report;
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
    if (_myday.selectedDateStr === dateStr) _mydayRenderEmpty('启动生成失败，请重试');
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

  _mydayShowProgressUI(dateStr, { stage: 'starting', message: '正在启动…' });
  const refreshBtn = document.getElementById('mydayRefreshBtn');
  if (refreshBtn) refreshBtn.classList.add('spinning');

  try {
    const resp = await fetch(apiUrl('/api/daily-report/generate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, force: !!force }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (data.status === 'done' && data.report) {
      data.report._full = true;
      _myday.cache[dateStr] = data.report;
      if (_myday.selectedDateStr === dateStr) _mydayRenderTasks(data.report);
      if (refreshBtn) refreshBtn.classList.remove('spinning');
      _mydayRenderCalendar();
      return;
    }

    _mydayStartPolling(dateStr);
    _mydayRenderCalendar();
  } catch (e) {
    console.warn('[MyDay] Generate failed for', dateStr, e);
    if (_myday.selectedDateStr === dateStr) _mydayRenderEmpty('加载失败，请重试');
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
      id: 'legacy-summary', title: '旧版报告',
      summary: `${legacyTasks.length} 个对话 (${done} 完成) — 点击 ↻ 重新生成`,
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
      <span>今日已有 ${convCount} 个对话</span>
      <button class="myday-generate-btn" onclick="_mydayTriggerGenerate()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2L12 6M12 18L12 22M4.93 4.93L7.76 7.76M16.24 16.24L19.07 19.07M2 12H6M18 12H22M4.93 19.07L7.76 16.24M16.24 7.76L19.07 4.93"/>
        </svg>
        生成日报
      </button>
    </div>`;
  }

  // ── Section: Today's TODOs (inherited from yesterday's plan) ──
  if (todayTodos.length > 0) {
    const todayDoneCount = todayTodos.filter(t => t.done).length;
    const todayRatio = todayTodos.length > 0 ? Math.round(todayDoneCount / todayTodos.length * 100) : 0;
    html += `<div class="myday-section-label">
      今日待办
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
      未完成
      <span class="myday-section-count">${unfinished.length}</span>
    </div>`;
    html += '<div class="myday-unfinished">';
    for (let ui = 0; ui < unfinished.length; ui++) html += _mydayUnfinishedRow(unfinished[ui], ui);
    html += '</div>';
  }

  // ── Section: Active work ──
  if (active.length > 0) {
    html += `<div class="myday-section-label">进行中 <span class="myday-section-count">${active.length}</span></div>`;
    for (const s of active) html += _mydayStreamRow(s);
  }

  // ── Section: Done ──
  if (done.length > 0) {
    html += `<div class="myday-section-label">已完成 <span class="myday-section-count">${done.length}</span></div>`;
    for (const s of done) html += _mydayStreamRow(s);
  }

  // ── Section: Tomorrow TODOs (LLM-generated plan for next day) ──
  if (tomorrow.length > 0) {
    const isToday = _myday.selectedDateStr === _mydayDateStr(new Date().getFullYear(), new Date().getMonth(), new Date().getDate());
    const todoLabel = isInherited ? '待办事项' : isToday ? '明日计划' : '次日计划';
    html += `<div class="myday-section-label" style="margin-top:6px">${todoLabel} <span class="myday-section-count">${tomorrow.length}</span></div>`;
    html += '<div class="myday-tomorrow">';
    for (const item of tomorrow) html += _mydayTodoRow(item);
    html += '</div>';
  }

  // ── Manual add ──
  html += `<div class="myday-add-todo">
    <button class="myday-add-btn" onclick="document.getElementById('mydayTodoInput').focus()" title="添加">＋</button>
    <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="添加待办…"
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
    ? `<div class="myday-stream-convs">${convCount} 个对话</div>` : '';

  return `
    <div class="myday-stream s-${st}" data-streamid="${escapeHtml(stream.id)}">
      <div class="myday-dot s-${st}"
        onclick="_mydayToggleStreamStatus('${escapeHtml(stream.id)}')"
        title="切换状态"></div>
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
  const carriedBadge = isCarried ? '<span class="myday-inherited-badge" style="background:rgba(245,158,11,0.12);color:#f59e0b">延续</span>' : '';
  const launchBtn = hasAction ? `
      <button class="myday-todo-launch"
        onclick="event.stopPropagation();_mydayStartTodoConv('${escapeHtml(item.id)}')"
        title="开始对话">${launchSvg}</button>` : '';
  return `
    <div class="myday-todo-item${isDone ? ' done' : ''}">
      <button class="myday-todo-check${isDone ? ' checked' : ''}"
        onclick="_mydayToggleTodo('${escapeHtml(item.id)}')"
        title="${isDone ? '标记未完成' : '标记完成'}">${checkSvg}</button>
      <span class="myday-todo-text"${qa_prefill ? ` title="${escapeHtml(qa_prefill)}"` : ''}>${escapeHtml(item.text)}</span>
      ${carriedBadge}
      ${launchBtn}
      <button class="myday-todo-del"
        onclick="event.stopPropagation();_mydayDeleteTodo('${escapeHtml(item.id)}')"
        title="删除">${delSvg}</button>
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
        title="开始对话">${launchSvg}</button>` : '';
  return `
    <div class="myday-todo-item inherited${isDone ? ' done' : ''}">
      <button class="myday-todo-check${isDone ? ' checked' : ''}"
        onclick="_mydayToggleInheritedTodo('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="${isDone ? '标记未完成' : '标记完成'}">${checkSvg}</button>
      <span class="myday-todo-text"${qa_prefill ? ` title="${escapeHtml(qa_prefill)}"` : ''}>${escapeHtml(item.text)}</span>
      <span class="myday-inherited-badge">昨日</span>
      ${launchBtn}
      <button class="myday-todo-del"
        onclick="event.stopPropagation();_mydayDeleteInheritedTodo('${escapeHtml(item.id)}', '${escapeHtml(originDate)}')"
        title="删除">${delSvg}</button>
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
        title="开始对话">${launchSvg}</button>` : '';
  return `
    <div class="myday-todo-item unfinished" style="opacity:0.55">
      <span style="display:inline-flex;align-items:center;width:22px;justify-content:center;flex-shrink:0">${dashCircle}</span>
      <span class="myday-todo-text">${escapeHtml(item.text)}</span>
      ${launchBtn}
    </div>`;
}

/* ═══════ Toggle inherited TODO (cross-day) ═══════ */
async function _mydayToggleInheritedTodo(todoId, originDate) {
  if (!todoId || !originDate) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.today_todos) return;

  const item = cached.today_todos.find(t => t.id === todoId);
  if (!item) return;
  const newDone = !item.done;

  // Optimistic update
  item.done = newDone;
  _mydayRenderTasks(cached);

  try {
    const resp = await fetch(apiUrl('/api/daily-report/inherited-todo-toggle'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ origin_date: originDate, todo_id: todoId, done: newDone }),
    });
    if (!resp.ok) {
      console.warn('[MyDay] Inherited todo toggle failed:', resp.status);
      item.done = !newDone;
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Inherited todo toggle error:', e);
    item.done = !newDone;
    _mydayRenderTasks(cached);
  }
}

/* ═══════ Toggle stream status (cycle: in_progress → done → blocked → in_progress) ═══════ */
async function _mydayToggleStreamStatus(streamId) {
  if (!streamId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.streams) return;

  const stream = cached.streams.find(s => s.id === streamId);
  if (!stream) return;
  const oldStatus = stream.status;
  const curIdx = _STATUS_CYCLE.indexOf(oldStatus);
  const newStatus = _STATUS_CYCLE[(curIdx + 1) % _STATUS_CYCLE.length];

  // Optimistic update
  stream.status = newStatus;
  if (newStatus === 'done') stream.remaining = null;
  stream._manual = true;
  _mydayRenderTasks(cached);

  try {
    const resp = await fetch(apiUrl('/api/daily-report/task-status'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, stream_id: streamId, status: newStatus }),
    });
    if (!resp.ok) {
      console.warn('[MyDay] Stream status toggle failed:', resp.status);
      stream.status = oldStatus;
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Stream status toggle error:', e);
    stream.status = oldStatus;
    _mydayRenderTasks(cached);
  }
  _mydayRenderCalendar();
}

/* ═══════ Toggle tomorrow TODO checkbox ═══════ */
async function _mydayToggleTodo(todoId) {
  if (!todoId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.tomorrow) return;

  const item = cached.tomorrow.find(t => t.id === todoId);
  if (!item) return;
  const newDone = !item.done;

  // Optimistic update
  item.done = newDone;
  _mydayRenderTasks(cached);

  try {
    const resp = await fetch(apiUrl('/api/daily-report/todo-toggle'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, todo_id: todoId, done: newDone }),
    });
    if (!resp.ok) {
      console.warn('[MyDay] Todo toggle failed:', resp.status);
      item.done = !newDone;
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Todo toggle error:', e);
    item.done = !newDone;
    _mydayRenderTasks(cached);
  }
}

/* ═══════ Delete a tomorrow TODO item ═══════ */
async function _mydayDeleteTodo(todoId) {
  if (!todoId) return;
  const dateStr = _myday.selectedDateStr;
  if (!dateStr) return;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.tomorrow) return;

  // Optimistic removal
  const idx = cached.tomorrow.findIndex(t => t.id === todoId);
  if (idx === -1) return;
  const removed = cached.tomorrow.splice(idx, 1)[0];
  _mydayRenderTasks(cached);

  try {
    const resp = await fetch(apiUrl('/api/daily-report/task'), {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, task_id: todoId }),
    });
    if (!resp.ok) {
      console.warn('[MyDay] Delete todo failed:', resp.status);
      cached.tomorrow.splice(idx, 0, removed);
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Delete todo error:', e);
    cached.tomorrow.splice(idx, 0, removed);
    _mydayRenderTasks(cached);
  }
  _mydayRenderCalendar();
}

/* ═══════ Delete an inherited TODO item (cross-day) ═══════ */
async function _mydayDeleteInheritedTodo(todoId, originDate) {
  if (!todoId || !originDate) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.today_todos) return;

  // Optimistic removal from today's inherited list
  const idx = cached.today_todos.findIndex(t => t.id === todoId);
  if (idx === -1) return;
  const removed = cached.today_todos.splice(idx, 1)[0];
  _mydayRenderTasks(cached);

  try {
    const resp = await fetch(apiUrl('/api/daily-report/inherited-todo'), {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ origin_date: originDate, todo_id: todoId }),
    });
    if (!resp.ok) {
      console.warn('[MyDay] Delete inherited todo failed:', resp.status);
      cached.today_todos.splice(idx, 0, removed);
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Delete inherited todo error:', e);
    cached.today_todos.splice(idx, 0, removed);
    _mydayRenderTasks(cached);
  }
  _mydayRenderCalendar();
}

/* ═══════ Add manual TODO task ═══════ */
async function _mydayAddTodo() {
  const input = document.getElementById('mydayTodoInput');
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const dateStr = _myday.selectedDateStr;
  if (!dateStr) return;

  try {
    const resp = await fetch(apiUrl('/api/daily-report/task'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, task: text, status: 'incomplete' }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.report) {
      data.report._full = true;
      _myday.cache[dateStr] = data.report;
      _mydayRenderTasks(data.report);
    }
  } catch (e) {
    console.warn('[MyDay] Add task failed:', e);
  }
  _mydayRenderCalendar();
}

/* ═══════ Delete manual TODO task ═══════ */
async function _mydayDeleteTask(taskId) {
  const dateStr = _myday.selectedDateStr;
  if (!dateStr || !taskId) return;

  try {
    const resp = await fetch(apiUrl('/api/daily-report/task'), {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, task_id: taskId }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.report) {
      data.report._full = true;
      _myday.cache[dateStr] = data.report;
      _mydayRenderTasks(data.report);
    }
  } catch (e) {
    console.warn('[MyDay] Delete task failed:', e);
  }
  _mydayRenderCalendar();
}

/* ═══════ Legacy manual todo status toggle (used by old-format reports) ═══════ */

/* ═══════ Status toggle (done ↔ incomplete) ═══════ */
async function _mydayToggleStatus(convId) {
  if (!convId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.tasks) return;

  const task = cached.tasks.find(t => t.conv_id === convId || t.id === convId);
  if (!task) return;
  const oldStatus = task.status;
  const newStatus = (oldStatus === 'done') ? 'incomplete' : 'done';

  // Optimistic update
  task.status = newStatus;
  task._manual = true;
  _mydayRenderTasks(cached);

  // Persist to server
  const isTodo = convId.startsWith('todo-');
  const body = { date: dateStr, status: newStatus };
  if (isTodo) body.task_id = convId;
  else body.conv_id = convId;

  try {
    const resp = await fetch(apiUrl('/api/daily-report/task-status'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      console.warn('[MyDay] Status toggle failed:', resp.status);
      task.status = oldStatus;
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Status toggle error:', e);
    task.status = oldStatus;
    _mydayRenderTasks(cached);
  }

  _mydayRenderCalendar();
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
  if (totalConvs) parts.push(`${totalConvs} 对话`);
  parts.push(`${streams.length} 工作流`);
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
    todayHtml += `<div class="myday-section-label">今日待办 <span class="myday-section-count">${todayDoneCount}/${todayTodos.length}</span></div>`;
    todayHtml += '<div class="myday-today-todos">';
    for (const item of todayTodos) todayHtml += _mydayInheritedTodoRow(item);
    todayHtml += '</div>';
  }

  container.innerHTML = `
    ${todayHtml}
    <div class="myday-empty">
      ${tofuSvg}
      <div class="myday-empty-title">${msg || '这天很安静'}</div>
      <div class="myday-empty-hint">没有找到对话记录</div>
    </div>
    <div class="myday-add-todo">
      <button class="myday-add-btn" onclick="document.getElementById('mydayTodoInput').focus()" title="添加">＋</button>
      <input type="text" class="myday-todo-input" id="mydayTodoInput" placeholder="添加待办…"
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
