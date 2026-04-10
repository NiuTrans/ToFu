/* ═══════════════════════════════════════════
   ui.js — Sidebar, Chat Rendering, Streaming
   ═══════════════════════════════════════════ */


/**
 * Strip <notranslate>/<nt> wrapper tags from text, keeping inner content.
 * Used when displaying originalContent so the user sees clean text.
 */
function stripNoTranslateTags(text) {
  if (!text) return text;
  return text.replace(/<\/?notranslate>/gi, '').replace(/<\/?nt>/gi, '');
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

let _lastConvListHash = "";
let _lastRenderedSearchQuery = "";   // guard: skip background re-renders in search mode

/* ★ PERF: Fast-path for conversation switch — instead of rebuilding the
 * entire sidebar from scratch (O(N) HTML generation + innerHTML assignment),
 * just move the .active class between two DOM elements (O(1)).
 * Returns true if the fast-path was sufficient, false if a full rebuild is needed. */
let _lastActiveConvId = null;
function _swapActiveConvItem(newActiveId) {
  if (sidebarSearchQuery) return false; // search mode — need full rebuild
  const oldId = _lastActiveConvId;
  if (oldId === newActiveId) return true; // no change
  _lastActiveConvId = newActiveId;
  /* Swap .active class in DOM */
  if (oldId) {
    const oldEl = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(oldId)}"]`);
    if (oldEl) oldEl.classList.remove('active');
  }
  if (newActiveId) {
    const newEl = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(newActiveId)}"]`);
    if (newEl) newEl.classList.add('active');
    else return false; // new conv not in DOM yet — need full rebuild
  }
  /* Invalidate the hash so a subsequent full renderConversationList()
   * won't skip due to stale hash (the hash includes active state). */
  _lastConvListHash = "";
  return true;
}

/* ── Folder tab bar ── */
let _lastFolderTabsHash = '';
function renderFolderTabs(folders, activeFolderId, allConvs) {
  const tabsEl = document.getElementById('folderTabs');
  if (!tabsEl) return;

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

  // Hash to avoid unnecessary re-renders (includes counts)
  const hash = `${activeFolderId||''}|U${uncategorizedCount}|${safeFolders.map(f=>`${f.id}|${f.name}|${f.color||''}|${f.order||0}|${countMap[f.id]||0}`).join(',')}`;
  if (hash === _lastFolderTabsHash) return;
  _lastFolderTabsHash = hash;

  const sortedFolders = [...safeFolders].sort((a, b) => (a.order || 0) - (b.order || 0));

  let html = '<div class="folder-tabs-scroll">';
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
    html += `<span class="folder-tab-dot" style="background:${fcolor}"></span>`;
    html += `<span class="folder-tab-name">${fname}</span>${badge}`;
    html += `</button>`;
  }
  // "+" add tab — always visible
  html += `<button class="folder-tab folder-tab-add" title="${t('sidebar.newFolder')}">`;
  html += `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
  html += `</button>`;
  html += '</div>';
  tabsEl.innerHTML = html;
}

function renderConversationList() {
  const listEl = document.getElementById("convList"),
    statsEl = document.getElementById("sidebarSearchStats");
  if (!sidebarSearchQuery) {
    _lastRenderedSearchQuery = "";   // reset when exiting search mode
    statsEl.classList.remove("visible");
    const all = conversations.filter((c) => c.messages.length > 0 || (c._serverMsgCount || 0) > 0 || c._needsLoad);

    const folders = typeof getFolders === 'function' ? getFolders() : [];
    const _activeFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    const foldersReady = typeof areFoldersLoaded === 'function' ? areFoldersLoaded() : true;

    /* ── Lightweight hash ── */
    const _quickHash = (arr) => arr.map(c =>
      `${c.id}|${c.title}|${c.updatedAt||""}|${c.id===activeConvId?1:0}|${activeStreams?.has(c.id)?1:0}|${c.activeTaskId||""}|${c._translating?1:0}|${c.folderId||""}`
    ).join("\n");
    const folderHash = folders.map(f => `${f.id}|${f.name}|${f.order}|${f.color||''}`).join(",");
    /* ── Render folder tabs (always, regardless of hash — tab visibility may change) ── */
    renderFolderTabs(folders, _activeFolderId, all);

    const hash = `AF${_activeFolderId||''}|FL${foldersReady?1:0}|${_quickHash(all)}|||F${folderHash}`;
    if (hash === _lastConvListHash) return;
    _lastConvListHash = hash;

    /* ── Filter by active folder tab ── */
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

    let listHtml = "";
    filtered.forEach((c) => {
      listHtml += _buildConvItemHTML(c, escapeHtml(stripNoTranslateTags(c.title)), "");
    });

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

    listEl.innerHTML = listHtml;
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
        const rl = matchRole === "user" ? "You" : "Claude";
        snip = `<div class="conv-item-snippet">${ico} ${rl}: ${highlightMatch(matchSnippet, query)}</div>`;
      }
      return _buildConvItemHTML(c, tHtml, snip);
    },
  );
  const newHtml = items.join("");
  if (newHtml === _lastConvListHash) return;
  _lastConvListHash = newHtml;
  listEl.innerHTML = newHtml;
}

function _buildConvItemHTML(c, titleHtml, snippetHtml) {
  // ★ Separate translating state from streaming for distinct sidebar indicators
  const translating = !!c._translating;
  let streaming = activeStreams.has(c.id) || c.activeTaskId;
  if (!streaming) {
    const prefix = c.id + ":";
    for (const k of activeStreams.keys()) { if (k.startsWith(prefix)) { streaming = true; break; } }
  }
  // ★ Detect if conversation is awaiting human input (any round with status=awaiting_human)
  let awaitingHuman = false;
  if (c.messages) {
    for (let i = c.messages.length - 1; i >= 0; i--) {
      const m = c.messages[i];
      if (m.role === 'assistant' && m.searchRounds) {
        for (const r of m.searchRounds) {
          if (r.status === 'awaiting_human') { awaitingHuman = true; break; }
        }
        if (awaitingHuman) break;
      }
    }
  }
  const eid = escapeHtml(c.id);
  const isActive = c.id === activeConvId ? " active" : "";
  const delSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
  const feishuBadge = c.source === 'feishu' ? `<span class="conv-feishu-badge" title="${t('sidebar.feishuConv')}">Feishu</span>` : '';
  const cpSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  // ★ Sidebar dot: amber blinking for awaiting-human, teal pulsing for translating, blue for streaming
  let dotHtml = '';
  if (awaitingHuman) {
    dotHtml = `<div class="conv-awaiting-human-dot" title="${t('sidebar.awaitingInput')}"></div>`;
  } else if (translating) {
    dotHtml = `<div class="conv-translating-dot" title="${t('sidebar.translating')}"></div>`;
  } else if (streaming) {
    dotHtml = '<div class="conv-streaming-dot"></div>';
  }
  // ★ Status tag next to date: "翻译中" / "回答中" for visual clarity
  let statusTag = '';
  if (translating) {
    statusTag = `<span class="conv-status-tag conv-status-translating">${t('sidebar.translatingTag')}</span>`;
  } else if (streaming) {
    statusTag = `<span class="conv-status-tag conv-status-streaming">${t('sidebar.answering')}</span>`;
  }
  const dupSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="14" height="14" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
  const folderSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
  const _isDebug = typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode;
  const copyIdBtn = _isDebug ? `<button class="conv-action-btn conv-copy-id" data-conv-id="${eid}" title="${t('sidebar.copyConvId')}">${cpSvg}</button>` : '';
  const folderClass = c.folderId ? ' in-folder' : '';
  return `<div class="conv-item${isActive}${folderClass}" data-conv-id="${eid}" draggable="true" title="ID: ${eid}">${dotHtml}<div class="conv-text"><div class="conv-title">${feishuBadge}${titleHtml}</div>${snippetHtml || ""}<div class="conv-date">${formatConvTime(c.updatedAt || c.createdAt)}${statusTag}</div></div><div class="conv-actions">${copyIdBtn}<button class="conv-action-btn conv-ref" data-conv-id="${eid}" data-conv-title="${escapeHtml(c.title || 'Untitled')}" title="${t('sidebar.refConv')}">@</button><button class="conv-action-btn conv-folder-assign" data-conv-id="${eid}" title="${t('sidebar.moveToFolder')}">${folderSvg}</button><button class="conv-action-btn conv-dup" data-conv-id="${eid}" title="${t('sidebar.duplicate')}">${dupSvg}</button><button class="conv-action-btn conv-delete" data-conv-id="${eid}" title="${t('sidebar.deleteConv')}">${delSvg}</button></div></div>`;
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
  for (const c of conversations) {
    if ((c.title || "").toLowerCase().includes(query)) {
      results.push({ conv: c, matchField: "title", matchSnippet: null });
    }
  }
  return results;
}

async function searchByContent(query, seq) {
  if (_searchAbort) { _searchAbort.abort(); _searchAbort = null; }
  const ac = new AbortController();
  _searchAbort = ac;
  try {
    const resp = await fetch(apiUrl(`/api/conversations/search?q=${encodeURIComponent(query)}`), { signal: ac.signal });
    if (!resp.ok) return [];
    const hits = await resp.json();
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

// ── Chat rendering ──
/* ── Lazy chat rendering with IntersectionObserver ── */
const _INITIAL_RENDER = 20;
let _lazyObserver = null;
let _lazyConvId = null;
let _lazyRenderedFrom = Infinity;

function _destroyLazyObserver() {
  if (_lazyObserver) {
    _lazyObserver.disconnect();
    _lazyObserver = null;
  }
  _loadingOlder = false;
}

function _ensureLazyObserver() {
  if (_lazyObserver) return;
  _lazyObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        const sentinel = e.target;
        _lazyObserver.unobserve(sentinel);
        _loadOlderMessages();
      });
    },
    {
      root: document.getElementById("chatContainer"),
      rootMargin: "600px 0px 0px 0px",
    },
  );
}

let _loadingOlder = false;
function _loadOlderMessages() {
  if (_loadingOlder) return;
  const conv = conversations.find((c) => c.id === _lazyConvId);
  if (!conv) return;
  const BATCH = 20;
  const endIdx = _lazyRenderedFrom;
  if (endIdx <= 0) return;
  _loadingOlder = true;
  const startIdx = Math.max(0, endIdx - BATCH);
  const inner = document.getElementById("chatInner");
  const sentinel = document.getElementById("_lazyLoadSentinel");
  if (!sentinel || !inner) {
    _loadingOlder = false;
    return;
  }

  const container = document.getElementById("chatContainer");

  /* Build all HTML strings first (cheaper than individual DOM creates) */
  let html = "";
  for (let i = startIdx; i < endIdx; i++) {
    html += renderMessage(conv.messages[i], i);
  }

  /* Single DOM mutation: measure → mutate → fix scroll — no intermediate frame */
  const prevScrollTop = container.scrollTop;
  const prevScrollHeight = container.scrollHeight;

  const wrapper = document.createElement("div");
  wrapper.innerHTML = html;
  const frag = document.createDocumentFragment();
  while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
  sentinel.after(frag);

  _lazyRenderedFrom = startIdx;

  /* Fix scroll synchronously BEFORE the browser paints */
  container.scrollTop =
    prevScrollTop + (container.scrollHeight - prevScrollHeight);

  /* Update or remove sentinel */
  if (startIdx <= 0) {
    sentinel.remove();
  } else {
    sentinel.querySelector("._lazy-count").textContent = startIdx;
    _lazyObserver.observe(sentinel);
  }
  _loadingOlder = false;
}

/**
 * Reliably scroll a container to the very bottom.
 * Uses double-rAF to wait for layout, then a fallback timer
 * to handle async content (images, KaTeX, code highlights).
 */
function _forceScrollToBottom(container, forceActualHeights) {
  if (!container) container = document.getElementById("chatContainer");
  if (!container) return;
  const inner = document.getElementById("chatInner");
  // Override CSS scroll-behavior:smooth so programmatic scrolls are instant.
  container.style.scrollBehavior = 'auto';

  if (forceActualHeights && inner) {
    // Disable content-visibility:auto so the browser computes REAL heights
    // synchronously instead of using the 120px estimate.  This makes
    // scrollHeight accurate on the very first read — no flash.
    inner.classList.add('cv-off');
    // Force sync reflow so heights are computed NOW.
    void container.scrollHeight;
  }

  container.scrollTop = container.scrollHeight;

  if (forceActualHeights && inner) {
    // Re-enable content-visibility:auto.  The browser caches the actual
    // heights it just computed (via "auto" in contain-intrinsic-size),
    // so scrollHeight stays correct and the scroll position doesn't shift.
    inner.classList.remove('cv-off');
  }

  // Safety net for async content (images, KaTeX, code highlights).
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  });
  setTimeout(() => {
    container.scrollTop = container.scrollHeight;
    container.style.scrollBehavior = '';
  }, 150);
}

/** Per-message fingerprint for surgical DOM diffing.
 *  Must change whenever the rendered HTML for this message would differ. */
function _msgFingerprint(msg) {
  const sr = msg.searchRounds || msg.searchResults;
  return (msg.role || "") + ":" +
    (msg.content || "").length + ":" +
    (msg.thinking || "").length + ":" +
    (msg.error || "").length + ":" +
    (msg.finishReason || "") + ":" +
    (msg.translatedContent || "").length + ":" +
    (msg._showingTranslation ? "T" : "F") + ":" +
    (msg._translateDone === false ? "P" : "") + ":" +
    (sr ? sr.length : 0) + ":" +
    (msg._igResult ? "IG" : "") + ":" +
    (msg._igResults ? msg._igResults.length : 0) + ":" +
    (msg._igError ? "IGE" : "") + ":" +
    (msg.modifiedFiles || 0) + ":" +
    (msg.images ? msg.images.length : 0) + ":" +
    (msg.pdfTexts ? msg.pdfTexts.length : 0);
}

function renderChat(conv, forceScroll) {
  /* ── Guard 1: skip if user is editing a message in this conversation ── */
  if (_editingMsgIdx !== null && conv.id === activeConvId) return;

  /* ── Guard 1b: skip background re-renders while branch panel is open ── */
  if (forceScroll === false && _activeBranch && conv.id === activeConvId) return;

  /* ── Guard 1c: protect active streaming bubble from destruction ──
   * A full renderChat() destroys the #streaming-msg element and replaces all
   * messages with static renderMessage() output.  The streaming assistant message
   * (which has msg.model set from the state/preset SSE event) gets a finish-bar
   * with only the model tag — appearing as if the message is done while the
   * sidebar still pulses and the stop button is active.
   * Fix: delegate to showStreamingUIForConv() which properly renders prev messages
   * statically and creates a fresh streaming bubble for the in-progress message. */
  if (conv.id === activeConvId && activeStreams.has(conv.id) && document.getElementById('streaming-msg')) {
    if (typeof showStreamingUIForConv === 'function') showStreamingUIForConv(conv.id);
    return;
  }

  /* ── Guard 2: fingerprint-based skip for background syncs (forceScroll===false) ── */
  const fp = _convRenderFingerprint(conv);
  if (
    forceScroll === false &&
    conv.id === activeConvId &&
    fp === _lastRenderedFingerprint
  ) {
    /* Data hasn't actually changed — skip the destructive re-render entirely */
    return;
  }

  const inner = document.getElementById("chatInner");
  const container = document.getElementById("chatContainer");

  /* ═══ Surgical update path (forceScroll === false) ═══
   * Instead of wiping inner.innerHTML (which destroys all DOM nodes,
   * resets content-visibility:auto size caches, and causes scroll flicker),
   * do per-message diffing: only touch messages that actually changed.
   * This preserves scroll position perfectly with ZERO visual flicker.
   *
   * Only use surgical mode when the DOM already has rendered messages
   * (i.e. not showing a welcome screen or loading skeleton). */
  const _hasMsgDom = inner && inner.querySelector('[id^="msg-"]');
  /* ★ FIX: During initial conversation load (_initialSwitchLoad), Phase 2 server
   * response triggers renderChat(conv, false).  The surgical path would do
   * outerHTML replacement which destroys content-visibility:auto size caches,
   * causing scrollHeight to collapse → visible scroll-jump-to-top before the
   * .then() callback scrolls back down.  Skip surgical mode for initial loads
   * so the full-render path runs with _forceScrollToBottom — no flash. */
  if (forceScroll === false && conv.id === activeConvId && conv.messages.length > 0 && _hasMsgDom && !conv._initialSwitchLoad) {
    const total = conv.messages.length;
    const startIdx = Math.max(0, total - _INITIAL_RENDER);
    let anyChange = false;

    /* 1) Update or add messages
     * ★ Perf: collect all outerHTML replacements first, then apply in one pass.
     * Each outerHTML assignment invalidates layout; batching avoids interleaving
     * layout reads (getElementById) with writes (outerHTML) — prevents forced reflows. */
    const _pendingUpdates = [];
    /* ★ Skip the streaming message — it's rendered as #streaming-msg,
     *   not as msg-N.  Without this skip, the else branch below would
     *   append a static renderMessage() (with finish-bar) for the streaming
     *   message, then step 3 would remove the live streaming bubble. */
    const _streamingActive = activeStreams.has(conv.id) && document.getElementById('streaming-msg');
    const _skipIdx = _streamingActive ? (total - 1) : -1;
    for (let i = startIdx; i < total; i++) {
      if (i === _skipIdx) continue;  // streaming message — leave #streaming-msg alone
      const msg = conv.messages[i];
      const el = document.getElementById("msg-" + i);
      if (el) {
        /* Element exists — check if content changed */
        const oldFp = el.getAttribute("data-mfp") || "";
        const newFp = _msgFingerprint(msg);
        if (oldFp !== newFp) {
          _pendingUpdates.push({ el, html: renderMessage(msg, i) });
          anyChange = true;
        }
      } else {
        /* New message — append */
        const wrapper = document.createElement("div");
        wrapper.innerHTML = renderMessage(msg, i);
        const newEl = wrapper.firstElementChild;
        if (newEl) inner.appendChild(newEl);
        anyChange = true;
      }
    }
    /* Apply all outerHTML replacements in a single write batch */
    for (const upd of _pendingUpdates) {
      upd.el.outerHTML = upd.html;
    }

    /* 2) Remove stale messages beyond the current count */
    const staleEls = inner.querySelectorAll('[id^="msg-"]');
    for (const el of staleEls) {
      const m = el.id.match(/^msg-(\d+)$/);
      if (m) {
        const idx = parseInt(m[1], 10);
        if (idx >= total || idx < (Math.max(0, total - _INITIAL_RENDER))) {
          el.remove();
          anyChange = true;
        }
      }
    }

    /* 3) Remove any leftover streaming bubble (task finished)
     * ★ Only remove if the stream has actually finished — don't destroy a live
     *   streaming bubble.  Guard 1c should have caught this, but belt-and-suspenders. */
    const leftoverStreaming = document.getElementById("streaming-msg");
    if (leftoverStreaming && !activeStreams.has(conv.id)) {
      leftoverStreaming.remove();
      anyChange = true;
    }

    if (anyChange) {
      buildTurnNav(conv);
    }
    _lastRenderedFingerprint = fp;
    _lazyConvId = conv.id;
    return;
  }

  /* ═══ Full re-render path (forceScroll !== false) ═══ */
  _destroyLazyObserver();
  _lazyConvId = conv.id;

  if (conv.messages.length === 0) {
    if (conv._needsLoad) {
      /* ── Loading skeleton: conv has server messages but they haven't arrived yet ── */
      inner.innerHTML = `<div class="welcome" id="welcome" style="opacity:0.5"><div class="welcome-icon" style="animation:pulse 1.5s infinite"></div><h2>Loading conversation…</h2><p>Fetching ${conv._serverMsgCount || ''} messages from server</p></div>`;
    } else {
      inner.innerHTML = `<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${BASE_PATH}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2><p>${t('welcome.subtitle')}</p><div class="feature-pills"><span class="feature-pill">Extended Thinking</span><span class="feature-pill">Search</span><span class="feature-pill">URL Fetch</span><span class="feature-pill">Image Input</span><span class="feature-pill">Co-Pilot</span><span class="feature-pill">Browser</span></div></div>`;
    }
    _lastRenderedFingerprint = fp;
    buildTurnNav(conv);
    return;
  }

  const total = conv.messages.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;

  let html = "";

  /* Lazy-load sentinel for older messages */
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }

  /* Render only the tail portion */
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(conv.messages[i], i);
  }

  inner.innerHTML = html;
  _lastRenderedFingerprint = fp;

  /* Observe the sentinel to trigger loading when scrolled up */
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }

  /* ★ PERF: Defer buildTurnNav to after paint — it scans ALL messages and
   * JSON.parse-s every tool round's args, which can take 50-200ms for large
   * conversations.  The turn nav is not critical for the initial render. */
  requestAnimationFrame(() => buildTurnNav(conv));

  /* Always scroll to the very bottom of the conversation.
   * hideUntilSettled=true: content-visibility:auto heights are estimated on
   * first paint, so hide until the 150ms timer has corrected scrollTop. */
  _forceScrollToBottom(container, true);
}

/* ★ Format relative time for finished messages */
function _fmtRelativeTime(ts) {
  const now = Date.now();
  const d = typeof ts === 'number' ? ts : new Date(ts).getTime();
  if (isNaN(d)) return '';
  const diffMs = now - d;
  if (diffMs < 0 || diffMs < 30000) return ''; // future or <30s — skip
  const s = Math.floor(diffMs / 1000);
  if (s < 60) return `${s}${t('time.secondsAgo')}`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}${t('time.minutesAgo')}`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}${t('time.hoursAgo')}`;
  const days = Math.floor(h / 24);
  if (days < 30) return `${days}${t('time.daysAgo')}`;
  return '';
}
function renderMessage(msg, idx) {
  const isUser = msg.role === "user" || msg.role === "optimizer";  // optimizer = endpoint review, render as user
  const time = msg.timestamp
    ? new Date(msg.timestamp).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
  /* ★ Relative time for assistant messages — show "xx前" to indicate freshness */
  let relTime = "";
  if (!isUser && msg.timestamp) {
    relTime = _fmtRelativeTime(msg.timestamp);
  }
  let body = "";
  if (msg.images?.length > 0) {
    const srcMap = { clip_render: "CLIP", vector_clip: "VEC", page_render: "SCAN", embedded: "RAW", pixmap_fallback: "PIX", pymupdf4llm: "FIG", figure_page_render: "FIG" };
    body += '<div class="msg-image-grid">';
    body += msg.images.map((img) => {
      const src = img.preview || "";
      const isPdf = !!img.pdfPage;
      const srcLabel = srcMap[img.pdfImageSource] || (isPdf ? "PDF" : "");
      const label = isPdf
        ? `P${img.pdfPage}/${img.pdfTotal} · ${img.sizeKB}KB`
        : `${img.sizeKB || "?"}KB`;
      const tip = img.caption
        ? `${img.caption}`.replace(/"/g, "&quot;")
        : isPdf ? `PDF page ${img.pdfPage}` : "";
      if (src && !src.endsWith("..."))
        return `<div class="msg-img-thumb${isPdf ? " pdf-page" : ""}" ${tip ? `title="${tip}"` : ""} onclick="openImagePreview('${src.replace(/'/g, "\\'")}')"><img src="${src}" alt="uploaded">${srcLabel ? `<div class="msg-img-badge">${srcLabel}</div>` : ""}<div class="msg-img-size">${label}</div></div>`;
      return `<div class="msg-img-thumb placeholder"><span class="msg-img-placeholder-icon"></span><div class="msg-img-size">${img.sizeKB || "?"}KB</div></div>`;
    }).join("");
    body += '</div>';
  }
  if (isUser && msg.pdfTexts?.length > 0) {
    body += '<div class="pdf-attachments-indicator">';
    msg.pdfTexts.forEach((pdf, pdfI) => {
      const sizeStr =
        pdf.textLength >= 1024
          ? `${(pdf.textLength / 1024).toFixed(1)}KB`
          : `${pdf.textLength} chars`;
      const scanBadge = pdf.isScanned ? " · scanned" : "";
      const imgCount = (msg.images || []).filter(
        (img) => img.pdfName === pdf.name,
      ).length;
      const imgStr =
        imgCount > 0 ? ` · ${imgCount} img${imgCount > 1 ? "s" : ""}` : "";
      const methodBadge = pdf.method === "vlm" ? ' · <b>VLM</b>' : '';
      const _ext = pdf.name ? pdf.name.slice(pdf.name.lastIndexOf('.')).toLowerCase() : '';
      const _docIconMap = {'.pdf':'📕', '.docx':'📝', '.pptx':'📊', '.xlsx':'📈', '.txt':'📄', '.md':'📄',
                           '.csv':'📊', '.json':'📄', '.xml':'📄', '.py':'🐍', '.js':'📜',
                           '.html':'🌐', '.yaml':'⚙️', '.yml':'⚙️'};
      const docIcon = _docIconMap[_ext] || '📄';
      body += `<div class="pdf-attach-badge" title="${escapeHtml(pdf.name)}" onclick="previewMsgPdfText(${idx},${pdfI})" style="cursor:pointer"><span class="pdf-attach-icon">${docIcon}</span><span class="pdf-attach-info"><span class="pdf-attach-name">${escapeHtml(pdf.name.length > 25 ? pdf.name.slice(0, 23) + "…" : pdf.name)}</span><span class="pdf-attach-meta">${pdf.pages} pages · ${sizeStr}${imgStr}${scanBadge}${methodBadge}</span></span></div>`;
    });
    body += "</div>";
  }
  // ── Reply quotes (user messages) — file badge style, supports array ──
  if (isUser) {
    const quotes = msg.replyQuotes || (msg.replyQuote ? [msg.replyQuote] : []);
    for (const rq of quotes) {
      const rqPreview = rq.replace(/\s+/g, " ").slice(0, 80);
      const rqChars = rq.length;
      const rqLines = rq.split("\n").length;
      body += `<div class="reply-quote-badge" title="${escapeHtml(rq.slice(0, 300))}">
        
        <span class="reply-quote-badge-info">
          <span class="reply-quote-badge-name">${escapeHtml(rqPreview)}${rqChars > 80 ? "…" : ""}</span>
          <span class="reply-quote-badge-meta">${rqChars} chars · ${rqLines} line${rqLines > 1 ? "s" : ""}</span>
        </span></div>`;
    }
    // ── Conversation reference badges ──
    if (msg.convRefs && msg.convRefs.length > 0) {
      for (const cr of msg.convRefs) {
        const crTitle = escapeHtml(cr.title || cr.id);
        body += `<div class="reply-quote-badge conv-ref-badge" title="引用对话: ${crTitle}">
          <span class="reply-quote-badge-icon">@</span>
          <span class="reply-quote-badge-info">
            <span class="reply-quote-badge-name">${crTitle}</span>
            <span class="reply-quote-badge-meta">引用对话</span>
          </span></div>`;
      }
    }
  }
  // ── Proactive agent banner ──
  if (msg._proactive) {
    const taskName = msg._proactiveTaskId ? `Task ${(msg._proactiveTaskId || "").slice(0, 8)}` : "Proactive Agent";
    body += `<div class="proactive-banner"><span class="pb-text"><span class="pb-name">${escapeHtml(taskName)}</span> — scheduled execution</span></div>`;
  }
  const rounds = getSearchRoundsFromMsg(msg);
  if (rounds.length > 0) body += renderSearchRoundsHTML(rounds, false);
  if (msg.thinking) {
    const thinkLen = msg.thinking.length;
    const thinkMeta = thinkLen >= 1024 ? ` (${Math.round(thinkLen / 1024)}k chars)` : ` (${thinkLen} chars)`;
    body += `<div class="thinking-block" onclick="_toggleThinking(this,${idx})"><div class="thinking-header"><span class="thinking-label">Thinking Process${thinkMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
  }
  // Track which branches have been inlined (rendered right after their anchor text)
  let _inlinedBranches = new Set();
  // ── Image Generation error card (from _igError metadata) ──
  // Renders a styled, color-coded error card based on error type.
  if (!isUser && msg._igError) {
    const ige = msg._igError;
    // Determine error-type CSS class and icon
    let errTypeClass = 'ig-error-generic';
    let errIcon = '⚠';
    if (ige.isRateLimit || ige.errorType === 'rate_limited') {
      errTypeClass = 'ig-error-ratelimit';
      errIcon = '⏳';
    } else if (ige.isContentBlocked || ige.errorType === 'content_blocked') {
      errTypeClass = 'ig-error-blocked';
      errIcon = '🚫';
    } else if (ige.isTimeout || ige.errorType === 'timeout') {
      errTypeClass = 'ig-error-timeout';
      errIcon = '⏱';
    } else if (ige.errorType === 'no_slot') {
      errIcon = '🔌';
    }
    body += `<div class="ig-result-wrapper">
      <div class="ig-error-card ${errTypeClass}">
        <div class="ig-error-icon">${errIcon}</div>
        <div class="ig-error-title">${escapeHtml(ige.title || 'Image generation failed')}</div>
        <div class="ig-error-text">${escapeHtml(ige.text || '')}</div>
        ${ige.detail ? `<div class="ig-error-detail">${escapeHtml(ige.detail)}</div>` : ''}
        ${ige.blockReason ? `<div class="ig-error-detail">Block reason: ${escapeHtml(ige.blockReason)}</div>` : ''}
        <button class="ig-retry-btn" onclick="_igRetryLastPrompt()">Retry</button>
      </div>
    </div>`;
  // ── Image Generation result card (from _igResult metadata) ──
  // When an assistant message has _igResult, render a styled card instead of raw markdown
  // so the card survives renderChat re-renders (e.g. when user sends a second image prompt).
  } else if (!isUser && msg._igResult && msg._igResult.image_url) {
    const ig = msg._igResult;
    const imgSrc = ig.image_url.startsWith('/') ? (typeof apiUrl === 'function' ? apiUrl(ig.image_url) : ig.image_url) : ig.image_url;
    const promptText = ig.prompt || '';
    const promptShort = promptText.length > 80 ? promptText.slice(0, 80) + '…' : promptText;
    const sizeStr = ig.file_size ? (typeof _formatFileSize === 'function' ? _formatFileSize(ig.file_size) : Math.round(ig.file_size / 1024) + ' KB') : '';
    const elapsedStr = ig.elapsed ? ig.elapsed + 's' : '';
    const arStr = ig.aspect_ratio || '';
    body += `<div class="ig-result-wrapper">
      <div class="ig-result-card">
        <img src="${imgSrc}" alt="${escapeHtml(promptText.slice(0,100))}"
             onclick="_openImageFullscreen(this.src)" />
        <div class="ig-result-footer">
          <span class="ig-result-prompt" title="${escapeHtml(promptText)}">${escapeHtml(promptShort)}</span>
          <div class="ig-result-meta">
            ${ig.model ? `<span class="ig-meta-pill">${escapeHtml(ig.model)}</span>` : ''}
            ${ig.provider_id ? `<span class="ig-meta-pill">@${escapeHtml(ig.provider_id)}</span>` : ''}
            ${arStr ? `<span class="ig-meta-pill">${escapeHtml(arStr)}</span>` : ''}
            ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
            ${elapsedStr ? `<span class="ig-meta-pill">${elapsedStr}</span>` : ''}
            ${ig.history_turns ? `<span class="ig-meta-pill ig-history-pill" title="${ig.history_turns} prior editing turn${ig.history_turns > 1 ? 's' : ''}">🔄 ${ig.history_turns}</span>` : ''}
          </div>
          <div class="ig-result-actions">
            <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
            <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
          </div>
        </div>
      </div>
    </div>`;
    // Also render any text content from the model (e.g. revised prompt)
    const textContent = (msg.content || '').replace(/!\[Generated Image\]\([^)]*\)\s*/g, '').trim();
    if (textContent) {
      body += `<div class="md-content">${renderMarkdown(textContent)}</div>`;
    }
  // ── Batch Image Generation results grid (from _igResults array) ──
  } else if (!isUser && msg._igResults && msg._igResults.length > 0) {
    const results = msg._igResults;
    // Skip rendering "pending" placeholders during active batch (DOM has live slots)
    const isPending = msg._igBatchPending && results.every(r => r.error === 'pending');
    if (isPending) {
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">Generating…</div></div>`;
    } else {
      const okResults = results.filter(r => r.ok && r.image_url);
      const cols = Math.min(results.length, 2);
      const _fmtSize = typeof _formatFileSize === 'function' ? _formatFileSize : (b => b > 0 ? Math.round(b / 1024) + ' KB' : '');
      const _shortModel = typeof _IG_MODEL_SHORT !== 'undefined' ? _IG_MODEL_SHORT : {};
      const distinctModels = new Set(results.map(r => r.model)).size;
      const bannerLabel = distinctModels > 1 ? `全模型 ${results.length}连抽` : `${results.length}连抽`;
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">${bannerLabel} · ${okResults.length}/${results.length} 成功</div><div class="ig-batch-grid ig-cols-${cols}">`;
      for (let ri = 0; ri < results.length; ri++) {
        const r = results[ri];
        if (r.ok && r.image_url) {
          const imgSrc = r.image_url.startsWith('/') ? (typeof apiUrl === 'function' ? apiUrl(r.image_url) : r.image_url) : r.image_url;
          const sizeStr = r.file_size ? _fmtSize(r.file_size) : '';
          const modelLabel = _shortModel[r.model] || r.model || '';
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}">
            <div class="ig-result-card">
              <img src="${imgSrc}" alt="${escapeHtml((r.prompt || '').slice(0,60))}"
                   onclick="_openImageFullscreen(this.src)" />
              <div class="ig-result-footer">
                <span class="ig-result-prompt">${escapeHtml(modelLabel)}</span>
                <div class="ig-result-meta">
                  ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
                  ${r.elapsed ? `<span class="ig-meta-pill">${r.elapsed}s</span>` : ''}
                </div>
                <div class="ig-result-actions">
                  <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
                  <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
                </div>
              </div>
            </div>
          </div>`;
        } else if (r.error === 'pending') {
          // Still pending — show mini spinner
          const modelLabel = _shortModel[r.model] || r.model || '';
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}">
            <div class="ig-generating ig-batch-loading">
              <div class="ig-gen-spinner"></div>
              <div class="ig-gen-title">${escapeHtml(modelLabel)}</div>
              <div class="ig-gen-subtitle">Pending…</div>
            </div>
          </div>`;
        } else {
          // Error slot — with error-type differentiation and retry button
          const errModel = _shortModel[r.model] || r.model || '?';
          let errTypeClass = 'ig-error-generic';
          let errIcon = '⚠';
          const et = r.errorType || '';
          if (et === 'rate_limited') { errTypeClass = 'ig-error-ratelimit'; errIcon = '⏳'; }
          else if (et === 'content_blocked') { errTypeClass = 'ig-error-blocked'; errIcon = '🚫'; }
          else if (et === 'timeout') { errTypeClass = 'ig-error-timeout'; errIcon = '⏱'; }
          const promptEsc = JSON.stringify(r.prompt || '').replace(/"/g, '&quot;');
          const modelEsc = JSON.stringify(r.model || '').replace(/"/g, '&quot;');
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}"><div class="ig-batch-error ${errTypeClass}">
            <div class="ig-error-icon">${errIcon}</div>
            <div class="ig-error-title">${escapeHtml(errModel)}</div>
            <div class="ig-error-text">${escapeHtml((r.error || 'Failed').slice(0,200))}</div>
            <button class="ig-slot-retry-btn" onclick="_igRetryBatchSlot(${idx},${ri},${promptEsc},${modelEsc})" title="Retry this slot">↻ Retry</button>
          </div></div>`;
        }
      }
      body += `</div></div>`;
    }
  } else if (msg.content) {
    try {
      let mdHtml;
      // Show translated content only when translation is active (not toggled off)
      const showTrans = !isUser && msg.translatedContent && msg._showingTranslation !== false;
      if (showTrans) {
        mdHtml = renderMarkdown(msg.translatedContent);
      } else if (isUser && msg._isEndpointReview) {
        // Critic messages are user-role but contain rich markdown
        mdHtml = renderMarkdown(msg.content);
      } else if (isUser) {
        mdHtml = escapeHtml(stripNoTranslateTags(msg.originalContent || msg.content));
      } else {
        mdHtml = renderMarkdown(msg.content);
      }
      // ── Inject anchored branch pills inline (assistant only) ──
      if (!isUser && msg.branches?.length) {
        const r = _injectAnchoredBranches(mdHtml, msg, idx);
        mdHtml = r.html;
        _inlinedBranches = r.inlinedSet;
      }
      body += `<div class="md-content${isUser ? " user-content" : ""}">${mdHtml}</div>`;
    } catch (e) {
  // ── emit_to_user: render emitted tool content inline below the comment ──
  if (!isUser && msg._emitContent) {
    const toolLabel = msg._emitToolName ? escapeHtml(msg._emitToolName) : 'Tool result';
    body += `<div class="emit-content-block">
      <div class="emit-content-header">📤 ${toolLabel}</div>
      <pre class="emit-content-output"><code>${escapeHtml(msg._emitContent)}</code></pre>
    </div>`;
  }
      body += `<div class="md-content${isUser ? " user-content" : ""}">${escapeHtml(msg.content)}</div>`;
    }
  }
  // ── Bilingual display ──
  if (isUser && msg.originalContent && msg.originalContent !== msg.content) {
    body += `<div class="bilingual-block bilingual-translated"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label">译文</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'user',${idx})" title="Copy translation"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content user-content">${escapeHtml(msg.content)}</div></div></div>`;
  }
  if (!isUser && msg.translatedContent && msg._showingTranslation !== false) {
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label">原文</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'assistant',${idx})" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(msg.content)}</div></div></div>`;
  }
  // ── Persistent "translating..." indicator (survives re-render / tab switch) ──
  if (!isUser && !msg.translatedContent && msg._translateDone === false) {
    const errText = msg._translateError;
    if (errText) {
      body += `<div class="translate-loading" id="translate-loading-${idx}" style="color:#f59e0b;cursor:pointer" onclick="translateMessage(${idx})">${t('translate.failed')}</div>`;
    } else {
      body += `<div class="translate-loading" id="translate-loading-${idx}"><span class="translate-spinner"></span> ${t('translate.translatingToCN')}</div>`;
    }
  }
  if (msg.error)
    body += `<div class="error-block">${escapeHtml(msg.error)}</div>`;
  if (!isUser) body += renderFileChangesBar(msg, idx);
  if (!isUser) body += renderFinishInfo(msg);
  const idAttr = typeof idx === "number" ? ` id="msg-${idx}"` : "";
  let actionBtns = "";
  if (typeof idx === "number") {
    const copyH = `<button class="msg-action-btn copy-msg-btn" onclick="event.stopPropagation();copyMessage(${idx})" title="Copy"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy</button>`;
    const editH = isUser
      ? `<button class="msg-action-btn" onclick="event.stopPropagation();startEditMessage(${idx})" title="Edit"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit</button>`
      : "";
    const regenH = isUser
      ? `<button class="msg-action-btn msg-regen-btn" onclick="event.stopPropagation();regenerateFromUser(${idx})" title="Regenerate response from this message"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Regen</button>`
      : "";
    const conv_ = getActiveConv();
    const isLastAssistant =
      !isUser &&
      conv_ &&
      idx === conv_.messages.length - 1 &&
      !activeStreams.has(conv_.id);
    const continueH = isLastAssistant
      ? `<button class="msg-action-btn msg-continue-btn" onclick="event.stopPropagation();continueAssistant()" title="Continue generating from where it left off"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Continue</button>`
      : "";
    const isShowingTrans = msg._showingTranslation;
    const translateH = !isUser
      ? `<button class="msg-action-btn msg-translate-btn${isShowingTrans ? " translated" : ""}" onclick="event.stopPropagation();translateMessage(${idx})" title="${isShowingTrans ? "Show Original" : "Translate"}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0014.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg> ${isShowingTrans ? "Original" : "Translate"}</button>`
      : "";
    const exportImgH = !isUser
      ? `<button class="msg-action-btn msg-export-img-btn" onclick="event.stopPropagation();ExportImages.exportMessageWithPreview(${idx})" title="Export as phone-screen images"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg> Export</button>`
      : "";
    actionBtns = `<div class="message-actions">${copyH}${editH}${regenH}${continueH}${translateH}${exportImgH}</div>`;
  }
  // ★ Tofu mascot avatars: Worker gets worker tofu, Planner gets planner tofu
  let avatarContent = (typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : "✦",
    roleName = "Agent";
  if (msg._isEndpointPlanner) {
    avatarContent = (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>';
    roleName = "Planner";
  }

  // ── Branch zone for assistant messages (only un-inlined branches + add button) ──
  let branchHtml = "";
  if (!isUser && typeof renderBranchZone === "function") {
    branchHtml = renderBranchZone(msg, idx, _inlinedBranches);
  }

  // ── Planner badge for planner messages ──
  let plannerBadge = "";
  if (msg._isEndpointPlanner) {
    plannerBadge = `<span class="ep-verdict-badge ep-verdict-planner">Plan</span>`;
  }

  // ── Critic verdict badge for endpoint review messages ──
  let criticBadge = "";
  if (msg._isEndpointReview) {
    if (msg._epApproved) {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-stop">Approved</span>`;
    } else if (msg._isStuck) {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-stuck">Stuck</span>`;
    } else {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-continue">Iteration ${msg._epIteration || ""}</span>`;
    }
  }

  // ── Avatar: tofu critic for reviews, onigiri mascot for user ──
  const userAvatar = msg._isEndpointReview
    ? ((typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>')
    : (typeof _USER_AVATAR_SVG !== 'undefined') ? _USER_AVATAR_SVG
    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
  const userLabel = msg._isEndpointReview ? "Critic" : "You";

  const relTimeHtml = relTime ? `<span class="message-reltime">${relTime}</span>` : '';
  const mfpAttr = typeof idx === "number" ? ` data-mfp="${_msgFingerprint(msg)}"` : "";
  const epWorkerCls = (!isUser && !msg._isEndpointPlanner && !msg._isEndpointReview) ? ' ep-worker-msg' : '';
  const epPlannerCls = msg._isEndpointPlanner ? ' ep-planner-msg' : '';
  const badgeHtml = plannerBadge || criticBadge;
  return `<div class="message${isUser ? ' user-msg' : ''}${msg._isEndpointReview ? ' ep-critic-msg' : ''}${epPlannerCls}${epWorkerCls}"${idAttr}${mfpAttr}><div class="message-avatar">${isUser ? userAvatar : avatarContent}</div><div class="message-content"><div class="message-header"><span class="message-role">${isUser ? userLabel : roleName}</span>${badgeHtml}<span class="message-time">${time}</span>${relTimeHtml}</div><div class="message-body">${body}</div>${branchHtml}${actionBtns}</div></div>`;
}
function _initSelectionPopup() {
  _selectionPopup = document.createElement("div");
  _selectionPopup.className = "selection-popup";
  _selectionPopup.style.display = "none";
  _selectionPopup.innerHTML = `
    <button class="selection-popup-btn" data-action="branch">${t('conv.branch')}</button>
    <button class="selection-popup-btn" data-action="reply">${t('conv.reply')}</button>`;
  document.body.appendChild(_selectionPopup);

  _selectionPopup.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const sel = window.getSelection();
    const text = sel.toString().trim();
    if (!text) { _hideSelectionPopup(); return; }

    const msgEl = sel.anchorNode?.parentElement?.closest?.(".message[id]");
    const msgIdx = msgEl ? parseInt(msgEl.id.replace("msg-", ""), 10) : -1;

    if (action === "branch" && msgIdx >= 0) {
      // Capture the live selection Range before it's cleared — we'll use it
      // to insert the branch element directly into the DOM at the exact spot.
      const range = sel.rangeCount > 0 ? sel.getRangeAt(0).cloneRange() : null;
      const title = text.slice(0, 40) + (text.length > 40 ? "…" : "");
      promptNewBranch(msgIdx, title, text, range);
    } else if (action === "reply") {
      _addReplyQuote(text, msgIdx);
    }
    sel.removeAllRanges();
    _hideSelectionPopup();
  });

  // Show popup on selection in chat area
  let _selMouseUpRaf = 0;
  document.addEventListener("mouseup", (e) => {
    if (_selectionPopup.contains(e.target)) return;
    cancelAnimationFrame(_selMouseUpRaf);
    _selMouseUpRaf = requestAnimationFrame(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.toString().trim().length < 5) {
        _hideSelectionPopup();
        return;
      }
      const msgEl = sel.anchorNode?.parentElement?.closest?.(".message[id]");
      if (!msgEl) { _hideSelectionPopup(); return; }
      if (msgEl.id === "streaming-msg") { _hideSelectionPopup(); return; }

      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      _selectionPopup.style.left = `${rect.left + rect.width / 2 - 60}px`;
      _selectionPopup.style.top = `${rect.top - 40 + window.scrollY}px`;
      _selectionPopup.style.display = "flex";
    });
  });

  document.addEventListener("mousedown", (e) => {
    if (!_selectionPopup.contains(e.target)) _hideSelectionPopup();
  });
}

