/* ═══════════════════════════════════════════════════════════════════
   conversation list — extracted from ui.js (split 2026-05-28)

   Conversation list rendering — sidebar conv list, folder tabs, search.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function stripNoTranslateTags(text) {
  if (!text) return text;
  return text
    .replace(/<\/?notranslate>/gi, '')
    .replace(/<\/?nt>/gi, '')
    .replace(/[⟦\[\(\{【〔《「『]\s*N\s*T\s*_\s*[0-9０-９]+\s*[⟧\]\)\}】〕》」』]/gi, '');
}

/**
 * Targeted single-message PATCH. Sends only the whitelisted keys in `patch`
 * to the server so chatInner actions (edit-only, translate toggle,
 * translation completion) don't fall back to the full-conversation PUT.
 *
 * @param {string} convId       Conversation id.
 * @param {number} msgIdx       Message index within conv.messages.
 * @param {object} patch        Partial message — only whitelisted keys are
 *                              accepted by the server. A literal `null`
 *                              value removes that key from the message.
 * @param {object} [opts]
 * @param {function} [opts.onError]  Callback on server error, receives the
 *                                   parsed error body.
 * @returns {Promise<object|null>}  Server response `{ok, msgCount, msg}` or
 *                                  null on failure.
 */
async function _patchMessageOnServer(convId, msgIdx, patch, /** @type {any} */ opts = {}) {
  if (!convId || msgIdx == null || !patch || Object.keys(patch).length === 0) return null;
  // Prefer stable id addressing when the message has _msgId. The conv is
  // expected on the global `conversations` array; if a caller has only the
  // index, the legacy index endpoint is still served. opts.msgId overrides
  // the lookup (useful when the caller has the id but no live `messages`).
  let msgId = opts.msgId || null;
  if (!msgId && typeof conversations !== 'undefined') {
    try {
      const c = conversations.find(x => x.id === convId);
      const m = c && c.messages && c.messages[msgIdx];
      if (m && m._msgId) msgId = m._msgId;
    } catch (_e) { /* ignore */ }
  }
  const result = await Api.conversations.patchMessage(convId, msgIdx, patch, { msgId });
  if (result && result._error) {
    console.warn('[patchMsg] conv=%s idx=%d msgId=%s status=%d body=%o',
      convId, msgIdx, (msgId || '').slice(0, 8) || '-', result._status, result._body);
    if (typeof opts.onError === 'function') opts.onError(result._body, result._status);
    return null;
  }
  return result;
}

function formatConvTime(ts) {
  if (!ts) return "";
  const d = new Date(ts),
    now = new Date(),
    pad = (n) => String(n).padStart(2, "0");
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const isToday = d.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  let datePart;
  if (isToday) datePart = "Today";
  else if (isYesterday) datePart = "Yesterday";
  else {
    const sameYear = d.getFullYear() === now.getFullYear();
    const months = [
      "Jan",
      "Feb",
      "Mar",
      "Apr",
      "May",
      "Jun",
      "Jul",
      "Aug",
      "Sep",
      "Oct",
      "Nov",
      "Dec",
    ];
    datePart = `${months[d.getMonth()]} ${d.getDate()}${sameYear ? "" : ", " + d.getFullYear()}`;
  }
  return `<span class="conv-date-text">${datePart}</span><span class="conv-date-sep">·</span><span class="conv-date-time">${time}</span>`;
}

/* ── Date grouping for the conversation list ──
 * Rows are sorted recency-first (_convSorter), so each date bucket is a
 * contiguous run. Each bucket gets a clickable header that folds its rows.
 * The ">30 days" ("older") bucket starts collapsed. */
const _CONV_OLDER_DAYS = 30;
/** Set of date-group keys the user has collapsed. "older" starts collapsed. */
const _collapsedConvGroups = new Set(['older']);

/** Classify a timestamp into a date-group key. */
function _convDateGroupKey(ts) {
  if (!ts) return 'older';
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const dayMs = 86400000;
  if (ts >= startToday) return 'today';
  if (ts >= startToday - dayMs) return 'yesterday';
  if (ts >= startToday - 6 * dayMs) return 'prev7';
  if (ts >= startToday - (_CONV_OLDER_DAYS - 1) * dayMs) return 'prev30';
  return 'older';
}

const _CONV_GROUP_I18N = {
  today: 'sidebar.dateToday',
  yesterday: 'sidebar.dateYesterday',
  prev7: 'sidebar.datePrev7',
  prev30: 'sidebar.datePrev30',
  older: 'sidebar.dateOlder',
};

