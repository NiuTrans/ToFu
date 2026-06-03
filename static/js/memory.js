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
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 11-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg> 停用 Memory';
    btn.className = "memory-action-btn memory-btn-off";
  } else {
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/><path d="M12 5v14"/></svg> 启用 Memory';
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
function switchMemoryTab(scope) {
  document.querySelectorAll(".memory-tab").forEach(t => t.classList.toggle("active", t.dataset.scope === scope));
  refreshMemoryList(scope);
}
function filterMemoryList(query) {
  _memoryFilter = (query || "").toLowerCase().trim();
  _renderMemoryCards(_memoryCache);
}

async function refreshMemoryList(scope) {
  const list = document.getElementById("memoryList");
  if (!list) { console.error("[Memory] #memoryList not found!"); return; }
  const activeTab = document.querySelector(".memory-tab.active");
  scope = scope || activeTab?.dataset?.scope || "all";

  // Show loading skeleton
  list.innerHTML = '<div class="memory-loading"><div class="memory-loading-dot"></div><div class="memory-loading-dot"></div><div class="memory-loading-dot"></div><span>加载中...</span></div>';

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
      <div class="memory-empty-title">加载失败</div>
      <div style="margin-top:4px;font-size:12px;opacity:.7">${_esc(e.message)}</div>
      <button class="memory-retry-btn" onclick="refreshMemoryList()">重试</button>
    </div>`;
  }
}

function _renderMemoryCards(memories) {
  const list = document.getElementById("memoryList");
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
        <div class="memory-empty-title">没有匹配「${_esc(_memoryFilter)}」的记忆</div>
        <div style="margin-top:4px;font-size:12px;opacity:.7">共 ${memories.length} 条记忆</div>
      </div>`;
    } else {
      list.innerHTML = `<div class="memory-empty">
        <span class="memory-empty-icon"></span>
        <div class="memory-empty-title">还没有积累任何记忆</div>
        <div style="margin-top:6px;font-size:12px;opacity:.7;line-height:1.7">
          AI 在对话中发现有用模式时会自动保存记忆<br>你也可以点击下方「+ 新建」手动添加
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
      errDiv.textContent = `渲染失败: ${sk.name}`;
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
  scopeBadge.textContent = sk.scope === "global" ? "全局" : "项目";
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
  toggle.title = sk.enabled ? "已启用 — 点击禁用" : "已禁用 — 点击启用";
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
  delBtn.title = "删除此记忆";
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

  // Body (collapsed by default)
  const body = document.createElement("div");
  body.className = "memory-card-body";
  const bodyInner = document.createElement("div");
  bodyInner.className = "memory-card-body-inner";
  bodyInner.innerHTML = _renderMemoryBody(sk.body || "(empty)");
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
    <div class="memory-stat"><span class="memory-stat-num">${total}</span><span class="memory-stat-label">总计</span></div>
    <div class="memory-stat"><span class="memory-stat-num memory-stat-active">${enabled}</span><span class="memory-stat-label">启用</span></div>
    <div class="memory-stat"><span class="memory-stat-num">${project}</span><span class="memory-stat-label">项目</span></div>
    <div class="memory-stat"><span class="memory-stat-num">${global}</span><span class="memory-stat-label">全局</span></div>`;
}

function _esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
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
    toggle.title = newEnabled ? '已启用 — 点击禁用' : '已禁用 — 点击启用';
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
      toggle.title = !newEnabled ? '已启用 — 点击禁用' : '已禁用 — 点击启用';
    }
    if (cacheItem) cacheItem.enabled = !newEnabled;
    _updateMemoryStats(_memoryCache);
  }
}
async function deleteMemory(id) {
  if (!confirm("确定要删除这条 Memory 吗？")) return;
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
  if (!name || !body) { status.textContent = "名称和内容为必填项"; return; }
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
      status.textContent = e.error || "Failed";
    }
  } catch (e) { status.textContent = "Error: " + e.message; }
}

// ══════════════════════════════════════════════════════
// ★ Skill-Package Install — drag-and-drop .zip into modal
// ══════════════════════════════════════════════════════