function _hideSelectionPopup() {
  if (_selectionPopup) _selectionPopup.style.display = "none";
}

// ── Reply quotes (multi-quote support) ──
function _addReplyQuote(text, msgIdx) {
  _pendingReplyQuotes.push(text);
  _renderReplyQuoteChips();
}

function _renderReplyQuoteChips() {
  let container = document.getElementById("reply-quote-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "reply-quote-container";
    container.className = "reply-quote-container";
    const inputActions = document.querySelector(".input-box .input-actions");
    if (inputActions) inputActions.parentElement.insertBefore(container, inputActions);
  }
  if (!_pendingReplyQuotes.length) {
    container.style.display = "none";
    return;
  }
  container.style.display = "flex";
  container.innerHTML = _pendingReplyQuotes.map((q, i) => {
    const preview = q.replace(/\s+/g, " ").slice(0, 50);
    const chars = q.length;
    const lines = q.split("\n").length;
    return `<div class="reply-quote-chip">
      
      <span class="reply-quote-chip-body">
        <span class="reply-quote-chip-label">${escapeHtml(preview)}${chars > 50 ? "…" : ""}</span>
        <span class="reply-quote-chip-meta">${chars} chars · ${lines} line${lines > 1 ? "s" : ""}</span>
      </span>
      <button class="reply-quote-chip-close" onclick="_removeReplyQuote(${i})" title="Remove">✕</button>
    </div>`;
  }).join("");
}

function _removeReplyQuote(idx) {
  _pendingReplyQuotes.splice(idx, 1);
  _renderReplyQuoteChips();
}

function clearReplyQuote() {
  _pendingReplyQuotes = [];
  _renderReplyQuoteChips();
}

function getPendingReplyQuotes() {
  return _pendingReplyQuotes.length > 0 ? [..._pendingReplyQuotes] : null;
}

// ══════════════════════════════════════════════════════
// ★ Conversation Reference Chips (@-mention)
// ══════════════════════════════════════════════════════
const _pendingConvRefs = [];  // [{id, title}]

function addConvRef(convId, convTitle) {
  // Don't add duplicates or self-references
  const activeConv = getActiveConv();
  if (activeConv && activeConv.id === convId) {
    showToast?.(t('convRef.cannotRef'), "warning");
    return;
  }
  if (_pendingConvRefs.some(r => r.id === convId)) {
    showToast?.(t('convRef.alreadyRef'), "info");
    return;
  }
  _pendingConvRefs.push({ id: convId, title: convTitle || "Untitled" });
  _renderConvRefChips();
  // Focus the input and show confirmation
  document.getElementById("userInput")?.focus();
  const shortTitle = (convTitle || "Untitled").slice(0, 30);
  showToast?.(`已引用: ${shortTitle}${convTitle && convTitle.length > 30 ? "…" : ""}`, "success");
}

function removeConvRef(index) {
  _pendingConvRefs.splice(index, 1);
  _renderConvRefChips();
}

function clearConvRefs() {
  _pendingConvRefs.length = 0;
  _renderConvRefChips();
}

function getPendingConvRefs() {
  return _pendingConvRefs.length > 0 ? _pendingConvRefs.map(r => ({...r})) : null;
}

function _renderConvRefChips() {
  let container = document.getElementById("conv-ref-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "conv-ref-container";
    container.className = "conv-ref-container";
    // Place inside .input-box, just above .input-actions toolbar
    const inputActions = document.querySelector(".input-box .input-actions");
    if (inputActions) inputActions.parentElement.insertBefore(container, inputActions);
  }
  if (!_pendingConvRefs.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = _pendingConvRefs.map((ref, i) => {
    const title = escapeHtml(ref.title.length > 45 ? ref.title.slice(0, 42) + "…" : ref.title);
    // Show message count instead of raw ID
    const localConv = (typeof conversations !== "undefined" ? conversations : []).find(c => c.id === ref.id);
    const msgCount = localConv?.messages?.length || 0;
    const subtitle = msgCount > 0 ? `${msgCount} 条消息` : "对话引用";
    return `<div class="conv-ref-chip" data-index="${i}">
      <span class="conv-ref-chip-icon">@</span>
      <span class="conv-ref-chip-info">
        <span class="conv-ref-chip-title">${title}</span>
        <span class="conv-ref-chip-id">${escapeHtml(subtitle)}</span>
      </span>
      <button class="conv-ref-chip-remove" data-index="${i}" title="移除引用">×</button>
    </div>`;
  }).join("");
  container.querySelectorAll(".conv-ref-chip-remove").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeConvRef(parseInt(btn.dataset.index));
    });
  });
  // Update toolbar @ button active state
  const refBtn = document.getElementById("convRefBtn");
  if (refBtn) refBtn.classList.toggle("has-refs", _pendingConvRefs.length > 0);
}

// ══════════════════════════════════════════════════════
// ★ Conversation Reference Picker (@-popover)
// ══════════════════════════════════════════════════════
function openConvRefPicker() {
  // Close if already open
  const existing = document.getElementById("conv-ref-picker");
  if (existing) { existing.remove(); return; }

  const btn = document.getElementById("convRefBtn");
  if (!btn) return;

  // Build the popover
  const picker = document.createElement("div");
  picker.id = "conv-ref-picker";
  picker.className = "conv-ref-picker";

  // Get conversations (from global state)
  const allConvs = (typeof conversations !== "undefined" ? conversations : [])
    .filter(c => c.id !== (typeof activeConvId !== "undefined" ? activeConvId : ""))
    .sort((a, b) => (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0));

  picker.innerHTML = `
    <div class="conv-ref-picker-header">
      <span class="conv-ref-picker-title">${t('convRef.title')}</span>
      <button class="conv-ref-picker-close" title="关闭">&times;</button>
    </div>
    <div class="conv-ref-picker-search">
      <input type="text" id="convRefSearchInput" placeholder="${t('convRef.searchPh')}" autocomplete="off" spellcheck="false" />
    </div>
    <div class="conv-ref-picker-list" id="convRefPickerList"></div>
  `;

  // Position relative to the button
  document.body.appendChild(picker);
  _positionConvRefPicker(picker, btn);

  // Populate list
  _renderConvRefPickerList(allConvs, "");

  // Wire events
  const searchInput = picker.querySelector("#convRefSearchInput");
  searchInput.focus();

  searchInput.addEventListener("input", () => {
    const q = searchInput.value.trim().toLowerCase();
    const filtered = allConvs.filter(c =>
      !q || (c.title || "").toLowerCase().includes(q) || (c.id || "").includes(q)
    );
    _renderConvRefPickerList(filtered, q);
  });

  picker.querySelector(".conv-ref-picker-close").addEventListener("click", () => picker.remove());

  // Click outside to close
  setTimeout(() => {
    function _outsideClick(e) {
      if (!picker.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        picker.remove();
        document.removeEventListener("mousedown", _outsideClick);
      }
    }
    document.addEventListener("mousedown", _outsideClick);
  }, 50);

  // Escape to close
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") picker.remove();
  });
}

function _positionConvRefPicker(picker, anchor) {
  const rect = anchor.getBoundingClientRect();
  // Position above the button
  picker.style.position = "fixed";
  picker.style.bottom = (window.innerHeight - rect.top + 8) + "px";
  picker.style.left = Math.max(12, rect.left - 120) + "px";
  picker.style.width = "340px";
  picker.style.maxHeight = "400px";
}

function _renderConvRefPickerList(convs, query) {
  const listEl = document.getElementById("convRefPickerList");
  if (!listEl) return;

  if (convs.length === 0) {
    listEl.innerHTML = `<div class="conv-ref-picker-empty">${query ? t('convRef.noMatch') : t('convRef.noOther')}</div>`;
    return;
  }

  // Check which are already referenced
  const refIds = new Set(_pendingConvRefs.map(r => r.id));

  listEl.innerHTML = convs.slice(0, 30).map(c => {
    const isReffed = refIds.has(c.id);
    const title = escapeHtml(c.title || "Untitled");
    const msgCount = (c.messages || []).length;
    const ts = (c.updatedAt || c.createdAt) ? _formatRelativeTime(c.updatedAt || c.createdAt) : "";
    return `<div class="conv-ref-picker-item${isReffed ? " is-referenced" : ""}" data-id="${escapeHtml(c.id)}" data-title="${escapeHtml(c.title || "Untitled")}">
      <div class="conv-ref-picker-item-main">
        <span class="conv-ref-picker-item-title">${title}</span>
        ${isReffed ? '<span class="conv-ref-picker-item-check">✓</span>' : ""}
      </div>
      <div class="conv-ref-picker-item-meta">
        <span>${msgCount} 条消息</span>${ts ? `<span>· ${ts}</span>` : ""}
      </div>
    </div>`;
  }).join("");

  // Click handlers
  listEl.querySelectorAll(".conv-ref-picker-item").forEach(item => {
    item.addEventListener("click", () => {
      const id = item.dataset.id;
      const title = item.dataset.title;
      if (item.classList.contains("is-referenced")) {
        // Remove it
        const idx = _pendingConvRefs.findIndex(r => r.id === id);
        if (idx >= 0) removeConvRef(idx);
        item.classList.remove("is-referenced");
        item.querySelector(".conv-ref-picker-item-check")?.remove();
      } else {
        addConvRef(id, title);
        item.classList.add("is-referenced");
        const mainDiv = item.querySelector(".conv-ref-picker-item-main");
        if (mainDiv && !mainDiv.querySelector(".conv-ref-picker-item-check")) {
          mainDiv.insertAdjacentHTML("beforeend", '<span class="conv-ref-picker-item-check">✓</span>');
        }
      }
    });
  });
}

function _formatRelativeTime(ts) {
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return t('time.justNow');
  if (mins < 60) return `${mins}${t('time.minutesAgoFull')}`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}${t('time.hoursAgoFull')}`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}${t('time.daysAgoFull')}`;
  return new Date(ts).toLocaleDateString();
}

// ── Scroll branch panel to bottom ──
function renderFinishInfo(msg) {
  if (!msg.finishReason && !msg.usage && !msg.model && !msg.preset && !msg.effort) return "";
  const parts = [];
  const _mid = msg.model || msg.preset || msg.effort || "";
  const u = msg.usage || {};
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : n.toString());
  const thk = u.reasoning_tokens || u.thinking_tokens || 0;

  // ★ Model tag — auto-detect brand from model_id
  const depthIcons = { medium: '', high: '', max: '' };
  const depthLabels = { medium: "Med", high: "Hi", max: "Max" };
  if (_mid) {
    const _brand = typeof _detectBrand === 'function' ? _detectBrand(_mid) : 'generic';
    const icon = (typeof _brandSvg === 'function') ? _brandSvg(_brand, 12) : '✦';
    const displayName = typeof _modelShortName === 'function' ? _modelShortName(_mid) : _mid;
    // Append thinking depth ONLY for thinking-capable models
    const depth = msg.thinkingDepth || "";
    let depthStr = "";
    const _isThinkModel = typeof _isThinkingCapable === 'function' ? _isThinkingCapable(_mid) : false;
    if (depth && depthLabels[depth] && _isThinkModel) {
      depthStr = ` ${depthIcons[depth] || ""}${depthLabels[depth]}`;
    }
    parts.push(
      `<span class="finish-tag preset" data-preset="${_brand}" title="Model: ${escapeHtml(_mid)}${depth ? ' · Depth: ' + depth : ''}">${icon} ${displayName}${depthStr}</span>`,
    );
  }

  // ★ Finish reason tag — separate from model
  if (msg.finishReason) {
    const normReasons = ["stop", "end_turn", "stop_sequence"];
    const isNorm = normReasons.includes(msg.finishReason);
    const warnReasons = [
      "length",
      "tool_rounds_exhausted",
      "max_tokens",
      "content_filter",
      "premature_close",
      "abnormal_stop",
    ];
    if (isNorm) {
      parts.push(`<span class="finish-tag ok">✓</span>`);
    } else if (msg.finishReason === "error") {
      parts.push(`<span class="finish-tag err">✕ Error</span>`);
    } else if (msg.finishReason === "aborted") {
      parts.push(`<span class="finish-tag warn">Stopped</span>`);
    } else if (msg.finishReason === "interrupted") {
      parts.push(`<span class="finish-tag warn"><span title="Server crashed during generation. Content recovered from last checkpoint — may be incomplete.">Interrupted</span></span>`);
    } else if (msg.finishReason === "server_offline") {
      parts.push(
        `<span class="finish-tag err"><span title="Server went offline during generation (e.g. VSCode disconnect, network drop). Partial response saved.">Server Offline</span></span>` +
        ` <button class="finish-reconnect-btn" onclick="_recoverOfflineConversations('manual_button')" ` +
        `title="Check server for completed result" style="` +
        `font-size:11px;padding:1px 8px;margin-left:4px;cursor:pointer;` +
        `background:var(--accent);color:#fff;border:none;border-radius:4px;` +
        `vertical-align:middle;opacity:0.9` +
        `">🔄 Reconnect</button>`
      );
    } else {
      const labels = {
        length: "Truncated",
        tool_use: "Tool",
        tool_calls: "Tool",
        content_filter: "<span title='" + t('msg.contentFiltered') + "'>Filtered</span>",
        tool_rounds_exhausted: "Tool limit",
        max_tokens: "Truncated",
        premature_close: "<span title='" + t('msg.prematureClose') + "'>" + t('msg.gatewayInterrupt') + "</span>",
        abnormal_stop: "<span title='" + t('msg.abnormalStop') + "'>" + t('msg.abnormalInterrupt') + "</span>",
      };
      const label = labels[msg.finishReason] || msg.finishReason;
      const cls = warnReasons.includes(msg.finishReason) ? "warn" : "";
      parts.push(`<span class="finish-tag ${cls}">${label}</span>`);
    }
  }
  if (u) {
    const inp = u.prompt_tokens || u.input_tokens || 0;
    const out = u.completion_tokens || u.output_tokens || 0;
    // ★ API rounds info
    const rounds = msg.apiRounds || [];
    const numRounds = rounds.length;
    /* ★ Compute display input: for Anthropic-style APIs, prompt_tokens is
     *   only the uncached portion. The total input = uncached + cw + cr. */
    const _cw0 = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const _cr0 = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    const _displayInp = (inp <= _cw0 + _cr0 && (_cw0 > 0 || _cr0 > 0))
      ? inp + _cw0 + _cr0   /* Anthropic: inp is uncached only */
      : inp;                /* OpenAI: inp is already total */
    if (_displayInp > 0 || out > 0) {
      let tokText = `${fmt(_displayInp)} → ${fmt(out)}`;
      if (thk > 0)
        tokText += ` <span style="color:#a78bfa;opacity:0.8">(${fmt(thk)}${t('msg.thinking')})</span>`;
      if (numRounds > 1)
        tokText += ` <span style="opacity:0.7">[${numRounds}${t('msg.rounds')}]</span>`;
      parts.push(`<span class="token-tag">${tokText}</span>`);
    }
    const cw = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    // ★ Enhanced cost display with per-round breakdown
    const costInfo = calcCostCny(u, _mid);
    if (costInfo && costInfo.costCny > 0) {
      const fCny = (v) =>
        v >= 0.01 ? "¥" + v.toFixed(3) : v > 0 ? "¥" + v.toFixed(4) : "¥0";
      const tipLines = [];
      if (numRounds > 1) {
        tipLines.push(`共 ${numRounds} 轮 API 调用`);
        tipLines.push("");
        rounds.forEach((rd, i) => {
          const ru = rd.usage || {};
          const ri = ru.prompt_tokens || ru.input_tokens || 0;
          const ro = ru.completion_tokens || ru.output_tokens || 0;
          const rt = ru.reasoning_tokens || ru.thinking_tokens || 0;
          const rcw =
            ru.cache_write_tokens || ru.cache_creation_input_tokens || 0;
          const rcr = ru.cache_read_tokens || ru.cache_read_input_tokens || 0;
          const rdCost = calcCostCny(ru, _mid);
          const rdCnyStr = rdCost ? fCny(rdCost.costCny) : "¥0";
          let rdLabel = `第${i + 1}轮`;
          if (rd.tag && rd.tag.includes("FALLBACK"))
            rdLabel += ` 回退(${rd.model || "?"})`;
          const rdTrace = (ru.trace_id && typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) ? ` ${ru.trace_id.slice(0,8)}` : '';
          tipLines.push(
            `${rdLabel}: ${fmt(ri)}→${fmt(ro)}${rt > 0 ? " ✶" + fmt(rt) : ""} = ${rdCnyStr}${rcr > 0 ? " cache:" + fmt(rcr) : ""}${rcw > 0 ? " cw:" + fmt(rcw) : ""}${rdTrace}`,
          );
        });
        tipLines.push("");
      }
      /* ★ Show uncached input correctly — for Anthropic-style APIs where
       *   prompt_tokens is only the uncached residual, show that as the
       *   standard-rate input line, not the confusing raw prompt_tokens total */
      const _si = costInfo.inputTokens || 0;
      const _totalInp = costInfo.totalInputTokens || inp;
      if (_totalInp > _si && _si >= 0) {
        tipLines.push(
          `Input: ${fmt(_si)} tokens → ${fCny(costInfo.inputCostCny)}`,
        );
      } else {
        tipLines.push(
          `Input: ${fmt(inp)} tokens → ${fCny(costInfo.inputCostCny)}`,
        );
      }
      if (cw > 0)
        tipLines.push(
          `Cache write: ${fmt(cw)} tokens → ${fCny(costInfo.cacheWriteCostCny)}`,
        );
      if (cr > 0)
        tipLines.push(
          `Cache read: ${fmt(cr)} tokens → ${fCny(costInfo.cacheReadCostCny)}`,
        );
      tipLines.push(
        `Output: ${fmt(out)} tokens → ${fCny(costInfo.outputCostCny)}`,
      );
      if (thk > 0)
        tipLines.push(`Thinking: ${fmt(thk)} tokens (含在 output 中)`);
      if (costInfo.cacheSavingsCny > 0)
        tipLines.push(`Cache 节省: ${fCny(costInfo.cacheSavingsCny)}`);
      tipLines.push(`──────────`);
      tipLines.push(`Total: ${formatCny(costInfo.costCny)}`);
      // ★ Show trace_id(s) for debugging — all unique trace_ids from rounds (debug mode only)
      if (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) {
        const traceIds = rounds.map(rd => (rd.usage || {}).trace_id).filter(Boolean);
        const lastTrace = traceIds.length ? traceIds[traceIds.length - 1] : (u.trace_id || '');
        if (lastTrace) {
          tipLines.push(`TraceId: ${lastTrace}`);
        }
      }
      let savingsHtml = "";
      if (costInfo.cacheSavingsCny > 0)
        savingsHtml = ` <span style="color:#34d399;font-size:0.85em">↓${fCny(costInfo.cacheSavingsCny)}</span>`;
      parts.push(
        `<span class="cost-tag cost-tag-detail" title="${tipLines.join("\n")}">${formatCny(costInfo.costCny)}${savingsHtml}</span>`,
      );
    }
  }
  // ★ Clickable trace tag — click to copy full trace_id (debug mode only)
  if (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) {
    const _allTraces = (msg.apiRounds || []).map(rd => (rd.usage || {}).trace_id).filter(Boolean);
    const _lastTrace = _allTraces.length ? _allTraces[_allTraces.length - 1] : ((msg.usage || {}).trace_id || '');
    if (_lastTrace) {
      const _allStr = _allTraces.length > 1 ? _allTraces.join('\n') : _lastTrace;
      // Escape for safe embedding inside inline onclick JS string literal (single-quoted)
      const _jsSafe = _allStr.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n');
      parts.push(
        `<span class="finish-tag" style="cursor:pointer;opacity:0.6;font-size:0.8em" title="点击复制 trace_id:\n${escapeHtml(_allStr)}" onclick="_safeClipboardWrite('${_jsSafe}');this.textContent='copied'">${escapeHtml(_lastTrace.slice(0,8))}</span>`
      );
    }
  }
  if (msg.fallbackModel) {
    parts.push(
      `<span class="finish-tag warn" title="原模型 ${escapeHtml(msg.fallbackFrom || "?")} 失败，已回退到 ${escapeHtml(msg.fallbackModel)}">Fallback → Opus</span>`,
    );
  }
  if (parts.length === 0) return "";
  return `<div class="message-finish">${parts.join("")}</div>`;
}

// ═══════════════════════════════════════════════════════════════════
// ★ File Changes Tracker — shows which files were written/patched
// ═══════════════════════════════════════════════════════════════════

/**
 * Extract file change info from searchRounds (during/after streaming).
 * Returns array of {path, action, ok} objects.
 */
function _extractFileChangesFromRounds(searchRounds) {
  if (!searchRounds || !searchRounds.length) return [];
  const changes = new Map(); // path → {action, ok, pending}
  for (const round of searchRounds) {
    const tn = round.toolName;

    // ★ run_command: extract file changes from meta.fileChanges if present
    if (tn === 'run_command' && round.results && round.results.length) {
      const meta = round.results[0];
      if (meta.fileChanges && Array.isArray(meta.fileChanges)) {
        for (const fc of meta.fileChanges) {
          if (!fc.path) continue;
          const prev = changes.get(fc.path);
          changes.set(fc.path, {
            action: fc.action || 'modified',
            ok: true,
            count: (prev?.count || 0) + 1,
          });
        }
      }
      continue;
    }

    if (tn !== 'write_file' && tn !== 'apply_diff' && tn !== 'insert_content') continue;
    // ★ For in-progress writes, show as pending (during streaming only)
    if (!round.results || !round.results.length) {
      if (round.status === 'searching' && round.toolArgs) {
        try {
          const args = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs;
          const paths = [];
          if (args.edits && Array.isArray(args.edits)) {
            for (const e of args.edits) if (e.path) paths.push(e.path);
          } else if (args.path) {
            paths.push(args.path);
          }
          for (const p of paths) {
            if (!changes.has(p)) {
              changes.set(p, {
                action: tn === 'apply_diff' ? 'patching…' : tn === 'insert_content' ? 'inserting…' : 'writing…',
                ok: true, count: 0, pending: true
              });
            }
          }
        } catch (_) {}
      }
      continue;
    }
    const meta = round.results[0];
    const ok = meta.writeOk !== false;
    // For apply_diff / insert_content with edits array, extract all paths
    if ((tn === 'apply_diff' || tn === 'insert_content') && round.toolArgs) {
      try {
        const args = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs;
        if (args.edits && Array.isArray(args.edits)) {
          for (const e of args.edits) {
            if (e.path) {
              const prev = changes.get(e.path);
              changes.set(e.path, {
                action: tn === 'insert_content' ? 'inserted' : 'patched',
                ok: prev ? (prev.ok && ok) : ok,
                count: (prev?.count || 0) + 1
              });
            }
          }
          continue;
        }
        if (args.path) {
          const prev = changes.get(args.path);
          changes.set(args.path, {
            action: tn === 'insert_content' ? 'inserted' : 'patched',
            ok: prev ? (prev.ok && ok) : ok,
            count: (prev?.count || 0) + 1
          });
          continue;
        }
      } catch (_) { /* ignore parse errors */ }
    }
    // Single-file write_file or single apply_diff
    if (tn === 'write_file' && round.toolArgs) {
      try {
        const args = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs;
        const path = args.path || '';
        if (path) {
          const badge = (meta.badge || '').toLowerCase();
          const action = badge.includes('created') ? 'created' : 'written';
          const prev = changes.get(path);
          changes.set(path, {
            action: prev ? (prev.action === 'created' ? 'created' : action) : action,
            ok: prev ? (prev.ok && ok) : ok,
            count: (prev?.count || 0) + 1
          });
          continue;
        }
      } catch (_) {}
    }
    // Fallback: use meta.title which typically has the filename
    const fallbackPath = (meta.title || '').replace(/^(✅|❌|📝)\s*/, '');
    if (fallbackPath) {
      const prev = changes.get(fallbackPath);
      changes.set(fallbackPath, {
        action: tn === 'apply_diff' ? 'patched' : tn === 'insert_content' ? 'inserted' : 'written',
        ok: prev ? (prev.ok && ok) : ok,
        count: (prev?.count || 0) + 1
      });
    }
  }
  return Array.from(changes.entries()).map(([path, info]) => ({
    path, action: info.action, ok: info.ok, count: info.count, pending: !!info.pending
  }));
}

