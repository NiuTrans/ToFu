/* ═══════════════════════════════════════════════════════════
   skills.js — Skills tab in Settings (App-Store style)
   Mirrors mcp-tab patterns from settings.js.
   ═══════════════════════════════════════════════════════════ */

var _skillsCatalog = [];          // entries from /api/v1/memory/catalog
var _skillsInstalled = [];        // package memories from /api/v1/memory
var _skillsScope = 'catalog';     // 'catalog' | 'installed'
var _skillsActiveCategory = 'all';
var _skillsSearchQuery = '';
var _skillsDropAttached = false;
var _skillsDragDepth = 0;
var _skillsPage = 1;              // 1-based page index
var _SKILLS_PAGE_SIZE = 12;       // cards per page (grid-friendly)

// ── Population (called from openSettings → _populateSkillsTab) ──
async function _populateSkillsTab() {
  try {
    var [cdata, ldata] = await Promise.all([
      Api.memory.catalog(),
      Api.memory.list('all'),
    ]);
    _skillsCatalog = (cdata && cdata.catalog) || [];
    var all = (ldata && (ldata.memories || ldata.skills)) || [];
    _skillsInstalled = all.filter(function (m) { return m.is_package; });
    _skillsRender();
    _skillsAttachDropZone();
  } catch (e) {
    debugLog('[Skills] Failed to load: ' + e.message, 'error');
    var grid = document.getElementById('skillsCatalogGrid');
    if (grid) grid.innerHTML = '<p class="stg-empty">' + escapeHtml(t('skills.loadFailed', { err: e.message })) + '</p>';
  }
}

function _skillsSetScope(scope) {
  _skillsScope = scope;
  _skillsPage = 1;
  document.querySelectorAll('.skills-scope-tab').forEach(function (t) {
    t.classList.toggle('active', t.dataset.scope === scope);
  });
  _skillsRender();
}

function _skillsFilter(q) {
  _skillsSearchQuery = (q || '').toLowerCase().trim();
  _skillsPage = 1;
  _skillsRender();
}

function _skillsSetCategory(cat) {
  _skillsActiveCategory = cat;
  _skillsPage = 1;
  _skillsRender();
}