/** Collapsible section-header markup for a date-group key + its row count. */
function _convGroupHeaderHtml(key, count, collapsed) {
  const label = t(_CONV_GROUP_I18N[key] || key);
  const chevron = `<svg class="conv-date-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
  return `<button type="button" class="conv-date-group${collapsed ? ' collapsed' : ''}" data-group="${key}" onclick="_toggleConvGroup('${key}')">` +
    `${chevron}<span class="conv-date-label">${label}</span><span class="conv-date-count">${count}</span></button>`;
}

/** Toggle a date group's collapsed state and re-render. */
function _toggleConvGroup(key) {
  if (_collapsedConvGroups.has(key)) _collapsedConvGroups.delete(key);
  else _collapsedConvGroups.add(key);
  _lastConvListHash = "";          // force a full rebuild past the hash guard
  renderConversationList();
}

let _lastConvListHash = "";
let _lastConvStructHash = "";        // struct part of the split hash (row identity/order/title/date/folder)
let _lastRenderedSearchQuery = "";   // guard: skip background re-renders in search mode

/* ★ PERF: Fast-path for conversation switch — instead of rebuilding the
 * entire sidebar from scratch (O(N) HTML generation + innerHTML assignment),
 * just move the .active class between two DOM elements (O(1)).
 * Returns true if the fast-path was sufficient, false if a full rebuild is needed. */
let _lastActiveConvId = null;
function _swapActiveConvItem(newActiveId) {
  if (sidebarSearchQuery) return false; // search mode — need full rebuild
  const oldId = _lastActiveConvId;
  /* No-op only if the DOM already reflects the active state. A prior
   * hash-skipped render can leave _lastActiveConvId pointing at a conv
   * whose .active class was never applied (or applied to the wrong row),
   * which is what makes the active indicator dot + status tag silently
   * disappear. Verify the target row actually carries .active before
   * trusting the cache; otherwise fall through and re-apply it. */
  if (oldId === newActiveId) {
    if (!newActiveId) return true;
    const cur = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(newActiveId)}"]`);
    if (cur && cur.classList.contains('active')) return true;
    if (!cur) return false; // not in DOM yet — need full rebuild
    document.querySelectorAll('.conv-item.active').forEach(el => {
      if (el !== cur) el.classList.remove('active');
    });
    cur.classList.add('active');
    _lastConvListHash = "";
    return true;
  }
  /* Locate the new row FIRST — if it isn't in the DOM yet we must NOT
   * mutate any state (neither _lastActiveConvId nor the old row's class),
   * otherwise the subsequent renderConversationList() can early-return on
   * a stale hash and leave the sidebar with no active row at all. */
  if (newActiveId) {
    const newEl = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(newActiveId)}"]`);
    if (!newEl) {
      /* New conv not in DOM yet — need a full rebuild. Force the hash to
       * miss so the caller's renderConversationList() actually repaints. */
      _lastConvListHash = "";
      return false;
    }
    /* Clear .active from every currently-active row (defensive: there
     * should be exactly one, but a desynced cache may have left several),
     * then activate the target. */
    document.querySelectorAll('.conv-item.active').forEach(el => {
      if (el !== newEl) el.classList.remove('active');
    });
    newEl.classList.add('active');
  } else if (oldId) {
    const oldEl = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(oldId)}"]`);
    if (oldEl) oldEl.classList.remove('active');
  }
  _lastActiveConvId = newActiveId;
  /* Invalidate the hash so a subsequent full renderConversationList()
   * won't skip due to stale hash (the hash includes active state). */
  _lastConvListHash = "";
  return true;
}

/* ── Folder tab bar ── */
let _lastFolderTabsHash = '';
let _lastFolderTabsContentHash = '';
let _lastFolderTabsStructHash = '';
let _folderTabsExpanded = false;



function renderFolderTabs(folders, activeFolderId, allConvs) {
  const tabsEl = document.getElementById('folderTabs');
  if (!tabsEl) return;
  try {
    _renderFolderTabsInner(tabsEl, folders, activeFolderId, allConvs);
  } catch (e) {
    console.error('[renderFolderTabs] Error:', e);
    // On error, ensure tabs aren't left in broken state — render minimal fallback
    try { tabsEl.innerHTML = '<div class="folder-tabs-scroll"><button class="folder-tab folder-tab-add" title="New folder"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button></div>'; } catch(_) {}
  }
}