/**
 * Render the file-changes bar for a message.
 * Dual source: prefers modifiedFileList from done event; falls back to searchRounds extraction.
 */
function renderFileChangesBar(msg, msgIdx) {
  // Build change list from authoritative server data or from searchRounds
  let files = [];
  if (msg.modifiedFileList && msg.modifiedFileList.length) {
    files = msg.modifiedFileList.map(f => ({
      path: f.path, action: f.action, ok: true, count: 1
    }));
  } else if (msg.searchRounds) {
    files = _extractFileChangesFromRounds(msg.searchRounds);
  }
  if (!files.length) return '';
  return _renderFileChangesHtml(files, false, msgIdx);
}

/**
 * Core HTML renderer for file changes bar.
 * @param {Array} files - [{path, action, ok, count}]
 * @param {boolean} isStreaming - if true, add pulse animation
 */
function _renderFileChangesHtml(files, isStreaming, msgIdx) {
  if (!files.length) return '';
  const pendingCount = files.filter(f => f.pending).length;
  const okCount = files.filter(f => f.ok && !f.pending).length;
  const failCount = files.filter(f => !f.ok).length;
  const totalFiles = files.length;

  // Action icons
  const actionIcon = (action, ok, pending) => {
    if (pending) return '<span class="fc-icon fc-pending">⟳</span>';
    if (!ok) return '<span class="fc-icon fc-fail">✕</span>';
    if (action === 'created') return '<span class="fc-icon fc-created">+</span>';
    if (action === 'deleted') return '<span class="fc-icon fc-deleted">−</span>';
    if (action === 'modified') return '<span class="fc-icon fc-modified">∆</span>';
    if (action === 'patched') return '<span class="fc-icon fc-patched">~</span>';
    return '<span class="fc-icon fc-written">⇢</span>';
  };

  // Summary line
  const summaryParts = [];
  if (okCount > 0) summaryParts.push(`${okCount} file${okCount > 1 ? 's' : ''} changed`);
  if (pendingCount > 0) summaryParts.push(`${pendingCount} in progress`);
  if (failCount > 0) summaryParts.push(`${failCount} failed`);
  const summaryText = summaryParts.join(', ');
  const pulseClass = isStreaming ? ' fc-pulse' : '';
  const summaryIcon = '';

  // File list items
  const fileItems = files.map(f => {
    const dir = f.path.includes('/') ? f.path.substring(0, f.path.lastIndexOf('/') + 1) : '';
    const fname = f.path.includes('/') ? f.path.substring(f.path.lastIndexOf('/') + 1) : f.path;
    const countBadge = f.count > 1 ? ` <span class="fc-count">×${f.count}</span>` : '';
    const pendingCls = f.pending ? ' fc-file-pending' : '';
    return `<div class="fc-file${f.ok ? '' : ' fc-file-err'}${pendingCls}" title="${escapeHtml(f.path)}">
      ${actionIcon(f.action, f.ok, f.pending)}
      <span class="fc-path"><span class="fc-dir">${escapeHtml(dir)}</span><span class="fc-fname">${escapeHtml(fname)}</span></span>
      <span class="fc-action">${escapeHtml(f.action)}${countBadge}</span>
    </div>`;
  }).join('');

  // ★ Undo button — only for finalized (non-streaming) messages with a valid msgIdx
  const undoBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-btn" onclick="event.stopPropagation();undoConvModifications(${msgIdx})" title="撤销本轮修改">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/></svg>` +
      `<span>Undo</span></button>`
    : '';

  // ★ Undo All button — always available as a separate interaction point
  const undoAllBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-all-btn" onclick="event.stopPropagation();undoAllModifications()" title="撤销所有对话中的所有修改">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/><line x1="12" y1="7" x2="12" y2="3"/><line x1="8" y1="7" x2="12" y2="7"/></svg>` +
      `<span>Undo All</span></button>`
    : '';

  const actionBtns = (undoBtn || undoAllBtn)
    ? `<div class="fc-actions">${undoBtn}${undoAllBtn}</div>` : '';

  // Auto-expand for ≤ 5 files so users see details immediately
  const autoExpand = totalFiles <= 5 ? ' fc-expanded' : '';
  return `<div class="file-changes-bar${pulseClass}${autoExpand}" data-fc-count="${totalFiles}">
    <div class="fc-summary" onclick="this.parentElement.classList.toggle('fc-expanded')">
      <span class="fc-summary-icon">${summaryIcon}</span>
      <span class="fc-summary-text">${summaryText}</span>
      ${actionBtns}
      <span class="fc-chevron">›</span>
    </div>
    <div class="fc-details">${fileItems}</div>
  </div>`;
}

// ★ Round type detection
function _isRoundFetch(round) {
  return (
    round.toolName === "fetch_url" ||
    (round.query || "").startsWith("📄") ||
    (round.query || "").startsWith("🌐") ||
    (round.query || "").startsWith("📑")
  );
}
function _isRoundSearch(round) {
  return round.toolName === "web_search";
}
function _isRoundCodeExec(round) {
  return round.toolName === "code_exec";
}
function _isRoundProject(round) {
  return [
    "read_files",
    "list_dir",
    "grep_search",
    "find_files",
    "write_file",
    "apply_diff",
    "insert_content",
    "run_command",
  ].includes(round.toolName);
}
function _isRoundBrowser(round) {
  return [
    "browser_list_tabs",
    "browser_read_tab",
    "browser_execute_js",
    "browser_screenshot",
    "browser_get_cookies",
    "browser_get_history",
    "browser_create_tab",
    "browser_close_tab",
    "browser_navigate",
  ].includes(round.toolName);
}
function _isRoundImageGen(round) {
  return round.toolName === "generate_image";
}
function _isRoundSwarm(round) {
  /* Only treat as swarm if the backend flagged it AND there's real swarm content */
  if (!round._swarm) return false;
  /* Must have at least one agent OR meaningful results to render the swarm panel.
     Even during active spawning, we don't show the panel until agents arrive. */
  if (!round._swarmAgents?.length && !round.results?.length) return false;
  return true;
}

/* ★ Tool display metadata — icon, label, color for non-search/fetch tools */
const _TOOL_DISPLAY = {
  web_search:    { icon: "", label: "Searching", color: "#60a5fa" },
  fetch_url:     { icon: "", label: "Fetching",  color: "#34d399" },
  spawn_agents:  { icon: "", label: "Swarm",     color: "#f59e0b" },
  check_agents:  { icon: "", label: "Agents",    color: "#f59e0b" },
  create_memory:  { icon: "", label: "Memory",     color: "#a78bfa" },
  schedule_task: { icon: "", label: "Schedule",  color: "#fb923c" },
  timer_create:  { icon: "⏱️", label: "Timer Watcher", color: "#a855f7" },
  timer_manage:  { icon: "⏱️", label: "Timer",   color: "#a855f7" },
  bash_exec:     { icon: "▶️", label: "Running",   color: "#f472b6" },
  desktop_click: { icon: "", label: "Desktop",   color: "#94a3b8" },
  desktop_type:  { icon: "⌨️", label: "Desktop",   color: "#94a3b8" },
  desktop_screenshot: { icon: "", label: "Desktop", color: "#94a3b8" },
  generate_image: { icon: "", label: "Image", color: "#e879f9" },
  ask_human: { icon: "", label: "Guidance", color: "#a5b4fc" },
};
function _getToolDisplay(round) {
  if (_TOOL_DISPLAY[round.toolName]) return _TOOL_DISPLAY[round.toolName];
  if (_isRoundFetch(round))   return { icon: "", label: "Fetching",  color: "#34d399" };
  if (_isRoundSearch(round))  return { icon: "", label: "Searching", color: "#60a5fa" };
  if (_isRoundSwarm(round))   return { icon: "", label: "Swarm",     color: "#f59e0b" };
  if (_isRoundProject(round)) return { icon: "", label: "Project",   color: "#60a5fa" };
  if (_isRoundBrowser(round)) return { icon: "", label: "Browser",   color: "#38bdf8" };
  // Generic fallback — use the tool name itself
  const name = (round.toolName || "tool").replace(/_/g, " ");
  return { icon: "⚡", label: name.charAt(0).toUpperCase() + name.slice(1), color: "#94a3b8" };
}

function _getRoundBlockClass(round) {
  if (_isRoundFetch(round)) return "fetch-block";
  return "";
}
function _getRoundIcon(round) {
  if (_isRoundProject(round)) {
    const m = {
      read_files: "file",
      list_dir: "folder",
      grep_search: "search",
      find_files: "find",
      write_file: "write",
      apply_diff: "diff",
      insert_content: "insert",
      run_command: "terminal",
    };
    return m[round.toolName] || "folder";
  }
  if (_isRoundBrowser(round)) {
    const m = {
      browser_list_tabs: "tabs",
      browser_read_tab: "read",
      browser_execute_js: "js",
      browser_screenshot: "screenshot",
      browser_get_cookies: "cookie",
      browser_get_history: "history",
      browser_create_tab: "newtab",
      browser_close_tab: "close",
      browser_navigate: "navigate",
    };
    return m[round.toolName] || "tabs";
  }
  // Web search / fetch / generic
  if (_isRoundSearch(round)) return "web_search";
  if (_isRoundFetch(round)) return "fetch";
  if (_isRoundCodeExec(round)) return "code_exec";
  return round.toolName || "generic";
}
function _getRoundColor(round) {
  if (_isRoundImageGen(round)) return "#e879f9";
  if (_isRoundProject(round)) return "#f59e0b";
  if (_isRoundBrowser(round)) return "#a78bfa";
  if (_isRoundFetch(round)) return "#34d399";
  if (_isRoundSearch(round)) return "#60a5fa";
  if (_isRoundCodeExec(round)) return "#f472b6";
  return "#94a3b8";
}

// ═══════════════════════════════════════════
//  ★ Code Execution — Inline code block (legacy, kept for compat)
// ═══════════════════════════════════════════
function _renderCodeExecBlock(round, isSearching) {
  const meta = (round.results || [])[0] || {};
  const cmd = escapeHtml(meta.command || round.query || "");
  if (isSearching) {
    return `<div class="code-exec-block code-exec-running">
         <div class="code-exec-header"><span class="code-exec-icon">⚡</span><span class="code-exec-label">Running...</span><span class="ptool-spinner"></span></div>
         <pre class="code-exec-cmd"><code>$ ${cmd}</code></pre>
       </div>`;
  }
  const exitCode = meta.exitCode ?? "?";
  const timedOut = meta.timedOut || false;
  const output = meta.output || "";
  const isOk = exitCode === "0" || exitCode === 0;
  const statusCls = timedOut
    ? "code-exec-timeout"
    : isOk
      ? "code-exec-ok"
      : "code-exec-err";
  const statusLabel = timedOut
    ? "Timeout"
    : isOk
      ? "✓ Done"
      : `✗ exit ${exitCode}`;
  const outputHtml = output
    ? `<pre class="code-exec-output"><code>${escapeHtml(output)}</code></pre>`
    : "";
  return `<div class="code-exec-block ${statusCls}">
       <div class="code-exec-header"><span class="code-exec-icon">⚡</span><span class="code-exec-label">Code Execution</span><span class="code-exec-status">${statusLabel}</span></div>
       <pre class="code-exec-cmd"><code>$ ${cmd}</code></pre>
       ${outputHtml}
     </div>`;
}

// ═══════════════════════════════════════════
//  ★ Unified Tool Activity Panel
// ═══════════════════════════════════════════
const _projToolSvg = {
  file: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  folder:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  search:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>',
  find: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/><line x1="8" y1="11" x2="14" y2="11"/></svg>',
  write:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  diff: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v18"/><path d="M8 8l4-4 4 4"/><path d="M8 16l4 4 4-4"/></svg>',
  terminal:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
};

// ── Browser Tools — SVG Icons ──
const _browserToolSvg = {
  tabs: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 3v6"/></svg>',
  read: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
  js: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  screenshot:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M3 9h2"/><path d="M19 9h2"/></svg>',
  cookie:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a10 10 0 1 0 10 10 4 4 0 0 1-5-5 4 4 0 0 1-5-5"/><circle cx="8" cy="14" r="1"/><circle cx="12" cy="18" r="1"/><circle cx="16" cy="14" r="1"/></svg>',
  history:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  newtab:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
  close:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>',
  navigate:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>',
};
// ── Web/Fetch/Generic Tools — SVG Icons ──
const _webToolSvg = {
  web_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/><circle cx="11" cy="11" r="3" stroke-dasharray="2 2"/></svg>',
  fetch: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  code_exec: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
  create_memory: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L9 9l-7 1 5 5-1 7 6-3 6 3-1-7 5-5-7-1z"/></svg>',
  schedule_task: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  ask_human: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  generic: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
};

const _imageGenSvg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';

/* ── Get the correct SVG for any tool type ── */
function _getToolSvg(round) {
  const icon = _getRoundIcon(round);
  if (_isRoundImageGen(round)) return _imageGenSvg;
  if (_isRoundProject(round)) return _projToolSvg[icon] || _projToolSvg.file;
  if (_isRoundBrowser(round)) return _browserToolSvg[icon] || _browserToolSvg.tabs;
  return _webToolSvg[icon] || _webToolSvg[round.toolName] || _webToolSvg.generic;
}

/* ── Human Guidance card — interactive Q&A card from the LLM ── */
/* ★ Auto-translate integration: when conv.autoTranslate is ON, the LLM's
 *   question & choice options are automatically translated EN→CN for display.
 *   The user's free-text reply is auto-translated CN→EN before sending.
 *   This mirrors the same auto-translate flow as regular chat messages. */
function _renderHumanGuidanceCard(round, svg) {
  const gid = escapeHtml(round.guidanceId || '');
  const rawQuestion = round.guidanceQuestion || 'The AI needs your input';
  const respType = round.guidanceType || 'free_text';
  const options = round.guidanceOptions || [];

  // ★ Use translated question if available (populated by _autoTranslateHumanGuidance)
  const displayQuestion = round._translatedQuestion || rawQuestion;
  const isTranslating = !!round._hgTranslating;
  // Render the question with full Markdown — same renderer as assistant messages
  const questionHtml = renderMarkdown(displayQuestion);

  // ★ Translating indicator (shown while async EN→CN translation is in-flight)
  const translatingIndicator = isTranslating
    ? `<div class="hg-translating-indicator"><span class="hg-spinner"></span> 正在翻译问题…</div>`
    : '';

  let inputHtml = '';
  if (respType === 'choice' && options.length > 0) {
    // ── Multiple-choice option cards ──
    // ★ Use translated labels/descriptions if available
    const optCardsHtml = options.map((opt, i) => {
      const origLabel = opt.label || `Option ${i + 1}`;
      const displayLabel = opt._translatedLabel || origLabel;
      const displayDesc = opt._translatedDescription || opt.description || '';
      const descHtml = displayDesc
        ? `<div class="hg-opt-desc">${renderMarkdown(displayDesc)}</div>`
        : '';
      // ★ Always send the ORIGINAL English label to backend (not the translated one)
      // ★ escapeHtml the JSON.stringify output so double-quotes don't break onclick="..." attribute
      const safeJsonLabel = escapeHtml(JSON.stringify(origLabel));
      return `<button class="hg-option-card" data-gid="${gid}" data-label="${escapeHtml(origLabel)}"
                      onclick="event.stopPropagation();submitHumanGuidanceChoice('${gid}',${safeJsonLabel})">
                <div class="hg-opt-label">${escapeHtml(displayLabel)}</div>
                ${descHtml}
              </button>`;
    }).join('');
    inputHtml = `<div class="hg-options-grid">${optCardsHtml}</div>`;
  } else {
    // ── Free-text input area ──
    // ★ No manual Translate button — auto-translate is handled automatically
    //   on submit (CN→EN) when conv.autoTranslate is ON.
    inputHtml = `<div class="hg-freetext-wrap">
      <textarea class="hg-textarea" id="hg-input-${gid}" rows="3"
                placeholder="输入你的回答（支持中文，会自动翻译）…"
                onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();submitHumanGuidanceFreeText('${gid}')}"></textarea>
      <div class="hg-freetext-actions">
        <button class="hg-submit-btn" onclick="event.stopPropagation();submitHumanGuidanceFreeText('${gid}')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          提交
        </button>
      </div>
    </div>`;
  }

  return `<div class="hg-card" data-gid="${gid}">
    <div class="hg-header">
      
      <span class="hg-title">AI 需要你的指导</span>
      <span class="hg-badge">等待回复</span>
    </div>
    ${translatingIndicator}
    <div class="hg-question">${questionHtml}</div>
    ${inputHtml}
  </div>`;
}

