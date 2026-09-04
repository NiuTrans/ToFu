/* ===== migrated source: ui/conversation_list.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   conversation list — extracted from ui.js (split 2026-05-28)

   Conversation list rendering — sidebar conv list, folder tabs, search.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── Finish-reason tiers (mirror finish_info.js) — decide the settled
 *   sidebar state of a turn that carries a finishReason.
 *   NORMAL: clean completion, no flag.
 *   ERR:    hard failure → red error state (same bucket as msg.error).
 *   Everything else that is neither NORMAL nor ERR is treated as an
 *   INCOMPLETE (interrupted / truncated / stopped) turn → amber state. */
const _FINISH_NORMAL = new Set(['stop', 'end_turn', 'stop_sequence', 'tool_use', 'tool_calls']);
const _FINISH_ERR = new Set(['error', 'server_offline']);

/* Is the assistant Turn part of an autopilot run that has
 * already CONCLUDED (produced a debrief report / carries a concluded record)?
 * The run id is stamped on the run's virtual-user (role:'user') turns, not on
 * assistant Turn, so we scan backwards from its stable Turn identity for the
 * nearest run stamp, then consult the conv's `autopilotSummaries`
 * sidecar — the backend-authoritative "run concluded" signal selected into
 * the typed Turn view (record.status==='concluded' OR content present). A conv with no
 * autopilot state returns false immediately (cheap). */
function _autopilotRunConcluded(c, turnId) {
  const summaries = c && c.autopilotSummaries;
  if (!summaries || typeof summaries !== 'object') return false;
  const turns = runtimeScope.ConversationTurnRead?.ordered?.(c) || [];
  const turnIndex = turns.findIndex((turn) => turn.turnId === turnId);
  if (turnIndex < 0) return false;
  let runId = null;
  for (let index = turnIndex; index >= 0; index -= 1) {
    const turn = turns[index];
    if (turn.projection?._autopilotRunId) {
      runId = turn.projection._autopilotRunId;
      break;
    }
    if (turn.actor === 'human' && index !== turnIndex) break;
  }
  if (!runId) return false;
  const rec = summaries[runId];
  return !!(rec && typeof rec === 'object' && (rec.status === 'concluded' || rec.content));
}

function formatConvTime(ts) {
  const labels = conversationTimestampLabels(
    ts, Date.now(), document.documentElement.lang,
    t('sidebar.dateToday'), t('sidebar.dateYesterday'),
  );
  if (!labels) return '';
  return `<span class="conv-date-text">${escapeHtml(labels.date)}</span><span class="conv-date-sep">·</span><span class="conv-date-time">${labels.time}</span>`;
}

/* ── Date grouping for the conversation list ──
 * Rows are sorted recency-first (_convSorter), so each date bucket is a
 * contiguous run. Each bucket gets a clickable header that folds its rows.
 * The ">30 days" ("older") bucket starts collapsed. */
const _CONV_OLDER_DAYS = 30;
/** Set of date-group keys currently collapsed. "older" starts collapsed by
 *  default; every other key only lands here via an explicit user toggle. */
const _collapsedConvGroups = new Set(['older']);
/** Keys the user has EXPLICITLY toggled this session. A user's choice always
 *  wins over the force-expand guards below (which exist only to stop the
 *  default-collapsed "older" bucket from hiding the whole list on load). */
const _userToggledConvGroups = new Set();

/** Classify a timestamp into a date-group key. */
/* Canonical date-group order for the sidebar sections. Sorting group-FIRST
 * keeps every date bucket a single contiguous run — the invariant the
 * grouped renderer relies on when emitting section headers. Floating active
 * conversations across group boundaries interleaved the sections into
 * 今天→昨天→今天 (duplicate headers). */
const _CONV_GROUP_RANK = { today: 0, yesterday: 1, prev7: 2, prev30: 3, older: 4 };

function _convGroupRank(c) {
  const r = _CONV_GROUP_RANK[_convDateGroupKey(c.updatedAt || c.createdAt)];
  return r === undefined ? _CONV_GROUP_RANK.older : r;
}

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
  return `<button type="button" class="conv-date-group${collapsed ? ' collapsed' : ''}" data-group="${key}" data-tofu-action="_toggleConvGroup('${key}')">` +
    `${chevron}<span class="conv-date-label">${label}</span><span class="conv-date-count">${count}</span></button>`;
}

/** Toggle a date group's collapsed state and re-render. */
function _toggleConvGroup(key) {
  _userToggledConvGroups.add(key);
  if (_collapsedConvGroups.has(key)) _collapsedConvGroups.delete(key);
  else _collapsedConvGroups.add(key);
  _lastConvListHash = "";          // force a full rebuild past the hash guard
  renderConversationList();
}

