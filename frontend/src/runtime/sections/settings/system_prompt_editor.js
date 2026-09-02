/* ===== migrated source: settings/system_prompt_editor.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/system_prompt_editor.js — per-block system-prompt editor

   The General tab keeps a compact summary row + an "Edit…" button. The
   editor modal loads the built-in system prompt split into BLOCKS (one per
   section) and renders each with a keep/drop toggle, plus a free-text area
   for additional instructions appended after the built-in prompt.

   Source of truth (two hidden inputs on the General tab):
     #settingSystem               → config.systemPrompt (custom additions)
     #settingSystemDisabledBlocks → JSON array of disabled block IDs
                                     → config.systemPromptBlocks.disabled
   saveSettings() reads both; openSettings() seeds them from config. This
   module mirrors them into the modal and writes edits back on Apply.

   Concatenated by Vite's module graph — shares window scope. No imports.
   ═══════════════════════════════════════════════════════════════════ */

/** Read the disabled-block-ID set from the hidden input (always an array). */
function _getDisabledBlocks() {
  var el = document.getElementById('settingSystemDisabledBlocks');
  if (!el) return [];
  try {
    var arr = JSON.parse(el.value || '[]');
    return Array.isArray(arr) ? arr.filter(function (x) { return !!x; }) : [];
  } catch (e) {
    return [];
  }
}

/** Write the disabled-block-ID set back to the hidden input. */
function _setDisabledBlocks(ids) {
  var el = document.getElementById('settingSystemDisabledBlocks');
  if (el) el.value = JSON.stringify(Array.from(new Set(ids || [])));
}

/** Refresh the compact summary line on the General tab. */
function _refreshSystemPromptSummary() {
  var ta = document.getElementById('settingSystem');
  var summary = document.getElementById('settingSystemSummary');
  if (!summary) return;
  var val = ((ta && ta.value) || '').trim();
  var disabled = _getDisabledBlocks();
  var parts = [];
  if (disabled.length) {
    var lbl = (typeof t === 'function') ? t('settings.systemPromptBlocksOff')
      : 'blocks off';
    parts.push(disabled.length + ' ' + lbl);
  }
  if (val) {
    var charsLbl = (typeof t === 'function') ? t('settings.systemPromptSet')
      : 'custom prompt set';
    parts.push(charsLbl + ' · ' + val.length + ' chars');
  }
  if (!parts.length) {
    summary.textContent = (typeof t === 'function')
      ? t('settings.systemPromptEmpty') : '(using all built-in blocks)';
    summary.classList.remove('has-value');
  } else {
    summary.textContent = parts.join(' · ');
    summary.classList.add('has-value');
  }
}

/* ── Modal preview mode ──
   Block visibility depends on project mode (code blocks) and tools. The
   modal previews tools-on by default with a checkbox to include the
   project/code blocks. The disabled SET is keyed on block ID and persists
   regardless of which preview mode is showing.

   Both mode variants are fetched ONCE on open and cached, so flipping the
   preview toggle re-renders instantly from memory — no network round-trip,
   no "Loading…" flash, no flicker. */
var _sysPromptPreviewProject = false;
var _sysPromptBlocksCache = { chat: null, project: null };

/** IDs of blocks whose TEXT changes in project/code mode vs chat mode.
 *  These are the blocks the preview toggle actually affects, so we badge
 *  them so the user can see what "code/project mode" rewrites. */
function _projectAffectedIds() {
  var chat = _sysPromptBlocksCache.chat || [];
  var proj = _sysPromptBlocksCache.project || [];
  var chatById = {};
  chat.forEach(function (b) { chatById[b.id] = (b.text || ''); });
  var ids = {};
  proj.forEach(function (b) {
    var ct = chatById[b.id];
    if (ct === undefined || ct !== (b.text || '')) ids[b.id] = true;
  });
  return ids;
}