function _renderUnifiedToolLine(round, isSearching) {
  const svg = _getToolSvg(round);
  const td = _getToolDisplay(round);
  const q = escapeHtml(round.query || "");
  const results = round.results || [];
  const meta = results[0] || {};


  // ★ Human Guidance — LLM is asking the user a question
  if (round.status === "awaiting_human" && round.guidanceId) {
    return _renderHumanGuidanceCard(round, svg);
  }

  // ★ Human Guidance — skipped (task ended before user answered)
  if (round.status === "done" && round.toolName === "ask_human" && round._hgSkipped) {
    const skippedQ = escapeHtml((round.guidanceQuestion || '').slice(0, 60));
    return `<div class="ptool-line hg-skipped-line">
      <span class="ptool-icon">${svg}</span>
      <span class="ptool-text">${td.label || 'Guidance'}${skippedQ ? ' — ' + skippedQ : ''}</span>
      <span class="ptool-badge ptool-badge-skip">未回答</span>
    </div>`;
  }

  // ★ Human Guidance — submitted but not yet confirmed by server (tool_result pending)
  if (round.status === "submitted" && round.toolName === "ask_human") {
    const respPreview = escapeHtml((round._hgUserResponse || '').slice(0, 80));
    return `<div class="ptool-line hg-submitted-line">
      <span class="ptool-icon">${svg}</span>
      <span class="ptool-text">${td.label || 'Guidance'}${respPreview ? ' — ' + respPreview : ''}</span>
      <span class="ptool-badge ptool-badge-done">✓ 已回答</span>
      <span class="hg-submitted-spinner" title="等待 AI 继续…"></span>
    </div>`;
  }

  // ★ Pending approval state — show approve/reject buttons
  if (round.status === "pending_approval" && round.approvalId) {
    const aid = escapeHtml(round.approvalId);
    const ameta = round.approvalMeta || {};
    let detailHtml = "";
    if (ameta.batchMode && ameta.editSummaries) {
      // ★ Batch apply_diff — show collapsible preview of all edits
      const edits = ameta.editSummaries;
      const maxPreviewLines = 12;
      let batchHtml = `<div class="ptool-batch-header">${edits.length} edit${edits.length > 1 ? "s" : ""} across ${ameta.path || "?"}</div>`;
      edits.forEach((ed, i) => {
        const sLines = (ed.search || "").split("\n");
        const rLines = (ed.replace || "").split("\n");
        const sShow = sLines.slice(0, maxPreviewLines);
        const rShow = rLines.slice(0, maxPreviewLines);
        let diffLines = "";
        sShow.forEach((l) => {
          diffLines += `<div class="ptool-diff-line ptool-diff-del"><span class="ptool-diff-sign">−</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
        });
        if (sLines.length > maxPreviewLines)
          diffLines += `<div class="ptool-diff-line ptool-diff-del ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${sLines.length - maxPreviewLines} more lines</span></div>`;
        diffLines += `<div class="ptool-diff-separator"></div>`;
        rShow.forEach((l) => {
          diffLines += `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
        });
        if (rLines.length > maxPreviewLines)
          diffLines += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${rLines.length - maxPreviewLines} more lines</span></div>`;
        const desc = ed.description ? escapeHtml(ed.description) : `Edit ${i + 1}`;
        const pathLabel = escapeHtml(ed.path || "?");
        batchHtml += `<details class="ptool-batch-edit"${i === 0 ? " open" : ""}>
          <summary class="ptool-batch-summary"><span class="ptool-batch-idx">#${i + 1}</span> <span class="ptool-batch-path">${pathLabel}</span> <span class="ptool-batch-desc">${desc}</span> <span class="ptool-batch-stats">${ed.searchLines || "?"}→${ed.replaceLines || "?"} lines</span></summary>
          <div class="ptool-diff-preview">${diffLines}</div>
        </details>`;
      });
      if ((ameta.editCount || edits.length) > edits.length)
        batchHtml += `<div class="ptool-batch-more">… and ${ameta.editCount - edits.length} more edits</div>`;
      detailHtml = `<div class="ptool-batch-preview">${batchHtml}</div>`;
    } else if (ameta.search != null && ameta.replace != null) {
      // Single apply_diff — show search→replace preview with line-by-line diff
      const searchLines = (ameta.search || "").split("\n");
      const replaceLines = (ameta.replace || "").split("\n");
      const totalSearchLines = ameta.searchLines || searchLines.length;
      const totalSearchChars = ameta.searchChars || ameta.search.length;
      const totalReplaceLines = ameta.replaceLines || replaceLines.length;
      const totalReplaceChars = ameta.replaceChars || ameta.replace.length;
      const maxLines = 30;
      const searchShow = searchLines.slice(0, maxLines);
      const replaceShow = replaceLines.slice(0, maxLines);
      let diffLines = "";
      searchShow.forEach((l) => {
        diffLines += `<div class="ptool-diff-line ptool-diff-del"><span class="ptool-diff-sign">−</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
      });
      if (totalSearchLines > maxLines)
        diffLines += `<div class="ptool-diff-line ptool-diff-del ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${totalSearchLines - maxLines} more lines (${totalSearchLines} lines · ${totalSearchChars.toLocaleString()} chars total)</span></div>`;
      diffLines += `<div class="ptool-diff-separator"></div>`;
      replaceShow.forEach((l) => {
        diffLines += `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
      });
      if (totalReplaceLines > maxLines)
        diffLines += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${totalReplaceLines - maxLines} more lines (${totalReplaceLines} lines · ${totalReplaceChars.toLocaleString()} chars total)</span></div>`;
      detailHtml = `<div class="ptool-diff-preview">${diffLines}</div>`;
    } else if (ameta.command != null) {
      // run_command — show command preview
      const cmdText = escapeHtml(ameta.command || "");
      detailHtml = `<div class="ptool-diff-preview"><pre class="ptool-cmd-code" style="margin:0;padding:8px 12px;font-size:12px;"><code>$ ${cmdText}</code></pre></div>`;
    } else if (ameta.contentPreview) {
      const previewLines = (ameta.contentPreview || "")
        .split("\n")
        .slice(0, 12);
      let previewContent = previewLines
        .map(
          (l) =>
            `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`,
        )
        .join("");
      if ((ameta.contentPreview || "").split("\n").length > 12)
        previewContent += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… more lines</span></div>`;
      detailHtml = `<div class="ptool-diff-preview">${previewContent}<div class="ptool-write-meta">${ameta.contentLines || "?"} lines · ${(ameta.contentChars || 0).toLocaleString()} chars</div></div>`;
    }
    return `<div class="ptool-pending-wrap">
         <div class="ptool-line ptool-pending">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           <span class="ptool-badge ptool-badge-warn">awaiting approval</span>
         </div>
         ${detailHtml}
         <div class="ptool-approval-btns">
           <button class="ptool-approve-btn" onclick="event.stopPropagation();resolveWriteApproval('${aid}',true)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg> Approve</button>
           <button class="ptool-reject-btn" onclick="event.stopPropagation();resolveWriteApproval('${aid}',false)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Reject</button>
         </div>
       </div>`;
  }

  // ★ Timer Watcher: render collapsible poll checks
  if (round._timerPolls && round._timerPolls.length > 0) {
    return _renderTimerWatcherBlock(round, svg);
  }
  // Timer tool with "searching" status but no polls yet — show initial waiting
  // After reconnection, backend now includes _timerPolls in state snapshots,
  // so this state should be brief (only before the first poll fires).
  if (round.toolName === "timer_create" && round.status === "searching" && !round._timerPolls) {
    // ★ Try to recover timer polls from the API if timerId is known
    if (round._timerTimerId && !round._timerPollsRecoveryAttempted) {
      round._timerPollsRecoveryAttempted = true;
      _recoverTimerPolls(round);
    }
    return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">⏱️</span>
         <span class="ptool-text">${q || "Timer Watcher"}</span>
         <span class="ptool-badge ptool-badge-warn">waiting for first poll…</span>
         <span class="ptool-spinner"></span>
       </div>`;
  }

  // ★ Interactive stdin: subprocess is waiting for user keyboard input
  if (round.status === "awaiting_stdin" && round.stdinId) {
    const cmdText = escapeHtml(round.query || round.stdinCommand || "");
    const promptText = escapeHtml(round.stdinPrompt || "");
    const sid = escapeHtml(round.stdinId);
    return `<div class="ptool-cmd-block ptool-cmd-stdin" data-rn="${round.roundNum}">
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${svg}</span>
           <span class="ptool-cmd-label">Waiting for input...</span>
           <span class="stdin-pulse"></span>
         </div>
         <pre class="ptool-cmd-code"><code>$ ${cmdText}</code></pre>
         ${promptText ? `<pre class="stdin-prompt-output"><code>${promptText}</code></pre>` : ''}
         <div class="stdin-input-area">
           <div class="stdin-input-row">
             <span class="stdin-caret">›</span>
             <input type="text" class="stdin-input" id="stdin-${sid}"
                    placeholder="Type your input here..."
                    onkeydown="if(event.key==='Enter'){event.preventDefault();submitStdinInput('${sid}',this.value)}" />
             <button class="stdin-submit-btn" onclick="submitStdinInput('${sid}', document.getElementById('stdin-${sid}').value)"
                     title="Send input">
               <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
             </button>
             <button class="stdin-eof-btn" onclick="submitStdinEof('${sid}')" title="Send EOF (close stdin)">
               EOF
             </button>
           </div>
         </div>
       </div>`;
  }

  if (isSearching) {
    // ★ run_command / code_exec: show running state with full command
    if (round.toolName === "run_command" || round.toolName === "code_exec") {
      const cmdText = escapeHtml(round.query || "");
      return `<div class="ptool-cmd-block ptool-cmd-running">
           <div class="ptool-cmd-header">
             <span class="ptool-cmd-icon">${svg}</span>
             <span class="ptool-cmd-label">Running...</span>
             <span class="ptool-spinner"></span>
           </div>
           <pre class="ptool-cmd-code"><code>$ ${cmdText}</code></pre>
         </div>`;
    }
    // ★ Web search: show orbit animation
    if (_isRoundSearch(round)) {
      return `<div class="ptool-line ptool-active ptool-search-line">
           <span class="ptool-icon"><div class="search-orbit-container" style="width:16px;height:16px"><div class="search-orbit-center" style="inset:4px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div></div></span>
           <span class="ptool-text">${q}</span>
           <span class="ptool-spinner"></span>
         </div>`;
    }
    return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${q}</span>
         <span class="ptool-spinner"></span>
       </div>`;
  }

  // ★ run_command / code_exec: render as inline terminal block with collapsible output
  if ((round.toolName === "run_command" || round.toolName === "code_exec") && (meta.command != null || meta.output != null)) {
    const cmd = escapeHtml(meta.command || round.query || "");
    const output = meta.output || "";
    const exitCode = meta.exitCode ?? "?";
    const timedOut = meta.timedOut || false;
    const isOk = exitCode === "0" || exitCode === 0;
    const statusCls = timedOut
      ? "ptool-cmd-timeout"
      : isOk
        ? "ptool-cmd-ok"
        : "ptool-cmd-err";
    const statusLabel = timedOut
      ? "timeout"
      : isOk
        ? "✓ done"
        : `✗ exit ${exitCode}`;
    let outputHtml = "";
    if (output) {
      outputHtml = `<div class="ptool-cmd-output-wrap">
           <div class="ptool-cmd-toggle" onclick="event.stopPropagation();var w=this.parentElement;w.classList.toggle('expanded');this.textContent=w.classList.contains('expanded')?'▾ Collapse':'▸ Show output';">▸ Show output</div>
           <pre class="ptool-cmd-output"><code>${escapeHtml(output)}</code></pre>
         </div>`;
    }
    return `<div class="ptool-cmd-block ${statusCls}" data-rn="${round.roundNum}">
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${svg}</span>
           <span class="ptool-cmd-label">${round.toolName === "code_exec" ? "Code Execution" : "Command"}</span>
           <span class="ptool-cmd-status">${statusLabel}</span>
         </div>
         <pre class="ptool-cmd-code"><code>$ ${cmd}</code></pre>
         ${outputHtml}
       </div>`;
  }

  // ★ Web search / fetch with 0 results — show descriptive reason
  if ((_isRoundSearch(round) || _isRoundFetch(round)) && results.length === 0) {
    const diag = round.searchDiag;
    let badgeText, badgeCls, detailHtml = "";
    if (diag) {
      if (diag.reason === "network_error") {
        badgeText = "network error";
        badgeCls = "ptool-badge-err";
        detailHtml = `<div class="ptool-search-diag">All search engines failed — server may have limited internet access.</div>`;
      } else if (diag.reason === "partial_network_error") {
        const failedEngines = Object.keys(diag.engine_errors || {}).join(", ") || "some engines";
        badgeText = "partial failure";
        badgeCls = "ptool-badge-warn";
        detailHtml = `<div class="ptool-search-diag">Network errors from ${escapeHtml(failedEngines)}; other engines returned no matches.</div>`;
      } else if (diag.reason === "exception") {
        badgeText = "✗ error";
        badgeCls = "ptool-badge-err";
        detailHtml = `<div class="ptool-search-diag">Search encountered an internal error.</div>`;
      } else {
        badgeText = "no matches";
        badgeCls = "ptool-badge-warn";
        detailHtml = `<div class="ptool-search-diag">All engines responded but found no matching results. Try different keywords.</div>`;
      }
    } else {
      badgeText = "no results";
      badgeCls = "ptool-badge-warn";
    }
    return `<div class="ptool-line${detailHtml ? " ptool-line-with-diag" : ""}">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${q}</span>
         <span class="ptool-badge ${badgeCls}">${badgeText}</span>
         ${_tcPreviewBtn(round)}
         ${detailHtml}
       </div>`;
  }

  // ★ Web search / fetch with results — collapsible result list inside panel
  if ((_isRoundSearch(round) || _isRoundFetch(round)) && results.length > 0) {
    const items = results.map((r) => {
      const fb = r.irrelevant
        ? `<span class="search-result-fetched" style="color:var(--text-muted);opacity:.6">✗ irrelevant</span>`
        : r.fetched
        ? `<span class="search-result-fetched${r.source === "PDF" ? " pdf" : ""}">✓ ${r.fetchedChars ? (r.fetchedChars > 1000 ? Math.round(r.fetchedChars / 1000) + "k" : r.fetchedChars) + " chars" : "fetched"}</span>`
        : "";
      return `<div class="search-result-item"><div class="search-result-title">${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a>` : `<span>${escapeHtml(r.title)}</span>`}<span class="search-result-source">${escapeHtml(r.source)}</span>${fb}</div>${r.snippet ? `<div class="search-result-snippet">${escapeHtml(r.snippet)}</div>` : ""}${r.url ? `<div class="search-result-url">${escapeHtml(r.url)}</div>` : ""}</div>`;
    }).join("");
    // ── Engine breakdown: show raw per-engine URLs (before dedup/filter) ──
    let engineBkdnHtml = "";
    const eb = round.engineBreakdown;
    if (eb && typeof eb === "object") {
      const engines = Object.keys(eb);
      if (engines.length > 0) {
        const totalRaw = engines.reduce((s, e) => s + (eb[e] ? eb[e].length : 0), 0);
        const ebInner = engines.map((eng) => {
          const urls = eb[eng] || [];
          const urlItems = urls.map((u) =>
            `<div class="eb-url-item"><a href="${escapeHtml(u.url)}" target="_blank" rel="noopener">${escapeHtml(u.title || u.url)}</a><div class="eb-url-text">${escapeHtml(u.url)}</div></div>`
          ).join("");
          return `<div class="eb-engine"><div class="eb-engine-name">${escapeHtml(eng)} <span class="eb-engine-count">(${urls.length})</span></div><div class="eb-engine-urls">${urlItems}</div></div>`;
        }).join("");
        engineBkdnHtml = `<div class="eb-section">
          <div class="eb-toggle" onclick="event.stopPropagation();this.parentElement.classList.toggle('eb-expanded')">🔍 Engine Sources <span class="eb-total">${totalRaw} raw → ${results.length} final</span> <span class="eb-arrow">▸</span></div>
          <div class="eb-content">${ebInner}</div>
        </div>`;
      }
    }
    return `<div class="ptool-results-block" data-rn="${round.roundNum}">
         <div class="ptool-line ptool-results-header" onclick="if(event.target.closest('[data-tc-preview]'))return;event.stopPropagation();this.parentElement.classList.toggle('expanded')">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           <span class="ptool-badge ptool-badge-info">${results.length} result${results.length !== 1 ? "s" : ""}</span>
           ${_tcPreviewBtn(round)}
           <span class="ptool-results-toggle">▼</span>
         </div>
         <div class="ptool-results-content">${items}${engineBkdnHtml}</div>
       </div>`;
  }

  // ★ Image generation: render inline image card
  if (_isRoundImageGen(round)) {
    const imgUri = meta.imageDataUri || "";
    const imgErr = meta.imageError || "";
    const prompt = meta.imagePrompt || escapeHtml(round.query || "").replace(/^🎨\s*Generating[^:]*:\s*/i, "");
    const imgAR = meta.imageAspectRatio || "";
    const imgRes = meta.imageResolution || "";
    const paramsBadges = (imgAR || imgRes)
      ? `<span class="ptool-badge ptool-badge-info ig-params">${imgAR ? escapeHtml(imgAR) : ""}${imgAR && imgRes ? " · " : ""}${imgRes ? escapeHtml(imgRes) : ""}</span>`
      : "";
    if (imgUri) {
      const projPath = meta.imageProjectPath || "";
      const projBadge = projPath
        ? `<div class="ig-project-path" title="Saved to project: ${escapeHtml(projPath)}">${escapeHtml(projPath)}</div>`
        : "";
      return `<div class="ptool-imagegen-block" data-rn="${round.roundNum}">
           <div class="ptool-line ptool-imagegen-header">
             <span class="ptool-icon">${svg}</span>
             <span class="ptool-text">${q}</span>
             ${paramsBadges}
             <span class="ptool-badge ptool-badge-ok">${escapeHtml(meta.badge || "✓ done")}</span>
             ${_tcPreviewBtn(round)}
           </div>
           <div class="imagegen-card">
             <img src="${imgUri}" alt="${escapeHtml((prompt || "").slice(0, 100))}" loading="lazy"
                  onclick="_openImageFullscreen(this.src)" />
             <div class="imagegen-card-footer">
               <span class="ig-prompt" title="${escapeHtml(prompt)}">${escapeHtml((prompt || "").slice(0, 80))}${(prompt || "").length > 80 ? "…" : ""}</span>
               <div class="ig-actions">
                 <button class="ig-action-btn" onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
                 <button class="ig-action-btn" onclick="event.stopPropagation();_openImageFullscreen(this.closest('.imagegen-card').querySelector('img').src)" title="Fullscreen">⛶</button>
               </div>
             </div>
             ${projBadge}
           </div>
         </div>`;
    } else if (imgErr) {
      return `<div class="ptool-imagegen-block ptool-imagegen-error" data-rn="${round.roundNum}">
           <div class="ptool-line">
             <span class="ptool-icon">${svg}</span>
             <span class="ptool-text">${q}</span>
             <span class="ptool-badge ptool-badge-err">failed</span>
             ${_tcPreviewBtn(round)}
           </div>
           <div class="imagegen-error">
             <div class="ig-error-title">Image generation failed</div>
             <div class="ig-error-text">${escapeHtml(imgErr)}</div>
           </div>
         </div>`;
    }
    // In-progress: no image yet, no error — show animated generating state
    const progressBadge = meta.badge || "generating…";
    const progressCls = progressBadge.includes("rate limited") ? "ptool-badge-err" : "ptool-badge-warn";
    return `<div class="ptool-imagegen-block ptool-imagegen-loading" data-rn="${round.roundNum}">
         <div class="ptool-line ptool-active">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           ${paramsBadges}
           <span class="ptool-badge ${progressCls}">${escapeHtml(progressBadge)}</span>
           <span class="ptool-spinner"></span>
         </div>
       </div>`;
  }

  // Determine badge
  let badgeHtml = "";
  if (meta.badge) {
    const isWrite =
      round.toolName === "write_file" || round.toolName === "apply_diff";
    const ok = meta.writeOk !== false;
    const cls = isWrite
      ? ok
        ? "ptool-badge-ok"
        : "ptool-badge-err"
      : "ptool-badge-info";
    badgeHtml = `<span class="ptool-badge ${cls}">${escapeHtml(meta.badge)}</span>`;
  } else if (meta.fetchedChars) {
    const fc = meta.fetchedChars;
    const txt = fc > 1000 ? Math.round(fc / 1000) + "k chars" : fc + " chars";
    badgeHtml = `<span class="ptool-badge ptool-badge-info">${txt}</span>`;
  }
  // ★ Generic tool done with no results and no badge — show ✓ done
  if (!badgeHtml && !_isRoundProject(round) && !_isRoundBrowser(round) && results.length === 0) {
    const elapsed = round._elapsed ? ` · ${round._elapsed}` : "";
    badgeHtml = `<span class="ptool-badge ptool-badge-ok">✓ done${elapsed}</span>`;
  }

  return `<div class="ptool-line">
       <span class="ptool-icon">${svg}</span>
       <span class="ptool-text">${q}</span>
       ${badgeHtml}
       ${_tcPreviewBtn(round)}
     </div>`;
}

// ★ Backwards compat alias
const _renderProjectToolLine = _renderUnifiedToolLine;

/* ── Timer poll recovery: fetch poll log from API when _timerPolls is missing ──
   This handles edge cases where the state snapshot doesn't include polls
   (e.g. old server version, or server restarted and lost in-memory state). */
async function _recoverTimerPolls(round) {
  const timerId = round._timerTimerId;
  if (!timerId) return;
  try {
    const resp = await fetch(apiUrl(`/api/timer/${timerId}/status?limit=50`),
                             { signal: AbortSignal.timeout(5000) });
    if (!resp.ok) return;
    const data = await resp.json();
    const polls = data.poll_log || [];
    if (polls.length > 0) {
      // poll_log is newest-first from the API, reverse for chronological order
      const chronological = [...polls].reverse();
      const recoveredPolls = chronological.map((p, idx) => ({
        pollNum: p.poll_num || p.pollNum || (idx + 1),
        decision: p.decision || 'wait',
        reason: (p.reason || '').slice(0, 200),
        tokensUsed: p.tokens_used || 0,
        timerId: timerId,
        ts: p.poll_time ? new Date(p.poll_time).getTime() : Date.now(),
      }));
      const triggered = chronological.some(p => p.decision === 'ready');

      // Apply to the round object — but also search the active conv's
      // searchRounds in case the round reference was replaced by a state event
      round._timerPolls = recoveredPolls;
      if (triggered) { round._timerTriggered = true; round.status = 'done'; }

      if (activeConvId) {
        const conv = conversations.find(c => c.id === activeConvId);
        const lastMsg = conv?.messages?.[conv.messages.length - 1];
        if (lastMsg?.searchRounds) {
          const liveRound = lastMsg.searchRounds.find(r =>
            r.toolName === 'timer_create' && r._timerTimerId === timerId && !r._timerPolls
          );
          if (liveRound && liveRound !== round) {
            liveRound._timerPolls = recoveredPolls;
            if (triggered) { liveRound._timerTriggered = true; liveRound.status = 'done'; }
          }
        }
        twUpdate(activeConvId);
      }
      console.info(`[Timer] Recovered ${recoveredPolls.length} polls for timer ${timerId.slice(0,12)}`);
    }
  } catch (e) {
    console.debug('[Timer] Poll recovery failed:', e.message);
  }
}

/* ── Timer Watcher Block ──
   Renders the timer_create tool call as a collapsible panel showing
   each poll check (wait/ready/error) with timestamps and reasons.
   While polling, shows a live "watching…" header; after trigger, shows "✓ triggered". */
function _renderTimerWatcherBlock(round, svg) {
  const polls = round._timerPolls || [];
  const isActive = round.status === "searching";
  const triggered = round._timerTriggered;
  const timerId = round._timerTimerId || "";
  const totalPolls = polls.filter(p => p.decision !== "started").length;
  const timerIdShort = timerId ? timerId.slice(0, 12) : "";

  // Header
  let headerLabel, headerCls;
  if (triggered) {
    headerLabel = `⏱️ Timer ${timerIdShort} — ✅ triggered after ${totalPolls} poll${totalPolls !== 1 ? "s" : ""}`;
    headerCls = "timer-watcher-triggered";
  } else if (round._timerOrphaned) {
    headerLabel = `⏱️ Timer ${timerIdShort} — ⚠️ task interrupted (${totalPolls} poll${totalPolls !== 1 ? "s" : ""}, timer still active in background)`;
    headerCls = "timer-watcher-orphaned";
  } else if (isActive) {
    headerLabel = `⏱️ Timer ${timerIdShort} — watching… (${totalPolls} poll${totalPolls !== 1 ? "s" : ""})`;
    headerCls = "timer-watcher-active";
  } else {
    headerLabel = `⏱️ Timer ${timerIdShort} — ${round.status || "done"} (${totalPolls} polls)`;
    headerCls = "";
  }

  // Build poll lines (most recent first for readability)
  const reversed = [...polls].reverse();
  const MAX_VISIBLE = 5;
  const visible = reversed.slice(0, MAX_VISIBLE);
  const hidden = reversed.length - MAX_VISIBLE;

  let pollLines = "";
  for (const p of visible) {
    let icon, cls;
    if (p.decision === "started") {
      icon = "🔔"; cls = "timer-poll-started";
    } else if (p.decision === "ready") {
      icon = "✅"; cls = "timer-poll-ready";
    } else if (p.decision === "error") {
      icon = "❌"; cls = "timer-poll-error";
    } else {
      icon = "⏳"; cls = "timer-poll-wait";
    }
    const ts = p.ts ? new Date(p.ts).toLocaleTimeString() : "";
    const reason = escapeHtml((p.reason || "").slice(0, 120));
    const pollLabel = p.decision === "started" ? "" : `#${p.pollNum}`;
    const tokens = p.tokensUsed ? ` · ${p.tokensUsed} tok` : "";
    pollLines += `<div class="timer-poll-line ${cls}">
      <span class="timer-poll-icon">${icon}</span>
      <span class="timer-poll-num">${pollLabel}</span>
      <span class="timer-poll-reason">${reason}</span>
      <span class="timer-poll-meta">${ts}${tokens}</span>
    </div>`;
  }

  let hiddenHtml = "";
  if (hidden > 0) {
    hiddenHtml = `<div class="timer-poll-hidden">${hidden} earlier check${hidden !== 1 ? "s" : ""} hidden</div>`;
  }

  const uid = "tmr-r" + round.roundNum;
  const expandedByDefault = isActive;  // auto-expand while active
  return `<div class="timer-watcher-block ${headerCls}" data-rn="${round.roundNum}">
       <div class="timer-watcher-header" onclick="event.stopPropagation();var w=document.getElementById('${uid}-wrap');w.classList.toggle('expanded');var t=this.querySelector('.timer-toggle');if(t)t.textContent=w.classList.contains('expanded')?'▾':'▸';">
         <span class="timer-watcher-label">${headerLabel}</span>
         ${isActive ? '<span class="ptool-spinner"></span>' : ''}
         <span class="timer-toggle">${expandedByDefault ? '▾' : '▸'}</span>
       </div>
       <div class="timer-watcher-body${expandedByDefault ? ' expanded' : ''}" id="${uid}-wrap">
         ${pollLines}${hiddenHtml}
       </div>
     </div>`;
}

function _renderUnifiedGroup(allRounds) {
  const anyActive = allRounds.some((r) => r.status === "searching");
  const count = allRounds.length;
  const headerLabel = anyActive
    ? `Working… (${count})`
    : `${count} tool${count > 1 ? "s" : ""} used`;
  const STATIC_LIMIT = 100;
  let lines, truncHtml = "";
  if (!anyActive && count > STATIC_LIMIT) {
    const tail = allRounds.slice(-50);
    lines = tail.map((r) => _renderUnifiedToolLine(r, false)).join("");
    const hiddenN = count - 50;
    truncHtml = `<div class="ptool-truncated" data-hidden-count="${hiddenN}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${hiddenN} earlier tool calls hidden — click to expand</span></div>`;
  } else {
    lines = allRounds
      .map((r) => _renderUnifiedToolLine(r, r.status === "searching"))
      .join("");
  }
  return `<div class="ptool-panel${anyActive ? " ptool-panel-active" : ""}">
       <div class="ptool-panel-header">
         <span class="ptool-panel-label">${headerLabel}</span>
       </div>
       <div class="ptool-panel-body" data-full-count="${count}">${truncHtml}${lines}</div>
     </div>`;
}

// ★ Backwards compat aliases
const _renderProjectGroup = _renderUnifiedGroup;
const _renderBrowserGroup = _renderUnifiedGroup;

// ── Tool content preview button ──
function _tcPreviewBtn(round) {
  if (!round || !round.toolContent) return "";
  return `<button class="tc-preview-btn" data-tc-preview data-tc-rn="${round.roundNum}" data-tc-tcid="${escapeHtml(round.toolCallId || '')}" title="Preview tool content">Preview</button>`;
}

function renderSearchRoundsHTML(rounds, isStreaming) {
  if (!rounds || rounds.length === 0) return "";
  // ★ UNIFIED: All tool rounds go into one panel (except swarm, which has its own dashboard)
  const toolRounds = [];
  let swarmGroup = null;
  for (const round of rounds) {
    if (_isRoundSwarm(round)) {
      if (swarmGroup) { swarmGroup.items.push(round); }
      else { swarmGroup = { items: [round] }; toolRounds.push({ _swarmPlaceholder: true, swarmGroup }); }
    } else {
      toolRounds.push(round);
    }
  }

  let html = "";
  // ★ Collect non-swarm rounds for unified panel
  const unifiedRounds = toolRounds.filter(r => !r._swarmPlaceholder);
  const swarmPlaceholder = toolRounds.find(r => r._swarmPlaceholder);

  if (unifiedRounds.length > 0) {
    html += _renderUnifiedGroup(unifiedRounds);
  }
  // Swarm panel rendered separately (its own dashboard UI)
  if (swarmGroup) {
    const bestRound = swarmGroup.items.find(r => r._swarmAgents) || swarmGroup.items[swarmGroup.items.length - 1];
    html += _buildSwarmPanelHTML(bestRound);
  }
  return html;
}

/* ── Lazy thinking expand ────────────────────────────────
   Don't dump 30-100k+ chars of thinking text into the DOM
   on every render — inject it only when the user expands.
   This prevents DevTools / Elements tab from choking.      */

function _toggleThinking(el, msgIdx) {
  el.classList.toggle("expanded");
  const txt = el.querySelector(".thinking-text");
  if (!txt || txt.textContent) return; // already populated
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (msg?.thinking) {
    txt.textContent = msg.thinking;
  }
}

/* Lazy-load branch thinking via <details> toggle event */
document.addEventListener("toggle", function (e) {
  const det = e.target;
  if (!det.classList?.contains("branch-thinking") || !det.open) return;
  const lazy = det.querySelector(".branch-think-lazy");
  if (!lazy || lazy.textContent) return; // already loaded
  const mIdx = +det.dataset.branchThinkMsgidx;
  const bIdx = +det.dataset.branchThinkBidx;
  const bMsgIdx = +det.dataset.branchThinkMidx;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[mIdx];
  const branch = msg?.branches?.[bIdx];
  const bMsg = branch?.messages?.[bMsgIdx];
  if (bMsg?.thinking) {
    lazy.textContent = bMsg.thinking;
  }
}, true);

function copyMessage(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[idx];
  if (!msg) return;
  // Copy what the user currently sees on screen:
  // - Assistant with active translation → translatedContent (Chinese)
  // - User with originalContent (auto-translated input) → originalContent (Chinese)
  // - Otherwise → content
  const isUser = msg.role === "user";
  const showTrans = !isUser && msg.translatedContent && msg._showingTranslation !== false;
  let textToCopy;
  if (showTrans) {
    textToCopy = msg.translatedContent;
  } else if (isUser && msg.originalContent) {
    textToCopy = msg.originalContent;
  } else {
    textToCopy = msg.content || "";
  }
  _safeClipboardWrite(textToCopy)
    .then(() => {
      const btn = document.querySelector(`#msg-${idx} .copy-msg-btn`);
      if (btn) {
        const orig = btn.innerHTML;
        btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
        btn.classList.add("copied");
        setTimeout(() => {
          btn.innerHTML = orig;
          btn.classList.remove("copied");
        }, 1500);
      }
    })
    .catch(() => {});
}

// ── Copy bilingual original text ──
function copyBilingualOriginal(btn, role, idx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[idx];
  if (!msg) return;
  const text = msg.content || '';
  _safeClipboardWrite(text).then(() => {
    const origHTML = btn.innerHTML;
    btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`;
    btn.classList.add('copied');
    setTimeout(() => { btn.innerHTML = origHTML; btn.classList.remove('copied'); }, 1500);
  }).catch(() => {});
}

// ── Translate message ──
async function translateMessage(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[idx];
  if (!msg || msg.role === "user") return;
  // If we already have a cached translation (manual or auto-translate), just toggle
  if (msg._translatedCache || msg.translatedContent) {
    if (msg._showingTranslation !== false) {
      // Currently showing translation → revert to original
      msg._showingTranslation = false;
    } else {
      // Currently showing original → switch to translation
      msg._showingTranslation = true;
    }
    saveConversations(conv.id);
    syncConversationToServerDebounced(conv);  // persist toggle state to server
    /* ★ FIX: Use surgical single-element replacement instead of full renderChat()
     *   to avoid destroying the #streaming-msg when a stream is active.
     *   renderChat(conv) without forceScroll=false does a full innerHTML wipe. */
    const el = document.getElementById(`msg-${idx}`);
    if (el) {
      const _ct = document.getElementById('chatContainer');
      const _sv = _ct ? _ct.scrollTop : -1;
      el.outerHTML = renderMessage(msg, idx);
      if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
    } else {
      renderChat(conv);
    }
    return;
  }
  // First time: call translate API via async task (survives page reload)
  // Clear any previous error state
  delete msg._translateError;
  delete msg._translateTaskId;
  msg._translateDone = false;
  const text = msg.content || "";
  if (!text.trim()) return;
  const btn = document.querySelector(`#msg-${idx} .msg-translate-btn`);
  if (!btn) return;
  // Detect target language: if mostly Chinese → English, otherwise → Chinese
  const chineseChars = (text.match(/[\u4e00-\u9fff]/g) || []).length;
  const targetLang = chineseChars / text.length > 0.3 ? "English" : "Chinese";
  // Save original content
  msg._originalContent = text;
  // Show loading state
  const origHTML = btn.innerHTML;
  btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="spin-icon"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Translating...`;
  btn.disabled = true;

  try {
    // Start async task — server will auto-commit result to DB even if we navigate away
    const taskId = await _startTranslateTask(
      text, targetLang, "",
      conv.id, idx, "translatedContent"
    );
    if (!taskId) throw new Error("Failed to start translation task");

    // Save taskId on message so _resumePendingTranslations can pick it up
    msg._translateTaskId = taskId;
    msg._translateField = "translatedContent";
    msg._translateDone = false;
    saveConversations(conv.id);

    // Poll for result
    const _pollManual = async (attempt) => {
      if (attempt > 30) {
        // Give up polling but server will still auto-commit
        btn.innerHTML = origHTML;
        btn.disabled = false;
        btn.title = "Translation in progress — will appear when you return";
        return;
      }
      await new Promise(r => setTimeout(r, attempt < 3 ? 1000 : 2000));

      const result = await _pollTranslateTask(taskId);
      if (result.status === 'done') {
        msg._translatedCache = result.translated;
        msg.translatedContent = result.translated;
        msg._showingTranslation = true;
        msg._translateDone = true;
        delete msg._translateTaskId;
        saveConversations(conv.id);
        syncConversationToServerDebounced(conv);
        /* ★ FIX: surgical render to avoid destroying #streaming-msg */
        if (activeConvId === conv.id) {
          const _te = document.getElementById(`msg-${idx}`);
          if (_te) {
            const _ct = document.getElementById('chatContainer');
            const _sv = _ct ? _ct.scrollTop : -1;
            _te.outerHTML = renderMessage(msg, idx);
            if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
          } else {
            renderChat(conv);
          }
        }
        return;
      }
      if (result.status === 'error' || result.status === 'not_found') {
        // Server auto-committed — reload from server
        console.warn(`[translateMessage] Task ${taskId} error/expired — reloading conv from server`);
        try {
          const resp = await fetch(apiUrl(`/api/conversations/${conv.id}`));
          if (resp.ok) {
            const data = await resp.json();
            if (data.messages?.[idx]?.translatedContent) {
              msg.translatedContent = data.messages[idx].translatedContent;
              msg._translatedCache = msg.translatedContent;
              msg._showingTranslation = true;
              msg._translateDone = true;
              delete msg._translateTaskId;
              saveConversations(conv.id);
              /* ★ FIX: surgical render to avoid destroying #streaming-msg */
              if (activeConvId === conv.id) {
                const _te2 = document.getElementById(`msg-${idx}`);
                if (_te2) {
                  const _ct = document.getElementById('chatContainer');
                  const _sv = _ct ? _ct.scrollTop : -1;
                  _te2.outerHTML = renderMessage(msg, idx);
                  if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
                } else {
                  renderChat(conv);
                }
              }
              return;
            }
          }
        } catch (e2) { /* ignore */ }

        msg._translateError = result.error || "Translation failed";
        delete msg._translateTaskId;
        btn.innerHTML = origHTML;
        btn.disabled = false;
        return;
      }
      // Still running
      _pollManual(attempt + 1);
    };
    _pollManual(0);
  } catch (e) {
    console.error("Translation task start failed:", e);
    const errMsg = e.message || "Translation failed";
    debugLog(`Translation error: ${errMsg}`, "error");
    btn.innerHTML = origHTML;
    btn.disabled = false;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> ${errMsg.length > 30 ? 'Failed' : errMsg}`;
    btn.title = errMsg;
    setTimeout(() => {
      btn.innerHTML = origHTML;
      btn.disabled = false;
      btn.title = '';
    }, 4000);
  }
}

// ── Edit messages ──
// ── Backup of main input state while editing ──
let _editBackupImages = [];
let _editBackupPdfs = [];
let _editBackupInput = "";

function startEditMessage(idx) {
  const conv = getActiveConv();
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const msg = conv.messages[idx];
  if (!msg || msg.role !== "user") return;
  const msgEl = document.getElementById("msg-" + idx);
  if (!msgEl) return;
  _editingMsgIdx = idx;
  // ★ Backup current shared input state, then load message's attachments
  _editBackupImages = [...pendingImages];
  _editBackupPdfs = [...pendingPdfTexts];
  const mainInput = document.getElementById("userInput");
  _editBackupInput = mainInput ? mainInput.value : "";
  // Load message's existing attachments into the shared state
  pendingImages = [...(msg.images || [])];
  pendingPdfTexts = [...(msg.pdfTexts || [])];
  // Clear any pending reply quote in the input area
  if (typeof clearReplyQuote === "function") clearReplyQuote();
  const bodyEl = msgEl.querySelector(".message-body");
  bodyEl.innerHTML = `<div class="edit-area"><div class="image-previews" id="editImagePreviews"></div><textarea class="edit-textarea" id="edit-textarea-${idx}"></textarea><div class="edit-actions"><button class="edit-cancel-btn" onclick="cancelEditMessage(${idx})">Cancel</button><button class="edit-save-btn" onclick="saveEditOnly(${idx})">Save</button><button class="edit-resend-btn" onclick="saveEditAndResend(${idx})">Save &amp; Resend</button></div><div class="edit-hint">Save: keep subsequent · Save &amp; Resend: truncate and regenerate · Drop/paste files to attach</div></div>`;
  // ★ Render AFTER DOM is built so #editImagePreviews exists
  renderImagePreviews();
  const ta = document.getElementById("edit-textarea-" + idx);
  if (ta) {
    ta.value = msg.originalContent || msg.content || "";
    ta.focus();
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 300) + "px";
    ta.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 300) + "px";
      if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean &&
          !this.value.includes(_pendingLogClean.originalText)) {
        if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
      }
    });
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        e.preventDefault();
        cancelEditMessage(idx);
      }
      // Ctrl+Shift+K — wrap selected text in <notranslate> tags
      if (e.key === "K" && e.ctrlKey && e.shiftKey) {
        e.preventDefault();
        if (typeof _wrapSelectionNoTranslate === 'function') _wrapSelectionNoTranslate(this);
      }
    });
    // ── Paste handler: reuse shared input path (same as #userInput) ──
    ta.addEventListener("paste", async (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      let hasImage = false;
      for (const item of items) {
        if (item.type.startsWith("image/")) {
          e.preventDefault();
          hasImage = true;
          const f = item.getAsFile();
          const d = await processImageFile(f);
          pendingImages.push(d);
          renderImagePreviews();
        }
      }
      if (!hasImage && typeof detectLogNoise === 'function') {
        const pastedText = e.clipboardData?.getData("text");
        if (pastedText && pastedText.length > 200) {
          setTimeout(() => {
            const result = detectLogNoise(ta.value);
            if (result && typeof showLogCleanBanner === 'function') showLogCleanBanner(result);
            else if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
          }, 50);
        }
      }
    });
  }
  const act = msgEl.querySelector(".message-actions");
  if (act) act.style.display = "none";
}

/** Restore shared input state from backup after edit completes or cancels. */
function _restoreInputFromBackup() {
  pendingImages = _editBackupImages;
  pendingPdfTexts = _editBackupPdfs;
  _editBackupImages = [];
  _editBackupPdfs = [];
  renderImagePreviews();
  if (typeof _vlmSaveState === 'function') _vlmSaveState();  // ★ Persist restored state
  const mainInput = document.getElementById("userInput");
  if (mainInput) mainInput.value = _editBackupInput;
  _editBackupInput = "";
}

function cancelEditMessage(idx) {
  _editingMsgIdx = null;
  _restoreInputFromBackup();
  // Dismiss log clean banner if it was shown for the edit textarea
  if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean &&
      typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
  const conv = getActiveConv();
  if (!conv) return;
  /* ★ FIX: Surgical single-message restore instead of full renderChat().
   * renderChat() without forceScroll=false does a full innerHTML wipe +
   * _forceScrollToBottom, which causes the page to jump to the bottom.
   * Since cancel doesn't change message data, we only need to restore
   * the original message DOM for the edited element. */
  const msgEl = document.getElementById("msg-" + idx);
  if (msgEl && conv.messages[idx]) {
    msgEl.outerHTML = renderMessage(conv.messages[idx], idx);
    _lastRenderedFingerprint = _convRenderFingerprint(conv);
  } else {
    renderChat(conv);
  }
}
function saveEditOnly(idx) {
  _editingMsgIdx = null;
  // Auto-apply log clean if banner is showing
  if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean) {
    const editTa = document.getElementById("edit-textarea-" + idx);
    if (editTa) editTa.value = editTa.value.replace(_pendingLogClean.originalText, _pendingLogClean.cleanedText);
    if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
  }
  const conv = getActiveConv();
  if (!conv) return;
  const ta = document.getElementById("edit-textarea-" + idx);
  if (!ta) return;
  const t = ta.value.trim();
  const msg = conv.messages[idx];
  // ★ Collect attachments from shared state (skip still-parsing PDFs)
  msg.images = [...pendingImages];
  msg.pdfTexts = pendingPdfTexts.filter(p => p.method !== "parsing");
  // ★ Restore main input state from backup
  _restoreInputFromBackup();
  if (!t && !(msg.images?.length > 0) && !(msg.pdfTexts?.length > 0)) return;
  // ★ Always set content to edited text first
  msg.content = t;
  // ★ Use per-conv autoTranslate (not global) — matches sendMessage behavior
  const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
  // ★ Auto-translate: detect Chinese and fire-and-forget translate task
  // Works for both previously-translated and never-translated messages
  if (_convAutoTranslate && t) {
    const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(t);
    if (hasChinese) {
      msg.originalContent = t;
      const convId = conv.id;
      (async () => {
        try {
          const taskId = await _startTranslateTask(t, "English", "Chinese", convId, idx, "content");
          if (taskId) {
            msg._translateTaskId = taskId;
            msg._translateField = "content";
            msg._translateDone = false;
            saveConversations(convId);
            const _poll = async (attempt) => {
              if (attempt > 20) return;
              await new Promise(r => setTimeout(r, attempt < 3 ? 1500 : 3000));
              const result = await _pollTranslateTask(taskId);
              if (result.status === 'done' && result.translated) {
                msg.content = result.translated;
                msg._translateDone = true;
                saveConversations(convId);
                syncConversationToServerDebounced(conversations.find(c => c.id === convId));
                if (activeConvId === convId) {
                  const msgEl = document.getElementById("msg-" + idx);
                  if (msgEl) msgEl.outerHTML = renderMessage(msg, idx);
                }
              } else if (result.status === 'running') { _poll(attempt + 1); }
            };
            _poll(0);
          }
        } catch (e) { console.error("Edit translation task failed:", e); }
      })();
    }
  }
  // Reply quotes and conv refs: keep as-is
  if (msg.replyQuote && !msg.replyQuotes) {
    msg.replyQuotes = [msg.replyQuote];
    delete msg.replyQuote;
  }
  saveConversations(conv.id);
  syncConversationToServerDebounced(conv);
  /* ★ FIX: Surgical single-message update instead of full renderChat().
   * renderChat() without forceScroll=false does a full innerHTML wipe +
   * _forceScrollToBottom, which causes the page to jump to the bottom.
   * Since saveEditOnly only changes one message, replace just that element. */
  const msgEl = document.getElementById("msg-" + idx);
  if (msgEl) {
    msgEl.outerHTML = renderMessage(msg, idx);
    _lastRenderedFingerprint = _convRenderFingerprint(conv);
  } else {
    renderChat(conv);
  }
}
async function saveEditAndResend(idx) {
  _editingMsgIdx = null;
  // Auto-apply log clean if banner is showing
  if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean) {
    const editTa = document.getElementById("edit-textarea-" + idx);
    if (editTa) editTa.value = editTa.value.replace(_pendingLogClean.originalText, _pendingLogClean.cleanedText);
    if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
  }
  const conv = getActiveConv();
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const ta = document.getElementById("edit-textarea-" + idx);
  if (!ta) return;
  const t = ta.value.trim();
  const msg = conv.messages[idx];
  // ★ Collect attachments from shared state (skip still-parsing PDFs)
  msg.images = [...pendingImages];
  msg.pdfTexts = pendingPdfTexts.filter(p => p.method !== "parsing");
  // ★ Restore main input state from backup
  _restoreInputFromBackup();
  if (!t && !(msg.images?.length > 0) && !(msg.pdfTexts?.length > 0)) return;
  delete msg.originalContent;
  msg.content = t;

  // ★ Use per-conv autoTranslate with BLOCKING poll (matches sendMessage exactly)
  const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
  let needsTranslation = false;
  if (_convAutoTranslate && t) {
    const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(t);
    if (hasChinese) {
      msg.originalContent = t;
      needsTranslation = true;
    }
  }

  // Reply quotes and conv refs: keep as-is
  if (msg.replyQuote && !msg.replyQuotes) {
    msg.replyQuotes = [msg.replyQuote];
    delete msg.replyQuote;
  }
  msg.timestamp = Date.now();
  conv.messages = conv.messages.slice(0, idx + 1);
  /* ★ FIX: After truncation, clear _needsLoad and reset _serverMsgCount.
   * Without this, startAssistantResponse's _needsLoad guard reloads from DB,
   * which brings back the old assistant messages that were just truncated.
   * Also reset _serverMsgCount so syncConversationToServer's "fewer messages"
   * guard doesn't block the truncated save. */
  conv._needsLoad = false;
  conv._serverMsgCount = conv.messages.length;
  if (conv.messages.filter((m) => m.role === "user").length === 1 && t) {
    // ★ Strip <notranslate>/<nt> wrapper tags so they don't appear in sidebar titles
    const titleSource = stripNoTranslateTags(msg.originalContent || t);
    conv.title = titleSource.slice(0, 60) + (titleSource.length > 60 ? "..." : "");
    document.getElementById("topbarTitle").textContent = conv.title;
  }
  saveConversations(conv.id);

  /* ── Surgical DOM truncation (same pattern as regenerateFromUser) ──
   * Remove only the message nodes after the edited message instead of
   * wiping innerHTML via renderChat, which causes a scroll-jump-to-top. */
  let usedSurgical = false;
  if (activeConvId === conv.id) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      const toRemove = [];
      inner.querySelectorAll('.message[id^="msg-"]').forEach(el => {
        const m = el.id.match(/^msg-(\d+)$/);
        if (m && parseInt(m[1], 10) > idx) toRemove.push(el);
      });
      const oldStreaming = document.getElementById("streaming-msg");
      if (oldStreaming) toRemove.push(oldStreaming);
      /* Also re-render the edited message itself (content changed) */
      const editedEl = document.getElementById("msg-" + idx);
      if (editedEl) {
        editedEl.outerHTML = renderMessage(msg, idx);
      }
      if (toRemove.length > 0 || inner.querySelector('.message[id^="msg-"]')) {
        for (const el of toRemove) el.remove();
        usedSurgical = true;
        _lastRenderedFingerprint = _convRenderFingerprint(conv);
        buildTurnNav(conv);
      }
    }
  }
  if (!usedSurgical) {
    renderChat(conv);
  }

  renderConversationList();

  // ── ★ NON-BLOCKING auto-translate: fire background, free UI immediately ──
  if (needsTranslation) {
    const convId = conv.id;
    conv._translating = true;
    conv._translateAborted = false;
    if (typeof updateSendButton === 'function') updateSendButton();
    renderConversationList();
    if (activeConvId === convId) {
      const msgEl = document.getElementById('msg-' + idx);
      if (msgEl) msgEl.classList.add('user-translating');
    }
    _translateThenRespond(conv, convId, msg, idx, t, { allowTruncate: true });
    return;
  }

  // ── Wait for VLM parsing to complete before starting generation ──
  if (msg.pdfTexts?.length > 0 && typeof _waitForVlmParsing === 'function') {
    await _waitForVlmParsing(msg, conv.id, idx);
  }

  /* ★ FIX: Persist the truncated messages to server BEFORE starting the new task. */
  await syncConversationToServer(conv, { allowTruncate: true });
  await startAssistantResponse(conv.id);
}

// ── Turn navigation ──
function _turnWriteInfo(conv, userMsgIdx) {
  /* Scan the assistant response for this turn — collect modified file names.
   * Returns array of short filenames, or null if no writes. */
  const files = new Set();
  for (let j = userMsgIdx + 1; j < conv.messages.length; j++) {
    const m = conv.messages[j];
    if (m.role === 'user') break; // hit the next turn
    for (const r of (m.searchRounds || [])) {
      if ((r.toolName === 'write_file' || r.toolName === 'apply_diff') && r.status === 'done') {
        // toolArgs is a JSON string, not a parsed object
        try {
          const args = typeof r.toolArgs === 'string' ? JSON.parse(r.toolArgs) : (r.toolArgs || {});
          if (args.path) {
            files.add(args.path.split('/').pop());
          } else if (Array.isArray(args.edits)) {
            // apply_diff batch mode: edits[].path
            for (const e of args.edits) {
              if (e?.path) files.add(e.path.split('/').pop());
            }
          }
        } catch (_) {
          // Malformed toolArgs — still mark as write turn even without filename
          files.add('(unknown)');
        }
      }
    }
  }
  return files.size > 0 ? [...files] : null;
}
/* ★ Perf: skip rebuild when user message count + last user content haven't changed.
 * buildTurnNav scans ALL messages and JSON.parse-s tool args (_turnWriteInfo),
 * which costs 50-200ms for large conversations. During streaming, only assistant
 * content changes — user messages are static, so the turn nav doesn't need updates. */