let _lastConvListHash = "";
let _lastConvStructHash = "";        // struct part of the split hash (row identity/order/title/date/folder)
let _lastRenderedSearchQuery = "";   // guard: skip background re-renders in search mode

/* PERF: Fast-path for conversation switch — instead of rebuilding the
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

/* i18n race fix: the locale chunk is a dynamic import that lands AFTER the
 * IndexedDB cache-first sidebar paint (hydrateConversationCatalogFromCache) on any cold
 * load — guaranteed on a cache-bypassing force refresh, where it queues
 * behind the multi-MB app chunk. Every t() read in that first paint therefore
 * renders the RAW KEY (sidebar.dateToday / sidebar.uncategorized / …), and
 * the hash guards above would skip every later repaint because the
 * conversation DATA never changed — leaving the keys stuck on screen.
 * _applyI18n() can't help either: this list is JS-built innerHTML without
 * data-i18n attributes. i18n ready() and every live language switch dispatch
 * `tofu:language-change` (same pattern as the pet / skills / settings /
 * memory panels), so invalidate ALL sidebar render guards and force one full
 * repaint once a real translation table exists. */
window.addEventListener('tofu:language-change', function () {
  if (!document.getElementById('convList')) return;
  _lastConvListHash = '';
  _lastConvStructHash = '';
  _lastRenderedSearchQuery = '';   // search-mode guard: force a rebuild too
  _lastFolderTabsHash = '';
  _lastFolderTabsContentHash = '';
  _lastFolderTabsStructHash = '';
  if (typeof renderConversationList === 'function') renderConversationList();
  if (typeof paperMode === 'undefined' || !paperMode) {
    const active = conversations.find(item => item.id === activeConvId);
    const topbar = document.getElementById('topbarTitle');
    if (topbar && active?.title === 'New Chat') {
      topbar.textContent = _conversationDisplayTitle(
        active.title, t('chat.newConversation'));
    } else if (topbar && !activeConvId
        && !topbar.querySelector('.topbar-folder-badge')) {
      topbar.textContent = _conversationDisplayTitle(
        'New Chat', t('chat.newConversation'));
    }
  }
});

/* Vertical project-rail collapsed/expanded state (icon-only ⇄ labeled),
 * persisted like the old expand state. Kept in localStorage so the choice
 * survives reloads and spans conversations. The toggle handler in
 * main_folders_mobile.js writes the SAME key. */
const RAIL_COLLAPSE_KEY = 'tofu_project_rail_collapsed';
const RAIL_HAS_FOLDERS_KEY = 'tofu_has_folders';
function _readRailCollapsed() {
  try { return localStorage.getItem(RAIL_COLLAPSE_KEY) === '1'; }
  catch (_e) { return false; }
}
/* Persist the "does this user have ≥1 folder" hint + keep the pre-paint
 * html[data-rail] attribute in sync. The inline script in index.html reads the
 * localStorage hint on the NEXT load to settle the sidebar width BEFORE first
 * paint (zero CLS). Syncing the attribute here also keeps THIS session correct
 * if the rail appears/disappears after load (first folder created, or the
 * zero-folder correction after a fresh install). */
function _persistRailHint(hasRail) {
  try {
    if (hasRail) localStorage.setItem(RAIL_HAS_FOLDERS_KEY, '1');
    else localStorage.removeItem(RAIL_HAS_FOLDERS_KEY);
  } catch (_e) { /* private-mode / disabled storage — hint is best-effort */ }
  try {
    const root = document.documentElement;
    if (hasRail) root.setAttribute('data-rail', _readRailCollapsed() ? 'collapsed' : 'full');
    else root.removeAttribute('data-rail');
  } catch (_e) { /* no document root — non-DOM context */ }
}



function renderFolderTabs(folders, activeFolderId, allConvs) {
  const tabsEl = document.getElementById('folderTabs');
  if (!tabsEl) return;
  try {
    _renderFolderTabsInner(tabsEl, folders, activeFolderId, allConvs);
  } catch (e) {
    console.error('[renderFolderTabs] Error:', e);
    // On error, ensure the rail isn't left in a broken state — minimal fallback.
    try { tabsEl.innerHTML = '<div class="project-rail-list"><button class="folder-tab folder-tab-add" title="New folder"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button></div>'; } catch(_) {}
  }
}

/* Derive a 1–2 char monogram for a project tile. A single glyph is a poor
 * recognition cue (the owner's complaint), so we prefer two: initials of the
 * first two whitespace/`-`/`_`/`/`-separated words (e.g. "Machine Learning" →
 * "ML", "arxiv-papers" → "AP"); for a single word, its first two LETTERS
 * ("chatui" → "CH"). CJK names take the FIRST TWO characters as-is (already
 * dense and legible). Falls back to "•" for an empty/symbol-only name. */