function _skillsSetPage(n) {
  _skillsPage = Math.max(1, n | 0);
  _skillsRender();
  // Scroll the grid back to the top when changing pages.
  var grid = document.getElementById('skillsCatalogGrid');
  if (grid && grid.scrollIntoView) {
    try { grid.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (_) { /* ignore */ }
  }
}

// Render a pagination bar: « ‹ 1 2 … N › ».  `total` is the total item
// count, `pageSize` the per-page cap.  Returns '' when only one page.
function _skillsRenderPagination(total, pageSize) {
  var pages = Math.ceil(total / pageSize);
  if (pages <= 1) return '';
  if (_skillsPage > pages) _skillsPage = pages;
  var cur = _skillsPage;
  // Build a compact window of page numbers around the current page.
  var nums = [];
  var add = function (n) { if (nums.indexOf(n) === -1 && n >= 1 && n <= pages) nums.push(n); };
  add(1); add(pages);
  for (var d = -2; d <= 2; d++) add(cur + d);
  nums.sort(function (a, b) { return a - b; });

  var from = (cur - 1) * pageSize + 1;
  var to = Math.min(total, cur * pageSize);
  var html = '<div class="skills-pagination">';
  html += '<span class="skills-page-info">' + escapeHtml(t('skills.pageInfo', { from: from, to: to, total: total })) + '</span>';
  html += '<div class="skills-page-ctrls">';
  var prevDis = cur <= 1 ? ' disabled' : '';
  var nextDis = cur >= pages ? ' disabled' : '';
  html += '<button class="skills-page-btn"' + prevDis + ' onclick="_skillsSetPage(' + (cur - 1) + ')" aria-label="Previous page">‹</button>';
  var prev = 0;
  nums.forEach(function (n) {
    if (prev && n - prev > 1) html += '<span class="skills-page-ellipsis">…</span>';
    var activeCls = n === cur ? ' is-active' : '';
    html += '<button class="skills-page-btn' + activeCls + '" onclick="_skillsSetPage(' + n + ')">' + n + '</button>';
    prev = n;
  });
  html += '<button class="skills-page-btn"' + nextDis + ' onclick="_skillsSetPage(' + (cur + 1) + ')" aria-label="Next page">›</button>';
  html += '</div></div>';
  return html;
}

function _skillsRender() {
  _skillsRenderHeader();
  _skillsRenderCategoryBar();
  if (_skillsScope === 'catalog') {
    _skillsRenderCatalog();
  } else {
    _skillsRenderInstalled();
  }
}

function _skillsRenderHeader() {
  var total = document.getElementById('skillsTotalCount');
  var cat = document.getElementById('skillsCatalogCount');
  if (total) total.textContent = t('skills.countInstalled', { n: _skillsInstalled.length });
  if (cat) {
    cat.textContent = t('skills.countCatalog', { n: _skillsCatalog.length });
    cat.style.display = _skillsScope === 'catalog' ? '' : 'none';
  }
}

function _skillsRenderCategoryBar() {
  var bar = document.getElementById('skillsCategoryBar');
  if (!bar) return;
  if (_skillsScope !== 'catalog') {
    bar.innerHTML = '';
    bar.style.display = 'none';
    return;
  }
  bar.style.display = '';
  var cats = {};
  _skillsCatalog.forEach(function (e) {
    var c = e.category || 'Other';
    cats[c] = (cats[c] || 0) + 1;
  });
  var html = '<button class="mcp-cat-pill' + (_skillsActiveCategory === 'all' ? ' active' : '') + '" onclick="_skillsSetCategory(\'all\')">' + escapeHtml(t('skills.scopeAll')) + ' <span class="mcp-cat-count">' + _skillsCatalog.length + '</span></button>';
  var order = ['Documents', 'Coding', 'Creative', 'Infrastructure', 'Productivity', 'Research', 'Other'];
  order.forEach(function (c) {
    if (!cats[c]) return;
    html += '<button class="mcp-cat-pill' + (_skillsActiveCategory === c ? ' active' : '') + '" onclick="_skillsSetCategory(\'' + c + '\')">' + escapeHtml(c) + ' <span class="mcp-cat-count">' + cats[c] + '</span></button>';
  });
  bar.innerHTML = html;
}

function _skillsFilteredCatalog() {
  return _skillsCatalog.filter(function (e) {
    if (_skillsActiveCategory !== 'all' && e.category !== _skillsActiveCategory) return false;
    if (_skillsSearchQuery) {
      var hay = (e.name + ' ' + e.description + ' ' + (e.tags || []).join(' ') + ' ' + (e.author || '')).toLowerCase();
      return hay.indexOf(_skillsSearchQuery) !== -1;
    }
    return true;
  });
}

function _skillsRenderCatalog() {
  var grid = document.getElementById('skillsCatalogGrid');
  if (!grid) return;
  var items = _skillsFilteredCatalog();
  if (!items.length) {
    grid.innerHTML = '<p class="stg-empty">' + escapeHtml(t('skills.noMatch')) + '</p>';
    return;
  }
  // Featured first, then alphabetical
  items.sort(function (a, b) {
    if (a.featured && !b.featured) return -1;
    if (!a.featured && b.featured) return 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  var total = items.length;
  var pages = Math.max(1, Math.ceil(total / _SKILLS_PAGE_SIZE));
  if (_skillsPage > pages) _skillsPage = pages;
  var start = (_skillsPage - 1) * _SKILLS_PAGE_SIZE;
  var slice = items.slice(start, start + _SKILLS_PAGE_SIZE);
  var html = slice.map(_skillsRenderCatalogCard).join('');
  grid.innerHTML = html + _skillsRenderPagination(total, _SKILLS_PAGE_SIZE);
}

function _skillsRenderCatalogCard(e) {
  var installed = !!e.installed;
  var icon = e.icon || Icon('package', 26);
  var iconHtml = /^<svg/i.test(icon) ? icon : escapeHtml(icon);
  var stateClass = installed ? ' is-installed' : '';
  var html = '<div class="mcp-app-card skill-card' + stateClass + '">';
  html += '<div class="mcp-app-icon">' + iconHtml + '</div>';
  html += '<div class="mcp-app-name"><span class="mcp-app-name-text">' + escapeHtml(e.name) + '</span>';
  if (e.author && /anthropic/i.test(e.author)) {
    html += '<span class="skill-badge-official">' + escapeHtml(t('skills.official')) + '</span>';
  }
  html += '</div>';
  if (e.author) {
    html += '<div class="skill-author">' + escapeHtml(t('skills.by', { author: e.author })) + '</div>';
  }
  html += '<div class="mcp-app-desc">' + escapeHtml(e.description || '') + '</div>';

  // Requirements warning
  var reqs = e.requires || {};
  var warnBits = [];
  if (Array.isArray(reqs.bins) && reqs.bins.length) warnBits.push(t('skills.reqBins', { bins: reqs.bins.join(', ') }));
  if (Array.isArray(reqs.env) && reqs.env.length) warnBits.push(t('skills.reqEnv', { env: reqs.env.join(', ') }));
  if (warnBits.length) {
    html += '<div class="skill-badge-warn">⚠ ' + escapeHtml(warnBits.join(' · ')) + '</div>';
  }

  // Footer: homepage link + install/installed action
  html += '<div class="skill-card-footer">';
  if (e.homepage) {
    html += '<a class="mcp-app-repo" href="' + escapeHtml(e.homepage) + '" target="_blank" rel="noopener" title="Homepage">' +
      '<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>' +
      ' ' + escapeHtml(t('skills.repo')) + '</a>';
  } else {
    html += '<span></span>';
  }
  html += '<div class="skill-card-actions">';
  if (installed) {
    var memId = e.installed_memory_id || e.id;
    html += '<span class="skill-installed-tag">' + escapeHtml(t('skills.installedTag')) + '</span>';
    html += '<button class="btn btn-secondary btn-xs" onclick="_skillsViewFiles(\'' + escapeHtml(memId) + '\')">' + escapeHtml(t('skills.viewFiles')) + '</button>';
    html += '<button class="btn btn-secondary btn-xs" onclick="_skillsUninstall(\'' + escapeHtml(memId) + '\')">' + escapeHtml(t('skills.uninstallBtn')) + '</button>';
  } else {
    html += '<button class="btn btn-primary btn-xs" onclick="_skillsCatalogInstall(\'' + escapeHtml(e.id) + '\', this)">' + escapeHtml(t('skills.installBtn')) + '</button>';
  }
  html += '</div>';
  html += '</div></div>';
  return html;
}

function _skillsRenderInstalled() {
  var grid = document.getElementById('skillsCatalogGrid');
  if (!grid) return;
  var items = _skillsInstalled.filter(function (m) {
    if (_skillsSearchQuery) {
      var hay = (m.name + ' ' + (m.description || '') + ' ' + (m.tags || []).join(' ')).toLowerCase();
      return hay.indexOf(_skillsSearchQuery) !== -1;
    }
    return true;
  });
  if (!items.length) {
    grid.innerHTML = '<p class="stg-empty">' + escapeHtml(t('skills.emptyInstalled')) + '</p>';
    return;
  }
  items.sort(function (a, b) { return (b.updated || '').localeCompare(a.updated || ''); });
  var total = items.length;
  var pages = Math.max(1, Math.ceil(total / _SKILLS_PAGE_SIZE));
  if (_skillsPage > pages) _skillsPage = pages;
  var start = (_skillsPage - 1) * _SKILLS_PAGE_SIZE;
  items = items.slice(start, start + _SKILLS_PAGE_SIZE);
  var html = items.map(function (m) {
    var ineligible = !m.eligible;
    var html2 = '<div class="mcp-app-card skill-card is-installed">';
    html2 += '<div class="mcp-app-icon">' + Icon('package', 26) + '</div>';
    html2 += '<div class="mcp-app-name"><span class="mcp-app-name-text">' + escapeHtml(m.name) + '</span>';
    html2 += '<span class="mcp-app-status ' + (m.enabled ? 'on' : 'off') + '"><span class="dot"></span>' + escapeHtml(m.enabled ? t('skills.statusOn') : t('skills.statusOff')) + '</span>';
    html2 += '</div>';
    html2 += '<div class="skill-author">' + escapeHtml(t('skills.scopeIdLine', { scope: m.scope, id: m.id })) + '</div>';
    html2 += '<div class="mcp-app-desc">' + escapeHtml(m.description || '') + '</div>';
    if (ineligible && Array.isArray(m.ineligible_reasons) && m.ineligible_reasons.length) {
      html2 += '<div class="skill-badge-warn">⚠ ' + escapeHtml(m.ineligible_reasons.join(' · ')) + '</div>';
    }
    html2 += '<div class="skill-card-footer"><span></span><div class="skill-card-actions">';
    html2 += '<button class="btn btn-secondary btn-xs" onclick="_skillsViewFiles(\'' + escapeHtml(m.id) + '\')">' + escapeHtml(t('skills.viewFiles')) + '</button>';
    html2 += '<button class="btn btn-secondary btn-xs" onclick="_skillsToggleEnabled(\'' + escapeHtml(m.id) + '\', this)">' + escapeHtml(m.enabled ? t('skills.disable') : t('skills.enable')) + '</button>';
    html2 += '<button class="btn btn-secondary btn-xs" onclick="_skillsUninstall(\'' + escapeHtml(m.id) + '\')">' + escapeHtml(t('skills.uninstallBtn')) + '</button>';
    html2 += '</div></div></div>';
    return html2;
  }).join('');
  grid.innerHTML = html + _skillsRenderPagination(total, _SKILLS_PAGE_SIZE);
}

// ── Actions ───────────────────────────────────────────────────

async function _skillsCatalogInstall(skillId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = t('skills.installing'); }
  _skillsToast(t('skills.downloadingInstalling', { id: skillId }));
  try {
    var r = await Api.memory.catalogInstall(skillId, 'project');
    var d = (r ? await r.json().catch(function () { return {}; }) : {});
    if (!r || !r.ok) {
      _skillsToast(t('skills.installFailed', { err: (d.error || r.statusText) }), 'error');
      if (btn) { btn.disabled = false; btn.textContent = t('skills.installBtn'); }
      return;
    }
    var hints = d.install_hints || [];
    var msg = t('skills.installedToast', { name: d.memory.name });
    if (hints.length) msg += t('skills.installHintSuffix', { files: hints.map(function (h) { return h.file; }).join(', ') });
    _skillsToast(msg, 'success');
    debugLog('[Skills] Installed: ' + d.memory.name, 'success');
    await _populateSkillsTab();
  } catch (e) {
    _skillsToast(t('skills.installError', { err: e.message }), 'error');
    if (btn) { btn.disabled = false; btn.textContent = t('skills.installBtn'); }
  }
}

async function _skillsUninstall(memoryId) {
  if (!await showConfirm(t('skills.uninstallConfirm', { id: memoryId }), { danger: true })) return;
  try {
    var r = await Api.memory.remove(memoryId);
    if (!r || !r.ok) {
      var d = (r ? await r.json().catch(function () { return {}; }) : {});
      _skillsToast(t('skills.uninstallFailed', { err: (d.error || (r && r.statusText) || t('skills.noResponse')) }), 'error');
      return;
    }
    _skillsToast(t('skills.uninstalledToast', { id: memoryId }), 'success');
    await _populateSkillsTab();
  } catch (e) {
    _skillsToast(t('skills.uninstallError', { err: e.message }), 'error');
  }
}

async function _skillsToggleEnabled(memoryId, btn) {
  try {
    var r = await Api.memory.toggle(memoryId);
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    await _populateSkillsTab();
  } catch (e) {
    _skillsToast(t('skills.toggleFailed', { err: e.message }), 'error');
  }
}

// ── File browser ───────────────────────────────────────────────

async function _skillsViewFiles(memoryId) {
  var overlay = document.getElementById('skillsFilesOverlay');
  var titleEl = document.getElementById('skillsFilesTitle');
  var descEl = document.getElementById('skillsFilesDesc');
  var listEl = document.getElementById('skillsFilesList');
  if (!overlay || !listEl) return;
  titleEl.textContent = memoryId;
  descEl.textContent = t('skills.filesLoading');
  listEl.innerHTML = '';
  overlay.style.display = 'flex';
  try {
    var d = await Api.memory.files(memoryId);
    if (!d) {
      descEl.textContent = t('skills.filesLoadFailed');
      return;
    }
    descEl.textContent = t('skills.filesCount', { n: d.count, root: d.root });
    var _fkSvg = function(inner) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>'; };
    var iconMap = {
      skill: _fkSvg('<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/>'),
      doc: _fkSvg('<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>'),
      script: _fkSvg('<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>'),
      config: _fkSvg('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>'),
      asset: _fkSvg('<path d="m16 6-8.414 8.586a2 2 0 0 0 2.829 2.829l8.414-8.586a4 4 0 1 0-5.657-5.657l-8.379 8.551a6 6 0 1 0 8.485 8.485l8.379-8.551"/>'),
    };
    var html = d.files.map(function (f) {
      var sz = _skillsFmtSize(f.size);
      var cls = f.kind === 'skill' ? ' is-skill' : '';
      return '<div class="skills-file-row' + cls + '">' +
        '<span class="skills-file-kind">' + (iconMap[f.kind] || '·') + '</span>' +
        '<span class="skills-file-path" title="' + escapeHtml(f.path) + '">' + escapeHtml(f.path) + '</span>' +
        '<span class="skills-file-size">' + sz + '</span></div>';
    }).join('');
    listEl.innerHTML = html;
  } catch (e) {
    descEl.textContent = t('skills.filesError', { err: e.message });
  }
}

function _skillsCloseFiles(evt) {
  var overlay = document.getElementById('skillsFilesOverlay');
  if (!overlay) return;
  if (evt && evt.target !== overlay) return;
  overlay.style.display = 'none';
}

function _skillsFmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

// ── Drag-and-drop & file picker ────────────────────────────────

function _skillsAttachDropZone() {
  if (_skillsDropAttached) return;
  _skillsDropAttached = true;
  var panel = document.getElementById('settingsTab_skills');
  var zone = document.getElementById('skillsDropZone');
  if (!panel || !zone) return;

  var hasFiles = function (e) {
    var dt = e.dataTransfer;
    if (!dt || !dt.types) return false;
    for (var i = 0; i < dt.types.length; i++) if (dt.types[i] === 'Files') return true;
    return false;
  };

  panel.addEventListener('dragenter', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    _skillsDragDepth++;
    zone.classList.add('is-dragging');
  });
  panel.addEventListener('dragover', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  panel.addEventListener('dragleave', function (e) {
    if (!hasFiles(e)) return;
    _skillsDragDepth = Math.max(0, _skillsDragDepth - 1);
    if (_skillsDragDepth === 0) zone.classList.remove('is-dragging');
  });
  panel.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    _skillsDragDepth = 0;
    zone.classList.remove('is-dragging');
    var files = e.dataTransfer.files;
    if (!files || !files.length) return;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (/\.zip$/i.test(f.name) || f.type === 'application/zip' || f.type === 'application/x-zip-compressed') {
        _skillsUploadZip(f);
        return;
      }
    }
    _skillsToast(t('skills.notZip'), 'error');
  });
}