let _turnNavFp = "";
function buildTurnNav(conv) {
  const nav = document.getElementById("turnNav");
  if (!nav) return;
  if (!conv || conv.messages.length === 0) {
    nav.innerHTML = "";
    _turnNavFp = "";
    return;
  }
  /* Fingerprint: count of user messages + last user message content (first 40 chars) */
  let _uCount = 0, _lastUContent = "";
  for (let i = 0; i < conv.messages.length; i++) {
    if (conv.messages[i].role === "user") {
      _uCount++;
      _lastUContent = (conv.messages[i].content || "").slice(0, 40);
    }
  }
  const _fp = _uCount + ":" + _lastUContent + ":" + conv.messages.length;
  if (_fp === _turnNavFp) return;
  _turnNavFp = _fp;
  let tn = 0;
  const turns = [];
  for (let i = 0; i < conv.messages.length; i++) {
    const role = conv.messages[i].role;
    if (role === "user") {
      tn++;
      const isCritic = !!conv.messages[i]._isEndpointReview;
      const writeFiles = _turnWriteInfo(conv, i);
      const rawPreview = (conv.messages[i].content || "").split("\n")[0].slice(0, 40);
      turns.push({
        num: tn,
        msgIdx: i,
        preview: isCritic ? `${rawPreview}` : rawPreview,
        isCritic: isCritic,
        writeFiles: writeFiles,
      });
    }
  }
  if (turns.length < 2) {
    nav.innerHTML = "";
    return;
  }
  nav.innerHTML =
    '<div class="turn-nav-label">Turns</div>' +
    turns
      .map((t) => {
        const safe = t.preview
          .replace(/&/g, "&amp;")
          .replace(/"/g, "&quot;")
          .replace(/</g, "&lt;");
        const criticCls = t.isCritic ? ' turn-dot-critic' : '';
        const writesCls = t.writeFiles ? ' turn-dot-writes' : '';
        const writeTip = t.writeFiles ? (' ' + t.writeFiles.join(', ')).replace(/"/g, '&quot;') : '';
        return `<div class="turn-dot${criticCls}${writesCls}" data-msg-idx="${t.msgIdx}" onclick="scrollToTurn(${t.msgIdx})" title="Turn ${t.num}: ${safe}${writeTip}">${t.num}</div>`;
      })
      .join("");
  requestAnimationFrame(() => updateActiveTurn());
}
function scrollToTurn(idx) {
  let el = document.getElementById("msg-" + idx);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  /* Message not in DOM yet — lazy-load all messages down to idx, then scroll */
  if (idx < _lazyRenderedFrom) {
    const conv = conversations.find((c) => c.id === _lazyConvId);
    if (!conv) return;
    const inner = document.getElementById("chatInner");
    const sentinel = document.getElementById("_lazyLoadSentinel");
    const container = document.getElementById("chatContainer");
    if (!inner || !container) return;

    const targetStart = Math.max(0, idx);
    const endIdx = _lazyRenderedFrom;
    let html = "";
    for (let i = targetStart; i < endIdx; i++) {
      html += renderMessage(conv.messages[i], i);
    }

    const prevScrollTop = container.scrollTop;
    const prevScrollHeight = container.scrollHeight;

    const wrapper = document.createElement("div");
    wrapper.innerHTML = html;
    const frag = document.createDocumentFragment();
    while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
    if (sentinel) {
      sentinel.after(frag);
    } else {
      inner.prepend(frag);
    }
    _lazyRenderedFrom = targetStart;

    /* Fix scroll position so current view doesn't jump */
    container.scrollTop = prevScrollTop + (container.scrollHeight - prevScrollHeight);

    /* Update or remove sentinel */
    if (sentinel) {
      if (targetStart <= 0) {
        sentinel.remove();
      } else {
        const countEl = sentinel.querySelector("._lazy-count");
        if (countEl) countEl.textContent = targetStart;
        if (_lazyObserver) _lazyObserver.observe(sentinel);
      }
    }

    /* Now scroll to the newly rendered element */
    el = document.getElementById("msg-" + idx);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}
/* ★ Perf: (1) cache one getBoundingClientRect for container, (2) only touch classList
 * when active dot actually changes, (3) break early once past threshold */
let _lastActiveDotIdx = -1;
function updateActiveTurn() {
  const nav = document.getElementById("turnNav");
  if (!nav || !nav.children.length) return;
  const ct = _getChatContainer() || document.getElementById("chatContainer");
  if (!ct) return;
  const ctRect = ct.getBoundingClientRect();
  const thr = ctRect.top + ctRect.height * 0.3;
  const dots = nav.querySelectorAll(".turn-dot");
  if (!dots.length) return;
  let ai = 0;
  for (let i = 0; i < dots.length; i++) {
    const el = document.getElementById("msg-" + dots[i].getAttribute("data-msg-idx"));
    if (el && el.getBoundingClientRect().top <= thr) ai = i;
  }
  if (ai !== _lastActiveDotIdx) {
    if (_lastActiveDotIdx >= 0 && _lastActiveDotIdx < dots.length)
      dots[_lastActiveDotIdx].classList.remove("active");
    dots[ai].classList.add("active");
    _lastActiveDotIdx = ai;
  }
}

// ══════════════════════════════════════════════
//  Streaming UI
// ══════════════════════════════════════════════
function _ensureStreamZones(body) {
  if (body.querySelector('[data-zone="tool"]')) return;
  body.innerHTML =
    '<div data-zone="tool"></div><div data-zone="thinking"></div><div data-zone="content"></div><div data-zone="fc"></div><div data-zone="status"></div>';
}

/* ★ Helper: check if user has an active text selection inside the streaming message area */
function _hasSelectionInStreaming() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
  const body = document.getElementById("streaming-body");
  if (!body) return false;
  for (let i = 0; i < sel.rangeCount; i++) {
    const r = sel.getRangeAt(i);
    if (body.contains(r.startContainer) || body.contains(r.endContainer))
      return true;
  }
  return false;
}
let _pendingStreamMsg = null;
let _pendingStreamTimer = null;
/* ★ Cached zone references — avoid querySelector on every frame */
let _streamZoneCache = { body: null, tool: null, think: null, content: null, fc: null, status: null };
function _getStreamZones() {
  const body = document.getElementById("streaming-body");
  if (!body) return null;
  if (_streamZoneCache.body !== body) {
    _ensureStreamZones(body);
    _streamZoneCache = {
      body,
      tool: body.querySelector('[data-zone="tool"]'),
      think: body.querySelector('[data-zone="thinking"]'),
      content: body.querySelector('[data-zone="content"]'),
      fc: body.querySelector('[data-zone="fc"]'),
      status: body.querySelector('[data-zone="status"]'),
    };
  }
  return _streamZoneCache;
}
function updateStreamingUI(msg) {
  const zones = _getStreamZones();
  if (!zones) return;
  const { body, tool: toolZone, think: thinkZone, content: contentZone, fc: fcZone, status: statusZone } = zones;
  const rounds = msg.searchRounds || [];
  const hasActiveSearch = rounds.some((r) => r.status === "searching");
  _syncSearchRoundsDOM(toolZone, rounds);
  // ★ Live file-changes tracker — update during streaming
  // PERF: _extractFileChangesFromRounds calls JSON.parse on every round's toolArgs
  // which is expensive for 50+ rounds.  Skip when rounds haven't changed.
  if (fcZone && rounds.length > 0) {
    // Quick fingerprint: count + last round's status + last round's toolName
    const _lastR = rounds[rounds.length - 1];
    const _fcFp = rounds.length + ':' + (_lastR.status || '') + ':' + (_lastR.toolName || '') + ':' + ((_lastR.results && _lastR.results.length) || 0);
    if (fcZone._roundsFp !== _fcFp) {
      fcZone._roundsFp = _fcFp;
      const liveFiles = _extractFileChangesFromRounds(rounds);
      const fcKey = liveFiles.map(f => `${f.path}:${f.action}:${f.ok}:${f.pending||''}`).join('|');
      if (fcZone.getAttribute('data-fc-key') !== fcKey) {
        fcZone.setAttribute('data-fc-key', fcKey);
        fcZone.innerHTML = liveFiles.length ? _renderFileChangesHtml(liveFiles, true) : '';
      }
    }
  } else if (fcZone) {
    if (fcZone.getAttribute('data-fc-key')) {
      fcZone.setAttribute('data-fc-key', '');
      fcZone.innerHTML = '';
    }
  }
  if (msg.thinking) {
    let block = thinkZone.querySelector(".thinking-block");
    if (!block) {
      const still = !msg.content;
      thinkZone.innerHTML = `<div class="thinking-block ${still ? "expanded" : ""}" onclick="this.classList.toggle('expanded')"><div class="thinking-header"><span class="thinking-label">${still ? "Thinking..." : "Thinking Process"}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
      block = thinkZone.querySelector(".thinking-block");
    }
    const textEl = block.querySelector(".thinking-text");
    if (textEl && textEl.textContent !== msg.thinking) textEl.textContent = msg.thinking;
    const labelEl = block.querySelector(".thinking-label");
    const _thinkLbl = msg.content ? "Thinking Process" : "Thinking...";
    if (labelEl && labelEl.textContent !== _thinkLbl)
      labelEl.textContent = _thinkLbl;
  }
  /* ★ FIX: Skip content DOM update while user has active selection to prevent flicker/deselect */
  if (_hasSelectionInStreaming()) {
    _pendingStreamMsg = msg;
    if (!_pendingStreamTimer) {
      _pendingStreamTimer = setInterval(() => {
        if (!_hasSelectionInStreaming() && _pendingStreamMsg) {
          const m = _pendingStreamMsg;
          _pendingStreamMsg = null;
          clearInterval(_pendingStreamTimer);
          _pendingStreamTimer = null;
          updateStreamingUI(m);
        }
      }, 300);
    }
    return;
  }
  _pendingStreamMsg = null;
  if (_pendingStreamTimer) {
    clearInterval(_pendingStreamTimer);
    _pendingStreamTimer = null;
  }
  /* ★ Incremental content rendering: only re-render the new "tail" of content.
   * We split content at the last stable paragraph/block boundary and only update
   * the tail portion, avoiding full DOM teardown on every token.
   *
   * PERF STRATEGY:
   * - Cache the rendered HTML of the "frozen" portion (everything before the last
   *   paragraph boundary).  This avoids calling renderMarkdown on potentially 10k+
   *   chars of already-rendered content on every frame.
   * - Move the freeze point forward aggressively: whenever the tail grows past
   *   REFREEZE_THRESHOLD chars, find a new paragraph boundary and advance.
   * - The tail (typically 200-800 chars) is the ONLY part re-rendered each frame.
   */
  try {
    if (msg.content) {
      const content = msg.content;
      const prevLen = contentZone._streamRendered || 0;
      const frozenLen = contentZone._frozenLen || 0;
      const REFREEZE_THRESHOLD = 600; // advance freeze point when tail exceeds this

      const mdContentEl = contentZone.querySelector(".md-content");
      const tailEl = mdContentEl && mdContentEl.querySelector(".md-stream-tail");
      const tailLen = content.length - frozenLen;

      if (frozenLen > 0 && tailLen < REFREEZE_THRESHOLD && tailEl && mdContentEl) {
        /* Fast path: tail is small, just re-render the tail portion.
         * The frozen HTML in the DOM is untouched — zero work for that part. */
        const tail = content.slice(frozenLen);
        try {
          tailEl.innerHTML = renderMarkdown(tail);
        } catch (_) {
          tailEl.innerHTML = escapeHtml(tail);
        }
        contentZone._streamRendered = content.length;
      } else {
        /* Need to (re)freeze: either first render, or tail grew past threshold.
         * Find the last stable paragraph boundary and split there. */
        const freezeIdx = content.lastIndexOf("\n\n", content.length - 60);

        if (freezeIdx > 100 && content.length > 300) {
          const frozenText = content.slice(0, freezeIdx);
          const tailText = content.slice(freezeIdx);

          /* ★ PERF: reuse cached frozen HTML if freeze point didn't move */
          let frozenHtml;
          if (frozenLen === freezeIdx && contentZone._frozenHtml) {
            frozenHtml = contentZone._frozenHtml;
          } else {
            frozenHtml = renderMarkdown(frozenText);
            contentZone._frozenHtml = frozenHtml;
          }

          const tailHtml = renderMarkdown(tailText);
          contentZone.innerHTML =
            `<div class="md-content">${frozenHtml}<span class="md-stream-tail">${tailHtml}</span></div>`;
          contentZone._frozenLen = freezeIdx;
        } else {
          /* Content too short to split — render whole thing */
          contentZone.innerHTML = `<div class="md-content">${renderMarkdown(content)}</div>`;
          contentZone._frozenLen = 0;
          contentZone._frozenHtml = null;
        }
        contentZone._streamRendered = content.length;
        /* Restore collapsed states for code blocks */
        contentZone.querySelectorAll("pre[data-collapsed]").forEach((pre) => {
          pre.setAttribute("data-collapsed", "true");
          const btn = pre.querySelector(".code-collapse-btn");
          if (btn) btn.textContent = "Expand";
        });
      }
    } else {
      contentZone.innerHTML = "";
      contentZone._streamRendered = 0;
      contentZone._frozenLen = 0;
      contentZone._frozenHtml = null;
    }
  } catch (e) {
    contentZone.innerHTML = `<div class="md-content">${escapeHtml(msg.content || "")}</div>`;
  }
  /* ★ Phase-aware status indicator — shows what the model is doing between visible outputs */
  const phase = msg.phase;
  /* ★ Build phase HTML and only update DOM when content actually changes (prevents flicker) */
  let _phaseKey = "";
  let _phaseHtml = "";
  const _phaseIcons = { llm_thinking: "", tool_exec: "", compacting: "" };
  if (phase && phase.phase === "thinking_active") {
    /* ★ Model is actively generating thinking tokens (works on ALL rounds,
     *   even when msg.content is already non-empty from previous tool rounds) */
    const _thLen = phase._thinkingLen || 0;
    const _thSize = _thLen >= 1024 ? `${(_thLen / 1024).toFixed(1)}k` : `${_thLen}`;
    _phaseKey = "thinking-active";
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">Reasoning<span class="stream-phase-counter" data-counter="thinking">${_thSize} chars</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "llm_thinking") {
    const icon = _phaseIcons[phase.phase];
    _phaseKey = "think:" + phase.detail + (phase.toolContext || "");
    const ctx = phase.toolContext
      ? `<span class="stream-phase-ctx">${escapeHtml(phase.toolContext)}</span>`
      : "";
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-icon">${icon}</span><span class="stream-phase-text">${escapeHtml(phase.detail)}${ctx}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "compacting") {
    _phaseKey = "compact:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "retrying") {
    _phaseKey = "retry:" + (phase.attempt || 0);
    _phaseHtml = `<div class="stream-phase stream-phase-retrying"><span class="stream-phase-icon">⟳</span><span class="stream-phase-text">${escapeHtml(phase.detail || 'Retrying…')}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "tool_exec" && !hasActiveSearch) {
    _phaseKey = "exec:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "working" && phase.detail) {
    /* Generic "working" phase from external backends (e.g. "Initializing Claude Code...") */
    _phaseKey = "working:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (hasActiveSearch) {
    _phaseKey = "search";
    _phaseHtml = "";
  } else if (!msg.content && !msg.thinking) {
    _phaseKey = "wait";
    _phaseHtml =
      '<div class="stream-status"><div class="pulse"></div> Waiting…</div>';
  } else if (!msg.content && msg.thinking) {
    _phaseKey = "think-only";
    const _thLen = msg.thinking.length;
    const _thSize = _thLen >= 1024 ? `${(_thLen / 1024).toFixed(1)}k` : `${_thLen}`;
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">Deep thinking<span class="stream-phase-counter">${_thSize} chars</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else {
    _phaseKey = "none";
    _phaseHtml = "";
  }
  if (statusZone.getAttribute("data-phase-key") !== _phaseKey) {
    statusZone.setAttribute("data-phase-key", _phaseKey);
    statusZone.innerHTML = _phaseHtml;
  }
  /* ★ Live counter update for thinking phases — avoids full DOM rebuild on every token */
  if (_phaseKey === "think-only" || _phaseKey === "thinking-active") {
    const _ctrEl = statusZone.querySelector('.stream-phase-counter');
    if (_ctrEl) {
      const _tl = (phase && phase._thinkingLen) || (msg.thinking ? msg.thinking.length : 0);
      const _ts = _tl >= 1024 ? `${(_tl / 1024).toFixed(1)}k` : `${_tl}`;
      _ctrEl.textContent = _ts + ' chars';
    }
  }
  /* ★ FIX: Only auto-scroll when user hasn't scrolled away (smaller threshold to avoid hijacking) */
  if (isNearBottom(80)) scrollToBottom();
}

function _syncSearchRoundsDOM(container, rounds) {
  // ★ Fast-path: skip if rounds haven't changed since last sync
  let _fp = rounds.length;
  for (let i = 0; i < rounds.length; i++) {
    const r = rounds[i];
    _fp = _fp * 31 + (r.roundNum | 0);
    _fp = _fp * 31 + (r.status === 'searching' ? 1 : r.status === 'done' ? 2
        : r.status === 'awaiting_human' ? 3 : r.status === 'submitted' ? 4
        : r.status === 'pending_approval' ? 5 : 0);
    _fp = _fp * 31 + ((r.results && r.results.length) || 0);
    _fp = _fp * 31 + (r.toolContent ? 1 : 0);
    _fp = _fp * 31 + (r._hgTranslating ? 1 : 0);
    if (r._translatedQuestion) _fp = _fp * 31 + r._translatedQuestion.length;
    if (r._timerPolls) _fp = _fp * 31 + r._timerPolls.length;
  }
  if (container._roundsFingerprint === _fp) return;
  container._roundsFingerprint = _fp;

  // ★ UNIFIED: split into toolRounds (one panel) and swarmRounds (own dashboard)
  const toolRounds = [], swarmRounds = [];
  for (const r of rounds) {
    if (_isRoundSwarm(r)) swarmRounds.push(r);
    else toolRounds.push(r);
  }

  // ── Unified tool panel: all tools in chronological order ──
  const unifiedPanel = container.querySelector(".ptool-panel");
  if (toolRounds.length > 0) {
    const anyActive = toolRounds.some((r) => r.status === "searching");
    const count = toolRounds.length;
    const headerLabel = anyActive
      ? `Working… (${count})`
      : `${count} tool${count > 1 ? "s" : ""} used`;
    let body;
    if (!unifiedPanel) {
      const el = document.createElement("div");
      el.className =
        "ptool-panel animation-slideUp" +
        (anyActive ? " ptool-panel-active" : "");
      el.innerHTML = `<div class="ptool-panel-header"><span class="ptool-panel-label">${headerLabel}</span></div><div class="ptool-panel-body"></div>`;
      container.appendChild(el);
      body = el.querySelector(".ptool-panel-body");
    } else {
      unifiedPanel.className =
        "ptool-panel" + (anyActive ? " ptool-panel-active" : "");
      const lbl = unifiedPanel.querySelector(".ptool-panel-label");
      if (lbl) lbl.textContent = headerLabel;
      body = unifiedPanel.querySelector(".ptool-panel-body");
    }
    if (body) {
      const _TOOL_VISIBLE_WINDOW = 50;
      const anyStreaming = toolRounds.some(r => r.status === "searching");
      const visibleRounds = anyStreaming && toolRounds.length > _TOOL_VISIBLE_WINDOW
        ? (() => {
            const active = toolRounds.filter(r => r.status === "searching" || r.status === "pending_approval");
            const done = toolRounds.filter(r => r.status !== "searching" && r.status !== "pending_approval");
            const tail = done.slice(-_TOOL_VISIBLE_WINDOW);
            if (done.length > _TOOL_VISIBLE_WINDOW && !body.querySelector('.ptool-truncated')) {
              const trunc = document.createElement("div");
              trunc.className = "ptool-truncated";
              const hiddenN = done.length - _TOOL_VISIBLE_WINDOW;
              trunc.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${hiddenN} earlier tool calls hidden — click to expand</span>`;
              trunc.onclick = () => { trunc.remove(); body._showAll = true; container._roundsFingerprint = null; _syncSearchRoundsDOM(container, rounds); };
              body.prepend(trunc);
            } else if (body.querySelector('.ptool-truncated')) {
              const truncEl = body.querySelector('.ptool-truncated');
              const hiddenN2 = done.length - _TOOL_VISIBLE_WINDOW;
              truncEl.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${hiddenN2} earlier tool calls hidden — click to expand</span>`;
            }
            return body._showAll ? toolRounds : [...tail, ...active];
          })()
        : toolRounds;
      for (const round of visibleRounds) {
        const rn = round.roundNum;
        const isActive = round.status === "searching";
        let slot = body.querySelector(`[data-prn="${rn}"]`);
        if (!slot) {
          slot = document.createElement("div");
          slot.setAttribute("data-prn", rn);
          if (round._hgTranslating) slot.setAttribute("data-hg-translating", "1");
          slot.innerHTML = _renderUnifiedToolLine(round, isActive);
          body.appendChild(slot);
        } else if (isActive || round.status === "pending_approval") {
          slot.innerHTML = _renderUnifiedToolLine(round, isActive);
        } else if (
          slot.querySelector(".ptool-active") ||
          slot.querySelector(".ptool-cmd-running") ||
          slot.querySelector(".ptool-pending") ||
          slot.querySelector(".code-exec-running")
        ) {
          slot.innerHTML = _renderUnifiedToolLine(round, false);
        } else if (slot.querySelector(".ptool-cmd-stdin")) {
          // ★ Stdin input card — avoid re-rendering while still awaiting_stdin
          //   to prevent destroying live DOM (input field) mid-type.
          if (round.status !== "awaiting_stdin" || !round.stdinId) {
            slot.innerHTML = _renderUnifiedToolLine(round, false);
          }
          // else: still awaiting — keep the live input field intact
        } else if (slot.querySelector(".hg-card")) {
          // ★ HG interactive card — avoid re-rendering while still awaiting_human
          //   to prevent destroying live DOM (buttons, textarea) mid-click.
          //   Only re-render when: status changed away from awaiting_human, or
          //   translation state (_hgTranslating) flipped.
          if (round.status !== "awaiting_human" || !round.guidanceId) {
            // Status transitioned → rebuild as submitted/done line
            slot.innerHTML = _renderUnifiedToolLine(round, false);
          } else {
            // Still awaiting — only update if translation state changed
            const prevTrans = slot.getAttribute("data-hg-translating") === "1";
            const nowTrans = !!round._hgTranslating;
            if (prevTrans !== nowTrans) {
              slot.innerHTML = _renderUnifiedToolLine(round, false);
              slot.setAttribute("data-hg-translating", nowTrans ? "1" : "0");
            }
            // ★ Also update translated question/options in-place (no full rebuild)
            else if (round._translatedQuestion) {
              const qEl = slot.querySelector(".hg-question");
              if (qEl) {
                const newHtml = renderMarkdown(round._translatedQuestion);
                if (qEl.innerHTML !== newHtml) qEl.innerHTML = newHtml;
              }
            }
          }
        } else if (slot.querySelector(".hg-submitted-line")) {
          // ★ Submitted HG line — only re-render when status transitions away
          if (round.status !== "submitted") {
            slot.innerHTML = _renderUnifiedToolLine(round, false);
          }
        } else if (round._timerPolls && round._timerPolls.length > 0) {
          // ★ Timer watcher: always re-render to show latest poll results.
          // This covers both the initial ptool-line → timer-watcher-block transition
          // AND subsequent poll additions to an existing timer-watcher-block.
          slot.innerHTML = _renderUnifiedToolLine(round, isActive);
        } else if (round.toolContent && !slot.querySelector('[data-tc-preview]')) {
          const ptLine = slot.querySelector('.ptool-line');
          if (ptLine) {
            ptLine.insertAdjacentHTML('beforeend', _tcPreviewBtn(round));
          }
        }
      }
    }
  }

  // ── Swarm: dedicated agent panel (separate from unified panel) ──
  for (const round of swarmRounds) {
    const rid = "swarm-" + round.roundNum;
    let block = container.querySelector(`[data-rid="${rid}"]`);
    if (!block) {
      block = document.createElement("div");
      block.setAttribute("data-rid", rid);
      block.className = "swarm-round-container animation-slideUp";
      block.innerHTML = _buildSwarmPanelHTML(round);
      container.appendChild(block);
    } else {
      block.innerHTML = _buildSwarmPanelHTML(round);
    }
  }
}

/* ★ Build the live swarm panel HTML (used during streaming) */
function _buildSwarmPanelHTML(round) {
  const agents = round._swarmAgents || [];
  const isActive = round.status === "searching" || round._swarmActive;
  const total = agents.length;
  const running = agents.filter(a => a.status === "running" || a.status === "thinking").length;
  const done = agents.filter(a => a.status === "done" || a.status === "completed").length;
  const failed = agents.filter(a => a.status === "failed" || a.status === "error").length;
  const pending = total - done - failed - running;
  const finished = done + failed;

  /* ── Elapsed timer ── */
  let elapsed = "";
  if (round._swarmStartTime) {
    const ms = (round._swarmEndTime || Date.now()) - round._swarmStartTime;
    const sec = Math.floor(ms / 1000);
    elapsed = sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
  }

  /* ── Header icon ── */
  const headerIcon = isActive
    ? `<span class="sw-header-icon" style="animation:swarmIconBounce 1.2s ease-in-out infinite">⚡</span>`
    : `<span class="sw-header-icon">⚡</span>`;

  /* ── Header subtitle counts ── */
  let headerSubtitle = "";
  if (total > 0) {
    const parts = [];
    if (isActive && running > 0) parts.push(`<span class="sw-cnt-running">${running} running</span>`);
    if (done > 0) parts.push(`<span class="sw-cnt-done">${done} done</span>`);
    if (failed > 0) parts.push(`<span class="sw-cnt-failed">${failed} failed</span>`);
    if (pending > 0 && isActive) parts.push(`${pending} queued`);
    headerSubtitle = `<span class="sw-header-subtitle">${parts.join(" · ")}</span>`;
  } else if (isActive) {
    headerSubtitle = `<span class="sw-header-subtitle">Planning…</span>`;
  }

  /* ── Status pill ── */
  let statusPill;
  if (total === 0 && isActive) {
    statusPill = `<span class="sw-status-pill sw-pill-planning"><span class="sw-spinner" style="width:10px;height:10px;border-width:1.5px"></span>Planning</span>`;
  } else if (isActive) {
    statusPill = `<span class="sw-status-pill sw-pill-running"><span class="sw-spinner" style="width:10px;height:10px;border-width:1.5px"></span>Running</span>`;
  } else if (failed > 0 && done === 0) {
    statusPill = `<span class="sw-status-pill sw-pill-error">✗ Failed</span>`;
  } else {
    statusPill = `<span class="sw-status-pill sw-pill-done">✓ Complete</span>`;
  }

  /* ── Progress bar (only when agents exist) ── */
  let progressBar = "";
  if (total > 0) {
    const pctDone = Math.round((done / total) * 100);
    const pctFailed = Math.round((failed / total) * 100);
    const pctRunning = Math.round((running / total) * 100);
    const fillStyle = (failed > 0 && done > 0) ? ` style="--ok-pct:${pctDone}%"` : "";
    const fillClass = failed > 0 && done > 0 ? " has-errors" : "";
    progressBar = `<div class="sw-progress">` +
      `<div class="sw-progress-track">` +
        `<div class="sw-progress-fill${fillClass}" style="width:${pctDone + pctFailed + pctRunning}%"${fillStyle}></div>` +
      `</div>` +
      `<div class="sw-progress-label">` +
        `<span>${finished}/${total} agents complete</span>` +
        (elapsed ? `<span>${elapsed}</span>` : "") +
      `</div>` +
    `</div>`;
  }

  /* ── Agent cards (collapsible) ── */
  let agentCards = "";
  if (agents.length > 0) {
    agentCards = agents.map((a, i) => {
      const statusIcon = {
        done: "", completed: "", failed: "", error: "",
        running: "", thinking: "", pending: "",
      };
      const sIcon = statusIcon[a.status] || "";
      const taskNum = `Task ${i + 1}`;
      const objective = escapeHtml((a.objective || "").slice(0, 300));
      const phase = a.phase || a.status || "";
      const preview = (a.preview || "").slice(0, 400);

      /* ── Status class ── */
      let sClass;
      if (a.status === "done" || a.status === "completed") sClass = "sw-a-done";
      else if (a.status === "failed" || a.status === "error") sClass = "sw-a-failed";
      else if (a.status === "running" || a.status === "thinking") sClass = "sw-a-running";
      else sClass = "sw-a-pending";

      /* ── Phase pill label ── */
      const phaseMap = {
        thinking: "Thinking…", tool_use: "Using tools", writing: "Writing…",
        searching: "Searching…", coding: "Coding…", analyzing: "Analyzing…",
        done: "Complete", completed: "Complete", failed: "Failed", error: "Error",
        pending: "Queued", running: "Working…",
      };
      const phaseLabel = phaseMap[phase] || phase || "Queued";

      /* ── Agent elapsed ── */
      let agentTimer = "";
      if (a.elapsed) agentTimer = `<span class="sw-a-timer">${a.elapsed}s</span>`;

      /* ── Agent body: objective + tools + preview ── */
      let bodyContent = "";

      // Objective — always show prominently
      if (objective) {
        bodyContent += `<div class="sw-a-objective">${objective}</div>`;
      }

      // Dependency chain
      if (a.dependsOn && a.dependsOn.length > 0) {
        const depHTML = a.dependsOn.map(depId => {
          const depAgent = agents.find(x => x.id === depId);
          const depLabel = depAgent ? `Task ${agents.indexOf(depAgent) + 1}` : depId;
          const depDone = depAgent && (depAgent.status === "done" || depAgent.status === "completed");
          return `<span class="sw-dep-tag ${depDone ? 'sw-dep-done' : ''}">${depDone ? '✓' : ''} ${escapeHtml(depLabel)}</span>`;
        }).join("");
        bodyContent += `<div class="sw-a-deps"><span class="sw-a-deps-label">Waits for:</span>${depHTML}</div>`;
      }

      // Tools used — compact inline
      if (a.tools && a.tools.length > 0) {
        const toolHTML = a.tools.slice(-6).map(t => {
          const td = _TOOL_DISPLAY[t];
          const icon = td ? td.icon : "⚡";
          const label = td ? (td.label || t) : t;
          return `<span class="sw-a-tool-tag" title="${escapeHtml(t)}">${icon} ${label}</span>`;
        }).join("");
        const more = a.tools.length > 6 ? `<span class="sw-a-tool-tag">+${a.tools.length - 6}</span>` : "";
        bodyContent += `<div class="sw-a-tools">${toolHTML}${more}</div>`;
      }

      // Preview — live stream with typing cursor
      if (preview && (a.status === "running" || a.status === "thinking")) {
        bodyContent += `<div class="sw-a-preview sw-a-preview-live">${escapeHtml(preview)}<span class="sw-typing-cursor">▍</span></div>`;
      } else if (preview && (a.status === "done" || a.status === "completed")) {
        bodyContent += `<div class="sw-a-preview">${escapeHtml(preview)}</div>`;
      } else if (preview && (a.status === "failed" || a.status === "error")) {
        bodyContent += `<div class="sw-a-err">${escapeHtml(preview.slice(0, 200))}</div>`;
      }

      // Meta line
      if (a.tokens || a.elapsed) {
        const metaParts = [];
        if (a.elapsed) metaParts.push(`${a.elapsed}s`);
        if (a.tokens) metaParts.push(`${a.tokens >= 1000000 ? (a.tokens/1000000).toFixed(1) + "m" : a.tokens > 1000 ? (a.tokens/1000).toFixed(1) + "k" : a.tokens} tok`);
        bodyContent += `<div class="sw-a-meta">${metaParts.join(' · ')}</div>`;
      }

      /* Auto-open running agents, collapse done ones */
      const autoOpen = (a.status === "running" || a.status === "thinking") ? " sw-a-open" : "";

      return `<div class="sw-agent ${sClass}${autoOpen}" data-agent-id="${escapeHtml(a.id || '')}">` +
        `<div class="sw-a-header" onclick="this.closest('.sw-agent').classList.toggle('sw-a-open')">` +
          `<span class="sw-a-status-icon">${sIcon}</span>` +
          `<span class="sw-a-role">${taskNum}</span>` +
          `<span class="sw-a-phase-pill">${phaseLabel}</span>` +
          agentTimer +
          `<span class="sw-a-chevron">▾</span>` +
        `</div>` +
        (bodyContent ? `<div class="sw-a-body">${bodyContent}</div>` : "") +
      `</div>`;
    }).join("");
  }

  /* ── Stats footer ── */
  let statsFooter = "";
  const footerParts = [];
  if (total > 0) footerParts.push(`⚡ ${total} parallel task${total > 1 ? "s" : ""}`);
  if (round._swarmStats) {
    const s = round._swarmStats;
    if (s.totalTokens) footerParts.push(`${s.totalTokens >= 1000000 ? (s.totalTokens/1000000).toFixed(1) + "m" : s.totalTokens > 1000 ? (s.totalTokens/1000).toFixed(1) + "k" : s.totalTokens} tokens`);
    if (s.totalCostUsd) footerParts.push(`$${s.totalCostUsd.toFixed(4)}`);
  }
  if (elapsed) footerParts.push(`${elapsed}`);
  if (footerParts.length > 0) {
    statsFooter = `<div class="sw-footer">${footerParts.join('<span class="sw-footer-sep">·</span>')}</div>`;
  }

  return `<div class="sw-panel${isActive ? ' sw-active' : ' sw-complete'}">` +
    `<div class="sw-header" onclick="this.closest('.sw-panel').classList.toggle('sw-collapsed')">` +
      `<div class="sw-header-left">` +
        headerIcon +
        `<div class="sw-header-info">` +
          `<span class="sw-header-title">Parallel Execution</span>` +
          headerSubtitle +
        `</div>` +
      `</div>` +
      `<div class="sw-header-right">` +
        statusPill +
        (elapsed ? `<span class="sw-header-timer">${elapsed}</span>` : "") +
        `<span class="sw-chevron">▾</span>` +
      `</div>` +
    `</div>` +
    progressBar +
    (agentCards ? `<div class="sw-agent-grid">${agentCards}</div>` : "") +
    statsFooter +
  `</div>`;
}

/* ★ Build the done HTML specifically for swarm rounds — reuses the panel layout */
function _buildSwarmDoneHTML(round, showNums) {
  /* If we have _swarmAgents, render the full panel */
  if (round._swarmAgents && round._swarmAgents.length > 0) {
    const patchedRound = Object.assign({}, round, { _swarmActive: false });
    return _buildSwarmPanelHTML(patchedRound);
  }
  /* No agents and no results — don't render empty swarm panels */
  const results = round.results || [];
  if (!results.length && !round._swarmAgents?.length) return "";
  /* Fallback: historical saved data without agent details — compact summary */
  const snippet = results[0]?.snippet || "";
  const elapsed = round._elapsed || "";
  return `<div class="sw-panel sw-complete">` +
    `<div class="sw-header">` +
      `<div class="sw-header-left">` +
        `` +
        `<div class="sw-header-info">` +
          `<span class="sw-header-title">Agent Swarm</span>` +
        `</div>` +
      `</div>` +
      `<div class="sw-header-right">` +
        `<span class="sw-status-pill sw-pill-done">✓ Complete</span>` +
        (elapsed ? `<span class="sw-header-timer">${elapsed}</span>` : "") +
      `</div>` +
    `</div>` +
    (snippet ? `<div class="sw-footer" style="opacity:0.7">${escapeHtml(snippet)}</div>` : "") +
  `</div>`;
}

function showStreamingUIForConv(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv || conv.messages.length === 0) return;
  _destroyLazyObserver();
  _lazyConvId = convId;
  _lastRenderedFingerprint = "";
  const inner = document.getElementById("chatInner");
  const prevMsgs = conv.messages.slice(0, -1);
  const total = prevMsgs.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;

  let html = "";
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(prevMsgs[i], i);
  }

  const lastMsg = conv.messages[conv.messages.length - 1];
  if (lastMsg && lastMsg.role === "assistant" && lastMsg._isEndpointPlanner && !lastMsg.done) {
    /* Endpoint planner phase — show planner streaming bubble */
    const time = new Date(lastMsg.timestamp || Date.now()).toLocaleTimeString(
      [],
      { hour: "2-digit", minute: "2-digit" },
    );
    html += `<div class="message ep-planner-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Planner</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Planning…</div></div></div></div>`;
  } else if (lastMsg && lastMsg.role === "assistant") {
    const time = new Date(lastMsg.timestamp || Date.now()).toLocaleTimeString(
      [],
      { hour: "2-digit", minute: "2-digit" },
    );
    html += `<div class="message ep-worker-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Agent</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Streaming…</div></div></div></div>`;
  } else if (lastMsg && lastMsg._isEndpointReview && !lastMsg.done) {
    /* Endpoint critic phase — show critic streaming bubble */
    const time = new Date(lastMsg.timestamp || Date.now()).toLocaleTimeString(
      [],
      { hour: "2-digit", minute: "2-digit" },
    );
    html += `<div class="message user-msg ep-critic-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Critic</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Reviewing…</div></div></div></div>`;
  }
  inner.innerHTML = html;
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }
  requestAnimationFrame(() => buildTurnNav(conv));
  _forceScrollToBottom(null, true);
  updateSendButton();
  if (lastMsg && (lastMsg.role === "assistant" || (lastMsg._isEndpointReview && !lastMsg.done))) {
    const buf = streamBufs.get(convId);
    /* ★ FIX: buf?.searchRounds is [] (truthy) even when empty, preventing
     *   fallback to getSearchRoundsFromMsg(lastMsg).  Use .length check. */
    const rounds = (buf?.searchRounds?.length ? buf.searchRounds : null)
                   || getSearchRoundsFromMsg(lastMsg);
    updateStreamingUI({
      thinking: buf?.thinking || lastMsg.thinking || "",
      content: buf?.content || lastMsg.content || "",
      searchRounds: rounds,
      phase: buf?.phase || null,
    });
    /* ★ FIX: After page refresh, SSE data may arrive AFTER this initial render.
     *   Schedule a deferred re-render (300ms) so that any SSE state event that
     *   arrives during the connection setup window gets rendered — without this,
     *   the user sees "Waiting…" until the NEXT SSE event triggers twUpdate. */
    const _deferConvId = convId;
    setTimeout(() => {
      if (activeConvId !== _deferConvId) return;           // user switched away
      if (!activeStreams.has(_deferConvId)) return;         // stream finished
      const dBuf = streamBufs.get(_deferConvId);
      if (!dBuf) return;
      updateStreamingUI({
        thinking: dBuf.thinking,
        content: dBuf.content,
        searchRounds: dBuf.searchRounds,
        phase: dBuf.phase,
      });
    }, 300);
  }
}