function _folderMonogram(name) {
  const s = String(name || '').trim();
  if (!s) return '•';
  // CJK (Han/Hiragana/Katakana/Hangul) — two chars carry meaning; use them raw.
  if (/[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/.test(s)) {
    return Array.from(s).slice(0, 2).join('');
  }
  const words = s.split(/[\s\-_/.]+/).filter(Boolean);
  if (words.length >= 2) {
    return (words[0][0] + words[1][0]).toUpperCase();
  }
  const w = words[0] || s;
  return w.slice(0, 2).toUpperCase();
}

/* Deterministic tile background for a project whose owner never picked a color.
 * Without this, every uncolored folder fell back to the single `var(--accent)`,
 * so N uncolored projects were one indistinguishable color block — defeating
 * the at-a-glance recognition the rail exists for. We derive a STABLE hue by
 * hashing the folder id (falling back to name) and spreading it around the
 * wheel, at a fixed pastel S/L that reads on both dark and parchment themes.
 * Same folder → same color across every render/session (hash is pure). */
function _folderColor(f) {
  if (f && f.color) return f.color;
  const key = String((f && (f.id || f.name)) || '');
  if (!key) return 'var(--accent)';
  let h = 0;
  for (let i = 0; i < key.length; i++) {
    h = (h * 31 + key.charCodeAt(i)) >>> 0;
  }
  return `hsl(${h % 360} 52% 55%)`;
}

function _renderFolderTabsInner(tabsEl, folders, activeFolderId, allConvs) {
  const safeFolders = folders || [];
  const safeConvs = allConvs || [];

  /* ── Zero-folder degradation ──
   * A user who never made a folder must NOT be forced into a two-pane rail
   * layout. With 0 folders the rail is hidden entirely (single-column list,
   * exactly as before) and only the discreet "+ New folder" quick-add entry
   * point shows. The rail materializes once ≥1 folder exists. The sidebar
   * `.has-rail` class drives the CSS grid/flex split. */
  const sidebarEl = tabsEl.closest('.sidebar') || document.getElementById('sidebar');
  const hasRail = safeFolders.length > 0;
  if (sidebarEl) sidebarEl.classList.toggle('has-rail', hasRail);
  // Persist the width hint + sync html[data-rail] so the NEXT load paints at
  // the right width (zero CLS), and so an appear/disappear this session is
  // reflected on the root immediately.
  _persistRailHint(hasRail);
  if (!hasRail) {
    tabsEl.innerHTML = '';
    _lastFolderTabsHash = '';
    _lastFolderTabsContentHash = '';
    _lastFolderTabsStructHash = '';
    return;
  }
  tabsEl.style.display = '';

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
    const isStreaming = convIsBusy(c) || c._translating || c._memoryPrefetching;
    if (isStreaming) streamingFolderIds.add(c.folderId);
  }

  /* Reflect the persisted collapsed/expanded choice on the sidebar every
   * render (cheap class toggle, outside the structural fast path so it always
   * self-heals to the stored value). */
  const railCollapsed = _readRailCollapsed();
  if (sidebarEl) sidebarEl.classList.toggle('rail-collapsed', railCollapsed);

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

  /* Rank by the project's most recent SIGNAL, counting creation as one: a
   * just-created project has no conversations yet, so activity-only ranking
   * scored it 0 and buried it at the rail bottom — exactly when the user is
   * about to file something into it. */
  const _folderSortTs = (f) => Math.max(lastActiveMap[f.id] || 0, f.createdAt || 0);
  const sortedFolders = [...safeFolders].sort((a, b) => _folderSortTs(b) - _folderSortTs(a) || (a.order || 0) - (b.order || 0));

  /* ── Vertical project rail ──
   * Each project is a full-width ROW (dot + name + count) so names of any
   * length align into clean columns — no ragged wrap. The rail scrolls
   * vertically when it overflows; there is no more +N expand toggle.
   * Row DOM contract is UNCHANGED from the pill era: `.folder-tab` with
   * `data-folder-id` (empty string = 未分类), an inner `.folder-tab-dot`
   * (streaming pulse) and `.folder-tab-name`, so _initFolderTabs' click /
   * context-menu / long-press / drag-drop handlers all keep working. */
  const railTitle = escapeHtml(t('sidebar.projects'));
  const collapseTip = railCollapsed ? escapeHtml(t('sidebar.expandRail')) : escapeHtml(t('sidebar.collapseRail'));
  let html = '';
  html += `<div class="project-rail-head">`;
  html += `<span class="project-rail-title">${railTitle}</span>`;
  html += `<button class="project-rail-collapse" title="${collapseTip}" aria-label="${collapseTip}">`;
  html += `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>`;
  html += `</button></div>`;
  html += '<div class="project-rail-list">';

  // "未分类" row — conversations not in any folder (uses the inbox glyph as its "dot")
  const ucBadge = uncategorizedCount > 0 ? `<span class="folder-tab-count">${uncategorizedCount}</span>` : '';
  html += `<button class="folder-tab folder-tab-uncat${!activeFolderId ? ' active' : ''}" data-folder-id="" title="${escapeHtml(t('sidebar.uncategorized'))}">`;
  html += `<span class="folder-tab-dot folder-tab-inbox-dot"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg></span>`;
  html += `<span class="folder-tab-name">${escapeHtml(t('sidebar.uncategorized'))}</span>${ucBadge}</button>`;

  // Project rows
  for (const f of sortedFolders) {
    const fcolor = escapeHtml(_folderColor(f));
    const fname = escapeHtml(f.name);
    const isActive = activeFolderId === f.id;
    const cnt = countMap[f.id] || 0;
    const badge = cnt > 0 ? `<span class="folder-tab-count">${cnt}</span>` : '';
    const dotStreaming = streamingFolderIds.has(f.id) ? ' streaming' : '';
    const mono = _folderMonogram(f.name);
    html += `<button class="folder-tab${isActive ? ' active' : ''}" data-folder-id="${escapeHtml(f.id)}" title="${fname}">`;
    html += `<span class="folder-tab-dot${dotStreaming}" style="background:${fcolor}" data-initial="${escapeHtml(mono)}" data-mono-len="${mono.length}"></span>`;
    html += `<span class="folder-tab-name">${fname}</span>${badge}`;
    html += `</button>`;
  }
  html += '</div>';
  // Footer "+ New project" row — always visible at the rail bottom.
  html += `<button class="folder-tab folder-tab-add" title="${escapeHtml(t('sidebar.newFolder'))}">`;
  html += `<span class="folder-tab-dot folder-tab-add-dot"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></span>`;
  html += `<span class="folder-tab-name">${escapeHtml(t('sidebar.newFolder'))}</span>`;
  html += `</button>`;
  tabsEl.innerHTML = html;
}

