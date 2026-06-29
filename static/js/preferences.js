/* ═══════════════════════════════════════════════════════════
   preferences.js — Preferences tab in Settings

   View / hand-edit the bounded personal-preference profile as
   INDIVIDUAL preferences (not a raw markdown textarea), grouped
   into sections ("Preferences" / "About the user"). Backed by
   GET/PUT /api/v1/profile (structured `items: [{header,text}]`).

   The assistant learns and auto-applies new preferences during a
   conversation; this panel is where the user reviews and corrects
   them. Backed by lib/memory/user_profile.py. Concatenated by
   lib/js_bundler.py — symbols share the window scope.
   ═══════════════════════════════════════════════════════════ */

var _prefsCap = 2500;        // server-reported hard cap (chars)
var _prefsItems = [];        // working copy: [{header, text}]
// Canonical sections always offered (in display order). Other headers found
// in the stored profile are appended as their own sections.
var _PREFS_SECTIONS = ['Preferences', 'About the user'];

// Called from openSettings() and switchSettingsTab('preferences').
async function _populatePreferencesTab() {
  await refreshPreferences();
}

async function refreshPreferences() {
  var status = document.getElementById('prefsStatus');
  if (status) status.textContent = (typeof t === 'function' ? t('prefsPanel.loading') : '加载中…');
  try {
    var d = await Api.profile.get();
    if (!d) throw new Error('empty response');
    _prefsCap = d.cap || _prefsCap;
    _prefsItems = Array.isArray(d.items)
      ? d.items.map(function (it) { return { header: it.header || '', text: it.text || '' }; })
      : [];
    _prefsRender();
    if (status) status.textContent = '';
  } catch (e) {
    if (status) status.textContent = (typeof t === 'function' ? t('prefsPanel.loadFailed') : '加载失败') + ': ' + e.message;
    debugLog && debugLog('[Prefs] load failed: ' + e.message, 'error');
  }
}

// Section title for display: translate the two canonical headers, pass others through.
function _prefsSectionTitle(header) {
  var _t = (typeof t === 'function') ? t : function (k) { return k; };
  if (header === 'Preferences') return _t('prefsPanel.secPreferences');
  if (header === 'About the user') return _t('prefsPanel.secAbout');
  return header || _t('prefsPanel.secOther');
}

// Render the grouped, editable list from _prefsItems into #prefsList.
function _prefsRender() {
  var list = document.getElementById('prefsList');
  if (!list) return;
  var _esc = (typeof escapeHtml === 'function') ? escapeHtml : function (s) { return s; };
  var _t = (typeof t === 'function') ? t : function (k) { return k; };

  // Determine the section order: canonical sections first, then any extras
  // present in the data, preserving first-seen order.
  var sections = _PREFS_SECTIONS.slice();
  _prefsItems.forEach(function (it) {
    var h = it.header || 'Preferences';
    if (sections.indexOf(h) === -1) sections.push(h);
  });

  var html = sections.map(function (header) {
    var rows = '';
    _prefsItems.forEach(function (it, idx) {
      var h = it.header || 'Preferences';
      if (h !== header) return;
      rows += '<div class="pref-item-row" data-idx="' + idx + '">' +
        '<span class="pref-item-dot"></span>' +
        '<input type="text" class="pref-item-input" value="' + _esc(it.text || '') +
        '" oninput="_prefsOnEdit(' + idx + ', this.value)" ' +
        'placeholder="' + _esc(_t('prefsPanel.itemPlaceholder')) + '">' +
        '<button class="pref-item-del" title="' + _esc(_t('prefsPanel.remove')) +
        '" onclick="_prefsRemove(' + idx + ')">' +
        (typeof Icon === 'function' ? Icon('x', 14) : '×') + '</button>' +
        '</div>';
    });
    return '<div class="pref-section" data-header="' + _esc(header) + '">' +
      '<div class="pref-section-head">' +
      '<span class="pref-section-title">' + _esc(_prefsSectionTitle(header)) + '</span>' +
      '<button class="pref-add-btn" onclick="_prefsAdd(\'' + _esc(header).replace(/'/g, "\\'") + '\')">' +
      (typeof Icon === 'function' ? Icon('plus', 13) : '+') +
      ' <span>' + _esc(_t('prefsPanel.addItem')) + '</span></button>' +
      '</div>' +
      (rows || '<div class="pref-section-empty">' + _esc(_t('prefsPanel.sectionEmpty')) + '</div>') +
      '</div>';
  }).join('');

  list.innerHTML = html;
  _prefsUpdateCharCount();
}

// Live edit of one item's text.
function _prefsOnEdit(idx, value) {
  if (_prefsItems[idx]) {
    _prefsItems[idx].text = value;
    _prefsUpdateCharCount();
  }
}

// Add a blank item to a section and focus it.
function _prefsAdd(header) {
  _prefsItems.push({ header: header || 'Preferences', text: '' });
  _prefsRender();
  // Focus the newly-added input (last row of that section).
  var inputs = document.querySelectorAll('.pref-section[data-header="' + header + '"] .pref-item-input');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

// Remove an item.
function _prefsRemove(idx) {
  _prefsItems.splice(idx, 1);
  _prefsRender();
}

// Approximate char count (matches the server's markdown serialization closely
// enough to warn before the cap; the authoritative count comes back on save).
function _prefsUpdateCharCount() {
  var badge = document.getElementById('prefsCharCount');
  if (!badge) return;
  var n = 0;
  _prefsItems.forEach(function (it) {
    var txt = (it.text || '').trim();
    if (txt) n += txt.length + 3;  // "- " + text + newline
  });
  badge.textContent = n + ' / ' + _prefsCap;
  badge.style.color = n > _prefsCap ? 'var(--danger, #e5484d)' : '';
}

async function savePreferences(btn) {
  var status = document.getElementById('prefsStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = (typeof t === 'function' ? t('prefsPanel.saving') : '保存中…');
  try {
    // Drop empties; trim. Server re-parses and returns the canonical items.
    var clean = _prefsItems
      .map(function (it) { return { header: it.header || 'Preferences', text: (it.text || '').trim() }; })
      .filter(function (it) { return it.text; });
    var res = await Api.profile.saveItems(clean);
    if (res && res.saved === false) throw new Error('server reported save failed');
    if (Array.isArray(res && res.items)) {
      _prefsItems = res.items.map(function (it) { return { header: it.header || '', text: it.text || '' }; });
      _prefsRender();
    }
    if (status) {
      var msg = (typeof t === 'function' ? t('prefsPanel.saved') : '已保存');
      if (res && res.over_cap) msg += ' · ' + (typeof t === 'function' ? t('prefsPanel.overCap') : '超出长度上限，下次整理时会自动压缩');
      status.textContent = msg;
    }
    debugLog && debugLog('[Prefs] saved (' + ((res && res.chars) || 0) + ' chars)', 'success');
  } catch (e) {
    if (status) status.textContent = (typeof t === 'function' ? t('prefsPanel.saveFailed') : '保存失败') + ': ' + e.message;
    debugLog && debugLog('[Prefs] save failed: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}