function finishStream(convId) {
  activeStreams.delete(convId);
  const conv = conversations.find((c) => c.id === convId);
  if (conv) {
    const lastMsg = conv.messages[conv.messages.length - 1];
    const contentLen = lastMsg?.content?.length || 0;
    const thinkingLen = lastMsg?.thinking?.length || 0;
    const hasError = !!lastMsg?.error;
    /* ★ CROSS-TALK DETECTION: count how many active streams exist at finish time.
     *   If >1 stream was active, there's elevated risk of cross-talk injection.
     *   Also check if the conv's message count changed unexpectedly. */
    const _fsActiveCount = activeStreams.size;  // checked AFTER delete above
    const _fsOtherStreams = [...activeStreams.keys()].filter(k => k !== convId).map(k => k.slice(0,8));
    console.warn(
      `[finishStream] conv=${convId.slice(0,8)} msgs=${conv.messages.length} ` +
      `lastRole=${lastMsg?.role} contentLen=${contentLen} thinkingLen=${thinkingLen} ` +
      `hasError=${hasError} taskId=${conv.activeTaskId?.slice(0,8)||'null'} ` +
      `otherActiveStreams=[${_fsOtherStreams.join(',')}] ` +
      `isActiveConv=${activeConvId === convId} activeConvId=${activeConvId?.slice(0,8)||'null'}`
    );
    if (_fsOtherStreams.length > 0) {
      console.warn(
        `[finishStream] ⚠️ CONCURRENT STREAMS: ${_fsOtherStreams.length} other stream(s) still active ` +
        `while finishing conv=${convId.slice(0,8)} — elevated cross-talk risk! ` +
        `Other convs: [${_fsOtherStreams.join(', ')}]`
      );
    }
    if (lastMsg?.role === 'assistant' && contentLen === 0 && thinkingLen === 0 && !hasError) {
      console.error(`[finishStream] ⚠️ EMPTY ASSISTANT MESSAGE DETECTED — conv=${convId.slice(0,8)} — this is likely the data loss bug!`, {
        message: JSON.parse(JSON.stringify(lastMsg)),
        convTitle: conv.title,
        messageCount: conv.messages.length,
      });
    }
    /* ★ FIX: Clean up any lingering awaiting_human / submitted rounds.
     *   When the task finishes (normally or via abort/timeout), any HG round
     *   that was never answered is now orphaned — the backend won't accept a
     *   response anymore.  Mark them as "done" so the sidebar amber dot clears
     *   and the card collapses to a "no response" line. */
    let _hgCleaned = 0;
    for (const m of conv.messages) {
      if (m.searchRounds) {
        for (const r of m.searchRounds) {
          if (r.status === 'awaiting_human' || r.status === 'submitted') {
            r.status = 'done';
            r.guidanceId = null;
            r._hgSkipped = true;  // marker: user never answered
            _hgCleaned++;
          }
        }
      }
    }
    if (_hgCleaned > 0) {
      console.info(`[finishStream] 🧹 Cleaned ${_hgCleaned} orphaned HG round(s) — conv=${convId.slice(0,8)}`);
    }
    conv.activeTaskId = null;
    conv._activeTaskClearedAt = Date.now();
    saveConversations(convId);
    syncConversationToServer(conv);
    /* ★ Eagerly update IndexedDB cache — syncConversationToServer also does this
     *   on success, but it may be guarded/skipped in some edge cases.  This ensures
     *   the cache always has the latest post-stream content for instant reload. */
    ConvCache.put(conv);
  } else {
    console.error(`[finishStream] conv not found for id=${convId.slice(0,8)} — cannot save!`);
  }
  // ── UI updates (wrapped in try/catch so auto-translate always runs) ──
  try {
    if (activeConvId === convId) {
      const sm = document.getElementById("streaming-msg");
      const hasEndpointTurns = conv && conv.messages.some(m => m._epIteration);
      if (sm && conv) {
        // Normal streaming finish — replace the streaming element with rendered message
        const idx = conv.messages.length - 1;
        const msg = conv.messages[idx];
        if (msg) {
          try {
            const html = renderMessage(msg, idx);
            if (html) {
              /* ★ FIX: Save/restore scrollTop around outerHTML replacement to prevent
               *   "jump upward" when the streaming-msg is replaced by the final message.
               *   Root cause: during streaming, the thinking-block is .expanded (max-height:none,
               *   showing full thinking text).  The final renderMessage renders it collapsed
               *   (max-height:0).  This height drop (potentially thousands of px) combined
               *   with _forceScrollToBottom scrolling to the new (smaller) scrollHeight
               *   causes a jarring visual jump.  Fix: preserve scroll position instead of
               *   forcing to bottom — the user is already looking at the content. */
              const _ct = document.getElementById('chatContainer');
              const _savedScroll = _ct ? _ct.scrollTop : -1;
              sm.outerHTML = html;
              if (_savedScroll >= 0 && _ct) {
                _ct.scrollTop = _savedScroll;
              }
            }
          } catch (e) {
            console.error('[finishStream] renderMessage/outerHTML failed:', e.message);
          }
        }
      } else if (hasEndpointTurns) {
        // ★ Endpoint mode after poll fallback — no streaming-msg element exists
        // (SSE timed out, poll was used). Do a full re-render to show all turns.
        console.info(`[finishStream] Endpoint mode full re-render — ` +
          `conv=${convId.slice(0,8)} msgs=${conv.messages.length}`);
        renderChat(conv);
      }
      /* ★ FIX: Don't force-scroll-to-bottom after stream finishes.
       *   The user is already reading the content at their current scroll position.
       *   Forcing to bottom after the streaming→final DOM swap causes a visible jump
       *   because the final message may be shorter (collapsed thinking, no phase indicator).
       *   Only scroll if the user was already near the bottom (within 80px). */
      if (isNearBottom(80)) scrollToBottom();
      if (conv) {
        buildTurnNav(conv);
        _lastRenderedFingerprint = _convRenderFingerprint(conv);
      }
    }
    renderConversationList();
    updateSendButton();
  } catch (uiErr) {
    console.error('[finishStream] UI update error (non-fatal, translate will still run):', uiErr.message);
  }
  // ── Auto-translate assistant response ──
  // ★ Use per-conversation setting, NOT the global (which reflects current viewed conv)
  const _convAutoTranslate = conv ? (conv.autoTranslate !== undefined ? !!conv.autoTranslate : true) : autoTranslate;
  if (_convAutoTranslate && conv) {
    const lastMsg = conv.messages[conv.messages.length - 1];
    if (
      lastMsg &&
      lastMsg.role === "assistant" &&
      lastMsg.content &&
      !lastMsg._igResult &&          // skip image gen results — nothing to translate
      !lastMsg._isImageGen           // skip image gen error messages
    ) {
      // ★ FIX: detect stale partial translations (e.g. translation started mid-stream
      //   with only partial content, then the full response grew much larger).
      //   If the existing translation is less than 15% of the content length, consider
      //   it stale and re-translate — even if _translateTaskId is already set.
      const hasStaleTranslation = lastMsg.translatedContent &&
        lastMsg.content.length > 500 &&  // only for non-trivial responses
        lastMsg.translatedContent.length < lastMsg.content.length * 0.15;
      if (hasStaleTranslation) {
        console.warn(`[finishStream] 🔄 Stale partial translation detected — ` +
          `translated=${lastMsg.translatedContent.length} vs content=${lastMsg.content.length} ` +
          `(${(lastMsg.translatedContent.length/lastMsg.content.length*100).toFixed(1)}%) — re-translating`);
        delete lastMsg.translatedContent;
        delete lastMsg._translatedCache;
        delete lastMsg._translateDone;
        delete lastMsg._translateTaskId;  // ★ clear task ID so re-translate can proceed
      }
      // Skip if a translate task is already running (and not stale)
      if (lastMsg._translateTaskId) {
        // Already have a valid translation or active task — skip
      } else
      if (!lastMsg.translatedContent) {
        // ★ FIX: When autoTranslate is explicitly ON, always translate — don't
        // rely on the language heuristic which fails for bilingual/mixed responses.
        // The heuristic is only a fallback for when autoTranslate state is unknown.
        let needsTranslation = true;  // autoTranslate is already confirmed ON above
        const idx = conv.messages.length - 1;
        if (needsTranslation) {
          // Mark pending immediately so renderMessage shows "翻译中…" indicator
          lastMsg._translateField = "translatedContent";
          lastMsg._translateDone = false;
          // Re-render the message to show the persistent "翻译中…" indicator
          // (we set _translateTaskId AFTER the task starts, but _translateDone=false triggers indicator)
          // Fire-and-forget: start async translate task
          _startAutoTranslateForMsg(conv, convId, idx, lastMsg);
        }
      }
    }
  }

  // ── ★ Drain pending message queue ──
  const queueDepth = pendingMessageQueue.has(convId) ? pendingMessageQueue.get(convId).length : 0;
  if (queueDepth > 0) {
    console.log(
      `%c[Queue] ⏭ Stream ended for conv=${convId.slice(0,8)} — ${queueDepth} message(s) queued, dispatching next in 300ms…`,
      'color:#a78bfa;font-weight:bold'
    );
    // Small delay for UX — let the user see the response settled before next auto-send
    setTimeout(() => _dispatchQueuedMessage(convId), 300);
  } else {
    console.log(`%c[Queue] ✓ Stream ended for conv=${convId.slice(0,8)}, no queued messages`, 'color:#6b7280');
  }
}

/**
 * Re-trigger EN→CN translation for any awaiting_human rounds that haven't
 * been translated yet.  Called after SSE state snapshot / page-load reconnection
 * so translations survive page refreshes.
 */
function _retriggerHgTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const _hgAutoTrans = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
  if (!_hgAutoTrans) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.searchRounds) return;
  for (const r of assistantMsg.searchRounds) {
    if (r.status === 'awaiting_human' && r.guidanceQuestion && !r._translatedQuestion && !r._hgTranslating) {
      console.log(`[HG-Translate] Re-triggering translation for guidance=${r.guidanceId} after reconnect`);
      _autoTranslateHumanGuidance(convId, r.roundNum, r.guidanceQuestion, r.guidanceType || 'free_text', r.guidanceOptions || []);
    }
  }
}

/**
 * Auto-translate Human Guidance question & options (EN→CN).
 * Called when a `human_guidance_request` SSE event arrives and conv.autoTranslate is ON.
 * Translates asynchronously; re-renders the HG card when translation completes.
 */
async function _autoTranslateHumanGuidance(convId, roundNum, question, responseType, options) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.searchRounds) return;
  const round = assistantMsg.searchRounds.find(r => r.roundNum === roundNum);
  if (!round || round.status !== 'awaiting_human') return;

  // ★ Helper: sync assistantMsg.searchRounds → buf.searchRounds so that the
  //   reactive rendering pipeline (twUpdate → updateStreamingUI → _syncSearchRoundsDOM)
  //   sees translation-related flags (_hgTranslating, _translatedQuestion, etc.).
  //   Without this, buf.searchRounds is a stale shallow copy from the
  //   human_guidance_request handler and never gets updated.
  function _syncHgToBuf() {
    const buf = streamBufs.get(convId);
    if (buf && assistantMsg.searchRounds) {
      buf.searchRounds = assistantMsg.searchRounds;
    }
  }

  // Mark as translating (shows spinner in the card)
  round._hgTranslating = true;
  _syncHgToBuf();
  twUpdate(convId);

  // ── Build a single translation batch: question + all option labels + descriptions ──
  // Concatenate all texts with a separator to make a single API call (cheaper & faster)
  const SEP = '\n‖‖‖\n'; // unique separator unlikely to appear in content
  const parts = [question];
  if (responseType === 'choice' && options.length > 0) {
    for (const opt of options) {
      parts.push(opt.label || '');
      parts.push(opt.description || '');
    }
  }
  const batchText = parts.join(SEP);

  try {
    console.log(`[HG-Translate] Starting EN→CN translation for guidance=${round.guidanceId}, parts=${parts.length}`);
    const translated = await _callTranslateAPI(batchText, 'Chinese', 'English');
    // Split back by separator
    const translatedParts = translated.split(/\n?‖‖‖\n?/);

    // Re-find the round (may have changed during async)
    const conv2 = conversations.find(c => c.id === convId);
    if (!conv2) return;
    const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
    if (!msg2 || !msg2.searchRounds) return;
    const round2 = msg2.searchRounds.find(r => r.roundNum === roundNum);
    if (!round2 || round2.status !== 'awaiting_human') return;

    // Apply translated question
    round2._translatedQuestion = translatedParts[0] || question;
    round2._hgTranslating = false;

    // Apply translated option labels & descriptions
    if (responseType === 'choice' && round2.guidanceOptions && translatedParts.length > 1) {
      for (let i = 0; i < round2.guidanceOptions.length; i++) {
        const labelIdx = 1 + i * 2;
        const descIdx = 2 + i * 2;
        if (translatedParts[labelIdx]) {
          round2.guidanceOptions[i]._translatedLabel = translatedParts[labelIdx];
        }
        if (translatedParts[descIdx] && round2.guidanceOptions[i].description) {
          round2.guidanceOptions[i]._translatedDescription = translatedParts[descIdx];
        }
      }
    }

    console.log(`[HG-Translate] ✓ Translation done for guidance=${round2.guidanceId}, ` +
      `question: ${question.length}→${round2._translatedQuestion.length} chars`);
    // ★ Sync translated properties to buf before re-render
    const buf2 = streamBufs.get(convId);
    if (buf2 && msg2.searchRounds) {
      buf2.searchRounds = msg2.searchRounds;
    }
    twUpdate(convId);
  } catch (e) {
    console.warn(`[HG-Translate] Translation failed: ${e.message} — showing original`);
    // Clear translating flag, show original untranslated
    const conv2 = conversations.find(c => c.id === convId);
    if (conv2) {
      const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
      const round2 = msg2?.searchRounds?.find(r => r.roundNum === roundNum);
      if (round2) {
        round2._hgTranslating = false;
        // ★ Sync cleared flag to buf before re-render
        const buf2 = streamBufs.get(convId);
        if (buf2 && msg2.searchRounds) {
          buf2.searchRounds = msg2.searchRounds;
        }
        twUpdate(convId);
      }
    }
  }
}

/**
 * Start auto-translate for an assistant message. Extracted so it can be
 * called from finishStream and from _resumePendingTranslations.
 */
async function _startAutoTranslateForMsg(conv, convId, idx, msg) {
  try {
    const taskId = await _startTranslateTask(
      msg.content, "Chinese", "English",
      convId, idx, "translatedContent"
    );
    if (taskId) {
      msg._translateTaskId = taskId;
      msg._translateField = "translatedContent";
      msg._translateDone = false;
      saveConversations(convId);
      // Re-render to show the "翻译中…" indicator persistently
      if (activeConvId === convId) {
        const el = document.getElementById(`msg-${idx}`);
        if (el) {
          // ★ Save/restore scroll to prevent content-visibility:auto layout shift
          const _ct = document.getElementById('chatContainer');
          const _sv = _ct ? _ct.scrollTop : -1;
          el.outerHTML = renderMessage(msg, idx);
          if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
        }
      }
      // Optimistic poll loop: try to show result quickly
      const _pollAndApply = async (attempt) => {
        if (attempt > 40) { // ~2 min max
          // Give up polling — server auto-committed to DB anyway
          msg._translateDone = true;
          msg._translateError = 'Translation timeout';
          saveConversations(convId);
          if (activeConvId === convId) {
            const el = document.getElementById(`msg-${idx}`);
            if (el) {
              const _ct = document.getElementById('chatContainer');
              const _sv = _ct ? _ct.scrollTop : -1;
              el.outerHTML = renderMessage(msg, idx);
              if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
            }
          }
          return;
        }
        await new Promise(r => setTimeout(r, attempt < 5 ? 2000 : 4000));
        const result = await _pollTranslateTask(taskId);
        if (result.status === 'done' && result.translated) {
          msg.translatedContent = result.translated;
          msg._showingTranslation = true;
          msg._translateDone = true;
          saveConversations(convId);
          syncConversationToServer(conv);
          if (activeConvId === convId) {
            const el = document.getElementById(`msg-${idx}`);
            if (el) {
              // ★ Save/restore scroll to prevent content-visibility:auto layout shift
              // that causes the view to jump to top during outerHTML replacement
              const _ct = document.getElementById('chatContainer');
              const _sv = _ct ? _ct.scrollTop : -1;
              el.outerHTML = renderMessage(msg, idx);
              if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
            }
            _lastRenderedFingerprint = _convRenderFingerprint(conv);
            // ★ Force scroll to bottom — the translated message is taller,
            // and the isNearBottom guard may fail after the layout shift
            scrollToBottom(true);
          }
        } else if (result.status === 'running') {
          _pollAndApply(attempt + 1);
        } else {
          // Error — mark and re-render
          msg._translateDone = true;
          msg._translateError = result.error || 'Translation failed';
          saveConversations(convId);
          if (activeConvId === convId) {
            const el = document.getElementById(`msg-${idx}`);
            if (el) {
              const _ct = document.getElementById('chatContainer');
              const _sv = _ct ? _ct.scrollTop : -1;
              el.outerHTML = renderMessage(msg, idx);
              if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
            }
          }
        }
      };
      _pollAndApply(0);
    }
  } catch (e) {
    console.error("Translation task start failed:", e);
  }
}