/* ═══════════════════════════════════════════════════════════════════
 * PERF: Windowed conversation-list rendering.
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

  /* Never auto-collapse the ONLY populated group. The "older" (>30d) bucket
   * starts collapsed, but for a user whose conversations are ALL older than 30
   * days that would hide every row behind a single collapsed header — the
   * sidebar looks empty and clicks land on nothing (no .conv-item is rendered).
   * When a single group holds all the rows, force it expanded regardless of
   * its remembered collapsed state, so there is always something to click. */
  const _soleGroupKey = Object.keys(counts).length === 1 ? Object.keys(counts)[0] : null;

  /* Bucket rows by group so each section header emits EXACTLY ONCE in
   * canonical rank order. (The contiguous-run walk would emit a duplicate
   * header whenever the incoming order isn't perfectly group-contiguous —
   * _convSorter now guarantees that, but bucketing makes the plan correct
   * for ANY input order, e.g. search results or the incremental fast path.) */
  const buckets = new Map();  // key -> conv[]
  for (const c of filtered) {
    const key = _convDateGroupKey(c.updatedAt || c.createdAt);
    let rows = buckets.get(key);
    if (!rows) { rows = []; buckets.set(key, rows); }
    rows.push(c);
  }
  const orderedKeys = [...buckets.keys()].sort((ka, kb) =>
    (_CONV_GROUP_RANK[ka] ?? _CONV_GROUP_RANK.older) - (_CONV_GROUP_RANK[kb] ?? _CONV_GROUP_RANK.older));

  /** @type {Array<{type:'header'|'conv', key?:string, count?:number, collapsed?:boolean, conv?:object}>} */
  const plan = [];
  for (const key of orderedKeys) {
    /* An explicit user toggle always wins; the active-conv / sole-group
     * force-expand only guards the default (never-user-touched) state so a
     * default-collapsed "older" bucket can't hide the whole list on load. */
    const collapsed = _collapsedConvGroups.has(key) &&
      (_userToggledConvGroups.has(key) || (key !== activeKey && key !== _soleGroupKey));
    plan.push({ type: 'header', key, count: buckets.get(key).length, collapsed });
    if (collapsed) continue;   // rows hidden under a collapsed header
    for (const c of buckets.get(key)) plan.push({ type: 'conv', conv: c });
  }
  return plan;
}