function _renderFolderTabsInner(tabsEl, folders, activeFolderId, allConvs) {
  // Always show tabs — even with 0 folders, show just the "+" button for discoverability
  tabsEl.style.display = '';

  const safeFolders = folders || [];
  const safeConvs = allConvs || [];

  // Compute counts per folder + uncategorized
  const folderIds = new Set(safeFolders.map(f => f.id));
  const countMap = {};
  let uncategorizedCount = 0;
  for (const c of safeConvs) {
    if (c.folderId && folderIds.has(c.folderId)) {
      countMap[c.folderId] = (countMap[c.folderId] || 0) + 1;
    } else {
      uncategorizedCount++;
    }
  }

  // Compute latest activity time per folder for sorting
  const lastActiveMap = {};
  for (const c of safeConvs) {
    if (c.folderId && folderIds.has(c.folderId)) {
      const ts = c.updatedAt || c.createdAt || 0;
      if (!lastActiveMap[c.folderId] || ts > lastActiveMap[c.folderId]) {
        lastActiveMap[c.folderId] = ts;
      }
    }
  }

  // Detect which folders have actively streaming/generating conversations
  const streamingFolderIds = new Set();
  for (const c of safeConvs) {
    if (!c.folderId || !folderIds.has(c.folderId)) continue;
    let isStreaming = (typeof activeStreams !== 'undefined' && activeStreams.has(c.id)) || c.activeTaskId || c._translating || c._memoryPrefetching;
    if (!isStreaming && typeof activeStreams !== 'undefined') {
      const prefix = c.id + ':';
      for (const k of activeStreams.keys()) { if (k.startsWith(prefix)) { isStreaming = true; break; } }
    }
    if (isStreaming) streamingFolderIds.add(c.folderId);
  }

  // Split hash: content hash (folders/counts/names) vs active-tab hash
  // When only the active tab changes, skip full DOM rebuild and just swap .active class
  const streamKey = [...streamingFolderIds].sort().join(',');
  const structHash = `U${uncategorizedCount}|${safeFolders.map(f=>`${f.id}|${f.name}|${f.color||''}|${lastActiveMap[f.id]||0}|${countMap[f.id]||0}`).join(',')}`;
  const contentHash = `${structHash}|S${streamKey}`;
  const fullHash = `${activeFolderId||''}|${contentHash}`;
  if (fullHash === _lastFolderTabsHash) return;

  const contentChanged = contentHash !== _lastFolderTabsContentHash;
  const structChanged = structHash !== _lastFolderTabsStructHash;
  _lastFolderTabsHash = fullHash;
  _lastFolderTabsContentHash = contentHash;
  _lastFolderTabsStructHash = structHash;

  // Fast path: only active tab and/or streaming state changed — update classes in-place, no DOM rebuild
  if (!structChanged) {
    const btns = tabsEl.querySelectorAll('.folder-tab[data-folder-id]');
    btns.forEach(btn => {
      const fid = btn.dataset.folderId;
      btn.classList.toggle('active', fid === (activeFolderId || ''));
      const dot = btn.querySelector('.folder-tab-dot');
      if (dot) dot.classList.toggle('streaming', streamingFolderIds.has(fid));
    });
    return;
  }

  const sortedFolders = [...safeFolders].sort((a, b) => (lastActiveMap[b.id] || 0) - (lastActiveMap[a.id] || 0) || (a.order || 0) - (b.order || 0));

  let html = '';
  html += '<div class="folder-tabs-scroll';
  // Preserve expanded state synchronously to avoid collapse→expand flash
  if (_folderTabsExpanded) html += ' expanded';
  html += '">';
  // "未分类" tab — shows conversations not in any folder (only when folders exist)
  if (sortedFolders.length > 0) {
    const ucBadge = uncategorizedCount > 0 ? `<span class="folder-tab-count">${uncategorizedCount}</span>` : '';
    html += `<button class="folder-tab${!activeFolderId ? ' active' : ''}" data-folder-id="">`;
    html += `<svg class="folder-tab-inbox-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;
    html += `<span class="folder-tab-name">${t('sidebar.uncategorized')}</span>${ucBadge}</button>`;
  }
  // Folder tabs
  for (const f of sortedFolders) {
    const fcolor = f.color ? escapeHtml(f.color) : 'var(--accent)';
    const fname = escapeHtml(f.name);
    const isActive = activeFolderId === f.id;
    const cnt = countMap[f.id] || 0;
    const badge = cnt > 0 ? `<span class="folder-tab-count">${cnt}</span>` : '';
    html += `<button class="folder-tab${isActive ? ' active' : ''}" data-folder-id="${escapeHtml(f.id)}" title="${fname}">`;
    const dotStreaming = streamingFolderIds.has(f.id) ? ' streaming' : '';
    html += `<span class="folder-tab-dot${dotStreaming}" style="background:${fcolor}"></span>`;
    html += `<span class="folder-tab-name">${fname}</span>${badge}`;
    html += `</button>`;
  }
  // "+" add tab — always visible
  html += `<button class="folder-tab folder-tab-add" title="${t('sidebar.newFolder')}">`;
  html += `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
  html += `</button>`;
  html += '</div>';
  // Expand/collapse toggle (hidden by default, shown via CSS when overflow detected)
  html += `<button class="folder-tabs-toggle">`;
  html += `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
  html += `<span class="folder-tabs-toggle-label"></span>`;
  html += `</button>`;
  tabsEl.innerHTML = html;

  // Check overflow after render — if content exceeds visible height, show toggle & count hidden
  requestAnimationFrame(() => {
    const scrollEl = tabsEl.querySelector('.folder-tabs-scroll');
    if (!scrollEl) return;
    const isOverflow = scrollEl.scrollHeight > scrollEl.clientHeight + 2;
    tabsEl.classList.toggle('has-overflow', isOverflow);
    // Count how many real folder tabs are hidden (below the fold)
    if (isOverflow) {
      const label = tabsEl.querySelector('.folder-tabs-toggle-label');
      if (label) {
        if (_folderTabsExpanded) {
          label.textContent = t('sidebar.lessFolders');
        } else {
          const collapsedMax = 94; // matches CSS max-height (3 rows)
          // Only count real folder tabs, exclude the "+" add button
          const tabs = scrollEl.querySelectorAll('.folder-tab:not(.folder-tab-add)');
          let hiddenCount = 0;
          tabs.forEach(tab => {
            if (tab.offsetTop + tab.offsetHeight > collapsedMax) hiddenCount++;
          });
          label.textContent = hiddenCount > 0 ? `+${hiddenCount}` : '';
        }
      }
    }
  });
}

/* ═══════════════════════════════════════════════════════════════════
 * ★ PERF: Windowed conversation-list rendering.
 *
 * Rendering all N conversation rows synchronously (innerHTML of the full
 * `filtered` set) was the dominant cold-load long-task: with a couple
 * thousand convs it produced >2000 DOM children, ~65k total page nodes,
 * 700ms+ of forced reflow, and starved LCP. Instead we render only the
 * first page up-front and append further pages as a bottom sentinel
 * scrolls into the #convList viewport.
 *
 * Correctness contract with the three fast paths in renderConversationList:
 *   • The active conv is always forced into the FIRST page (see
 *     _renderConvWindow) so its .active indicator / status dot never
 *     depends on a row that hasn't been scrolled into existence.
 *   • The status-only fast path SKIPS rows not currently in the DOM
 *     (windowed-out) instead of bailing to a full rebuild — windowed rows
 *     read live status via _buildConvItemHTML when later appended.
 *   • _swapActiveConvItem already falls back to a full rebuild when its
 *     target row isn't found, which re-windows from the top.
 * ═══════════════════════════════════════════════════════════════════ */
const _CONV_WINDOW_PAGE = 50;          // rows rendered per page
const _CONV_WINDOW_PREFETCH_PX = 600;  // append the next page this far before the sentinel is reached
let _convVirtual = { observer: null, sentinel: null };

/** Disconnect any active windowing observer and drop the sentinel ref. */
function _teardownConvVirtual() {
  if (_convVirtual.observer) {
    try { _convVirtual.observer.disconnect(); } catch (_e) { /* ignore */ }
    _convVirtual.observer = null;
  }
  _convVirtual.sentinel = null;
}

/**
 * Build the render plan for `filtered`: a flat list of items, each either a
 * date-group header, a conversation row, or the collapsed-"older" toggle.
 * `filtered` is recency-sorted so each date bucket is one contiguous run.
 * The ">30 days" bucket is collapsed behind a toggle unless expanded (or the
 * active conv lives in it, or it's the ONLY populated group — in those cases
 * it's force-expanded so its rows are always visible and clickable).
 *
 * @returns {Array<{type:'header'|'conv'|'older'|'older-collapse', key?:string, conv?:object, count?:number}>}
 */
function _buildConvPlan(filtered) {
  /* Per-group row counts so each header shows its size. */
  const counts = {};
  for (const c of filtered) {
    const key = _convDateGroupKey(c.updatedAt || c.createdAt);
    counts[key] = (counts[key] || 0) + 1;
  }
  /* Keep the active conv's group always expanded so its row never hides. */
  const activeKey = activeConvId
    ? (() => { const a = filtered.find(c => c.id === activeConvId); return a ? _convDateGroupKey(a.updatedAt || a.createdAt) : null; })()
    : null;

  /* ★ Never auto-collapse the ONLY populated group. The "older" (>30d) bucket
   * starts collapsed, but for a user whose conversations are ALL older than 30
   * days that would hide every row behind a single collapsed header — the
   * sidebar looks empty and clicks land on nothing (no .conv-item is rendered).
   * When a single group holds all the rows, force it expanded regardless of
   * its remembered collapsed state, so there is always something to click. */
  const _soleGroupKey = Object.keys(counts).length === 1 ? Object.keys(counts)[0] : null;

  /** @type {Array<{type:'header'|'conv', key?:string, count?:number, collapsed?:boolean, conv?:object}>} */
  const plan = [];
  let curGroup = null;
  let curCollapsed = false;
  for (const c of filtered) {
    const key = _convDateGroupKey(c.updatedAt || c.createdAt);
    if (key !== curGroup) {
      curCollapsed = _collapsedConvGroups.has(key) && key !== activeKey && key !== _soleGroupKey;
      plan.push({ type: 'header', key, count: counts[key], collapsed: curCollapsed });
      curGroup = key;
    }
    if (curCollapsed) continue;   // rows hidden under a collapsed header
    plan.push({ type: 'conv', conv: c });
  }
  return plan;
}

/** Render a single plan item to HTML. */
function _planItemHtml(item) {
  if (item.type === 'header') return _convGroupHeaderHtml(item.key, item.count, item.collapsed);
  const c = item.conv;
  return _buildConvItemHTML(c, escapeHtml(stripNoTranslateTags(c.title)), "");
}

/**
 * Render `filtered` into `listEl` with bottom-sentinel windowing. Renders
 * the first page (extended downward if needed so the active conv is always
 * included), then lazily appends subsequent pages on scroll. Rows are grouped
 * under date-section headers via the render plan from _buildConvPlan().
 */
function _renderConvWindow(listEl, filtered) {
  _teardownConvVirtual();

  const plan = _buildConvPlan(filtered);

  /* Ensure the active row is within the initial window so its .active
   * class + status dot are present immediately (sorted recent-first means
   * this is almost always near the top, but a click on an old conv can be
   * deep). Index is into the PLAN (headers shift positions). */
  let firstEnd = _CONV_WINDOW_PAGE;
  if (activeConvId) {
    const ai = plan.findIndex(it => it.type === 'conv' && it.conv.id === activeConvId);
    if (ai >= firstEnd) firstEnd = ai + 1;
  }
  firstEnd = Math.min(firstEnd, plan.length);

  let html = "";
  for (let i = 0; i < firstEnd; i++) {
    html += _planItemHtml(plan[i]);
  }
  listEl.innerHTML = html;

  /* Everything fits in the first window — no sentinel/observer needed. */
  if (firstEnd >= plan.length) return;

  let cursor = firstEnd;
  const sentinel = document.createElement('div');
  sentinel.className = 'conv-window-sentinel';
  sentinel.setAttribute('aria-hidden', 'true');
  listEl.appendChild(sentinel);
  _convVirtual.sentinel = sentinel;

  const obs = new IntersectionObserver((entries) => {
    /* Ignore stale callbacks from a sentinel that's been torn down. */
    if (_convVirtual.sentinel !== sentinel) return;
    if (!entries.some(e => e.isIntersecting)) return;

    const end = Math.min(cursor + _CONV_WINDOW_PAGE, plan.length);
    let frag = "";
    for (let i = cursor; i < end; i++) {
      frag += _planItemHtml(plan[i]);
    }
    sentinel.insertAdjacentHTML('beforebegin', frag);
    cursor = end;

    if (cursor >= plan.length) {
      _teardownConvVirtual();
      return;
    }
    /* The sentinel may still be inside the prefetch zone after the append
     * (true→true gives no new callback). Re-observe on the next frame to
     * force a fresh intersection check so paging chains until the sentinel
     * is pushed below the prefetch margin. */
    obs.unobserve(sentinel);
    requestAnimationFrame(() => {
      if (_convVirtual.sentinel === sentinel && _convVirtual.observer === obs) {
        obs.observe(sentinel);
      }
    });
  }, { root: listEl, rootMargin: `0px 0px ${_CONV_WINDOW_PREFETCH_PX}px 0px` });

  obs.observe(sentinel);
  _convVirtual.observer = obs;
}

function renderConversationList() {
  const listEl = document.getElementById("convList"),
    statsEl = document.getElementById("sidebarSearchStats");
  if (!sidebarSearchQuery) {
    const _wasSearching = !!_lastRenderedSearchQuery;
    _lastRenderedSearchQuery = "";   // reset when exiting search mode
    statsEl.classList.remove("visible");
    const all = conversations.filter((c) => c.messages.length > 0 || (c._serverMsgCount || 0) > 0 || c._needsLoad);

    const folders = typeof getFolders === 'function' ? getFolders() : [];
    const _activeFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    const foldersReady = typeof areFoldersLoaded === 'function' ? areFoldersLoaded() : true;

    const folderHash = folders.map(f => `${f.id}|${f.name}|${f.order}|${f.color||''}`).join(",");
    /* ── Render folder tabs (always, regardless of hash — tab visibility may change) ── */
    renderFolderTabs(folders, _activeFolderId, all);

    /* ── Filter by active folder tab (done BEFORE hashing so the hash and the
     *    in-place fast-path both operate on the actually-visible row set) ── */
    let filtered = all;
    if (_activeFolderId) {
      // Specific folder selected — show only its conversations
      const activeFolder = folders.find(f => f.id === _activeFolderId);
      if (!activeFolder) { // folder was deleted while viewing it
        if (typeof setActiveFolderId === 'function') setActiveFolderId(null);
        return;
      }
      filtered = all.filter(c => c.folderId === _activeFolderId);
    } else if (folders.length > 0) {
      // Default "未分类" view — show only conversations NOT in any folder
      const folderIds = new Set(folders.map(f => f.id));
      filtered = all.filter(c => !c.folderId || !folderIds.has(c.folderId));
    } else if (!foldersReady) {
      // Folders not yet loaded — filter out conversations that have a folderId
      // from server settings to avoid flashing them in uncategorized view
      filtered = all.filter(c => !c.folderId);
    }
    // else: folders loaded and empty — show everything (no folders exist)

    /* ── Split hash: struct (row identity/order/title/date/folder) vs status
     *    (active / streaming / translating / memory-prefetch / awaiting-human).
     *    When only status changed we patch each row's .active class + dot +
     *    status tag IN PLACE — no innerHTML rebuild, no full reparse/relayout
     *    of the sidebar (the dominant long-task cost during a send's
     *    translate→stream→done lifecycle). Mirrors the folder-tab fast path. ── */
    const _structHash = `AF${_activeFolderId||''}|FL${foldersReady?1:0}|CG${[..._collapsedConvGroups].sort().join('.')}|F${folderHash}|` +
      filtered.map(c => `${c.id}|${c.title}|${c.updatedAt||""}|${c.folderId||""}`).join("\n");
    const _statusHash = filtered.map(c => {
      const f = _convStatusFlags(c);
      return `${c.id===activeConvId?1:0}${f.streaming?1:0}${f.translating?1:0}${f.memoryPrefetching?1:0}${f.awaitingHuman?1:0}`;
    }).join(",");
    const _fullHash = `${_structHash}|||${_statusHash}`;
    if (_fullHash === _lastConvListHash) return;

    /* Coming out of search mode the DOM holds search-result rows (different
     * set/order/snippets) — force a full rebuild even if struct hash matches. */
    if (_wasSearching) _lastConvStructHash = "\u0000force-rebuild";
    const _structChanged = _structHash !== _lastConvStructHash;
    _lastConvListHash = _fullHash;
    _lastConvStructHash = _structHash;

    /* ── Fast path: only status changed → patch existing rows in place. ── */
    if (!_structChanged && filtered.length > 0 &&
        listEl.firstElementChild && !listEl.querySelector('.folder-view-empty')) {
      for (const c of filtered) {
        const row = listEl.querySelector(`.conv-item[data-conv-id="${CSS.escape(c.id)}"]`);
        /* Windowed rows not yet scrolled into view aren't in the DOM — skip
         * them (they pick up live status when appended) rather than bailing
         * to a full rebuild on every status tick. Struct-hash equality
         * guarantees the rendered rows are an exact prefix of `filtered`,
         * so a missing row always means "windowed out", never "desynced". */
        if (!row) continue;
        _applyConvItemStatus(row, c);
      }
      /* Keep _lastActiveConvId in sync so _swapActiveConvItem stays O(1). */
      _lastActiveConvId = activeConvId;
      return;
    }

    let listHtml = null;  // non-null only for the empty / special states below

    /* ── Empty state ── */
    if (filtered.length === 0 && (_activeFolderId || folders.length > 0)) {
      const isUncategorized = !_activeFolderId;
      const emptyIcon = isUncategorized
        ? `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;margin-bottom:8px"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`
        : `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;margin-bottom:8px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
      const emptyText = isUncategorized ? t('sidebar.allCategorized') : t('sidebar.folderEmpty');
      const emptyHint = isUncategorized
        ? t('sidebar.newChatAppear')
        : t('sidebar.clickNewChat');
      listHtml = `<div class="folder-view-empty">${emptyIcon}` +
        `<div style="font-size:12px;color:var(--text-tertiary)">${emptyText}</div>` +
        `<div style="font-size:11px;color:var(--text-tertiary);opacity:0.6;margin-top:4px">${emptyHint}</div>` +
        `</div>`;
    }

    /* ── Render: the empty / special state goes through a plain innerHTML
     *    assignment; the normal (possibly large) list is windowed so the DOM
     *    node count and synchronous build cost stay bounded regardless of how
     *    many thousands of conversations exist. ── */
    if (listHtml !== null) {
      _teardownConvVirtual();
      listEl.innerHTML = listHtml;
    } else {
      _renderConvWindow(listEl, filtered);
    }
    /* ★ Keep _lastActiveConvId in sync after a full rebuild so
     * _swapActiveConvItem can do O(1) swaps on subsequent switches. */
    _lastActiveConvId = activeConvId;
  } else {
    const query = sidebarSearchQuery;

    /* ── Guard: skip background re-renders while search results are shown ──
     * Background triggers (60s server poll, streaming saves, cross-tab sync,
     * visibilitychange) call renderConversationList() even during an active
     * search.  Without this guard, every background call would:
     *   1. flash the DOM with title-only partial results
     *   2. fire a NEW /api/conversations/search HTTP request
     *   3. re-render merged results when the response arrives
     * causing the sidebar to visibly "auto-refresh" in a loop.
     * Fix: once search results for a query are rendered, skip re-rendering
     * until the user actually changes the query (which resets this via the
     * input handler calling renderConversationList with a new sidebarSearchQuery). */
    if (query === _lastRenderedSearchQuery) return;
    _lastRenderedSearchQuery = query;

    // Phase 1: instant title matches (local, ~0 ms)
    const titleHits = searchByTitle(query);
    _renderSearchResults(titleHits, query, listEl, statsEl, true);

    // Phase 2: async content/thinking search (server)
    const seq = ++_searchSeq;
    searchByContent(query, seq).then(contentHits => {
      if (contentHits === null) return;           // stale or aborted
      if (sidebarSearchQuery !== query) return;   // user typed more

      // merge: title hits + content hits (deduplicate by conv id)
      const seen = new Set(titleHits.map(h => h.conv.id));
      const merged = [...titleHits];
      for (const h of contentHits) {
        if (!seen.has(h.conv.id)) { merged.push(h); seen.add(h.conv.id); }
      }
      _renderSearchResults(merged, query, listEl, statsEl, false);
    });
  }
}

function _renderSearchResults(results, query, listEl, statsEl, isPartial) {
  /* Search replaces the whole list DOM — stop any list-mode windowing
   * observer so it can't append conv rows into the search results. */
  _teardownConvVirtual();
  statsEl.classList.add("visible");
  const suffix = isPartial ? ' <span class="search-loading">searching…</span>' : "";
  statsEl.innerHTML = `${results.length} result${results.length !== 1 ? "s" : ""}${suffix}`;
  if (results.length === 0 && isPartial) {
    listEl.innerHTML = `<div class="sidebar-search-empty"><div class="sidebar-search-empty-icon"></div>Searching…</div>`;
    _lastConvListHash = "";
    return;
  }
  if (results.length === 0) {
    listEl.innerHTML = `<div class="sidebar-search-empty"><div class="sidebar-search-empty-icon"></div>No matches for "<strong>${escapeHtml(query)}</strong>"</div>`;
    _lastConvListHash = "";
    return;
  }
  const items = results.map(
    ({ conv: c, matchField, matchSnippet, matchRole }) => {
      const tHtml =
        matchField === "title"
          ? highlightMatch(c.title, query)
          : escapeHtml(c.title);
      let snip = "";
      if (matchSnippet) {
        const ico = "";
        /* ID match: no role prefix ("You:"/"Claude:") — snippet is the ID itself. */
        if (matchField === "id") {
          snip = `<div class="conv-item-snippet">${highlightMatch(matchSnippet, query)}</div>`;
        } else {
          const rl = matchRole === "user" ? "You" : "Claude";
          snip = `<div class="conv-item-snippet">${ico} ${rl}: ${highlightMatch(matchSnippet, query)}</div>`;
        }
      }
      return _buildConvItemHTML(c, tHtml, snip);
    },
  );
  const newHtml = items.join("");
  if (newHtml === _lastConvListHash) return;
  _lastConvListHash = newHtml;
  listEl.innerHTML = newHtml;
}

/* ★ PERF: static action-button SVGs hoisted to module scope — these never
 * change per row, so building them once instead of per-conv shrinks the
 * per-item string work on every full rebuild. */
const _CONV_DEL_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
const _CONV_CP_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const _CONV_DUP_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="14" height="14" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
const _CONV_FOLDER_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
const _CONV_RENAME_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>`;

/**
 * Compute the four mutually-relevant status flags for a conversation row.
 * Shared by the status-hash, full-rebuild HTML, and in-place patch paths so
 * all three agree on exactly when a dot / tag should show.
 *
 * @param {Object} c — conversation object
 * @returns {{streaming:boolean, translating:boolean, memoryPrefetching:boolean, awaitingHuman:boolean}}
 */
function _convStatusFlags(c) {
  const translating = !!c._translating;
  const memoryPrefetching = !!c._memoryPrefetching;
  let streaming = activeStreams.has(c.id) || !!c.activeTaskId;
  if (!streaming) {
    const prefix = c.id + ":";
    for (const k of activeStreams.keys()) { if (k.startsWith(prefix)) { streaming = true; break; } }
  }
  // ★ Awaiting human input — scan back to the most recent assistant message
  //   with toolRounds (breaks early; typically inspects only the tail).
  let awaitingHuman = false;
  if (c.messages) {
    for (let i = c.messages.length - 1; i >= 0; i--) {
      const m = c.messages[i];
      if (m.role === 'assistant' && m.toolRounds) {
        for (const r of m.toolRounds) {
          if (r.status === 'awaiting_human') { awaitingHuman = true; break; }
        }
        break;  // only the latest assistant turn carries a live awaiting-human round
      }
    }
  }
  return { streaming, translating, memoryPrefetching, awaitingHuman };
}

/**
 * Build the dot + status-tag HTML for a row given its status flags.
 * Priority: awaiting-human > translating > memory-prefetch > streaming.
 * @returns {{dotHtml:string, statusTag:string}}
 */
function _convStatusHtml(f) {
  let dotHtml = '';
  if (f.awaitingHuman) {
    dotHtml = `<div class="conv-awaiting-human-dot" title="${t('sidebar.awaitingInput')}"></div>`;
  } else if (f.translating) {
    dotHtml = `<div class="conv-translating-dot" title="${t('sidebar.translating')}"></div>`;
  } else if (f.memoryPrefetching) {
    dotHtml = `<div class="conv-memprefetch-dot" title="${t('sidebar.memoryPrefetch')}"></div>`;
  } else if (f.streaming) {
    dotHtml = '<div class="conv-streaming-dot"></div>';
  }
  let statusTag = '';
  if (f.translating) {
    statusTag = `<span class="conv-status-tag conv-status-translating">${t('sidebar.translatingTag')}</span>`;
  } else if (f.memoryPrefetching) {
    statusTag = `<span class="conv-status-tag conv-status-memprefetch">${t('sidebar.memoryPrefetchTag')}</span>`;
  } else if (f.streaming) {
    statusTag = `<span class="conv-status-tag conv-status-streaming">${t('sidebar.answering')}</span>`;
  }
  return { dotHtml, statusTag };
}

/**
 * ★ PERF: patch a row's status (active class, leading dot, trailing status
 * tag) IN PLACE — no innerHTML rebuild of the whole list. Called by the
 * status-only fast path in renderConversationList(). Only mutates the dot
 * and status-tag nodes; the title / date / action buttons are untouched.
 */
function _applyConvItemStatus(row, c) {
  row.classList.toggle('active', c.id === activeConvId);
  const f = _convStatusFlags(c);
  const { dotHtml, statusTag } = _convStatusHtml(f);

  /* Leading dot: it's the first child of .conv-item when present (before
   * .conv-text). Reconcile by comparing the current dot markup. */
  const curDot = row.querySelector(':scope > .conv-translating-dot, :scope > .conv-memprefetch-dot, :scope > .conv-streaming-dot, :scope > .conv-awaiting-human-dot');
  const curDotHtml = curDot ? curDot.outerHTML : '';
  if (curDotHtml !== dotHtml) {
    if (curDot) curDot.remove();
    if (dotHtml) row.insertAdjacentHTML('afterbegin', dotHtml);
  }

  /* Trailing status tag: lives inside .conv-date. */
  const dateEl = row.querySelector('.conv-date');
  if (dateEl) {
    const curTag = dateEl.querySelector('.conv-status-tag');
    const curTagHtml = curTag ? curTag.outerHTML : '';
    if (curTagHtml !== statusTag) {
      if (curTag) curTag.remove();
      if (statusTag) dateEl.insertAdjacentHTML('beforeend', statusTag);
    }
  }
}

function _buildConvItemHTML(c, titleHtml, snippetHtml) {
  const f = _convStatusFlags(c);
  const { dotHtml, statusTag } = _convStatusHtml(f);
  const eid = escapeHtml(c.id);
  const isActive = c.id === activeConvId ? " active" : "";
  const feishuBadge = c.source === 'feishu' ? `<span class="conv-feishu-badge" title="${t('sidebar.feishuConv')}">Feishu</span>` : '';
  const _isDebug = typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode;
  const copyIdBtn = _isDebug ? `<button class="conv-action-btn conv-copy-id" data-conv-id="${eid}" title="${t('sidebar.copyConvId')}">${_CONV_CP_SVG}</button>` : '';
  const folderClass = c.folderId ? ' in-folder' : '';
  return `<div class="conv-item${isActive}${folderClass}" data-conv-id="${eid}" draggable="true" title="ID: ${eid}">${dotHtml}<div class="conv-text"><div class="conv-title">${feishuBadge}${titleHtml}</div>${snippetHtml || ""}<div class="conv-date">${formatConvTime(c.updatedAt || c.createdAt)}${statusTag}</div></div><div class="conv-actions">${copyIdBtn}<button class="conv-action-btn conv-rename" data-conv-id="${eid}" title="${t('sidebar.renameConv')}">${_CONV_RENAME_SVG}</button><button class="conv-action-btn conv-ref" data-conv-id="${eid}" data-conv-title="${escapeHtml(c.title || 'Untitled')}" title="${t('sidebar.refConv')}">@</button><button class="conv-action-btn conv-folder-assign" data-conv-id="${eid}" title="${t('sidebar.moveToFolder')}">${_CONV_FOLDER_SVG}</button><button class="conv-action-btn conv-dup" data-conv-id="${eid}" title="${t('sidebar.duplicate')}">${_CONV_DUP_SVG}</button><button class="conv-action-btn conv-delete" data-conv-id="${eid}" title="${t('sidebar.deleteConv')}">${_CONV_DEL_SVG}</button></div></div>`;
}

function highlightMatch(text, query) {
  if (!query) return escapeHtml(text);
  const e = escapeHtml(text);
  const q = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return e.replace(
    new RegExp(`(${q})`, "gi"),
    '<span class="sidebar-search-highlight">$1</span>',
  );
}

function _extractText(val) {
  if (typeof val === "string") return val;
  if (Array.isArray(val)) return val.map((v) => (typeof v === "string" ? v : v?.text || "")).join(" ");
  return "";
}

/* ── Two-tier search: instant title match (local) + async content search (server) ── */
let _searchAbort = null;   // AbortController for in-flight search API
let _searchSeq = 0;        // monotonic counter to discard stale results

function searchByTitle(query) {
  if (!query) return [];
  const results = [];
  const seen = new Set();
  /* ── Conv ID match (exact or prefix) ──
   * Conv IDs are lowercase alphanumeric (14 chars) like "mosnzwji2h8kwo".
   * If the user pastes an ID (full or partial ≥4 chars, no spaces), surface
   * that conversation first — lets you jump to a known ID without scrolling.
   * Gate by /^[a-z0-9]+$/ + length ≥ 4 so ordinary search words don't
   * accidentally trigger an ID scan that overlaps with title text. */
  if (/^[a-z0-9]{4,}$/.test(query)) {
    for (const c of conversations) {
      if (c.id && c.id.toLowerCase().includes(query)) {
        results.push({
          conv: c,
          matchField: "id",
          matchSnippet: `ID: ${c.id}`,
          matchRole: null,
        });
        seen.add(c.id);
      }
    }
  }
  for (const c of conversations) {
    if (seen.has(c.id)) continue;
    if ((c.title || "").toLowerCase().includes(query)) {
      results.push({ conv: c, matchField: "title", matchSnippet: null });
      seen.add(c.id);
    }
  }
  return results;
}

async function searchByContent(query, seq) {
  if (_searchAbort) { _searchAbort.abort(); _searchAbort = null; }
  const ac = new AbortController();
  _searchAbort = ac;
  try {
    const hits = await Api.conversations.search(query, { signal: ac.signal });
    if (!Array.isArray(hits)) return [];
    if (seq !== _searchSeq) return null;       // stale — discard
    const convMap = new Map(conversations.map(c => [c.id, c]));
    return hits
      .map(h => {
        const c = convMap.get(h.id);
        if (!c) return null;
        return { conv: c, matchField: h.matchField, matchSnippet: h.matchSnippet, matchRole: h.matchRole };
      })
      .filter(Boolean);
  } catch (e) {
    if (e.name === 'AbortError') return null;  // cancelled — don't render
    console.warn('[search] server error, falling back to local', e);
    return _localContentSearch(query);          // fallback
  } finally {
    if (_searchAbort === ac) _searchAbort = null;
  }
}

/** Local fallback content search (used only if server unreachable) */
function _localContentSearch(query) {
  const results = [];
  for (const c of conversations) {
    if ((c.title || "").toLowerCase().includes(query)) continue; // already in title results
    let found = false;
    for (let i = c.messages.length - 1; i >= 0; i--) {
      const msg = c.messages[i];
      const rawContent = _extractText(msg.content);
      const content = rawContent.toLowerCase();
      if (content.includes(query)) {
        const idx = content.indexOf(query);
        const s = Math.max(0, idx - 30);
        const e = Math.min(content.length, idx + query.length + 50);
        const snip = (s > 0 ? "…" : "") + rawContent.slice(s, e) + (e < content.length ? "…" : "");
        results.push({ conv: c, matchField: "content", matchSnippet: snip, matchRole: msg.role });
        found = true;
        break;
      }
    }
    if (!found) {
      for (let i = c.messages.length - 1; i >= 0; i--) {
        const msg = c.messages[i];
        const rawTh = _extractText(msg.thinking);
        const th = rawTh.toLowerCase();
        if (th.includes(query)) {
          const idx = th.indexOf(query);
          const s = Math.max(0, idx - 30);
          const e = Math.min(th.length, idx + query.length + 50);
          const snip = (s > 0 ? "…" : "") + rawTh.slice(s, e) + (e < th.length ? "…" : "");
          results.push({ conv: c, matchField: "thinking", matchSnippet: snip, matchRole: "assistant" });
          found = true;
          break;
        }
      }
    }
  }
  return results;
}

// ── Shared helpers: streaming bubble & surgical DOM truncation ──

/**
 * Re-resolve an assistant message reference by its stable `_msgId`.
 *
 * The streaming pipeline closes over `assistantMsg` for the lifetime of
 * one SSE connection. Phase-2 reconciliation (`loadConversationMessages`)
 * or any push that reorders `conv.messages` can leave that closure
 * pointing at a detached object — content keeps accumulating into a ref
 * that the renderer never sees, the symptom being "Autopilot content
 * only appears after stop + force-refresh".
 *
 * Use this on every SSE event (or any post-await checkpoint) to rebind
 * the local `assistantMsg` to whatever object currently lives in
 * `conv.messages` for that `_msgId`. Falls back to the closure-bound
 * argument when no id is set yet (very early init or legacy messages).
 *
 * @param {Object} conv  — the conversation object
 * @param {string} msgId — the `_msgId` minted by `_ensureMsgId`
 * @param {Object} fallback — original closure-bound assistantMsg
 * @returns {Object} the live message object (or fallback if not found)
 */
function _resolveAssistantById(conv, msgId, fallback) {
  if (!conv || !msgId) return fallback;
  const msgs = conv.messages;
  if (!Array.isArray(msgs)) return fallback;
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m && m._msgId === msgId) return m;
  }
  return fallback;
}

/**
 * Find the message in conv.messages that carries an `_autopilotPending`
 * payload, regardless of whether it's at the tail. The done-event
 * handler stamps the payload on whatever assistantMsg ref it had at the
 * time; later pushes / reconciliations can move it off the tail before
 * `finishStream` runs.
 *
 * @param {Object} conv
 * @returns {{msg: Object, idx: number}|null}
 */
function _findAutopilotPendingCarrier(conv) {
  if (!conv || !Array.isArray(conv.messages)) return null;
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._autopilotPending) return { msg: m, idx: i };
  }
  return null;
}