/** Render the blocks list into the modal from a fetched blocks array. */
function _renderSystemPromptBlocks(blocks) {
  var list = document.getElementById('sysPromptBlocksList');
  if (!list) return;
  var disabled = _getDisabledBlocks();
  var projectIds = _projectAffectedIds();
  list.innerHTML = '';
  if (!blocks || !blocks.length) {
    list.innerHTML = '<div class="settings-toggle-desc">'
      + ((typeof t === 'function') ? t('settings.systemPromptLoadFailed')
          : 'Failed to load built-in prompt') + '</div>';
    return;
  }
  var CHEVRON = '<svg class="sysprompt-block-chevron" viewBox="0 0 24 24" '
    + 'fill="none" stroke="currentColor" stroke-width="2.5" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<polyline points="9 18 15 12 9 6"></polyline></svg>';

  blocks.forEach(function (b) {
    var off = disabled.indexOf(b.id) !== -1;
    var isProj = !!projectIds[b.id];
    var card = document.createElement('div');
    card.className = 'sysprompt-block' + (off ? ' is-off' : '')
      + (isProj ? ' is-project' : '');

    var header = document.createElement('div');
    header.className = 'sysprompt-block-head';
    header.setAttribute('role', 'button');
    header.setAttribute('tabindex', '0');

    var chev = document.createElement('span');
    chev.innerHTML = CHEVRON;

    var titleWrap = document.createElement('div');
    titleWrap.className = 'sysprompt-block-title';
    var titleText = document.createElement('span');
    titleText.textContent = b.title || b.id;
    titleWrap.appendChild(titleText);
    if (b.dynamic) {
      var badge = document.createElement('span');
      badge.className = 'sysprompt-block-badge';
      badge.textContent = (typeof t === 'function')
        ? t('settings.systemPromptDynamic') : 'dynamic';
      titleWrap.appendChild(badge);
    }
    if (isProj) {
      var pbadge = document.createElement('span');
      pbadge.className = 'sysprompt-block-badge is-project-badge';
      pbadge.textContent = (typeof t === 'function')
        ? t('settings.systemPromptProjectBlock') : 'project';
      pbadge.title = (typeof t === 'function')
        ? t('settings.systemPromptProjectBlockTip') : '';
      titleWrap.appendChild(pbadge);
    }

    var toggle = document.createElement('label');
    toggle.className = 'stg-toggle sysprompt-block-toggle';
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !off;
    input.setAttribute('data-block-id', b.id);
    input.addEventListener('change', function () {
      var d = _getDisabledBlocks();
      var idx = d.indexOf(b.id);
      if (this.checked) { if (idx !== -1) d.splice(idx, 1); }
      else if (idx === -1) { d.push(b.id); }
      _setDisabledBlocks(d);
      card.classList.toggle('is-off', !this.checked);
    });
    var track = document.createElement('span');
    track.className = 'stg-toggle-track';
    track.innerHTML = '<span class="stg-toggle-thumb"></span>';
    toggle.appendChild(input);
    toggle.appendChild(track);
    // The toggle lives inside the click-to-expand header — stop its clicks
    // from also collapsing/expanding the card.
    toggle.addEventListener('click', function (e) { e.stopPropagation(); });

    header.appendChild(chev.firstChild);
    header.appendChild(titleWrap);
    header.appendChild(toggle);

    var pre = document.createElement('pre');
    pre.className = 'sysprompt-block-text';
    pre.textContent = b.text || '';

    function _toggleOpen() { card.classList.toggle('is-open'); }
    header.addEventListener('click', _toggleOpen);
    header.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleOpen(); }
    });

    card.appendChild(header);
    card.appendChild(pre);
    list.appendChild(card);
  });
}

/** (Re)render the preview-mode header (toggle + label) from current state. */
function _renderModeHeader() {
  var modeEl = document.getElementById('sysPromptBlocksMode');
  if (!modeEl) return;
  var modeLabel = _sysPromptPreviewProject
    ? ((typeof t === 'function') ? t('settings.systemPromptPreviewProject')
        : 'preview: code/project mode')
    : ((typeof t === 'function') ? t('settings.systemPromptPreviewChat')
        : 'preview: chat mode');
  modeEl.innerHTML = '<label class="sysprompt-preview-toggle">'
    + '<span class="stg-toggle stg-dv-toggle">'
    + '<input type="checkbox" id="sysPromptPreviewProjectCb"'
    + (_sysPromptPreviewProject ? ' checked' : '') + '>'
    + '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>'
    + '</span>'
    + '<span>' + ((typeof t === 'function')
        ? t('settings.systemPromptPreviewCode') : 'show code/project blocks')
    + '</span></label> '
    + '<span class="sysprompt-blocks-mode-label">' + modeLabel + '</span>';
  var cb = document.getElementById('sysPromptPreviewProjectCb');
  // Flipping the preview is a PURE re-render from cache — no network, so the
  // list never collapses to a "Loading…" state and there is no flicker.
  if (cb) cb.addEventListener('change', function () {
    _sysPromptPreviewProject = this.checked;
    _renderModeHeader();
    _renderActiveBlocks();
  });
}

