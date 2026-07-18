/* ═══════════════════════════════════════════
   memory.js — Memory (Accumulated Experience)
   ═══════════════════════════════════════════ */
// ══════════════════════════════════════════════════════
// ★ Memory (Accumulated Experience) — v2 Redesigned
// ══════════════════════════════════════════════════════
let _memoryCache = [];          // cache for filter/search
let _memoryFilter = "";         // current search query

function toggleMemory() {
  if (!memoryEnabled) { openMemoryModal(); return; }
  _applyMemoryUI(false);
  _saveConvToolState();
  debugLog("Memory applied: OFF (AI still accumulates in background)", "success");
}
function toggleMemoryFromModal() {
  _applyMemoryUI(!memoryEnabled);
  _saveConvToolState();
  updateSubmenuCounts();
  debugLog(`Memory applied: ${memoryEnabled ? "ON — existing memories injected into context" : "OFF — AI still accumulates in background"}`, "success");
  closeMemoryModal();
}
function openMemoryModal() {
  console.log("[Memory] Opening modal...");
  const overlay = document.getElementById("memoryModal");
  overlay.classList.add("open");
  _memoryFilter = "";
  const search = document.getElementById("memorySearchInput");
  if (search) search.value = "";
  refreshMemoryList();
  _updateMemoryModalBtn();
  _attachMemoryDropZone();
}
function closeMemoryModal() {
  document.getElementById("memoryModal").classList.remove("open");
  const addSec = document.getElementById("memoryAddSection");
  if (addSec) addSec.style.display = "none";
}
function _updateMemoryModalBtn() {
  const btn = document.getElementById("memoryModalToggleBtn");
  if (!btn) return;
  if (memoryEnabled) {
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 11-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg> ' + _esc(t('memory.disable'));
    btn.className = "memory-action-btn memory-btn-off";
  } else {
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/><path d="M12 5v14"/></svg> ' + _esc(t('memory.enable'));
    btn.className = "memory-action-btn memory-btn-on";
  }
}
function toggleMemoryAddForm() {
  const s = document.getElementById("memoryAddSection");
  if (!s) return;
  const isHidden = s.style.display === "none" || !s.style.display;
  s.style.display = isHidden ? "block" : "none";
  if (isHidden) s.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
// Open the Memory modal with the manual-create form already expanded.
// Used by the "新建记忆" entry point in the Settings → Skills tab so users
// can find the manual-add flow without hunting for the toolbar button.
function openMemoryCreateForm() {
  openMemoryModal();
  const s = document.getElementById("memoryAddSection");
  if (s) {
    s.style.display = "block";
    setTimeout(() => s.scrollIntoView({ behavior: "smooth", block: "nearest" }), 50);
  }
  const name = document.getElementById("memoryNewName");
  if (name) setTimeout(() => name.focus(), 60);
}
function switchMemoryTab(scope) {
  document.querySelectorAll(".memory-tab").forEach(t => t.classList.toggle("active", t.dataset.scope === scope));
  refreshMemoryList(scope);
}
function filterMemoryList(query) {
  _memoryFilter = (query || "").toLowerCase().trim();
  _renderMemoryCards(_memoryCache);
}

// _memoryListTargetId lets the same render path drive either the standalone
// modal (#memoryList, default) or the embedded Settings → Memory & Preferences
// section (#prefsMemoryList). It's set for the duration of one refresh so the
// downstream _renderMemoryCards / empty-state writers target the right node.
let _memoryListTargetId = "memoryList";
function _memoryListEl() { return document.getElementById(_memoryListTargetId); }

async function refreshMemoryList(scope, targetId) {
  _memoryListTargetId = targetId || "memoryList";
  const list = _memoryListEl();
  if (!list) { console.error(`[Memory] #${_memoryListTargetId} not found!`); return; }
  const activeTab = document.querySelector(".memory-tab.active");
  scope = scope || activeTab?.dataset?.scope || "all";

  // Show loading skeleton
  list.innerHTML = '<div class="memory-loading"><div class="memory-loading-dot"></div><div class="memory-loading-dot"></div><div class="memory-loading-dot"></div><span>' + _esc(t('memory.loading')) + '</span></div>';

  try {
    const d = await Api.memory.list(scope);
    if (!d) throw new Error("empty response");
    console.log("[Memory] Got", (d.memories || d.skills || []).length, "memories");
    _memoryCache = d.memories || d.skills || [];
    _updateMemoryStats(_memoryCache);
    _renderMemoryCards(_memoryCache);
  } catch (e) {
    console.error("[Memory] Fetch error:", e);
    list.innerHTML = `<div class="memory-empty">
      <span class="memory-empty-icon"></span>
      <div class="memory-empty-title">${_esc(t('memory.loadFailed'))}</div>
      <div style="margin-top:4px;font-size:12px;opacity:.7">${_esc(e.message)}</div>
      <button class="memory-retry-btn" onclick="refreshMemoryList()">${_esc(t('memory.retry'))}</button>
    </div>`;
  }
}

function _renderMemoryCards(memories) {
  const list = _memoryListEl ? _memoryListEl() : document.getElementById("memoryList");
  if (!list) return;

  // Apply filter
  let filtered = memories;
  if (_memoryFilter) {
    filtered = memories.filter(sk =>
      (sk.name || "").toLowerCase().includes(_memoryFilter) ||
      (sk.description || "").toLowerCase().includes(_memoryFilter) ||
      (sk.tags || []).some(t => t.toLowerCase().includes(_memoryFilter))
    );
  }

  if (!filtered.length) {
    if (_memoryFilter && memories.length) {
      list.innerHTML = `<div class="memory-empty">
        <span class="memory-empty-icon"></span>
        <div class="memory-empty-title">${_esc(t('memory.noMatch', { q: _memoryFilter }))}</div>
        <div style="margin-top:4px;font-size:12px;opacity:.7">${_esc(t('memory.matchCount', { n: memories.length }))}</div>
      </div>`;
    } else {
      list.innerHTML = `<div class="memory-empty">
        <span class="memory-empty-icon"></span>
        <div class="memory-empty-title">${_esc(t('memory.emptyTitle'))}</div>
        <div style="margin-top:6px;font-size:12px;opacity:.7;line-height:1.7">
          ${t('memory.emptyHint')}
        </div>
      </div>`;
    }
    return;
  }

  // Build cards using DOM for safety
  const frag = document.createDocumentFragment();
  filtered.forEach(sk => {
    try {
      const card = _buildMemoryCardEl(sk);
      frag.appendChild(card);
    } catch (e) {
      console.error("[Memory] Render error for", sk.name, e);
      const errDiv = document.createElement("div");
      errDiv.className = "memory-card memory-card-error";
      errDiv.textContent = t('memory.renderFailed', { name: sk.name });
      frag.appendChild(errDiv);
    }
  });
  list.innerHTML = "";
  list.appendChild(frag);
}

function _buildMemoryCardEl(sk) {
  const card = document.createElement("div");
  card.className = "memory-card" + (sk.enabled ? "" : " is-disabled");
  card.dataset.id = sk.id;

  // Header row: expand icon + name + scope + toggle + delete
  const header = document.createElement("div");
  header.className = "memory-card-header";
  header.onclick = function() { toggleMemoryBody(this); };

  const expandIcon = document.createElement("span");
  expandIcon.className = "memory-card-expand-icon";
  expandIcon.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M3 1l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  header.appendChild(expandIcon);

  const nameEl = document.createElement("span");
  nameEl.className = "memory-card-name";
  nameEl.textContent = sk.name;
  header.appendChild(nameEl);

  const scopeBadge = document.createElement("span");
  scopeBadge.className = "memory-card-scope " + sk.scope;
  scopeBadge.textContent = sk.scope === "global" ? t('memory.scopeGlobal') : t('memory.scopeProject');
  header.appendChild(scopeBadge);

  // Package badge (SKILL.md directory package)
  if (sk.is_package) {
    const pkg = document.createElement("span");
    pkg.className = "memory-card-pkg";
    pkg.title = sk.package_dir || "Skill package";
    pkg.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>SKILL';
    header.appendChild(pkg);
  }

  // Actions inline in header
  const actions = document.createElement("div");
  actions.className = "memory-card-actions";
  actions.onclick = function(e) { e.stopPropagation(); };

  // Toggle switch (iOS-style pill)
  const toggle = document.createElement("span");
  toggle.className = "memory-toggle-switch" + (sk.enabled ? " on" : "");
  toggle.title = sk.enabled ? t('memory.toggleOnTip') : t('memory.toggleOffTip');
  const track = document.createElement("span");
  track.className = "memory-toggle-track";
  const thumb = document.createElement("span");
  thumb.className = "memory-toggle-thumb";
  track.appendChild(thumb);
  toggle.appendChild(track);
  toggle.onclick = function(e) { e.stopPropagation(); toggleMemoryEnabled(sk.id); };
  actions.appendChild(toggle);

  // Delete button
  const delBtn = document.createElement("button");
  delBtn.className = "memory-delete-btn";
  delBtn.title = t('memory.deleteTip');
  delBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  delBtn.onclick = function(e) { e.stopPropagation(); deleteMemory(sk.id); };
  actions.appendChild(delBtn);

  header.appendChild(actions);
  card.appendChild(header);

  // Description
  if (sk.description) {
    const desc = document.createElement("div");
    desc.className = "memory-card-desc";
    desc.textContent = sk.description;
    card.appendChild(desc);
  }

  // Tags
  if (sk.tags && sk.tags.length) {
    const tagsRow = document.createElement("div");
    tagsRow.className = "memory-card-tags";
    sk.tags.forEach(t => {
      const tag = document.createElement("span");
      tag.className = "memory-card-tag";
      tag.textContent = t;
      tagsRow.appendChild(tag);
    });
    card.appendChild(tagsRow);
  }

  // Body (collapsed by default). Markdown is rendered lazily on first
  // expand — parsing every card's body up front made the list build slow
  // and the expand toggle janky.
  const body = document.createElement("div");
  body.className = "memory-card-body";
  const bodyInner = document.createElement("div");
  bodyInner.className = "memory-card-body-inner";
  bodyInner.dataset.raw = sk.body || "(empty)";
  body.appendChild(bodyInner);
  card.appendChild(body);

  return card;
}

function _updateMemoryStats(memories) {
  const el = document.getElementById("memoryStats");
  if (!el) return;
  const total = memories.length;
  const enabled = memories.filter(s => s.enabled).length;
  const project = memories.filter(s => s.scope === "project").length;
  const global = memories.filter(s => s.scope === "global").length;
  el.innerHTML = `
    <div class="memory-stat"><span class="memory-stat-num">${total}</span><span class="memory-stat-label">${_esc(t('memory.statTotal'))}</span></div>
    <div class="memory-stat"><span class="memory-stat-num memory-stat-active">${enabled}</span><span class="memory-stat-label">${_esc(t('memory.statEnabled'))}</span></div>
    <div class="memory-stat"><span class="memory-stat-num">${project}</span><span class="memory-stat-label">${_esc(t('memory.statProject'))}</span></div>
    <div class="memory-stat"><span class="memory-stat-num">${global}</span><span class="memory-stat-label">${_esc(t('memory.statGlobal'))}</span></div>`;
}

function _esc(s) {
  return escapeHtml(s);
}
function _renderMemoryBody(md) {
  if (typeof marked !== "undefined") {
    try { return marked.parse(md); } catch (e) { console.warn("[Memory] marked.parse error:", e); }
  }
  return "<pre>" + _esc(md) + "</pre>";
}

function toggleMemoryBody(headerEl) {
  const card = headerEl.closest(".memory-card");
  if (!card) return;
  const body = card.querySelector(".memory-card-body");
  const icon = headerEl.querySelector(".memory-card-expand-icon");
  const isOpen = body.classList.toggle("open");
  if (icon) icon.classList.toggle("expanded", isOpen);
  // Lazy-render markdown the first time this card is expanded.
  if (isOpen) {
    const inner = body.querySelector(".memory-card-body-inner");
    if (inner && inner.dataset.raw != null) {
      inner.innerHTML = _renderMemoryBody(inner.dataset.raw);
      delete inner.dataset.raw;
    }
  }
}
async function toggleMemoryEnabled(id) {
  // Optimistic in-place update — no full reload
  const card = document.querySelector(`.memory-card[data-id="${id}"]`);
  const toggle = card?.querySelector('.memory-toggle-switch');
  const cacheItem = _memoryCache.find(m => m.id === id);
  const newEnabled = cacheItem ? !cacheItem.enabled : true;

  // Instant UI feedback
  if (card) card.classList.toggle('is-disabled', !newEnabled);
  if (toggle) {
    toggle.classList.toggle('on', newEnabled);
    toggle.title = newEnabled ? t('memory.toggleOnTip') : t('memory.toggleOffTip');
  }
  if (cacheItem) cacheItem.enabled = newEnabled;
  _updateMemoryStats(_memoryCache);

  try {
    const r = await Api.memory.toggle(id);
    if (!r || !r.ok) throw new Error(`HTTP ${r ? r.status : 'no response'}`);
    const updated = await r.json();
    // Sync server truth back to cache
    if (cacheItem) Object.assign(cacheItem, updated);
  } catch (e) {
    // Rollback on failure
    debugLog("Toggle memory failed: " + e.message, "error");
    if (card) card.classList.toggle('is-disabled', newEnabled);
    if (toggle) {
      toggle.classList.toggle('on', !newEnabled);
      toggle.title = !newEnabled ? t('memory.toggleOnTip') : t('memory.toggleOffTip');
    }
    if (cacheItem) cacheItem.enabled = !newEnabled;
    _updateMemoryStats(_memoryCache);
  }
}
async function deleteMemory(id) {
  if (!await showConfirm(t('memory.deleteConfirm'), { danger: true })) return;
  const card = document.querySelector(`.memory-card[data-id="${id}"]`);
  // Animate out immediately
  if (card) {
    card.style.transition = 'opacity .2s, transform .2s';
    card.style.opacity = '0';
    card.style.transform = 'scale(.96)';
  }
  try {
    const r = await Api.memory.remove(id);
    if (!r || !r.ok) throw new Error(`HTTP ${r ? r.status : 'no response'}`);
    // Remove from cache and DOM
    _memoryCache = _memoryCache.filter(m => m.id !== id);
    if (card) card.remove();
    _updateMemoryStats(_memoryCache);
    // Show empty state if no cards left
    const list = document.getElementById("memoryList");
    if (list && !list.children.length) _renderMemoryCards(_memoryCache);
    debugLog("Memory deleted", "success");
  } catch (e) {
    debugLog("Delete memory failed: " + e.message, "error");
    // Rollback visibility
    if (card) { card.style.opacity = '1'; card.style.transform = ''; }
  }
}
async function createMemoryFromModal() {
  const name = document.getElementById("memoryNewName").value.trim();
  const desc = document.getElementById("memoryNewDesc").value.trim();
  const body = document.getElementById("memoryNewBody").value.trim();
  const scope = document.getElementById("memoryNewScope").value;
  const tags = document.getElementById("memoryNewTags").value.split(",").map(t => t.trim()).filter(Boolean);
  const status = document.getElementById("memoryModalStatus");
  if (!name || !body) { status.textContent = t('memory.nameBodyRequired'); return; }
  try {
    const r = await Api.memory.create({ name, description: desc, body, scope, tags });
    if (r && r.ok) {
      const newMem = await r.json();
      ["memoryNewName","memoryNewDesc","memoryNewBody","memoryNewTags"].forEach(id => { document.getElementById(id).value = ""; });
      document.getElementById("memoryAddSection").style.display = "none";
      status.textContent = "";
      // Insert new card in-place instead of full reload
      const mem = newMem.memory || newMem;
      _memoryCache.unshift(mem);
      const list = document.getElementById("memoryList");
      if (list) {
        // Clear empty-state if present
        if (list.querySelector('.memory-empty')) list.innerHTML = '';
        const card = _buildMemoryCardEl(mem);
        card.style.animation = 'memorySlam .3s cubic-bezier(.2,1,.3,1)';
        list.prepend(card);
      }
      _updateMemoryStats(_memoryCache);
      debugLog(`Memory created: ${name}`, "success");
    } else {
      const e = await r.json();
      status.textContent = e.error || t('memory.createFailed');
    }
  } catch (e) { status.textContent = t('memory.errorPrefix', { err: e.message }); }
}