let _memoryDropAttached = false;
let _memoryDragDepth = 0;  // counter for nested dragenter/dragleave

function _attachMemoryDropZone() {
  if (_memoryDropAttached) return;
  _memoryDropAttached = true;

  const modal = document.getElementById("memoryModal");
  const card  = modal ? modal.querySelector(".memory-modal") : null;
  if (!modal || !card) return;

  // Only react to OS-drag-of-files (dataTransfer.types contains "Files").
  // Avoid swallowing intra-app drags of our own cards.
  const hasFiles = (e) => {
    const dt = e.dataTransfer;
    if (!dt || !dt.types) return false;
    for (let i = 0; i < dt.types.length; i++) {
      if (dt.types[i] === "Files") return true;
    }
    return false;
  };

  modal.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    _memoryDragDepth++;
    card.classList.add("is-dragging");
  });
  modal.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  modal.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    _memoryDragDepth = Math.max(0, _memoryDragDepth - 1);
    if (_memoryDragDepth === 0) card.classList.remove("is-dragging");
  });
  modal.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    _memoryDragDepth = 0;
    card.classList.remove("is-dragging");
    const files = e.dataTransfer.files;
    if (!files || !files.length) return;
    // Install first zip we find.
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      if (f && (/\.zip$/i.test(f.name) || f.type === "application/zip" ||
                f.type === "application/x-zip-compressed")) {
        _uploadSkillPackage(f);
        return;
      }
    }
    _showInstallToast("拖入的文件不是 .zip 技能包", true);
  });
}

function installSkillFromFileInput(inputEl) {
  const f = inputEl && inputEl.files && inputEl.files[0];
  if (!f) return;
  _uploadSkillPackage(f);
  inputEl.value = "";  // allow re-selecting the same file
}

async function _uploadSkillPackage(file) {
  const activeTab = document.querySelector(".memory-tab.active");
  const tabScope = activeTab?.dataset?.scope;
  const scope = (tabScope === "global") ? "global" : "project";

  _showInstallToast(`正在安装 ${file.name} …`);
  const fd = new FormData();
  fd.append("file", file);
  fd.append("scope", scope);

  try {
    const r = await Api.memory.install(fd);
    const d = (r ? await r.json().catch(() => ({})) : {});
    if (!r || !r.ok) {
      _showInstallToast(`安装失败: ${d.error || (r && r.statusText) || 'no response'}`, true);
      debugLog("Skill install failed: " + (d.error || (r && r.statusText) || 'no response'), "error");
      return;
    }

    const mem = d.memory || {};
    const hints = d.install_hints || [];
    let msg = `已安装技能包 "${mem.name}" (scope=${mem.scope})`;
    if (d.replaced) msg += " · 已覆盖旧版";
    if (hints.length) {
      const files = hints.map(h => h.file).join(", ");
      msg += ` · 发现安装脚本 ${files}（出于安全未自动执行）`;
    }
    _showInstallToast(msg);
    debugLog(msg, "success");

    // Insert new card on top, no full refresh.
    _memoryCache.unshift(mem);
    const list = document.getElementById("memoryList");
    if (list) {
      if (list.querySelector('.memory-empty')) list.innerHTML = '';
      const cardEl = _buildMemoryCardEl(mem);
      cardEl.style.animation = 'memorySlam .3s cubic-bezier(.2,1,.3,1)';
      list.prepend(cardEl);
    }
    _updateMemoryStats(_memoryCache);
  } catch (e) {
    _showInstallToast("安装异常: " + e.message, true);
    debugLog("Skill install error: " + e.message, "error");
  }
}

function _showInstallToast(text, isError) {
  const modal = document.getElementById("memoryModal");
  const card = modal ? modal.querySelector(".memory-modal") : null;
  if (!card) return;
  // Remove existing toast
  const existing = card.querySelector(".memory-install-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");
  toast.className = "memory-install-toast" + (isError ? " is-error" : "");
  toast.textContent = text;
  card.appendChild(toast);
  setTimeout(() => {
    toast.style.transition = "opacity .3s";
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 300);
  }, isError ? 5000 : 3500);
}