/** Render the cached blocks for the active preview mode (instant). */
function _renderActiveBlocks() {
  var cached = _sysPromptPreviewProject
    ? _sysPromptBlocksCache.project : _sysPromptBlocksCache.chat;
  _renderSystemPromptBlocks(cached || []);
}

/** Fetch BOTH mode variants once, cache them, then render. */
async function _loadSystemPromptBlocks() {
  var list = document.getElementById('sysPromptBlocksList');
  // Only show the loading placeholder on a true cold load — never on a
  // preview toggle, which renders straight from cache.
  if (list && !_sysPromptBlocksCache.chat && !_sysPromptBlocksCache.project) {
    list.innerHTML = '<div class="settings-toggle-desc">'
      + ((typeof t === 'function') ? t('settings.systemPromptLoading')
          : 'Loading built-in prompt…') + '</div>';
  }
  _renderModeHeader();
  try {
    var results = await Promise.all([
      Api.serverConfig.systemPromptBlocks(false, true),
      Api.serverConfig.systemPromptBlocks(true, true),
    ]);
    _sysPromptBlocksCache.chat = (results[0] && results[0].blocks) || [];
    _sysPromptBlocksCache.project = (results[1] && results[1].blocks) || [];
    _renderActiveBlocks();
  } catch (e) {
    if (typeof debugLog === 'function') {
      debugLog('[sysPromptEditor] loadBlocks failed: ' + (e && e.message), 'error');
    }
    _renderSystemPromptBlocks([]);
  }
}

function openSystemPromptEditor() {
  var src = document.getElementById('settingSystem');
  var area = document.getElementById('sysPromptEditorArea');
  if (!src || !area) return;
  area.value = src.value || '';
  var status = document.getElementById('sysPromptEditorStatus');
  if (status) status.textContent = '';
  // Default the preview to the user's likely mode: if they have a project
  // path configured, show code blocks.
  try {
    _sysPromptPreviewProject = !!(typeof config !== 'undefined'
      && config && config.projectPath);
  } catch (e) { _sysPromptPreviewProject = false; }
  document.getElementById('sysPromptModal').classList.add('open');
  _sysPromptBlocksCache = { chat: null, project: null };
  _loadSystemPromptBlocks();
  setTimeout(function () { area.focus(); }, 50);
}

function closeSystemPromptEditor() {
  document.getElementById('sysPromptModal').classList.remove('open');
}

/** Write the editor content back to the hidden inputs (does NOT persist —
 *  saveSettings() does that when the user saves the settings panel). The
 *  disabled-block set is already kept in sync on every toggle change. */
function applySystemPromptEditor() {
  var src = document.getElementById('settingSystem');
  var area = document.getElementById('sysPromptEditorArea');
  if (src && area) src.value = area.value;
  _refreshSystemPromptSummary();
  closeSystemPromptEditor();
}

/** Re-enable all built-in blocks (clear the disabled set). */
function resetSystemPromptBlocks() {
  _setDisabledBlocks([]);
  // Clearing the disabled set is a pure re-render — reuse the cache if we
  // have it, otherwise cold-load.
  if (_sysPromptBlocksCache.chat || _sysPromptBlocksCache.project) {
    _renderActiveBlocks();
  } else {
    _loadSystemPromptBlocks();
  }
}

if (typeof window !== 'undefined') {
  runtimeScope.openSystemPromptEditor = openSystemPromptEditor;
  runtimeScope.closeSystemPromptEditor = closeSystemPromptEditor;
  runtimeScope.applySystemPromptEditor = applySystemPromptEditor;
  runtimeScope.resetSystemPromptBlocks = resetSystemPromptBlocks;
  runtimeScope._refreshSystemPromptSummary = _refreshSystemPromptSummary;
}