/** Render a single plan item to HTML. */
function _planItemHtml(item) {
  if (item.type === 'header') return _convGroupHeaderHtml(item.key, item.count, item.collapsed);
  const c = item.conv;
  return _buildConvItemHTML(c, escapeHtml(_conversationDisplayTitle(
    c.title, t('chat.newConversation'))), "");
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


/* ── C3/C4 — global keyset pagination of the sidebar window ─────────────
 * The top-N sidebar window (Epic D4) is a performance floor, not a ceiling:
 * conversations that sort past it must stay REACHABLE. While the server
 * total exceeds what is loaded, a "N earlier · Load more" affordance hangs
 * below the windowed list (CSS at styles.css .conv-load-more); clicking it —
 * or scrolling it into view — fetches the next keyset page
 * (Api.conversations.listPage) and merges it via mergeConversationCatalogRows. */
let _loadingMoreGlobalConvs = false;

/* True only for the GLOBAL list (not a folder view, not search) when the
 * server reports more conversations than are currently in memory. */
function _hasMoreGlobalConvs() {
  if (typeof getActiveFolderId === 'function' && getActiveFolderId()) return false;
  const total = typeof getServerTotalCount === 'function' ? getServerTotalCount() : null;
  if (!Number.isFinite(total)) return false;
  return total > conversations.length;
}

function _unloadedGlobalConvCount() {
  const total = typeof getServerTotalCount === 'function' ? getServerTotalCount() : null;
  if (!Number.isFinite(total)) return 0;
  return Math.max(0, total - conversations.length);
}

/* Append the load-more affordance to `listEl` (no-op when caught up). */
function _appendLoadMoreAffordance(listEl) {
  if (!listEl || !_hasMoreGlobalConvs()) return;
  const n = _unloadedGlobalConvCount();
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'conv-load-more';
  btn.textContent = (typeof t === 'function'
    ? t('sidebar.loadMoreEarlier')
    : '{n} earlier conversations not loaded · Load more').replace('{n}', String(n));
  btn.addEventListener('click', () => { loadMoreGlobalConvs(); });
  listEl.appendChild(btn);
  /* C3: scrolling the affordance into view auto-loads (same handler as the
   * click). The window sentinel pages IN-MEMORY rows; this pages the SERVER
   * once the in-memory rows run out. */
  if (typeof IntersectionObserver === 'function') {
    const obs = new IntersectionObserver((entries) => {
      if (!entries.some(e => e.isIntersecting)) return;
      obs.disconnect();
      loadMoreGlobalConvs();
    }, { root: listEl, rootMargin: '0px 0px 200px 0px' });
    obs.observe(btn);
  }
}

/* Fetch the next keyset page (cursor = oldest in-memory conv) and merge it. */
async function loadMoreGlobalConvs() {
  if (_loadingMoreGlobalConvs || !_hasMoreGlobalConvs()) return;
  _loadingMoreGlobalConvs = true;
  try {
    let oldestTs = Infinity, oldestId = null;
    for (const c of conversations) {
      if (!c) continue;
      const ts = c.updatedAt || c.createdAt || 0;
      if (ts < oldestTs) { oldestTs = ts; oldestId = c.id; }
    }
    if (oldestId == null) return;
    const data = await Api.conversations.listPage(oldestTs, oldestId, 200);
    const rows = (data && (data.conversations || data.items)) || [];
    if (rows.length && typeof mergeConversationCatalogRows === 'function') {
      const added = mergeConversationCatalogRows(rows);
      if (added > 0) renderConversationList();
    }
  } catch (e) {
    console.warn('[sidebar] loadMoreGlobalConvs failed:', e && e.message);
  } finally {
    _loadingMoreGlobalConvs = false;
  }
}

function renderConversationList() {
  const listEl = document.getElementById("convList"),
    statsEl = document.getElementById("sidebarSearchStats");
  if (!sidebarSearchQuery) {
    const _wasSearching = !!_lastRenderedSearchQuery;
    _lastRenderedSearchQuery = "";   // reset when exiting search mode
    statsEl.classList.remove("visible");
    const all = conversations.filter((c) =>
      (runtimeScope.ConversationTurnRead?.ordered?.(c)?.length || 0) > 0
      || (c._serverTurnCount || 0) > 0 || c._turnSnapshotRequired);

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
      // Folders not yet loaded — FAIL OPEN: show every conversation. Hiding a
      // conv that carries a folderId behind a transient (or failed) folder load
      // makes real, server-present conversations invisible — the exact "I lose
      // conversations" symptom. A brief flash of a foldered conv in the
      // uncategorized view is strictly better than dropping it; once folders
      // load the normal branch re-partitions it correctly.
      filtered = all;
    }
    // else: folders loaded and empty — show everything (no folders exist)

    /* ── Split hash: struct (row identity/order/title/date/folder) vs status
     *    (active / streaming / translating / memory-prefetch / awaiting-human).
     *    When only status changed we patch each row's .active class + dot +
     *    status tag IN PLACE — no innerHTML rebuild, no full reparse/relayout
     *    of the sidebar (the dominant long-task cost during a send's
     *    translate→stream→done lifecycle). Mirrors the folder-tab fast path. ── */
    /* DBG: per-row action buttons (copy-conv-ID) are baked into the row HTML
     * by _buildConvItemHTML under the debug flag — include it in the struct
     * hash or toggling debug mode in Settings early-returns here and the
     * buttons only appear on the next full page load. */
    const _structHash = `AF${_activeFolderId||''}|FL${foldersReady?1:0}|DBG${(typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode)?1:0}|CG${[..._collapsedConvGroups].sort().join('.')}|F${folderHash}|` +
      filtered.map(c => `${c.id}|${c.title}|${c.updatedAt||""}|${c.folderId||""}|${(c.projectSummary && c.projectSummary.text) ? "S" : ""}`).join("\n");
    const _statusHash = filtered.map(c => {
      const f = _convStatusFlags(c);
      return `${c.id===activeConvId?1:0}${f.streaming?1:0}${f.translating?1:0}${f.memoryPrefetching?1:0}${f.awaitingHuman?1:0}${f.waiting?1:0}${f.errored?1:0}${f.incomplete?1:0}${f.unconfirmed?1:0}`;
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
      /* C3/C4: global list only — hang the keyset "load earlier" affordance
       * below the windowed rows (folder views filter a client-side subset,
       * so a global page count would mislead there). */
      if (!_activeFolderId) _appendLoadMoreAffordance(listEl);
    }
    /* Keep _lastActiveConvId in sync after a full rebuild so
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
  const suffix = isPartial ? ` <span class="search-loading">${escapeHtml(t("sidebar.searching"))}</span>` : "";
  const countText = t(results.length !== 1 ? "sidebar.searchResults" : "sidebar.searchResult", { n: results.length });
  statsEl.innerHTML = `${escapeHtml(countText)}${suffix}`;
  if (results.length === 0 && isPartial) {
    listEl.innerHTML = `<div class="sidebar-search-empty"><div class="sidebar-search-empty-icon"></div>${escapeHtml(t("sidebar.searching"))}</div>`;
    _lastConvListHash = "";
    return;
  }
  if (results.length === 0) {
    /* split/join for literal {q} substitution — avoids $-pattern
     * interpretation in String.replace and keeps the query HTML-escaped. */
    const noMatch = escapeHtml(t("sidebar.searchNoMatches"))
      .split("{q}")
      .join(`<strong>${escapeHtml(query)}</strong>`);
    listEl.innerHTML = `<div class="sidebar-search-empty"><div class="sidebar-search-empty-icon"></div>${noMatch}</div>`;
    _lastConvListHash = "";
    return;
  }
  const items = results.map(
    ({ conv: c, matchField, matchSnippet, matchRole }) => {
      const tHtml =
        matchField === "title"
          ? highlightMatch(_conversationDisplayTitle(
            c.title, t('chat.newConversation')), query)
          : escapeHtml(_conversationDisplayTitle(
            c.title, t('chat.newConversation')));
      let snip = "";
      if (matchSnippet) {
        const ico = "";
        /* ID match: no role prefix ("You:"/"Claude:") — snippet is the ID itself. */
        if (matchField === "id") {
          snip = `<div class="conv-item-snippet">${highlightMatch(matchSnippet, query)}</div>`;
        } else {
          const rl = matchRole === "user" ? t("sidebar.searchRoleYou") : t("sidebar.searchRoleAssistant");
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

/* PERF: static action-button SVGs hoisted to module scope — these never
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
 * @returns {{streaming:boolean, translating:boolean, memoryPrefetching:boolean, awaitingHuman:boolean, waiting:boolean, errored:boolean, incomplete:boolean}}
 */
/**
 * Single busy predicate shared by the sidebar and composer. The answer is
 * derived exclusively from pending/running Turns in the normalized store.
 *
 * NOTE: this answers "is conv X busy", not "should the composer show Stop" — the
 * composer is always about ``activeConvId`` while the sidebar shows every conv,
 * so a lit background dot with an idle composer is correct when they refer to
 * different conversations.
 *
 * @param {Object} conv — conversation object
 * @returns {boolean}
 */
function convIsBusy(conv) {
  if (!conv) return false;
  const state = runtimeScope.ConversationTurnRead?.state?.(conv);
  return Object.values(state?.turnsById || {}).some((turn) =>
    turn && (turn.status === 'pending' || turn.status === 'running'));
}

/** Read the current phase from TurnStore; never cache or mirror it. */
function convRateLimitPhase(conversationOrId) {
  const phase = runtimeScope.ConversationTurnRead
    ?.state?.(conversationOrId)?.livePhase;
  return presentConversationRateLimit(phase);
}

function _convStatusFlags(c) {
  const translating = !!c._translating;
  const memoryPrefetching = !!c._memoryPrefetching;
  const streaming = convIsBusy(c);
  /* The sidebar derives the same typed phase as the active Surface. */
  const rateLimited = !!(streaming && convRateLimitPhase(c));
  const turns = runtimeScope.ConversationTurnRead?.ordered?.(c) || [];
  const latestAssistant = [...turns].reverse().find((turn) =>
    turn.actor === 'assistant' || turn.actor === 'planner');
  /* A leftover awaiting_human round on a settled turn is unanswerable
   * (server restarts leave it persisted forever) — the amber dot must reflect
   * a LIVE wait only, which requires the turn (hence the conv) to be busy. */
  const awaitingHuman = streaming && Boolean(latestAssistant?.projection?.toolRounds?.some(
    (round) => round?.status === 'awaiting_human',
  ));

  // Only the latest assistant/planner Turn defines the settled sidebar state.
  let errored = false;
  let incomplete = false;
  let waiting = false;
  if (!streaming && latestAssistant) {
    const projection = latestAssistant.projection || {};
    const finishReason = projection.finishReason;
    waiting = Boolean(projection.waitingOn) && !Boolean(latestAssistant.settlement?.error) && !Boolean(projection.error);
    const hardError = latestAssistant.status === 'failed'
      || Boolean(latestAssistant.settlement?.error) || Boolean(projection.error)
      || _FINISH_ERR.has(finishReason);
    if (hardError) {
      errored = true;
    } else if (latestAssistant.status === 'interrupted'
        || latestAssistant.status === 'truncated') {
      incomplete = true;
    } else if (finishReason) {
      incomplete = !_FINISH_NORMAL.has(finishReason);
    } else {
      const hasOutput = Boolean(
        projection.content || projection.thinking
        || projection.toolRounds?.length || projection.imageGeneration?.results?.length
        || projection.segments?.length,
      );
      incomplete = latestAssistant.status !== 'completed' || !hasOutput;
    }
    if ((incomplete || errored) && !hardError
        && _autopilotRunConcluded(c, latestAssistant.turnId)) {
      incomplete = false;
      errored = false;
    }
  } else if (!streaming && !latestAssistant && c.lastMsgRole === 'assistant') {
    const finishReason = c.lastFinishReason;
    if (c.lastMsgError || _FINISH_ERR.has(finishReason)) errored = true;
    else if (finishReason) incomplete = !_FINISH_NORMAL.has(finishReason);
    else incomplete = !c.lastMsgHasOutput;
  }
  /* Freshness of the busy verdict (). NOT a second
   * busy-ness source: `streaming` above is unchanged and still authoritative.
   * This only records whether the authoritative channel is currently
   * DELIVERING, so a busy dot inherited from a frame that arrived before the
   * socket went dark is rendered as "unconfirmed" rather than as settled fact.
   * Only meaningful while `streaming` — an idle conv has no claim to soften. */
  const healthState = c._conversationSyncHealth?.state;
  const unconfirmed = Boolean(streaming
    && ['recovering', 'degraded', 'offline'].includes(healthState));
  if (waiting) incomplete = false;
  return { streaming, translating, memoryPrefetching, awaitingHuman, waiting, errored, incomplete, unconfirmed, rateLimited };
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
    dotHtml = f.unconfirmed
      ? `<div class="conv-streaming-dot conv-state-unconfirmed" title="${t('sidebar.stateUnconfirmed')}"></div>`
      : f.rateLimited
        ? `<div class="conv-streaming-dot conv-ratelimit-dot" title="${t('sidebar.rateLimited')}"></div>`
        : '<div class="conv-streaming-dot"></div>';
  } else if (f.errored) {
    dotHtml = `<div class="conv-error-dot" title="${t('sidebar.errorState')}"></div>`;
  } else if (f.waiting) {
    dotHtml = `<div class="conv-waiting-dot" title="${t('sidebar.waitingState')}"></div>`;
  } else if (f.incomplete) {
    dotHtml = `<div class="conv-incomplete-dot" title="${t('sidebar.incompleteState')}"></div>`;
  }
  let statusTag = '';
  if (f.translating) {
    statusTag = `<span class="conv-status-tag conv-status-translating">${t('sidebar.translatingTag')}</span>`;
  } else if (f.memoryPrefetching) {
    statusTag = `<span class="conv-status-tag conv-status-memprefetch">${t('sidebar.memoryPrefetchTag')}</span>`;
  } else if (f.streaming) {
    statusTag = f.unconfirmed
      ? `<span class="conv-status-tag conv-status-streaming conv-state-unconfirmed" title="${t('sidebar.stateUnconfirmed')}">${t('sidebar.answering')}?</span>`
      : f.rateLimited
        ? `<span class="conv-status-tag conv-status-ratelimit" title="${t('sidebar.rateLimited')}">${t('sidebar.rateLimitedTag')}</span>`
        : `<span class="conv-status-tag conv-status-streaming">${t('sidebar.answering')}</span>`;
  } else if (f.errored) {
    statusTag = `<span class="conv-status-tag conv-status-error" title="${t('sidebar.errorState')}">${t('sidebar.errorTag')}</span>`;
  } else if (f.waiting) {
    statusTag = `<span class="conv-status-tag conv-status-waiting" title="${t('sidebar.waitingState')}">${t('sidebar.waitingTag')}</span>`;
  } else if (f.incomplete) {
    statusTag = `<span class="conv-status-tag conv-status-incomplete" title="${t('sidebar.incompleteState')}">${t('sidebar.incompleteTag')}</span>`;
  }
  return { dotHtml, statusTag };
}

/**
 * PERF: patch a row's status (active class, leading dot, trailing status
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
  const curDot = row.querySelector(':scope > .conv-translating-dot, :scope > .conv-memprefetch-dot, :scope > .conv-streaming-dot, :scope > .conv-awaiting-human-dot, :scope > .conv-error-dot, :scope > .conv-waiting-dot, :scope > .conv-incomplete-dot');
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
  const copyBadge = c.clonedFrom ? `<span class="conv-copy-badge" title="${t('sidebar.copyBadgeTip')}">${t('sidebar.copyBadge')}</span>` : '';
  // Sidebar conversation summaries are paused: backend generation is disabled,
  // so the row intentionally has no dormant presentation/action surface.
  const summaryBadge = '';
  const _isDebug = typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode;
  const copyIdBtn = _isDebug ? `<button class="conv-action-btn conv-copy-id" data-conv-id="${eid}" title="${t('sidebar.copyConvId')}">${_CONV_CP_SVG}</button>` : '';
  const folderClass = c.folderId ? ' in-folder' : '';
  return `<div class="conv-item${isActive}${folderClass}" data-conv-id="${eid}" draggable="true" title="ID: ${eid}">${dotHtml}<div class="conv-text"><div class="conv-title">${feishuBadge}${summaryBadge}${titleHtml}</div>${snippetHtml || ""}<div class="conv-date">${formatConvTime(c.updatedAt || c.createdAt)}${copyBadge}${statusTag}</div></div><div class="conv-actions">${copyIdBtn}<button class="conv-action-btn conv-rename" data-conv-id="${eid}" title="${t('sidebar.renameConv')}">${_CONV_RENAME_SVG}</button><button class="conv-action-btn conv-ref" data-conv-id="${eid}" data-conv-title="${escapeHtml(c.title || 'Untitled')}" title="${t('sidebar.refConv')}">@</button><button class="conv-action-btn conv-folder-assign" data-conv-id="${eid}" title="${t('sidebar.moveToFolder')}">${_CONV_FOLDER_SVG}</button><button class="conv-action-btn conv-dup" data-conv-id="${eid}" title="${t('sidebar.duplicate')}">${_CONV_DUP_SVG}</button><button class="conv-action-btn conv-delete" data-conv-id="${eid}" title="${t('sidebar.deleteConv')}">${_CONV_DEL_SVG}</button></div></div>`;
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
    const turns = runtimeScope.ConversationTurnRead?.ordered?.(c) || [];
    let found = false;
    for (let i = turns.length - 1; i >= 0; i--) {
      const turn = turns[i];
      const rawContent = _extractText(turn.projection?.content);
      const content = rawContent.toLowerCase();
      if (content.includes(query)) {
        const idx = content.indexOf(query);
        const s = Math.max(0, idx - 30);
        const e = Math.min(content.length, idx + query.length + 50);
        const snip = (s > 0 ? "…" : "") + rawContent.slice(s, e) + (e < content.length ? "…" : "");
        const matchRole = turn.actor === 'human' ? 'user' : 'assistant';
        results.push({ conv: c, matchField: "content", matchSnippet: snip, matchRole });
        found = true;
        break;
      }
    }
    if (!found) {
      for (let i = turns.length - 1; i >= 0; i--) {
        const turn = turns[i];
        const rawTh = _extractText(turn.projection?.thinking);
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