function _skillsInstallFromInput(input) {
  var f = input && input.files && input.files[0];
  if (!f) return;
  _skillsUploadZip(f);
  input.value = '';
}

async function _skillsUploadZip(file) {
  _skillsToast(t('skills.installingFile', { name: file.name }));
  var fd = new FormData();
  fd.append('file', file);
  fd.append('scope', 'project');
  try {
    var r = await Api.memory.install(fd);
    var d = (r ? await r.json().catch(function () { return {}; }) : {});
    if (!r || !r.ok) {
      _skillsToast(t('skills.installFailed', { err: (d.error || (r && r.statusText) || t('skills.noResponse')) }), 'error');
      return;
    }
    var hints = d.install_hints || [];
    var msg = t('skills.installedToast', { name: d.memory.name });
    if (hints.length) msg += t('skills.installHintSuffixUpload', { files: hints.map(function (h) { return h.file; }).join(', ') });
    _skillsToast(msg, 'success');
    await _populateSkillsTab();
  } catch (e) {
    _skillsToast(t('skills.installError', { err: e.message }), 'error');
  }
}

// ── Toast helper ──────────────────────────────────────────────

// ── Cross-modal entry: open Settings → Skills tab from Memory modal ──

function _openSkillsStoreFromMemory() {
  // Close memory modal first
  if (typeof closeMemoryModal === 'function') closeMemoryModal();
  // Open settings, then switch to Skills tab once it's open
  if (typeof openSettings === 'function') {
    openSettings();
    setTimeout(function () {
      if (typeof switchSettingsTab === 'function') switchSettingsTab('skills');
    }, 50);
  }
}

function _skillsToast(text, kind) {
  // Remove existing
  document.querySelectorAll('.skills-toast').forEach(function (t) { t.remove(); });
  var el = document.createElement('div');
  el.className = 'skills-toast' + (kind === 'error' ? ' is-error' : kind === 'success' ? ' is-success' : '');
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(function () {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(function () { el.remove(); }, 300);
  }, kind === 'error' ? 5000 : 3500);
}
