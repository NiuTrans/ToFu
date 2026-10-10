/* ===== migrated source: widgets/chip_input.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/chip_input.js — Reusable tag/chip input for lists of short
   strings (domains, etc.). Replaces the cramped multi-line <textarea>
   that scrolled awkwardly for domain lists.

   Usage:
     ChipInput.init('settingSkipDomains', ['youtube.com', 'x.com']);
     var domains = ChipInput.getValues('settingSkipDomains');

   The container element must exist with the given id and class
   `chip-input`. Values are deduped + trimmed; blank entries dropped.
   Adding accepts Enter / comma / blur, and a pasted multi-line/comma
   blob is split into many chips at once.

   Concatenated by Vite's module graph — shared window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

runtimeScope.ChipInput = (function () {
  var _store = {};   // containerId -> array of values

  function _normalize(list) {
    var seen = {};
    var out = [];
    (list || []).forEach(function (v) {
      var s = String(v == null ? '' : v).trim();
      if (!s || seen[s]) return;
      seen[s] = true;
      out.push(s);
    });
    return out;
  }

  function _render(id) {
    var box = document.getElementById(id);
    if (!box) return;
    var values = _store[id] || [];
    var placeholder = box.getAttribute('data-placeholder') || '';
    var chips = values.map(function (v, i) {
      return String(safeHtml`<span class="chip"><span class="chip-text">${v}</span>`) +
        String(safeHtml`<button type="button" class="chip-x icon-box" title="${t('common.remove') || '移除'}"
          data-tofu-action="ChipInput.remove('${raw(id)}', ${raw(String(i))})"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></span>`);
    });
    box.innerHTML = chips.join('') + safeHtml`<input type="text" class="chip-add"
        placeholder="${placeholder}"
        data-tofu-action-keydown="ChipInput._onKey(event, '${raw(id)}')"
        data-tofu-action-blur="ChipInput._onBlur('${raw(id)}')"
        data-tofu-action-paste="ChipInput._onPaste(event, '${raw(id)}')">`;
  }

  function _splitBlob(text) {
    return String(text || '').split(/[\s,;]+/);
  }

  function init(id, values) {
    _store[id] = _normalize(values);
    _render(id);
  }

  function getValues(id) {
    return (_store[id] || []).slice();
  }

  function add(id, raw) {
    var additions = _splitBlob(raw);
    var cur = _store[id] || [];
    _store[id] = _normalize(cur.concat(additions));
    _render(id);
    // Keep focus on the add-field for fast multi-entry.
    var box = document.getElementById(id);
    var input = box && box.querySelector('.chip-add');
    if (input) input.focus();
  }

  function remove(id, index) {
    var cur = _store[id] || [];
    cur.splice(index, 1);
    _store[id] = cur;
    _render(id);
  }

  function _onKey(ev, id) {
    if (ev.key === 'Enter' || ev.key === ',') {
      ev.preventDefault();
      add(id, ev.target.value);
      ev.target.value = '';
    } else if (ev.key === 'Backspace' && !ev.target.value) {
      // Backspace on empty field removes the last chip.
      var cur = _store[id] || [];
      if (cur.length) remove(id, cur.length - 1);
    }
  }

  function _onBlur(id) {
    var box = document.getElementById(id);
    var input = box && box.querySelector('.chip-add');
    if (input && input.value.trim()) {
      add(id, input.value);
      input.value = '';
    }
  }

  function _onPaste(ev, id) {
    var text = (ev.clipboardData || /** @type {any} */ (window).clipboardData).getData('text');
    if (text && /[\s,;\n]/.test(text)) {
      ev.preventDefault();
      add(id, text);
      ev.target.value = '';
    }
  }

  return { init: init, getValues: getValues, add: add, remove: remove,
           _onKey: _onKey, _onBlur: _onBlur, _onPaste: _onPaste };
})();

