/* ═══════════════════════════════════════════
   skills.js — Skills (Accumulated Experience)
   ═══════════════════════════════════════════ */
// ══════════════════════════════════════════════════════
// ★ Skills (Accumulated Experience) — v2 Redesigned
// ══════════════════════════════════════════════════════
let _skillsCache = [];          // cache for filter/search
let _skillsFilter = "";         // current search query

function toggleSkills() {
  if (!skillsEnabled) { openSkillsModal(); return; }
  _applySkillsUI(false);
  _saveConvToolState();
  debugLog("Skills applied: OFF (AI still accumulates in background)", "success");
}
function toggleSkillsFromModal() {
  _applySkillsUI(!skillsEnabled);
  _saveConvToolState();
  updateSubmenuCounts();
  debugLog(`Skills applied: ${skillsEnabled ? "ON — existing skills injected into context" : "OFF — AI still accumulates in background"}`, "success");
  closeSkillsModal();
}
function openSkillsModal() {
  console.log("[Skills] Opening modal...");
  document.getElementById("skillsModal").classList.add("open");
  _skillsFilter = "";
  const search = document.getElementById("skillsSearchInput");
  if (search) search.value = "";
  refreshSkillsList();
  _updateSkillsModalBtn();
}
function closeSkillsModal() {
  document.getElementById("skillsModal").classList.remove("open");
  const addSec = document.getElementById("skillsAddSection");
  if (addSec) addSec.style.display = "none";
}
function _updateSkillsModalBtn() {
  const btn = document.getElementById("skillsModalToggleBtn");
  if (!btn) return;
  if (skillsEnabled) {
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 11-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg> 停用 Skills';
    btn.className = "skills-action-btn skills-btn-off";
  } else {
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/><path d="M12 5v14"/></svg> 启用 Skills';
    btn.className = "skills-action-btn skills-btn-on";
  }
}
function toggleSkillsAddForm() {
  const s = document.getElementById("skillsAddSection");
  if (!s) return;
  const isHidden = s.style.display === "none" || !s.style.display;
  s.style.display = isHidden ? "block" : "none";
  if (isHidden) s.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function switchSkillsTab(scope) {
  document.querySelectorAll(".skills-tab").forEach(t => t.classList.toggle("active", t.dataset.scope === scope));
  refreshSkillsList(scope);
}
function filterSkillsList(query) {
  _skillsFilter = (query || "").toLowerCase().trim();
  _renderSkillCards(_skillsCache);
}

async function refreshSkillsList(scope) {
  const list = document.getElementById("skillsList");
  if (!list) { console.error("[Skills] #skillsList not found!"); return; }
  const activeTab = document.querySelector(".skills-tab.active");
  scope = scope || activeTab?.dataset?.scope || "all";

  // Show loading skeleton
  list.innerHTML = '<div class="skills-loading"><div class="skills-loading-dot"></div><div class="skills-loading-dot"></div><div class="skills-loading-dot"></div><span>加载中...</span></div>';

  try {
    const url = apiUrl(`/api/skills?scope=${scope}`);
    console.log("[Skills] Fetching:", url);
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
    const d = await r.json();
    console.log("[Skills] Got", (d.skills || []).length, "skills");
    _skillsCache = d.skills || [];
    _updateSkillsStats(_skillsCache);
    _renderSkillCards(_skillsCache);
  } catch (e) {
    console.error("[Skills] Fetch error:", e);
    list.innerHTML = `<div class="skills-empty">
      <span class="skills-empty-icon"></span>
      <div class="skills-empty-title">加载失败</div>
      <div style="margin-top:4px;font-size:12px;opacity:.7">${_esc(e.message)}</div>
      <button class="skills-retry-btn" onclick="refreshSkillsList()">重试</button>
    </div>`;
  }
}

function _renderSkillCards(skills) {
  const list = document.getElementById("skillsList");
  if (!list) return;

  // Apply filter
  let filtered = skills;
  if (_skillsFilter) {
    filtered = skills.filter(sk =>
      (sk.name || "").toLowerCase().includes(_skillsFilter) ||
      (sk.description || "").toLowerCase().includes(_skillsFilter) ||
      (sk.tags || []).some(t => t.toLowerCase().includes(_skillsFilter))
    );
  }

  if (!filtered.length) {
    if (_skillsFilter && skills.length) {
      list.innerHTML = `<div class="skills-empty">
        <span class="skills-empty-icon"></span>
        <div class="skills-empty-title">没有匹配「${_esc(_skillsFilter)}」的技能</div>
        <div style="margin-top:4px;font-size:12px;opacity:.7">共 ${skills.length} 个技能</div>
      </div>`;
    } else {
      list.innerHTML = `<div class="skills-empty">
        <span class="skills-empty-icon"></span>
        <div class="skills-empty-title">还没有积累任何技能</div>
        <div style="margin-top:6px;font-size:12px;opacity:.7;line-height:1.7">
          AI 在对话中发现有用模式时会自动保存技能<br>你也可以点击下方「+ 新建」手动添加
        </div>
      </div>`;
    }
    return;
  }

  // Build cards using DOM for safety
  const frag = document.createDocumentFragment();
  filtered.forEach(sk => {
    try {
      const card = _buildSkillCardEl(sk);
      frag.appendChild(card);
    } catch (e) {
      console.error("[Skills] Render error for", sk.name, e);
      const errDiv = document.createElement("div");
      errDiv.className = "skill-card skill-card-error";
      errDiv.textContent = `渲染失败: ${sk.name}`;
      frag.appendChild(errDiv);
    }
  });
  list.innerHTML = "";
  list.appendChild(frag);
}

function _buildSkillCardEl(sk) {
  const card = document.createElement("div");
  card.className = "skill-card" + (sk.enabled ? "" : " is-disabled");
  card.dataset.id = sk.id;

  // Header row: expand icon + name + scope + toggle + delete
  const header = document.createElement("div");
  header.className = "skill-card-header";
  header.onclick = function() { toggleSkillBody(this); };

  const expandIcon = document.createElement("span");
  expandIcon.className = "skill-card-expand-icon";
  expandIcon.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M3 1l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  header.appendChild(expandIcon);

  const nameEl = document.createElement("span");
  nameEl.className = "skill-card-name";
  nameEl.textContent = sk.name;
  header.appendChild(nameEl);

  const scopeBadge = document.createElement("span");
  scopeBadge.className = "skill-card-scope " + sk.scope;
  scopeBadge.textContent = sk.scope === "global" ? "全局" : "项目";
  header.appendChild(scopeBadge);

  // Actions inline in header
  const actions = document.createElement("div");
  actions.className = "skill-card-actions";
  actions.onclick = function(e) { e.stopPropagation(); };

  // Toggle switch (iOS-style pill)
  const toggle = document.createElement("span");
  toggle.className = "skill-toggle-switch" + (sk.enabled ? " on" : "");
  toggle.title = sk.enabled ? "已启用 — 点击禁用" : "已禁用 — 点击启用";
  const track = document.createElement("span");
  track.className = "skill-toggle-track";
  const thumb = document.createElement("span");
  thumb.className = "skill-toggle-thumb";
  track.appendChild(thumb);
  toggle.appendChild(track);
  toggle.onclick = function(e) { e.stopPropagation(); toggleSkillEnabled(sk.id); };
  actions.appendChild(toggle);

  // Delete button
  const delBtn = document.createElement("button");
  delBtn.className = "skill-delete-btn";
  delBtn.title = "删除此技能";
  delBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  delBtn.onclick = function(e) { e.stopPropagation(); deleteSkill(sk.id); };
  actions.appendChild(delBtn);

  header.appendChild(actions);
  card.appendChild(header);

  // Description
  if (sk.description) {
    const desc = document.createElement("div");
    desc.className = "skill-card-desc";
    desc.textContent = sk.description;
    card.appendChild(desc);
  }

  // Tags
  if (sk.tags && sk.tags.length) {
    const tagsRow = document.createElement("div");
    tagsRow.className = "skill-card-tags";
    sk.tags.forEach(t => {
      const tag = document.createElement("span");
      tag.className = "skill-card-tag";
      tag.textContent = t;
      tagsRow.appendChild(tag);
    });
    card.appendChild(tagsRow);
  }

  // Body (collapsed by default)
  const body = document.createElement("div");
  body.className = "skill-card-body";
  const bodyInner = document.createElement("div");
  bodyInner.className = "skill-card-body-inner";
  bodyInner.innerHTML = _renderSkillBody(sk.body || "(empty)");
  body.appendChild(bodyInner);
  card.appendChild(body);

  return card;
}

function _updateSkillsStats(skills) {
  const el = document.getElementById("skillsStats");
  if (!el) return;
  const total = skills.length;
  const enabled = skills.filter(s => s.enabled).length;
  const project = skills.filter(s => s.scope === "project").length;
  const global = skills.filter(s => s.scope === "global").length;
  el.innerHTML = `
    <div class="skills-stat"><span class="skills-stat-num">${total}</span><span class="skills-stat-label">总计</span></div>
    <div class="skills-stat"><span class="skills-stat-num skills-stat-active">${enabled}</span><span class="skills-stat-label">启用</span></div>
    <div class="skills-stat"><span class="skills-stat-num">${project}</span><span class="skills-stat-label">项目</span></div>
    <div class="skills-stat"><span class="skills-stat-num">${global}</span><span class="skills-stat-label">全局</span></div>`;
}

function _esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
function _renderSkillBody(md) {
  if (typeof marked !== "undefined") {
    try { return marked.parse(md); } catch (e) { console.warn("[Skills] marked.parse error:", e); }
  }
  return "<pre>" + _esc(md) + "</pre>";
}

function toggleSkillBody(headerEl) {
  const card = headerEl.closest(".skill-card");
  if (!card) return;
  const body = card.querySelector(".skill-card-body");
  const icon = headerEl.querySelector(".skill-card-expand-icon");
  const isOpen = body.classList.toggle("open");
  if (icon) icon.classList.toggle("expanded", isOpen);
}
async function toggleSkillEnabled(id) {
  try {
    const r = await fetch(apiUrl(`/api/skills/${id}/toggle`), { method: "POST" });
    if (r.ok) refreshSkillsList();
  } catch (e) { debugLog("Toggle skill failed: " + e.message, "error"); }
}
async function deleteSkill(id) {
  if (!confirm("确定要删除这个 Skill 吗？")) return;
  try {
    const r = await fetch(apiUrl(`/api/skills/${id}`), { method: "DELETE" });
    if (r.ok) refreshSkillsList();
  } catch (e) { debugLog("Delete skill failed: " + e.message, "error"); }
}
async function createSkillFromModal() {
  const name = document.getElementById("skillNewName").value.trim();
  const desc = document.getElementById("skillNewDesc").value.trim();
  const body = document.getElementById("skillNewBody").value.trim();
  const scope = document.getElementById("skillNewScope").value;
  const tags = document.getElementById("skillNewTags").value.split(",").map(t => t.trim()).filter(Boolean);
  const status = document.getElementById("skillsModalStatus");
  if (!name || !body) { status.textContent = "名称和内容为必填项"; return; }
  try {
    const r = await fetch(apiUrl("/api/skills"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: desc, body, scope, tags }),
    });
    if (r.ok) {
      ["skillNewName","skillNewDesc","skillNewBody","skillNewTags"].forEach(id => { document.getElementById(id).value = ""; });
      document.getElementById("skillsAddSection").style.display = "none";
      status.textContent = "";
      refreshSkillsList();
      debugLog(`Skill created: ${name}`, "success");
    } else {
      const e = await r.json();
      status.textContent = e.error || "Failed";
    }
  } catch (e) { status.textContent = "Error: " + e.message; }
}