// ── Stream connection ──
async function connectToTask(convId, taskId, retries = 0) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  /* ★ CROSS-TALK DETECTION: log full stream context at connection time */
  console.info(
    `[connectToTask] 🔗 Connecting — conv=${convId.slice(0,8)} task=${taskId.slice(0,8)} ` +
    `activeConvId=${(typeof activeConvId !== 'undefined' ? activeConvId?.slice(0,8) : 'N/A')||'null'} ` +
    `msgs=${conv.messages.length} activeStreams=[${[...activeStreams.keys()].map(k=>k.slice(0,8)).join(',')}] ` +
    `retries=${retries}`
  );
  let assistantMsg = conv.messages[conv.messages.length - 1];

  /* ★ Endpoint mode reconnection: if the last message is a critic review
   *   (role=user, _isEndpointReview), we need to create a fresh assistant
   *   message for the next worker turn that's about to start. Also strip
   *   duplicate DB-loaded endpoint turns — the SSE will re-send them. */
  const hasEpTurns = conv.messages.some(m => m._epIteration);
  if (hasEpTurns && assistantMsg && assistantMsg.role !== "assistant") {
    // The last message is a critic review — create a placeholder assistant msg
    // for the new worker turn the backend is about to start
    assistantMsg = {
      role: "assistant",
      content: "",
      thinking: "",
      searchRounds: [],
      timestamp: new Date().toISOString(),
      _epIteration: (assistantMsg._epIteration || 0) + 1,
    };
    conv.messages.push(assistantMsg);
  }

  if (!assistantMsg || assistantMsg.role !== "assistant") {
    /* ★ FIX: Defensive recovery — if the last message is not assistant (e.g.
     *   loadConversationMessages Phase 2 overwrote conv.messages during a race
     *   with startAssistantResponse), push a fresh assistant message so the SSE
     *   stream has somewhere to accumulate content. Without this, connectToTask
     *   silently bails out → no streaming UI, but sidebar shows pulsing dot. */
    console.warn(
      `[connectToTask] ⚠️ Last msg is ${assistantMsg?.role || 'missing'}, not assistant — ` +
      `pushing recovery assistant msg for conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`
    );
    assistantMsg = {
      role: "assistant",
      content: "",
      thinking: "",
      timestamp: Date.now(),
      searchRounds: [],
      model: conv.model || (typeof serverModel !== 'undefined' ? serverModel : ''),
    };
    conv.messages.push(assistantMsg);
  }
  if (!activeStreams.has(convId)) {
    const controller = new AbortController();
    activeStreams.set(convId, { controller, taskId, assistantMsg });
    renderConversationList();
    updateSendButton();
    if (activeConvId === convId) {
      _lastRenderedFingerprint = "";
      const inner = document.getElementById("chatInner");
      const lastIdx = conv.messages.length - 1;
      const existing = document.getElementById(`msg-${lastIdx}`);
      if (existing) existing.remove();
      if (!document.getElementById("streaming-msg")) {
        const time = new Date(
          assistantMsg.timestamp || Date.now(),
        ).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        const el = document.createElement("div");
        /* ★ FIX: detect endpoint planner phase so reconnection shows "Planner"
         *   instead of "Agent".  Check: the assistantMsg has _isEndpointPlanner,
         *   or the conv has endpointEnabled and no worker turns yet (iteration 0). */
        const _isEpPlanner = assistantMsg._isEndpointPlanner
          || (conv.endpointEnabled && !conv.messages.some(m => m._epIteration));
        const _reconAvatar = _isEpPlanner
          ? ((typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦')
          : ((typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦');
        const _reconRole = _isEpPlanner ? 'Planner' : 'Agent';
        const _reconStatus = _isEpPlanner ? 'Planning…' : 'Connecting…';
        const _reconClass = _isEpPlanner ? 'ep-planner-msg' : 'ep-worker-msg';
        el.className = `message ${_reconClass}`;
        el.id = "streaming-msg";
        el.innerHTML = `<div class="message-avatar">${_reconAvatar}</div><div class="message-content"><div class="message-header"><span class="message-role">${_reconRole}</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> ${_reconStatus}</div></div></div>`;
        inner.appendChild(el);
        scrollToBottom();
      }
    }
    twStart(convId);
    const buf = streamBufs.get(convId);
    if (assistantMsg.searchRounds)
      buf.searchRounds = [...assistantMsg.searchRounds];
    else if (assistantMsg.searchResults)
      buf.searchRounds = [
        {
          roundNum: 1,
          query: assistantMsg.searchQuery || "search",
          results: assistantMsg.searchResults,
          status: "done",
        },
      ];
  }
  const stream = activeStreams.get(convId);
  let sseWorked = false;
  try {
    sseWorked = await _trySSE(convId, taskId, stream, assistantMsg);
  } catch (e) {
    if (e.name === "AbortError") {
      // ★ User clicked Stop — set finishReason BEFORE finishStream
      const _abortConv = conversations.find(c => c.id === convId);
      if (_abortConv) {
        const _abortMsg = _abortConv.messages[_abortConv.messages.length - 1];
        if (_abortMsg && _abortMsg.role === 'assistant') {
          _abortMsg.finishReason = 'aborted';
          console.log(`[connectToTask] User abort — set finishReason='aborted' for conv=${convId.slice(0,8)}`);
        }
      }
      twStop(convId);
      finishStream(convId);
      return;
    }
    debugLog(`SSE failed: ${e.message}`, "warn");
  }
  if (!sseWorked && !stream.controller.signal.aborted) {
    debugLog(`Falling back to polling for ${taskId.slice(0, 8)}`, "warn");
    await _pollFallback(convId, taskId, stream, assistantMsg);
  }
}

async function _trySSE(convId, taskId, stream, assistantMsg) {
  let lastSave = Date.now(),
    gotData = false;
  const sseTimeout = setTimeout(() => {
    if (!gotData) stream.controller.abort();
  }, 30000);
  let buf = streamBufs.get(convId);
  /* ── Endpoint critic-phase guard ──
   * When the critic is running, it streams delta/tool events through the same
   * SSE pipe.  We must NOT accumulate those into the worker's assistantMsg.
   * Instead, we accumulate into a separate criticBuf and show a dedicated
   * streaming bubble for the critic's review.  */
  let _epCriticPhase = false;
  let _epCriticMsg = null;   // the critic message object in conv.messages
  let _epCriticBuf = null;   // {content, thinking, searchRounds}
  let _roundThinkingLen = 0; // thinking chars accumulated in current LLM call (reset on phase events)
  let _lastEventId = null; // ★ Item 6: track SSE event ID for reconnection
  function _processSSELine(line) {
    // ★ Capture id: field for Last-Event-ID reconnection
    if (line.startsWith("id: ")) {
      _lastEventId = line.slice(4).trim();
      return false;
    }
    if (!line.startsWith("data: ")) return false;
    const ds = line.slice(6).trim();
    if (!ds) return false;
    let ev;
    try {
      ev = JSON.parse(ds);
    } catch {
      return false;
    }
    /* ★ Continue checkpoint: searchRounds to merge with newly streamed ones */
    if (ev.type === "state") {
      /* ★ Endpoint mode reconnection: rebuild conv.messages from endpointTurns
       *   and set the correct phase (working/reviewing) so streaming goes to
       *   the right target (assistantMsg vs _epCriticMsg). */
      if (ev.endpointMode && (ev.endpointPhase === 'planning' || (ev.endpointTurns && ev.endpointTurns.length > 0))) {
        /* ★ Fresh first connection in planning phase with no turns yet:
         *   startAssistantResponse already created the planner bubble — skip
         *   the full reconnection handler (renderChat + re-create streaming-msg)
         *   to avoid a visual flash. Just update the planner assistantMsg data. */
        const _hasTurns = ev.endpointTurns && ev.endpointTurns.length > 0;
        if (ev.endpointPhase === 'planning' && document.getElementById('streaming-msg')) {
          const conv = conversations.find(c => c.id === convId);
          if (conv) {
            _epCriticPhase = false;
            let plannerMsg = conv.messages.find(m => m._isEndpointPlanner);
            if (!plannerMsg) {
              // assistantMsg from startAssistantResponse or connectToTask should already be the planner
              if (assistantMsg && assistantMsg.role === 'assistant') {
                assistantMsg._isEndpointPlanner = true;
                plannerMsg = assistantMsg;
              }
            }
            if (plannerMsg) {
              plannerMsg.content = ev.content || plannerMsg.content || "";
              plannerMsg.thinking = ev.thinking || plannerMsg.thinking || "";
              if (ev.searchRounds) plannerMsg.searchRounds = ev.searchRounds;
              assistantMsg = plannerMsg;
              if (buf) {
                buf.thinking = assistantMsg.thinking;
                buf.content = assistantMsg.content;
                if (ev.searchRounds) buf.searchRounds = ev.searchRounds;
              }
            }
            /* ★ FIX: Update the streaming-msg DOM to show Planner role/avatar
             *   in case connectToTask created it with Agent styling */
            const sm = document.getElementById('streaming-msg');
            if (sm && activeConvId === convId) {
              if (!sm.classList.contains('ep-planner-msg')) {
                sm.classList.remove('ep-worker-msg');
                sm.classList.add('ep-planner-msg');
              }
              const roleEl = sm.querySelector('.message-role');
              if (roleEl && roleEl.textContent !== 'Planner') roleEl.textContent = 'Planner';
              const avatarEl = sm.querySelector('.message-avatar');
              if (avatarEl && typeof _TOFU_PLANNER_SVG !== 'undefined') {
                avatarEl.innerHTML = _TOFU_PLANNER_SVG;
              }
            }
            console.debug(`[SSE state] Endpoint planning — skipping full reconnect (turns=${(ev.endpointTurns||[]).length})`);
          }
        } else {
        const conv = conversations.find(c => c.id === convId);
        if (conv) {
          // Rebuild: keep base messages, replace endpoint turns with server copy
          let baseEnd = 0;
          for (let i = 0; i < conv.messages.length; i++) {
            if (!conv.messages[i]._epIteration && !conv.messages[i]._isEndpointReview && !conv.messages[i]._isEndpointPlanner) {
              baseEnd = i + 1;
            }
          }
          const baseMsgs = conv.messages.slice(0, baseEnd);
          conv.messages = baseMsgs.concat(ev.endpointTurns || []);

          if (ev.endpointPhase === 'planning') {
            // Planner is in progress — create a planner assistant msg
            _epCriticPhase = false;
            let plannerMsg = conv.messages.find(m => m._isEndpointPlanner);
            if (!plannerMsg) {
              plannerMsg = {
                role: "assistant", content: ev.content || "", thinking: ev.thinking || "",
                searchRounds: ev.searchRounds || [],
                timestamp: new Date().toISOString(),
                _isEndpointPlanner: true,
              };
              conv.messages.push(plannerMsg);
            }
            plannerMsg.content = ev.content || "";
            plannerMsg.thinking = ev.thinking || "";
            if (ev.searchRounds) plannerMsg.searchRounds = ev.searchRounds;
            assistantMsg = plannerMsg;
            if (buf) {
              buf.thinking = assistantMsg.thinking;
              buf.content = assistantMsg.content;
              if (ev.searchRounds) buf.searchRounds = ev.searchRounds;
            }
          } else if (ev.endpointPhase === 'reviewing') {
            // Critic is in progress — create a critic msg and set phase
            _epCriticPhase = true;
            _epCriticMsg = {
              role: "user", content: ev.content || "", thinking: ev.thinking || "",
              searchRounds: ev.searchRounds || [],
              timestamp: new Date().toISOString(),
              _isEndpointReview: true, _epIteration: ev.endpointIteration || 1,
              _epApproved: false, _isStuck: false,
            };
            conv.messages.push(_epCriticMsg);
            _epCriticBuf = {
              content: (_epCriticMsg.content || "").replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd(),
              thinking: _epCriticMsg.thinking, searchRounds: [],
            };
            streamBufs.set(convId, _epCriticBuf);
            buf = _epCriticBuf;
            // Point assistantMsg to the last completed worker turn
            const lastWorker = [...conv.messages].reverse().find(m => m.role === "assistant");
            if (lastWorker) assistantMsg = lastWorker;
          } else {
            // Worker is in progress — find or create the current worker msg
            _epCriticPhase = false;
            const iterNum = ev.endpointIteration || 1;
            let workerMsg = conv.messages.find(m =>
              m.role === "assistant" && m._epIteration === iterNum);
            if (!workerMsg) {
              workerMsg = {
                role: "assistant", content: "", thinking: "",
                searchRounds: [], timestamp: new Date().toISOString(),
                _epIteration: iterNum,
              };
              conv.messages.push(workerMsg);
            }
            workerMsg.content = ev.content || "";
            workerMsg.thinking = ev.thinking || "";
            if (ev.searchRounds) workerMsg.searchRounds = ev.searchRounds;
            assistantMsg = workerMsg;
            if (buf) {
              buf.thinking = assistantMsg.thinking;
              buf.content = assistantMsg.content;
              if (ev.searchRounds) buf.searchRounds = ev.searchRounds;
            }
          }

          console.info(`[SSE state] Endpoint reconnect — phase=${ev.endpointPhase} ` +
            `iter=${ev.endpointIteration} epTurns=${(ev.endpointTurns || []).length} ` +
            `totalMsgs=${conv.messages.length}`);

          // Re-render if active
          if (activeConvId === convId) {
            renderChat(conv);
            /* ★ FIX: renderChat rendered ALL messages including the in-progress
             *   one.  We need to remove that last static element before creating
             *   the streaming-msg, otherwise the in-progress message shows twice:
             *   once as a "dead" static element and once as the live streaming bubble. */
            const lastMsgIdx = conv.messages.length - 1;
            const staleRenderedEl = document.getElementById(`msg-${lastMsgIdx}`);
            if (staleRenderedEl) staleRenderedEl.remove();

            // Re-create streaming-msg for the in-progress turn
            const inner = document.getElementById("chatInner");
            const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            if (_epCriticPhase) {
              const criticHtml = `<div class="message user-msg ep-critic-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Critic</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Reviewing…</div></div></div></div>`;
              if (inner) inner.insertAdjacentHTML("beforeend", criticHtml);
            } else if (ev.endpointPhase === 'planning') {
              const plannerHtml = `<div class="message ep-planner-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Planner</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Planning…</div></div></div></div>`;
              if (inner) inner.insertAdjacentHTML("beforeend", plannerHtml);
            } else {
              const workerHtml = `<div class="message ep-worker-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Agent</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Thinking…</div></div></div></div>`;
              if (inner) inner.insertAdjacentHTML("beforeend", workerHtml);
            }
            buildTurnNav(conv);
          }
        }
        } /* close else (full reconnection) */
      } else if (_epCriticPhase && _epCriticMsg) {
        /* State snapshot during critic phase → update critic msg */
        _epCriticMsg.content = ev.content || "";
        _epCriticMsg.thinking = ev.thinking || "";
        if (_epCriticBuf) {
          _epCriticBuf.content = (_epCriticMsg.content || "").replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd();
          _epCriticBuf.thinking = _epCriticMsg.thinking;
        }
      } else {
        assistantMsg.content = ev.content || "";
        assistantMsg.thinking = ev.thinking || "";
        if (ev.error) assistantMsg.error = ev.error;
        if (ev.searchRounds) {
          /* Merge: keep checkpoint rounds + new ones from state snapshot */
          const existing = assistantMsg._continueSearchRounds || [];
          assistantMsg.searchRounds = existing.concat(ev.searchRounds || []);
          if (buf)
            buf.searchRounds = assistantMsg.searchRounds;
        }
        if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
        if (ev.usage) assistantMsg.usage = ev.usage;
        if (ev.model) assistantMsg.model = ev.model;
        else if (ev.preset) assistantMsg.model = ev.preset;
        else if (ev.effort) assistantMsg.model = ev.effort;
        if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
        if (buf) {
          buf.thinking = assistantMsg.thinking;
          buf.content = assistantMsg.content;
        }
      }
      twUpdate(convId);
      // ★ Re-trigger HG translations on state snapshot (handles page refresh / SSE reconnect)
      if (ev.searchRounds) _retriggerHgTranslations(convId);
    } else if (ev.type === "delta") {
      if (_epCriticPhase) {
        /* Accumulate into critic bubble instead of worker */
        if (_epCriticMsg) {
          if (ev.thinking) _epCriticMsg.thinking = (_epCriticMsg.thinking || "") + ev.thinking;
          if (ev.content)  _epCriticMsg.content  = (_epCriticMsg.content  || "") + ev.content;
          if (_epCriticBuf) {
            _epCriticBuf.thinking = _epCriticMsg.thinking || "";
            /* Strip [VERDICT: STOP/CONTINUE] tag during live streaming so
               the user never sees the raw structured marker.  The backend
               sends the fully-stripped content in endpoint_critic_msg later,
               but stripping here avoids a flash of the raw tag. */
            const _rawCritic = _epCriticMsg.content || "";
            _epCriticBuf.content = _rawCritic.replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd();
          }
        }
        twUpdate(convId);
      } else {
        if (ev.thinking) {
          assistantMsg.thinking = (assistantMsg.thinking || "") + ev.thinking;
          if (buf) buf.thinking = assistantMsg.thinking;
          _roundThinkingLen += ev.thinking.length;
        }
        if (ev.content) {
          assistantMsg.content = (assistantMsg.content || "") + ev.content;
          if (buf) buf.content = assistantMsg.content;
        }
        /* ★ CROSS-TALK DETECTION: verify assistantMsg is still the last message
         *   in the correct conversation. If conv.messages was overwritten by
         *   loadConversationMessages Phase 2, assistantMsg is now a dangling ref. */
        const _deltaConv = conversations.find(c => c.id === convId);
        if (_deltaConv) {
          const _deltaLastMsg = _deltaConv.messages[_deltaConv.messages.length - 1];
          if (_deltaLastMsg !== assistantMsg) {
            console.error(
              `[SSE delta] ⛔ DANGLING REF: assistantMsg is NOT the last message in conv=${convId.slice(0,8)}! ` +
              `conv.messages[${_deltaConv.messages.length-1}].role=${_deltaLastMsg?.role||'none'} ` +
              `assistantMsg ref has contentLen=${(assistantMsg.content||'').length}. ` +
              `Data is being accumulated into a DETACHED object — will be lost on next render!`
            );
          }
        }
        /* ★ Phase management during deltas:
         *   - Content delta arrived → model is producing visible output, clear phase
         *   - Thinking-only delta → model is reasoning, show thinking indicator
         *     This works on ALL rounds (even when msg.content is already non-empty
         *     from previous tool rounds) */
        if (buf) {
          if (ev.content) {
            buf.phase = null;
          } else if (ev.thinking && !ev.content) {
            buf.phase = { phase: "thinking_active", _thinkingLen: _roundThinkingLen };
          }
        }
        twUpdate(convId);
      }
    } else if (ev.type === "phase") {
      _roundThinkingLen = 0; // reset thinking counter on new phase
      if (_epCriticPhase) {
        /* Phase events during critic review — update critic buf instead */
        if (_epCriticBuf)
          _epCriticBuf.phase = { phase: ev.phase, detail: ev.detail || "",
            tools: ev.tools || [], toolContext: ev.toolContext || "", round: ev.round || 0 };
      } else if (buf) {
        buf.phase = {
          phase: ev.phase,
          detail: ev.detail || "",
          tools: ev.tools || [],
          toolContext: ev.toolContext || "",
          round: ev.round || 0,
        };
      }
      twUpdate(convId);
    } else if (ev.type === "tool_start") {
      if (_epCriticPhase) {
        /* Critic's tool usage → accumulate into critic message */
        if (_epCriticMsg) {
          const r = {
            roundNum: ev.roundNum, query: ev.query, results: null,
            status: "searching", toolName: ev.toolName || null,
            toolCallId: ev.toolCallId || null, toolArgs: ev.toolArgs || null,
            llmRound: ev.llmRound ?? null, _swarm: false,
          };
          if (!_epCriticMsg.searchRounds) _epCriticMsg.searchRounds = [];
          _epCriticMsg.searchRounds.push(r);
          if (_epCriticBuf) _epCriticBuf.searchRounds = _epCriticMsg.searchRounds;
        }
        twUpdate(convId);
      } else {
        const r = {
          roundNum: ev.roundNum,
          query: ev.query,
          results: null,
          status: "searching",
          toolName: ev.toolName || null,
          toolCallId: ev.toolCallId || null,
          toolArgs: ev.toolArgs || null,
          llmRound: ev.llmRound ?? null,
          _swarm: ev._swarm || false,
        };
        // ★ Preserve per-round assistantContent for Continue replay
        if (ev.assistantContent) r.assistantContent = ev.assistantContent;
        if (!assistantMsg.searchRounds) assistantMsg.searchRounds = [];
        assistantMsg.searchRounds.push(r);
        /* Track swarm round number so swarm_phase events can find it */
        if (r._swarm) assistantMsg._swarmRoundNum = r.roundNum;
        if (buf)
          buf.searchRounds = assistantMsg.searchRounds;
        twUpdate(convId);
      }
    } else if (ev.type === "human_guidance_request") {
      /* ── Human Guidance: LLM is asking the user a question ── */
      if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "awaiting_human";
          r.guidanceId = ev.guidanceId;
          r.guidanceQuestion = ev.question;
          r.guidanceType = ev.responseType;
          r.guidanceOptions = ev.options ? ev.options.map(o => ({...o})) : [];
        }
      }
      if (buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);
      // ★ Update sidebar to show amber blinking dot for awaiting-human state
      renderConversationList();
      // ★ Auto-translate question & options (EN→CN) when autoTranslate is ON.
      //   This mirrors the finishStream auto-translate flow for assistant messages.
      //   Fire-and-forget: translates asynchronously, re-renders card when done.
      const _hgConv = conversations.find(c => c.id === convId);
      const _hgAutoTrans = _hgConv ? (_hgConv.autoTranslate !== undefined ? !!_hgConv.autoTranslate : true) : !!autoTranslate;
      if (_hgAutoTrans && ev.question) {
        _autoTranslateHumanGuidance(convId, ev.roundNum, ev.question, ev.responseType, ev.options || []);
      }
    } else if (ev.type === "stdin_request") {
      /* ── Stdin Request: subprocess is waiting for user keyboard input ── */
      if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "awaiting_stdin";
          r.stdinId = ev.stdinId;
          r.stdinPrompt = ev.prompt;
          r.stdinCommand = ev.command;
        }
      }
      if (buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);
    } else if (ev.type === "stdin_resolved") {
      /* ── Stdin Resolved: user input was sent, command continues ── */
      if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "searching";
          r.stdinId = null;
          r.stdinPrompt = null;
        }
      }
      if (buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);
    } else if (ev.type === "write_approval_request") {
      if (_epCriticPhase) { /* skip approval during critic phase */ }
      else if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "pending_approval";
          r.approvalId = ev.approvalId;
          r.approvalMeta = ev.meta;
        }
      }
      if (!_epCriticPhase && buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);
    } else if (ev.type === "tool_result") {
      if (_epCriticPhase && _epCriticMsg) {
        /* Critic's tool result → accumulate into critic message */
        if (_epCriticMsg.searchRounds) {
          const r = _epCriticMsg.searchRounds.find(r => r.roundNum === ev.roundNum);
          if (r) { r.results = ev.results; r.status = "done"; if (ev.searchDiag) r.searchDiag = ev.searchDiag; if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown; }
        }
        if (_epCriticBuf) _epCriticBuf.searchRounds = _epCriticMsg.searchRounds || [];
        twUpdate(convId);
      } else if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.results = ev.results;
          r.status = "done";
          r.approvalId = null;
          r.approvalMeta = null;
          r.guidanceId = null;
          if (ev.searchDiag) r.searchDiag = ev.searchDiag;
          if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
        }
      }
      /* ★ Toast for create_memory */
      if (ev.results && ev.results.some(r => r.toolName === 'create_memory')) {
        const sk = ev.results.find(r => r.toolName === 'create_memory');
        const ok = sk.memoryOk === true || (sk.badge && sk.badge.includes('saved'));
        if (typeof showToast === 'function') {
          const sName = sk.memoryName || 'Memory';
          const sScope = sk.memoryScope || 'project';
          const title = ok ? `${sName}` : 'Memory Failed';
          const body = ok
            ? `Saved to ${sScope} scope — available in future sessions`
            : (sk.snippet || sk.title || 'Unknown error');
          showToast('', title, body, ok ? 5000 : 8000);
        }
      }
      if (buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);
      // ★ If this was an ask_human tool_result, refresh sidebar to clear amber dot
      if (ev.results && ev.results.some(r2 => r2.toolName === 'ask_human')) {
        renderConversationList();
      }
    } else if (ev.type === "tool_complete") {
      // ★ Store raw tool content for continue context restoration
      if (_epCriticPhase && _epCriticMsg) {
        if (_epCriticMsg.searchRounds) {
          const r = _epCriticMsg.searchRounds.find(r => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId);
          if (r) r.toolContent = ev.toolContent || null;
        }
        if (_epCriticBuf)
          _epCriticBuf.searchRounds = _epCriticMsg.searchRounds || [];
      } else if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(
          (r) => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId,
        );
        if (r) {
          r.toolContent = ev.toolContent || null;
        }
      }
      // ★ Sync to buf and let the reactive pipeline (twUpdate → _syncSearchRoundsDOM)
      //   handle preview button rendering — no fragile direct DOM injection needed.
      if (buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    } else if (ev.type === "emit_ref") {
      // ★ emit_to_user: store emitted tool content on message for inline rendering
      assistantMsg._emitContent = ev.emitContent || '';
      assistantMsg._emitToolName = ev.emitToolName || '';
      if (buf) buf._emitContent = assistantMsg._emitContent;
      if (buf) buf._emitToolName = assistantMsg._emitToolName;
      twUpdate(convId);

    } else if (ev.type === "timer_poll_check") {
      /* ═══ Timer Watcher inline poll progress ═══
         Each poll emits a sub-event attached to the timer_create tool round.
         We store polls as _timerPolls[] on the round for collapsible rendering. */
      if (assistantMsg.searchRounds) {
        const r = assistantMsg.searchRounds.find(r => r.roundNum === ev.roundNum);
        if (r) {
          if (!r._timerPolls) r._timerPolls = [];
          // ★ Dedup: skip if this pollNum already exists (from state snapshot)
          const _alreadyHas = r._timerPolls.some(p => p.pollNum === ev.pollNum && p.decision === ev.decision);
          if (!_alreadyHas) {
            r._timerPolls.push({
              pollNum: ev.pollNum,
              decision: ev.decision,
              reason: ev.reason || "",
              tokensUsed: ev.tokensUsed || 0,
              timerId: ev.timerId || "",
              ts: Date.now(),
            });
          }
          // Keep the round in "searching" state while timer is polling
          if (ev.decision === "ready") {
            r.status = "done";
            r._timerTriggered = true;
          } else {
            r.status = "searching";
          }
          r._timerTimerId = ev.timerId;
        }
      }
      if (buf)
        buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    /* ═══ Swarm mode events ═══ */
    } else if (ev.type === "swarm_phase") {
      /* Master-level swarm lifecycle: planning → spawning → wave_start → complete */
      if (!assistantMsg.searchRounds) assistantMsg.searchRounds = [];
      const _findSwarmRound = () => {
        const rn = assistantMsg._swarmRoundNum;
        return (assistantMsg.searchRounds || []).find(r => r._swarm && (rn ? r.roundNum === rn : true));
      };
      if (ev.phase === "spawning" || ev.phase === "planning" || ev.phase === "spawn_more") {
        /* Upgrade the existing tool_start round into a swarm panel */
        let sr = _findSwarmRound();
        const agentData = (ev.agents || []).map((a, i) => ({
          id: a.agentId || a.id || `agent-${i}`,
          role: a.role || "general",
          objective: a.objective || "",
          context: a.context || "",
          dependsOn: a.depends_on || a.dependsOn || [],
          status: "pending",
          phase: "waiting",
          preview: "",
          tools: [],
        }));
        if (sr) {
          sr.query = "Agent Swarm";
          sr._swarmActive = true;
          sr._swarmStartTime = sr._swarmStartTime || Date.now();
          if (ev.phase === "spawn_more" && agentData.length) {
            /* Append new agents from spawn_more — don't replace existing ones */
            if (!sr._swarmAgents) sr._swarmAgents = [];
            const existingIds = new Set(sr._swarmAgents.map(a => a.id));
            for (const ad of agentData) {
              if (!existingIds.has(ad.id)) sr._swarmAgents.push(ad);
            }
          } else if (agentData.length) {
            sr._swarmAgents = agentData;
          }
        } else {
          sr = {
            roundNum: (assistantMsg.searchRounds.length + 1),
            query: "Agent Swarm",
            results: null,
            status: "searching",
            toolName: "spawn_agents",
            _swarm: true,
            _swarmActive: true,
            _swarmStartTime: Date.now(),
            _swarmAgents: agentData,
          };
          assistantMsg.searchRounds.push(sr);
          assistantMsg._swarmRoundNum = sr.roundNum;
        }
      } else if (ev.phase === "complete") {
        /* Swarm finished */
        const sr = _findSwarmRound();
        if (sr) {
          sr.status = "done";
          sr._swarmActive = false;
          const elapsed = sr._swarmStartTime ? ((Date.now() - sr._swarmStartTime) / 1000).toFixed(1) + "s" : "";
          sr._elapsed = elapsed;
          sr._swarmStats = {
            totalTokens: ev.totalTokens || 0,
            totalCostUsd: ev.totalCost || 0,
            agentCount: ev.agentCount || 0,
            failedCount: ev.failedCount || 0,
          };
          /* Update agent data from final results */
          if (ev.agents && sr._swarmAgents) {
            for (const ea of ev.agents) {
              const agent = sr._swarmAgents.find(a => a.id === ea.agentId || a.id === ea.id);
              if (agent) {
                agent.status = ea.status === "completed" ? "done" : (ea.status || "done");
                if (ea.preview || ea.summary) agent.preview = ea.preview || ea.summary;
                if (ea.elapsed) agent.elapsed = ea.elapsed;
                if (ea.tokens) agent.tokens = ea.tokens;
              }
            }
          }
          for (const a of (sr._swarmAgents || [])) {
            if (a.status === "pending" || a.status === "running") a.status = "done";
          }
        }
      }
      if (buf) buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    } else if (ev.type === "swarm_agent_phase") {
      /* An individual agent changed phase (starting, thinking, tool_use, done, error) */
      const sr = (assistantMsg.searchRounds || []).find(r => r._swarmActive);
      if (sr) {
        if (!sr._swarmAgents) sr._swarmAgents = [];
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (!agent && ev.agentId) {
          /* ID not found — check if there's an existing agent with the same
             objective that hasn't been matched yet (stale from spawning event).
             This happens when the spawning event uses placeholder IDs that
             differ from the actual agent IDs assigned by the scheduler. */
          if (ev.objective) {
            const objNorm = ev.objective.trim().toLowerCase();
            agent = sr._swarmAgents.find(a =>
              a.id !== ev.agentId &&
              !a._idConfirmed &&
              (a.status === "pending" || a.status === "running" || a.phase === "starting" || a.phase === "Queued" || !a.phase) &&
              a.objective && (a.objective.trim().toLowerCase().startsWith(objNorm) || objNorm.startsWith(a.objective.trim().toLowerCase()))
            );
          }
          if (agent) {
            /* Re-map: update the stale placeholder ID to the real agent ID */
            agent.id = ev.agentId;
            agent._idConfirmed = true;
          } else {
            /* Genuinely new agent (e.g. from spawn_more) — add dynamically */
            agent = { id: ev.agentId, role: ev.role || "agent", objective: ev.objective || "",
                      status: "running", phase: "starting", preview: "", tools: [], _idConfirmed: true };
            sr._swarmAgents.push(agent);
          }
        }
        if (agent) agent._idConfirmed = true;
        if (agent) {
          agent.status = ev.status || agent.status;
          agent.phase = ev.phase || agent.phase;
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.objective) agent.objective = ev.objective;
          if (ev.error) agent.preview = ev.error;
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
        }
      }
      if (buf) buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    } else if (ev.type === "swarm_agent_progress") {
      /* Agent progress: tool usage, partial results, etc. */
      const sr = (assistantMsg.searchRounds || []).find(r => r._swarmActive);
      if (sr && sr._swarmAgents) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          agent.status = ev.status || "running";
          agent.phase = ev.phase || agent.phase;
          if (ev.preview) agent.preview = ev.preview;
          if (ev.toolNames) {
            agent.phase = "tool_use";
            if (!agent.tools) agent.tools = [];
            for (const tn of ev.toolNames) {
              if (!agent.tools.includes(tn)) agent.tools.push(tn);
            }
            agent.preview = `Using ${ev.toolNames.join(", ")}`;
          }
        }
      }
      if (buf) buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    } else if (ev.type === "swarm_agent_complete") {
      /* Individual agent finished */
      const sr = (assistantMsg.searchRounds || []).find(r => r._swarmActive || r._swarm);
      if (sr && sr._swarmAgents) {
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        /* Fallback: match by objective if ID doesn't match (ID remap) */
        if (!agent && ev.objective) {
          const objNorm = ev.objective.trim().toLowerCase();
          agent = sr._swarmAgents.find(a =>
            a.objective && (a.objective.trim().toLowerCase().startsWith(objNorm) || objNorm.startsWith(a.objective.trim().toLowerCase())) &&
            a.status !== "done" && a.status !== "failed"
          );
          if (agent) agent.id = ev.agentId;
        }
        if (agent) {
          agent.status = ev.status === "failed" ? "failed" : "done";
          agent.phase = ev.status === "failed" ? "error" : "done";
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
          if (ev.error) agent.preview = ev.error;
        }
      }
      if (buf) buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    } else if (ev.type === "swarm_agent_error") {
      const sr = (assistantMsg.searchRounds || []).find(r => r._swarmActive || r._swarm);
      if (sr && sr._swarmAgents) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          agent.status = "failed";
          agent.phase = "error";
          agent.preview = ev.error || ev.content || "Agent failed";
        }
      }
      if (buf) buf.searchRounds = assistantMsg.searchRounds || [];
      twUpdate(convId);

    } else if (ev.type === "messages_snapshot") {
      if (typeof showMessagesInDebug === "function")
        showMessagesInDebug(
          ev.messages,
          ev.label || `Round ${ev.round} · ${ev.messageCount}条`,
          true,
          convId,
          ev.tools || undefined,
        );

    /* ═══ Endpoint mode events ═══ */
    } else if (ev.type === "endpoint_iteration") {
      const isPlanning = ev.phase === "planning";
      const isReview = ev.phase === "reviewing";
      const phase = isPlanning ? "Planning" : (isReview ? "Reviewing" : "Working");
      if (!assistantMsg._epIter) assistantMsg._epIter = 0;
      assistantMsg._epIter = ev.iteration;
      const _isActiveConv = (activeConvId === convId);

      if (isPlanning) {
        /* ── Entering planner phase ── */
        _epCriticPhase = false;
        const conv = conversations.find(c => c.id === convId);
        // The planner streams into the initial assistantMsg (created by sendMessage)
        // Mark it as a planner message
        assistantMsg._isEndpointPlanner = true;
        assistantMsg.content = "";
        assistantMsg.thinking = "";

        if (_isActiveConv) {
          // Update the streaming bubble to show planner role
          const sm = document.getElementById("streaming-msg");
          if (sm) {
            sm.classList.add("ep-planner-msg");
            const roleEl = sm.querySelector(".message-role");
            if (roleEl) roleEl.textContent = "Planner";
            const avatarEl = sm.querySelector(".message-avatar");
            if (avatarEl) avatarEl.innerHTML = (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦';
            const bodyEl = sm.querySelector(".message-body");
            if (bodyEl) bodyEl.innerHTML = '<div class="stream-status"><div class="pulse"></div> Planning…</div>';
          }
        }

      } else if (isReview) {
        /* ── Entering critic phase ── */
        _epCriticPhase = true;

        // 1. Finalize the worker's streaming bubble (DOM — only if active)
        const conv = conversations.find(c => c.id === convId);
        assistantMsg.content = assistantMsg.content || "";
        assistantMsg.done = true;
        assistantMsg._epIteration = ev.iteration;
        if (_isActiveConv) {
          const sm = document.getElementById("streaming-msg");
          if (sm && conv) {
            const assistIdx = conv.messages.indexOf(assistantMsg);
            if (assistIdx >= 0) sm.outerHTML = renderMessage(assistantMsg, assistIdx);
          }
        }

        // 2. Create a critic message object in conv.messages for live streaming
        if (conv) {
          /* ★ Dedup: remove any stale DB-loaded critic for this iteration */
          const staleCriticIdx = conv.messages.findIndex(m =>
            m._isEndpointReview && m._epIteration === ev.iteration);
          if (staleCriticIdx >= 0) {
            conv.messages.splice(staleCriticIdx, 1);
            console.info(`[endpoint_iteration] Dedup — removed stale critic at idx=${staleCriticIdx} ` +
              `for iteration=${ev.iteration}`);
          }

          _epCriticMsg = {
            role: "user",
            content: "",
            thinking: "",
            searchRounds: [],
            timestamp: new Date().toISOString(),
            _isEndpointReview: true,
            _epIteration: ev.iteration,
            _epApproved: false,
            _isStuck: false,
          };
          conv.messages.push(_epCriticMsg);

          // 3. Create a streaming element for the critic (DOM — only if active)
          if (_isActiveConv) {
            const inner = document.getElementById("chatInner");
            const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            const criticStreamHtml = `<div class="message user-msg ep-critic-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Critic</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Reviewing…</div></div></div></div>`;
            if (inner) inner.insertAdjacentHTML("beforeend", criticStreamHtml);
          }

          // 4. Create a separate stream buffer for the critic
          _epCriticBuf = { content: "", thinking: "", searchRounds: [] };
          streamBufs.set(convId, _epCriticBuf);
          buf = _epCriticBuf;

          if (_isActiveConv) {
            buildTurnNav(conv);
            _forceScrollToBottom();
          }
        }
      } else if (!isPlanning) {
        /* ── Working phase ── */
        _epCriticPhase = false;

        /* After the planner finishes, the first worker turn (iteration 1) needs
         * a new assistant message + streaming bubble since the planner's bubble
         * was finalized by endpoint_planner_done.  For subsequent iterations,
         * endpoint_new_turn handles this.
         *
         * ★ FIX: Also handle the case where streaming-msg STILL EXISTS but
         *   belongs to the planner (endpoint_planner_done didn't finalize it,
         *   e.g. because activeConvId !== convId at that moment, or plannerIdx
         *   was -1).  In this case, finalize the planner element first, then
         *   create a fresh worker streaming bubble. */
        const conv = conversations.find(c => c.id === convId);
        if (conv) {
          let existingSm = document.getElementById("streaming-msg");

          /* ★ Detect stale planner streaming-msg: if the existing streaming-msg
           *   has ep-planner-msg class, the planner's finalization was missed.
           *   Finalize it now before creating the worker bubble. */
          if (existingSm && existingSm.classList.contains('ep-planner-msg')) {
            console.warn(`[endpoint_iteration] ⚠️ Stale planner streaming-msg detected — ` +
              `finalizing planner before starting worker phase (iter=${ev.iteration})`);
            const plannerMsg = conv.messages.find(m => m._isEndpointPlanner);
            if (plannerMsg && _isActiveConv) {
              plannerMsg.done = true;
              const plannerIdx = conv.messages.indexOf(plannerMsg);
              if (plannerIdx >= 0) {
                existingSm.outerHTML = renderMessage(plannerMsg, plannerIdx);
              } else {
                existingSm.remove();
              }
            } else if (_isActiveConv) {
              existingSm.remove();
            }
            existingSm = null;  // force creation of a new worker streaming-msg
          }

          if (!existingSm) {
            /* ★ Dedup: remove any stale DB-loaded worker for this iteration */
            const staleIdx = conv.messages.findIndex(m =>
              m.role === "assistant" && m._epIteration === ev.iteration);
            if (staleIdx >= 0) {
              conv.messages.splice(staleIdx);
            }

            const newAssistant = {
              role: "assistant",
              content: "",
              thinking: "",
              searchRounds: [],
              timestamp: new Date().toISOString(),
              _epIteration: ev.iteration,
            };
            conv.messages.push(newAssistant);
            assistantMsg = newAssistant;

            // Reset stream buffer for the new worker turn
            const newBuf = { content: "", thinking: "", searchRounds: [] };
            streamBufs.set(convId, newBuf);
            buf = newBuf;

            // Create streaming element — only if this conv is active
            if (_isActiveConv) {
              const inner = document.getElementById("chatInner");
              const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
              const streamHtml = `<div class="message ep-worker-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Agent</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Thinking…</div></div></div></div>`;
              if (inner) inner.insertAdjacentHTML("beforeend", streamHtml);
              buildTurnNav(conv);
              _forceScrollToBottom();
            }
          }
        }
      }

      if (_isActiveConv) {
        const bannerText = isPlanning
          ? `Endpoint Planning`
          : `Endpoint ${phase} — Iteration ${ev.iteration}`;
        const banner = document.getElementById("ep-iter-banner");
        if (banner) {
          banner.textContent = bannerText;
        } else {
          const sm = document.getElementById("streaming-msg");
          if (sm) {
            const b = document.createElement("div");
            b.id = "ep-iter-banner";
            b.className = "ep-iter-banner" + (isPlanning ? " ep-iter-banner-planner" : "");
            b.textContent = bannerText;
            const content = sm.querySelector(".message-content");
            if (content) content.prepend(b);
          }
        }
      }

    } else if (ev.type === "endpoint_planner_done") {
      /* ── Planner finished — finalize the planner streaming bubble, prepare for worker ── */
      const conv = conversations.find(c => c.id === convId);
      if (conv) {
        // Update the planner message with final content
        assistantMsg.content = ev.content || assistantMsg.content;
        assistantMsg.thinking = ev.thinking || assistantMsg.thinking;
        assistantMsg._isEndpointPlanner = true;
        assistantMsg.done = true;
        if (ev.usage) assistantMsg.usage = ev.usage;

        // Re-render the streaming element as a static planner bubble (DOM — only if active)
        if (activeConvId === convId) {
          const sm = document.getElementById("streaming-msg");
          const plannerIdx = conv.messages.indexOf(assistantMsg);
          if (sm && plannerIdx >= 0) {
            sm.outerHTML = renderMessage(assistantMsg, plannerIdx);
          } else if (sm) {
            /* ★ FIX: assistantMsg is a dangling ref (conv.messages was replaced,
             * e.g. by loadConversationMessages Phase 2).  The planner message
             * exists in conv.messages under a different object.  Re-add assistantMsg
             * to conv.messages if missing, or at minimum remove the streaming-msg
             * so the working phase handler creates a fresh assistant message. */
            const existingPlanner = conv.messages.find(m => m._isEndpointPlanner);
            if (existingPlanner) {
              /* Planner exists from backend sync — just remove the streaming bubble */
              sm.outerHTML = renderMessage(existingPlanner, conv.messages.indexOf(existingPlanner));
            } else {
              /* No planner in conv.messages — re-insert our flagged copy */
              const userIdx = conv.messages.findIndex(m => m.role === "user");
              const insertAt = userIdx >= 0 ? userIdx + 1 : conv.messages.length;
              conv.messages.splice(insertAt, 0, assistantMsg);
              sm.outerHTML = renderMessage(assistantMsg, insertAt);
            }
            console.warn(`[endpoint_planner_done] ⚠️ Dangling assistantMsg ref — ` +
              `recovered by ${existingPlanner ? 'using existing planner' : 're-inserting'} ` +
              `conv=${convId.slice(0,8)}`);
          }
        }

        // Save & update nav
        saveConversations(convId);
        if (activeConvId === convId) {
          buildTurnNav(conv);
          _forceScrollToBottom();
        }
      }

    } else if (ev.type === "endpoint_critic_msg") {
      /* ── Critic finished — finalize the critic streaming bubble ── */
      _epCriticPhase = false;
      const conv = conversations.find(c => c.id === convId);
      if (conv) {
        // Update the critic message with final content from event
        // (the event content has the verdict tag stripped by the backend)
        if (_epCriticMsg) {
          _epCriticMsg.content = ev.content;
          _epCriticMsg._epApproved = !!ev.should_stop;
          _epCriticMsg._isStuck = ev.is_stuck || false;
          _epCriticMsg.done = true;
        }

        // Re-render the streaming element as a static critic bubble (DOM — only if active)
        if (activeConvId === convId) {
          const sm = document.getElementById("streaming-msg");
          const criticIdx = _epCriticMsg ? conv.messages.indexOf(_epCriticMsg) : -1;
          if (sm && criticIdx >= 0) {
            sm.outerHTML = renderMessage(_epCriticMsg, criticIdx);
          }
        }

        // Clean up critic state
        _epCriticMsg = null;
        _epCriticBuf = null;

        // Save & update nav
        saveConversations(convId);
        if (activeConvId === convId) {
          buildTurnNav(conv);
          _forceScrollToBottom();
        }
      }

    } else if (ev.type === "endpoint_new_turn") {
      /* ── Worker starts a new revision turn — renders as normal assistant reply ── */
      _epCriticPhase = false;
      const conv = conversations.find(c => c.id === convId);
      if (conv) {
        /* ★ Dedup: if this iteration already exists from a DB-loaded endpoint turn
         *   (page reload reconnection), remove the stale DB version first and
         *   re-use a fresh streaming assistant message.  Also remove any stale
         *   critic messages for this iteration and beyond. */
        const staleIdx = conv.messages.findIndex(m =>
          m.role === "assistant" && m._epIteration === ev.iteration);
        if (staleIdx >= 0) {
          // Remove this iteration's worker turn and everything after it
          // (subsequent critic + worker turns will be re-streamed)
          conv.messages.splice(staleIdx);
          console.info(`[endpoint_new_turn] Dedup — removed stale turns from idx=${staleIdx} ` +
            `for iteration=${ev.iteration}, conv=${convId.slice(0,8)}`);
        }

        const newAssistant = {
          role: "assistant",
          content: "",
          thinking: "",
          searchRounds: [],
          timestamp: new Date().toISOString(),
          _epIteration: ev.iteration,
        };
        conv.messages.push(newAssistant);
        assistantMsg = newAssistant;

        // Reset stream buffer for the new worker turn
        const newBuf = { content: "", thinking: "", searchRounds: [] };
        streamBufs.set(convId, newBuf);
        buf = newBuf;

        // DOM operations — only if this conv is currently viewed
        if (activeConvId === convId) {
          // Create new streaming-msg element — looks like normal assistant reply
          const inner = document.getElementById("chatInner");
          const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          const streamHtml = `<div class="message ep-worker-msg" id="streaming-msg"><div class="message-avatar">${(typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦'}</div><div class="message-content"><div class="message-header"><span class="message-role">Agent</span><span class="message-time">${time}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> Thinking…</div></div></div></div>`;
          if (inner) inner.insertAdjacentHTML("beforeend", streamHtml);

          // Update banner & turn-nav
          const banner = document.getElementById("ep-iter-banner");
          if (banner) banner.textContent = `Endpoint Iteration ${ev.iteration}`;
          buildTurnNav(conv);

          _forceScrollToBottom();
        }
      }

    } else if (ev.type === "endpoint_complete") {
      _epCriticPhase = false;
      assistantMsg.endpointResult = {
        totalIterations: ev.totalIterations,
        reason: ev.reason,
      };
      const reasonLabel = { approved: "Approved", stuck: "Stuck", max_iterations: "Max Iterations", error: "Error", aborted: "Aborted" }[ev.reason] || ev.reason;

      if (activeConvId === convId) {
        const banner = document.getElementById("ep-iter-banner");
        if (banner) banner.textContent = `Done — ${reasonLabel} (${ev.totalIterations} iterations)`;

        /* Clean up any dangling streaming-msg element (e.g. if endpoint_new_turn
           was emitted but no worker phase ran before max_iterations break). */
        const danglingSm = document.getElementById("streaming-msg");
        if (danglingSm && !danglingSm.querySelector(".md-content")) {
          /* Only remove if still showing placeholder ("Thinking…" / "Reviewing…"),
             not if it has real content that hasn't been finalized yet. */
          danglingSm.remove();
        }
      }
      /* Remove empty assistant message from data regardless of active view */
      const conv = conversations.find(c => c.id === convId);
      if (conv && assistantMsg && !assistantMsg.content) {
        const idx = conv.messages.indexOf(assistantMsg);
        if (idx >= 0) conv.messages.splice(idx, 1);
      }

      /* ★ FIX: Clean up ghost assistant messages — unmarked assistants left by
       * startAssistantResponse() that weren't properly absorbed into the endpoint
       * planner turn.  Only scan AFTER the last base user message to avoid
       * accidentally removing legitimate historical assistant messages from
       * previous non-endpoint conversation turns. */
      if (conv) {
        const hasEpTurns = conv.messages.some(m => m._isEndpointPlanner || m._epIteration);
        if (hasEpTurns) {
          /* Find the last "base" user message (not an endpoint review) —
           * ghost assistants can only appear between this user message
           * and the first endpoint-marked message (planner). */
          let lastBaseUserIdx = -1;
          for (let i = conv.messages.length - 1; i >= 0; i--) {
            if (conv.messages[i].role === 'user' && !conv.messages[i]._isEndpointReview) {
              lastBaseUserIdx = i;
              break;
            }
          }
          let cleaned = 0;
          /* Only scan messages AFTER the last base user message */
          for (let i = conv.messages.length - 1; i > lastBaseUserIdx && i >= 0; i--) {
            const m = conv.messages[i];
            if (m.role === "assistant"
                && !m._isEndpointPlanner
                && !m._epIteration
                && !m._isEndpointReview) {
              /* This is a ghost — an assistant message without endpoint markers
               * sitting after the last user message (created by startAssistantResponse) */
              console.warn(`[endpoint_complete] 🧹 Removing ghost assistant at idx=${i} ` +
                `contentLen=${(m.content||'').length} conv=${convId.slice(0,8)}`);
              conv.messages.splice(i, 1);
              cleaned++;
            }
          }
          if (cleaned > 0) {
            console.info(`[endpoint_complete] Cleaned ${cleaned} ghost assistant(s) from conv=${convId.slice(0,8)}`);
          }
        }
      }

    } else if (ev.type === "sse_timeout") {
      /* SSE connection hit max duration — backend task is STILL RUNNING.
         Show a toast and return false (not done). The stream will close,
         _trySSE will detect !streamDone and return false, triggering _pollFallback. */
      if (typeof showToast === 'function') {
        showToast('', 'Connection Switched',
          'Long-running task: SSE stream reached max duration. Switching to polling — your task is still running in the background.',
          10000);
      }
      console.warn(
        `[_trySSE] SSE timeout notice received — taskId=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
        `contentSoFar=${assistantMsg.content?.length || 0}chars thinkingSoFar=${assistantMsg.thinking?.length || 0}chars ` +
        `toolRounds=${assistantMsg.searchRounds?.length || 0} — backend continues, switching to poll fallback`
      );
      // Return false — NOT a done event. Task is still running.
      return false;

    } else if (ev.type === "done") {
      /* ★ DIAGNOSTIC: log task completion details for debugging silent completions */
      const _dContentLen = assistantMsg.content?.length || 0;
      const _dThinkLen = assistantMsg.thinking?.length || 0;
      const _dToolRounds = assistantMsg.searchRounds?.length || 0;
      /* ★ CROSS-TALK DETECTION: verify the conv we're writing to still matches */
      const _dConv = conversations.find(c => c.id === convId);
      const _dMsgCount = _dConv?.messages?.length || 0;
      const _dIsActive = activeConvId === convId;
      console.log(
        `[connectToTask] DONE event received — task=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
        `finishReason=${ev.finishReason || 'none'} ` +
        `contentLen=${_dContentLen} thinkingLen=${_dThinkLen} ` +
        `toolRounds=${_dToolRounds} error=${ev.error || 'none'} ` +
        `model=${ev.model || 'unknown'} msgCount=${_dMsgCount} ` +
        `isActiveConv=${_dIsActive} activeConvId=${activeConvId?.slice(0,8)||'null'}`
      );
      if (_dContentLen === 0 && _dThinkLen === 0 && !ev.error) {
        console.error(
          `[connectToTask] ⚠ SUSPICIOUS DONE: task=${taskId.slice(0,8)} completed with ` +
          `ZERO content and ZERO thinking but no error flag. ` +
          `finishReason=${ev.finishReason} — possible silent completion bug!`
        );
      }
      if (ev._diagnostics) {
        console.warn(
          `[connectToTask] 🔍 SERVER DIAGNOSTICS for task=${taskId.slice(0,8)}:`,
          ev._diagnostics
        );
      }
      if (ev.error) assistantMsg.error = ev.error;
      if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
      if (ev.model) assistantMsg.model = ev.model;
      else if (ev.preset) assistantMsg.model = ev.preset;
      else if (ev.effort) assistantMsg.model = ev.effort;
      if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
      if (ev.toolSummary) assistantMsg.toolSummary = ev.toolSummary;
      if (ev.fallbackModel) assistantMsg.fallbackModel = ev.fallbackModel;
      if (ev.fallbackFrom) assistantMsg.fallbackFrom = ev.fallbackFrom;
      /* ★ Continue: merge modifiedFiles & modifiedFileList with existing */
      if (ev.modifiedFiles != null) {
        if (assistantMsg._continueModifiedFiles) {
          assistantMsg.modifiedFiles = assistantMsg._continueModifiedFiles + ev.modifiedFiles;
          delete assistantMsg._continueModifiedFiles;
        } else {
          assistantMsg.modifiedFiles = ev.modifiedFiles;
        }
      }
      if (ev.modifiedFileList) {
        if (assistantMsg._continueModifiedFileList) {
          // Merge: old files + new files, dedup by path (new action wins)
          const merged = new Map();
          for (const f of assistantMsg._continueModifiedFileList) merged.set(f.path, f);
          for (const f of ev.modifiedFileList) merged.set(f.path, f);
          assistantMsg.modifiedFileList = Array.from(merged.values());
          delete assistantMsg._continueModifiedFileList;
        } else {
          assistantMsg.modifiedFileList = ev.modifiedFileList;
        }
      }
      if (ev.taskId) assistantMsg._taskId = ev.taskId;
      /* ★ Continue: merge usage & apiRounds with existing */ if (ev.usage) {
        if (assistantMsg._continueUsage) {
          const cu = assistantMsg._continueUsage;
          const nu = ev.usage;
          assistantMsg.usage = {};
          for (const k of new Set([...Object.keys(cu), ...Object.keys(nu)])) {
            const cv = cu[k],
              nv = nu[k];
            assistantMsg.usage[k] =
              typeof cv === "number" && typeof nv === "number"
                ? cv + nv
                : (nv ?? cv);
          }
          delete assistantMsg._continueUsage;
        } else {
          assistantMsg.usage = ev.usage;
        }
      }
      if (ev.apiRounds) {
        if (assistantMsg._continueApiRounds) {
          assistantMsg.apiRounds = assistantMsg._continueApiRounds.concat(
            ev.apiRounds,
          );
          delete assistantMsg._continueApiRounds;
        } else {
          assistantMsg.apiRounds = ev.apiRounds;
        }
      }
      /* ★ Clean up continue checkpoint markers */
      delete assistantMsg._continueSearchRounds;
      delete assistantMsg._continueContentPrefix;
      delete assistantMsg._continueModifiedFiles;
      delete assistantMsg._continueModifiedFileList;
      return true;
    }
    return false;
  }
  try {
    // ★ Item 6: If we have a previous Last-Event-ID (from a prior connection
    //   attempt for this task), send it so the server can resume from that
    //   cursor instead of replaying the full state snapshot.
    const _sseHeaders = {};
    if (stream._lastEventId) {
      _sseHeaders['Last-Event-ID'] = stream._lastEventId;
      console.info(`[_trySSE] Reconnecting with Last-Event-ID=${stream._lastEventId} for task=${taskId.slice(0,8)}`);
    }
    const resp = await fetch(apiUrl(`/api/chat/stream/${taskId}`), {
      signal: stream.controller.signal,
      headers: _sseHeaders,
    });
    if (!resp.ok) {
      clearTimeout(sseTimeout);
      if (resp.status === 404) return false;
      throw new Error(`HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "",
      streamDone = false;
    while (!streamDone) {
      const { done: rd, value } = await reader.read();
      if (rd) {
        /* ★ Process any remaining data in buffer after stream closes */ if (
          buffer.trim()
        ) {
          const remaining = buffer.split("\n");
          for (const line of remaining) {
            if (_processSSELine(line)) {
              streamDone = true;
            }
          }
        }
        break;
      }
      gotData = true;
      clearTimeout(sseTimeout);
      _streamTimerTouch(convId); // ★ Any bytes (including keepalives) prove server is alive
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        const isDone = _processSSELine(line);
        if (isDone) {
          streamDone = true;
          break;
        }
      }
      const now = Date.now();
      if (now - lastSave > 3000) {
        /* ★ CROSS-TALK DETECTION: verify the conv we're about to save still has
         *   the right message count and the assistantMsg ref is valid */
        const _saveConv = conversations.find(c => c.id === convId);
        if (_saveConv) {
          const _saveLast = _saveConv.messages[_saveConv.messages.length - 1];
          if (_saveLast !== assistantMsg) {
            console.error(
              `[_trySSE] ⛔ PERIODIC SAVE: assistantMsg ref DETACHED from conv=${convId.slice(0,8)}! ` +
              `conv.messages[-1].role=${_saveLast?.role||'none'} ≠ assistantMsg. ` +
              `Streaming data is accumulating into a ghost object!`
            );
          }
          if (activeStreams.size > 1) {
            console.info(
              `[_trySSE] 📊 Periodic save: conv=${convId.slice(0,8)} msgs=${_saveConv.messages.length} ` +
              `contentLen=${(assistantMsg.content||'').length} ` +
              `concurrentStreams=${activeStreams.size} ` +
              `otherConvs=[${[...activeStreams.keys()].filter(k=>k!==convId).map(k=>k.slice(0,8)).join(',')}]`
            );
          }
        }
        saveConversations(convId);
        lastSave = now;
      }
      /* ★ No IndexedDB cache write during streaming — the server checkpoints
       *   to PostgreSQL every 5s (checkpoint_task_partial), which is always fresher.
       *   Cache is only updated in finishStream() when the stream completes. */
    }
    if (!streamDone) {
      /* ★ SSE stream closed prematurely without receiving 'done' event
         (e.g. proxy/TCP timeout on long-running tasks). Reset controller
         and return false so connectToTask falls back to polling. */
      const _accContent = assistantMsg.content?.length || 0;
      const _accThinking = assistantMsg.thinking?.length || 0;
      const _accRounds = assistantMsg.searchRounds?.length || 0;
      console.error(
        `[_trySSE] ⚠ SSE PREMATURE CLOSE — taskId=${taskId.slice(0,8)} ` +
        `contentAccumulated=${_accContent}chars thinkingAccumulated=${_accThinking}chars ` +
        `toolRounds=${_accRounds} lastEventId=${_lastEventId || 'none'} — falling back to poll. ` +
        `If poll returns empty content, this accumulated data will be OVERWRITTEN!\n` +
        `Possible causes: proxy timeout, TCP reset, server crash, nginx buffering.\n` +
        `Check server logs for matching task ID: ${taskId}`
      );
      /* ★ Emergency save: persist whatever we accumulated via SSE before poll overwrites it */
      saveConversations(convId);
      /* ★ No emergency cache write — server DB has 5s-fresh checkpoint data.
       *   Writing partial SSE-accumulated data to cache would create a stale
       *   snapshot that's WORSE than what the server already has. */
      // ★ Item 6: Save last event ID so reconnection can resume from cursor
      if (_lastEventId) stream._lastEventId = _lastEventId;
      stream.controller = new AbortController();
      return false;
    }
    twStop(convId);
    finishStream(convId);
    return true;
  } catch (e) {
    clearTimeout(sseTimeout);
    if (e.name === "AbortError") {
      /* ★ Check if this was a timer-probe abort (task already done on server,
       *   SSE pipe is stale) vs a user-initiated stop.
       *   Timer probe sets stream._probeAbort = true before aborting. */
      if (stream._probeAbort) {
        delete stream._probeAbort;
        console.warn(`[_trySSE] ★ Timer probe abort — task done on server, SSE was stale. lastEventId=${_lastEventId || 'none'}`);
        if (_lastEventId) stream._lastEventId = _lastEventId;
        stream.controller = new AbortController();
        return false;  // → triggers _pollFallback to retrieve completed result
      }
      if (!gotData) {
        stream.controller = new AbortController();
        return false;
      }
      throw e;  // re-throw user abort with gotData → connectToTask handles it
    }
    throw e;
  }
}

async function _pollFallback(convId, taskId, stream, assistantMsg) {
  let lastSave = Date.now();
  const buf = streamBufs.get(convId);
  const _preExistingContent = assistantMsg.content?.length || 0;
  const _preExistingThinking = assistantMsg.thinking?.length || 0;
  console.warn(`[_pollFallback] START — conv=${convId.slice(0,8)} taskId=${taskId.slice(0,8)} preExistingContent=${_preExistingContent}chars preExistingThinking=${_preExistingThinking}chars`);
  // Poll until the task finishes, the user aborts, or server is confirmed dead.
  let _pollIter = 0;
  let _consecutiveErrors = 0;     // ★ Circuit breaker: track consecutive network failures
  const _MAX_CONSECUTIVE_ERRORS = 10; // ★ After 10 failures (~5s), do health check
  let _rttEma = 300; // ★ Item 8: exponential moving average of poll RTT (ms), seed 300ms
  while (true) {
    if (stream.controller.signal.aborted) {
      console.warn(`[_pollFallback] ABORTED at iteration ${_pollIter} — conv=${convId.slice(0,8)}`);
      twStop(convId);
      finishStream(convId);
      return;
    }
    const _pollStart = Date.now();
    try {
      const resp = await fetch(apiUrl(`/api/chat/poll/${taskId}`));
      if (!resp.ok) {
        if (resp.status === 404) {
          console.error(`[_pollFallback] 404 NOT FOUND — taskId=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
            `existingContent=${assistantMsg.content?.length||0}chars existingThinking=${assistantMsg.thinking?.length||0}chars — ` +
            `${(assistantMsg.content || assistantMsg.thinking) ? 'PRESERVING existing accumulated data' : 'NO DATA to preserve, marking error'}`);
          if (!assistantMsg.content && !assistantMsg.thinking)
            assistantMsg.error = "Task not found";
          twStop(convId);
          finishStream(convId);
          return;
        }
        throw new Error(`Poll HTTP ${resp.status}`);
      }
      _consecutiveErrors = 0; // ★ Reset on any successful response
      const data = await resp.json();

      /* ★ Endpoint mode: poll returns endpointTurns with the full multi-turn
       *   structure.  Rebuild conv.messages from it instead of overwriting
       *   a single assistantMsg with the current turn's content. */
      if (data.endpointMode && data.endpointTurns && data.endpointTurns.length > 0) {
        const conv = conversations.find(c => c.id === convId);
        if (conv) {
          // Find where original messages end (non-endpoint messages)
          let baseEnd = 0;
          for (let i = 0; i < conv.messages.length; i++) {
            if (!conv.messages[i]._epIteration && !conv.messages[i]._isEndpointReview && !conv.messages[i]._isEndpointPlanner) {
              baseEnd = i + 1;
            }
          }
          const baseMsgs = conv.messages.slice(0, baseEnd);
          const prevEpCount = conv._epPollTurnCount || 0;
          const newEpCount = data.endpointTurns.length;

          // Replace endpoint turns with the server's authoritative copy
          conv.messages = baseMsgs.concat(data.endpointTurns);
          conv._epPollTurnCount = newEpCount;

          // Point assistantMsg to the last assistant message for metadata/finishStream
          const lastAssist = [...conv.messages].reverse().find(m => m.role === "assistant");
          if (lastAssist) {
            assistantMsg = lastAssist;
          }

          // ★ DO NOT overwrite completed turn content with data.content!
          // data.content is the IN-PROGRESS turn (not yet in endpointTurns).
          // Completed turns in endpointTurns already have their full content.

          console.info(`[_pollFallback] Endpoint sync — conv=${convId.slice(0,8)} ` +
            `baseMsgs=${baseMsgs.length} endpointTurns=${newEpCount} ` +
            `totalMsgs=${conv.messages.length} prevTurns=${prevEpCount}`);

          // ★ Re-render the full conversation when new completed turns arrive
          if (newEpCount !== prevEpCount && activeConvId === convId) {
            renderChat(conv);
          }
        }
      } else {
        /* ★ Normal (non-endpoint) mode: update single assistantMsg */
        /* Data loss detection: warn if poll overwrites accumulated content with empty/shorter content */
        if (data.content != null) {
          const oldLen = assistantMsg.content?.length || 0;
          const newLen = data.content.length;
          if (oldLen > 0 && newLen < oldLen * 0.5) {
            console.error(`[_pollFallback] ⚠️ CONTENT REGRESSION — conv=${convId.slice(0,8)} ` +
              `oldContentLen=${oldLen} newContentLen=${newLen} — poll is overwriting ${oldLen - newLen} chars of accumulated content!`);
          }
          assistantMsg.content = data.content;
          if (buf) buf.content = assistantMsg.content;
        }
        if (data.thinking != null) {
          const oldThinkLen = assistantMsg.thinking?.length || 0;
          const newThinkLen = data.thinking.length;
          if (oldThinkLen > 0 && newThinkLen < oldThinkLen * 0.5) {
            console.error(`[_pollFallback] ⚠️ THINKING REGRESSION — conv=${convId.slice(0,8)} ` +
              `oldThinkingLen=${oldThinkLen} newThinkingLen=${newThinkLen} — poll is overwriting thinking!`);
          }
          assistantMsg.thinking = data.thinking;
          if (buf) buf.thinking = assistantMsg.thinking;
        }
      }
      if (data.error) assistantMsg.error = data.error;
      if (data.finishReason) assistantMsg.finishReason = data.finishReason;
      if (data.usage) {
        if (assistantMsg._continueUsage) {
          // Merge usage: sum numeric fields
          const cu = assistantMsg._continueUsage;
          for (const k of Object.keys(data.usage)) {
            const cv = cu[k], nv = data.usage[k];
            data.usage[k] = typeof cv === 'number' && typeof nv === 'number' ? cv + nv : (nv ?? cv);
          }
        }
        assistantMsg.usage = data.usage;
      }
      if (data.preset) assistantMsg.preset = data.preset;
      else if (data.effort) assistantMsg.preset = data.effort;
      if (data.model) assistantMsg.model = data.model;
      if (data.thinkingDepth) assistantMsg.thinkingDepth = data.thinkingDepth;
      if (data.toolSummary) assistantMsg.toolSummary = data.toolSummary;
      if (data.fallbackModel) assistantMsg.fallbackModel = data.fallbackModel;
      if (data.fallbackFrom) assistantMsg.fallbackFrom = data.fallbackFrom;
      /* ★ Continue: merge modifiedFiles & modifiedFileList with checkpoint */
      if (data.modifiedFiles != null) {
        if (assistantMsg._continueModifiedFiles) {
          assistantMsg.modifiedFiles = assistantMsg._continueModifiedFiles + data.modifiedFiles;
          delete assistantMsg._continueModifiedFiles;
        } else {
          assistantMsg.modifiedFiles = data.modifiedFiles;
        }
      }
      if (data.modifiedFileList) {
        if (assistantMsg._continueModifiedFileList) {
          const merged = new Map();
          for (const f of assistantMsg._continueModifiedFileList) merged.set(f.path, f);
          for (const f of data.modifiedFileList) merged.set(f.path, f);
          assistantMsg.modifiedFileList = Array.from(merged.values());
          delete assistantMsg._continueModifiedFileList;
        } else {
          assistantMsg.modifiedFileList = data.modifiedFileList;
        }
      }
      if (data.taskId) assistantMsg._taskId = data.taskId;
      if (data.apiRounds) {
        const existingApiRounds = assistantMsg._continueApiRounds || [];
        assistantMsg.apiRounds = existingApiRounds.concat(data.apiRounds);
      }
      if (data.searchRounds) {
        const existingRounds = assistantMsg._continueSearchRounds || [];
        assistantMsg.searchRounds = existingRounds.concat(data.searchRounds);
        if (buf) buf.searchRounds = assistantMsg.searchRounds;
      }
      if (buf) buf.phase = data.phase || null;
      twUpdate(convId);
      const now = Date.now();
      if (now - lastSave > 3000) {
        saveConversations(convId);
        lastSave = now;
      }
      /* ★ No cache write during polling — server DB is always fresher */
      if (data.status !== "running") {
        /* ★ If status is 'interrupted', the server crashed mid-generation.
           Mark finishReason so the UI shows the recovery indicator. */
        if (data.status === 'interrupted' && !assistantMsg.finishReason) {
          assistantMsg.finishReason = 'interrupted';
          console.warn(`[_pollFallback] Task ${taskId.slice(0,8)} was interrupted (server crash recovery) — ` +
            `recovered content=${assistantMsg.content?.length||0}chars thinking=${assistantMsg.thinking?.length||0}chars`);
        }
        /* ★ Clean up continue checkpoint markers (poll fallback) */
        delete assistantMsg._continueSearchRounds;
        delete assistantMsg._continueApiRounds;
        delete assistantMsg._continueUsage;
        delete assistantMsg._continueModifiedFiles;
        delete assistantMsg._continueModifiedFileList;
        twStop(convId);
        finishStream(convId);
        return;
      }
    } catch (e) {
      if (e.name === "AbortError") {
        twStop(convId);
        finishStream(convId);
        return;
      }
      _consecutiveErrors++;
      debugLog(`Poll error (${_consecutiveErrors}/${_MAX_CONSECUTIVE_ERRORS}): ${e.message}`, "warn");
      if (typeof _reportClientError === 'function') _reportClientError(`[poll] ${e.message}`);

      // ★ Circuit breaker: after N consecutive failures, check server health.
      //   For VSCode port forwarding drops, the outage may last 10-60s while
      //   the tunnel re-establishes. We enter a "network recovery wait" mode
      //   that waits up to 2 minutes before truly giving up.
      if (_consecutiveErrors >= _MAX_CONSECUTIVE_ERRORS) {
        console.error(`[_pollFallback] ⚠️ CIRCUIT BREAKER — ${_consecutiveErrors} consecutive poll failures for conv=${convId.slice(0,8)}`);
        const alive = await _checkServerHealth();
        if (!alive) {
          // ★ Network Recovery Wait: instead of immediately giving up, wait
          //   up to 2 minutes for the server to come back (VSCode reconnect).
          //   During this wait, check health every 5 seconds.
          const _RECOVERY_WAIT_MS = 120000; // 2 minutes
          const _RECOVERY_POLL_MS = 5000;   // check every 5s
          const _recoveryStart = Date.now();
          let _recovered = false;
          console.warn(`[_pollFallback] 🔄 Entering network recovery wait (up to ${_RECOVERY_WAIT_MS/1000}s) for conv=${convId.slice(0,8)}`);
          showToast('🔄', 'Connection Lost',
            'Server unreachable — waiting for reconnection… Task is still running on the server.', 8000);
          while (Date.now() - _recoveryStart < _RECOVERY_WAIT_MS) {
            if (stream.controller.signal.aborted) {
              twStop(convId);
              finishStream(convId);
              return;
            }
            await new Promise(r => setTimeout(r, _RECOVERY_POLL_MS));
            // Force a fresh health check (bypass cache)
            _lastHealthCheck = 0;
            const nowAlive = await _checkServerHealth();
            if (nowAlive) {
              console.warn(`[_pollFallback] ✅ Server is BACK after ${Math.round((Date.now() - _recoveryStart)/1000)}s — resuming poll for conv=${convId.slice(0,8)}`);
              _recovered = true;
              _consecutiveErrors = 0;
              showToast('✅', 'Reconnected', 'Server connection restored — resuming…', 4000);
              break;
            }
            console.debug(`[_pollFallback] Still waiting for server… ${Math.round((Date.now() - _recoveryStart)/1000)}s elapsed`);
          }
          if (!_recovered) {
            console.error(`[_pollFallback] 💀 SERVER STILL DEAD after ${_RECOVERY_WAIT_MS/1000}s recovery wait — force-finishing for conv=${convId.slice(0,8)} ` +
              `content=${assistantMsg.content?.length||0}chars thinking=${assistantMsg.thinking?.length||0}chars`);
            assistantMsg.finishReason = 'server_offline';
            assistantMsg.error = '⚠️ Server offline — response may be incomplete. This notice will clear automatically when the server comes back.';
            saveConversations(convId);
            twStop(convId);
            finishStream(convId);
            showToast('⚠️', 'Server Offline',
              'Backend server did not reconnect within 2 minutes. Your partial response has been saved. It will recover automatically when the server comes back.',
              12000);
            // ★ Start periodic recovery polling so the result is auto-recovered later
            _startOfflineRecoveryPolling();
            return;
          }
          // If recovered, fall through and continue the poll loop
        } else {
          // Server is alive but poll failed (maybe task was cleaned up) — continue trying a bit more
          _consecutiveErrors = Math.floor(_MAX_CONSECUTIVE_ERRORS / 2); // partial reset
        }
      }
    }
    // ★ Item 8: Measure RTT for adaptive delay
    const _pollRtt = Date.now() - _pollStart;
    _rttEma = Math.round(_rttEma * 0.7 + _pollRtt * 0.3); // EMA with α=0.3
    _pollIter++;
    // ★ RTT-adaptive poll interval: when the tunnel is fast (RTT < 100ms),
    //   poll more aggressively (min 300ms sleep). When slow (RTT > 500ms),
    //   back off to avoid wasting bandwidth. After the initial burst (first
    //   4 polls), gradually ramp the base interval.
    //   Effective interval = sleep + RTT ≈ target responsiveness.
    const _baseDelay = _pollIter < 4 ? 300 : Math.min(300 + _pollIter * 100, 1500);
    // Scale by RTT: fast tunnel → shorter sleep; slow tunnel → longer sleep
    const _rttFactor = Math.max(0.5, Math.min(2.0, _rttEma / 200));
    const _pollDelay = Math.round(Math.min(_baseDelay * _rttFactor, 2000));
    await new Promise((r) => setTimeout(r, _pollDelay));
  }
  // Loop only exits via return (task done, abort, server dead, or 404) — no infinite hang.
}

function updateSendButton() {
  const btn = document.getElementById("sendBtn");
  const conv = getActiveConv();

  // ── Detect branch streaming: if in branch mode, check branch-specific stream ──
  let branchStreaming = false;
  let branchStreamKey = null;
  if (_activeBranch && conv) {
    const bk = _branchKey(conv.id, _activeBranch.msgIdx, _activeBranch.branchIdx);
    if (_branchStreams.has(bk)) {
      branchStreaming = true;
      branchStreamKey = bk;
    }
  }

  // Also detect any branch stream for this conversation (even if not in branch mode)
  let anyBranchStreaming = false;
  if (conv && !branchStreaming) {
    const prefix = conv.id + ":";
    for (const k of _branchStreams.keys()) {
      if (k.startsWith(prefix)) { anyBranchStreaming = true; break; }
    }
  }

  const mainStreaming =
    activeStreams.has(activeConvId) || (conv && conv.activeTaskId);
  const translating = conv && conv._translating;
  const streaming = branchStreaming || mainStreaming || anyBranchStreaming || translating;

  if (streaming) {
    const queueCount = (conv && pendingMessageQueue.has(conv.id)) ? pendingMessageQueue.get(conv.id).length : 0;
    btn.className = "send-btn stop-btn";
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`
      + (queueCount > 0 ? `<span class="queue-badge">${queueCount}</span>` : '');
    btn.onclick = () => {
      // ── Priority 0: stop translation ──
      if (translating && conv) {
        console.log(`[stopBtn] Aborting translation — conv=${conv.id.slice(0,8)}`);
        conv._translateAborted = true;
        conv._translating = false;
        // Abort the in-flight sync fetch if present
        if (conv._translateAbortCtrl) {
          conv._translateAbortCtrl.abort();
          conv._translateAbortCtrl = null;
        }
        updateSendButton();
        renderConversationList();
        return;
      }
      // ── Priority 1: stop active branch stream ──
      if (branchStreaming && branchStreamKey) {
        const bs = _branchStreams.get(branchStreamKey);
        if (bs) {
          // ★ Pre-set finishReason before abort kills the SSE reader
          const _bMsg = conv.messages[_activeBranch.msgIdx];
          const _bBranch = _bMsg?.branches?.[_activeBranch.branchIdx];
          if (_bBranch?.messages) {
            const _bLast = _bBranch.messages[_bBranch.messages.length - 1];
            if (_bLast?.role === 'assistant') _bLast.finishReason = 'aborted';
          }
          bs.controller.abort();
          fetch(apiUrl(`/api/chat/abort/${bs.taskId}`), { method: "POST" }).catch(() => {});
          // Clean up branch state
          const msg = conv.messages[_activeBranch.msgIdx];
          const branch = msg?.branches?.[_activeBranch.branchIdx];
          if (branch) branch.activeTaskId = null;
          _finishBranchStream(conv, _activeBranch.msgIdx, _activeBranch.branchIdx, branch, branchStreamKey);
        }
        return;
      }
      // ── Priority 2: stop any branch stream for this conv ──
      if (anyBranchStreaming && conv) {
        const prefix = conv.id + ":";
        for (const [k, bs] of _branchStreams.entries()) {
          if (k.startsWith(prefix)) {
            // ★ Pre-set finishReason before abort kills the SSE reader
            const _p2parts = k.split(":");
            const _p2mi = parseInt(_p2parts[1]);
            const _p2bi = parseInt(_p2parts[2]);
            const _p2msg = conv.messages[_p2mi];
            const _p2branch = _p2msg?.branches?.[_p2bi];
            if (_p2branch?.messages) {
              const _p2last = _p2branch.messages[_p2branch.messages.length - 1];
              if (_p2last?.role === 'assistant') _p2last.finishReason = 'aborted';
            }
            bs.controller.abort();
            fetch(apiUrl(`/api/chat/abort/${bs.taskId}`), { method: "POST" }).catch(() => {});
            // Parse key to get msgIdx, branchIdx
            const parts = k.split(":");
            const mi = parseInt(parts[1]);
            const bi = parseInt(parts[2]);
            const msg = conv.messages[mi];
            const branch = msg?.branches?.[bi];
            if (branch) branch.activeTaskId = null;
            _finishBranchStream(conv, mi, bi, branch, k);
          }
        }
        return;
      }
      // ── Priority 3: stop main stream ──
      const s = activeStreams.get(activeConvId);
      if (s) {
        console.log(`[stopBtn] Aborting main stream — conv=${activeConvId.slice(0,8)} task=${s.taskId?.slice(0,8)}`);
        // ★ Pre-set finishReason before abort kills the SSE reader
        if (conv) {
          const _stopMsg = conv.messages[conv.messages.length - 1];
          if (_stopMsg && _stopMsg.role === 'assistant') {
            _stopMsg.finishReason = 'aborted';
          }
        }
        s.controller.abort();
        fetch(apiUrl(`/api/chat/abort/${s.taskId}`), { method: "POST" }).catch(
          () => {},
        );
      } else if (conv && conv.activeTaskId) {
        fetch(apiUrl(`/api/chat/abort/${conv.activeTaskId}`), {
          method: "POST",
        }).catch(() => {});
        // ★ Pre-set finishReason for the no-stream abort path too
        const _noStreamMsg = conv.messages[conv.messages.length - 1];
        if (_noStreamMsg && _noStreamMsg.role === 'assistant') {
          _noStreamMsg.finishReason = 'aborted';
        }
        conv.activeTaskId = null;
        conv._activeTaskClearedAt = Date.now();
        finishStream(activeConvId);
      }
    };
  } else {
    btn.className = "send-btn";
    btn.innerHTML = `<span style="font-size:13px;font-weight:600;letter-spacing:.5px">⏎</span>`;
    btn.onclick = sendMessage;
  }
}
