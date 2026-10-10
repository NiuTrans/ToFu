/* ===== migrated source: widgets/dropdown.js ===== */
/*
 * Unified dropdown — one styled popup for BOTH native selects and custom
 * popovers.
 *
 * WHY THIS EXISTS
 * ───────────────
 * A native ``<select>`` opens an OS-rendered popup that ignores page CSS
 * entirely, so it always looked like an alien widget inside the tofu shell —
 * the exact complaint that split the app's dropdowns into two visual
 * languages (native OS menu in settings vs. the styled ``.preset-dropdown``
 * in the composer). The only way to unify them is to render the select's
 * popup as DOM too, in the same warm tofu popover language.
 *
 * DESIGN — progressive enhancement, the select stays the source of truth
 * ─────────────────────────────────────────────────────────────────────
 * A select carries ``data-tofu-action-change`` that existing handlers depend
 * on; we must not break that contract. So the enhancement keeps the real
 * ``<select>`` as the value + change authority:
 *   1. Wrap the select in a ``.tofu-dd`` span; hide the native control
 *      visually (it stays focusable/screen-reader visible).
 *   2. Render a styled trigger button + a body-anchored DOM listbox that
 *      mirror the select's ``<option>``s.
 *   3. Choosing an option sets ``select.value`` and dispatches a bubbling
 *      ``change`` — the SAME event a user picking from the native popup would
 *      fire — so every existing ``data-tofu-action-change`` handler runs
 *      unchanged. Per-option ``data-*`` (e.g. MCP ``data-autofill``) rides
 *      along because the option element itself is selected.
 *
 * OPT-IN: only selects with ``data-tofu-dd`` (or ``.tofu-dd-native``) are
 * enhanced; everything else keeps the native popup. Enhancement is idempotent
 * and re-entrant, so it is safe to call after every settings re-render.
 *
 * Behavior mirrors the composer's provider-menu pattern: one open menu at a
 * time, body-anchored so no scrolling ancestor clips it, closes on outside
 * click / scroll / Escape, and flips below the trigger when there is no room
 * above.
 */

var _TOFU_DD_OPEN = null; // the wrapper whose menu is currently open, or null

function _tofuDdSelectOf(wrap) {
  return wrap ? wrap.querySelector('select') : null;
}

function _tofuDdClose() {
  var menu = document.getElementById('tofuDdMenu');
  if (menu) {
    if (typeof menu._tofuDdTeardown === 'function') menu._tofuDdTeardown();
    menu.remove();
  }
  if (_TOFU_DD_OPEN) {
    _TOFU_DD_OPEN.classList.remove('open');
    var trigger = _TOFU_DD_OPEN.querySelector('.tofu-dd-trigger');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  }
  _TOFU_DD_OPEN = null;
}

/* Read the select's options into menu entries, preserving order, disabled
 * state, the selected marker, and any per-option label override. */
function _tofuDdOptions(select) {
  var out = [];
  Array.from(select.options).forEach(function(opt, index) {
    out.push({
      index: index,
      value: opt.value,
      label: opt.textContent || opt.value,
      disabled: opt.disabled,
      selected: opt.selected,
    });
  });
  return out;
}

function _tofuDdPosition(wrap, menu) {
  var trigger = wrap.querySelector('.tofu-dd-trigger');
  if (!trigger) return;
  var rect = trigger.getBoundingClientRect();
  var menuWidth = menu.offsetWidth;
  var menuHeight = menu.offsetHeight;
  /* Prefer below the trigger (a select opens downward); flip above when the
   * viewport bottom would clip it. Settings panels scroll, so clamp left. */
  var top = rect.bottom + 5;
  if (top + menuHeight > window.innerHeight - 8 && rect.top - menuHeight - 5 > 8) {
    top = rect.top - menuHeight - 5;
  }
  var left = Math.max(8, Math.min(rect.left, window.innerWidth - menuWidth - 8));
  menu.style.top = top + 'px';
  menu.style.left = left + 'px';
  menu.style.minWidth = rect.width + 'px';
}

function _tofuDdOpen(wrap) {
  var select = _tofuDdSelectOf(wrap);
  if (!select || select.disabled) return;
  if (_TOFU_DD_OPEN === wrap) { _tofuDdClose(); return; }
  _tofuDdClose();

  var options = _tofuDdOptions(select);
  var menu = document.createElement('div');
  menu.id = 'tofuDdMenu';
  menu.className = 'tofu-dd-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', select.getAttribute('aria-label') || '');

  var activeEntry = null;
  options.forEach(function(opt) {
    var entry = document.createElement('button');
    entry.type = 'button';
    entry.className = 'tofu-dd-item' + (opt.selected ? ' active' : '');
    entry.setAttribute('role', 'option');
    entry.setAttribute('aria-selected', opt.selected ? 'true' : 'false');
    entry.dataset.ddIndex = String(opt.index);
    entry.disabled = opt.disabled;
    var label = document.createElement('span');
    label.className = 'tofu-dd-item-label';
    label.textContent = opt.label;
    entry.appendChild(label);
    var check = document.createElement('span');
    check.className = 'tofu-dd-item-check';
    check.setAttribute('aria-hidden', 'true');
    check.textContent = '✓';
    entry.appendChild(check);
    entry.addEventListener('click', function() {
      _tofuDdChoose(wrap, opt.index);
    });
    menu.appendChild(entry);
    if (opt.selected) activeEntry = entry;
  });

  document.body.appendChild(menu);
  wrap.classList.add('open');
  var trigger = wrap.querySelector('.tofu-dd-trigger');
  if (trigger) trigger.setAttribute('aria-expanded', 'true');
  _TOFU_DD_OPEN = wrap;
  _tofuDdPosition(wrap, menu);
  if (activeEntry) activeEntry.focus({ preventScroll: true });

  /* Close on outside interaction. A scroll elsewhere detaches a body-anchored
   * menu from its trigger, so close rather than float (same rule as the
   * composer provider menu). Capture so inner-menu scrolling isn't caught. */
  var onDocPointer = function(event) {
    if (!menu.contains(event.target) && !wrap.contains(event.target)) _tofuDdClose();
  };
  var onScroll = function(event) {
    if (!menu.contains(event.target)) _tofuDdClose();
  };
  var onKey = function(event) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      _tofuDdClose();
      var tr = wrap.querySelector('.tofu-dd-trigger');
      if (tr) tr.focus({ preventScroll: true });
    }
  };
  /* Attach the outside-interaction listeners on the NEXT tick. The opening
   * gesture (trigger click / Enter) itself produces trailing pointerdown/click
   * events in the same task; a synchronously-attached capture listener would
   * immediately see that same gesture and close the menu it just opened — the
   * exact bug image-gen.js:240 guards against with setTimeout(0). Escape and
   * item-clicks are handled on the menu/entries directly, so deferring the
   * document-level outside-close is safe. */
  var attached = false;
  var attach = function() {
    if (attached || !menu.isConnected) return;
    attached = true;
    document.addEventListener('pointerdown', onDocPointer, true);
    document.addEventListener('scroll', onScroll, true);
  };
  setTimeout(attach, 0);
  document.addEventListener('keydown', onKey, true);
  /* Stash teardown on the menu so close/remove detaches listeners. */
  menu._tofuDdTeardown = function() {
    attached = true; /* neutralize a pending deferred attach */
    document.removeEventListener('pointerdown', onDocPointer, true);
    document.removeEventListener('scroll', onScroll, true);
    document.removeEventListener('keydown', onKey, true);
  };
}

function _tofuDdChoose(wrap, index) {
  var select = _tofuDdSelectOf(wrap);
  if (!select) { _tofuDdClose(); return; }
  var opt = select.options[index];
  if (!opt || opt.disabled) { _tofuDdClose(); return; }
  if (select.selectedIndex !== index) {
    select.selectedIndex = index;
    /* Fire the same bubbling change the native popup would, so the delegated
     * data-tofu-action-change handler resolves against the select element. */
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  _tofuDdSyncTrigger(wrap, select);
  _tofuDdClose();
  var trigger = wrap.querySelector('.tofu-dd-trigger');
  if (trigger) trigger.focus({ preventScroll: true });
}

function _tofuDdSyncTrigger(wrap, select) {
  var label = wrap.querySelector('.tofu-dd-value');
  if (!label) return;
  var opt = select.options[select.selectedIndex];
  label.textContent = opt ? (opt.textContent || opt.value) : '';
  var placeholder = select.getAttribute('data-dd-placeholder');
  if (!opt && placeholder) {
    label.textContent = placeholder;
    label.classList.add('is-placeholder');
  } else {
    label.classList.remove('is-placeholder');
  }
}

/* Enhance a single select (idempotent): wrap it, hide the native control,
 * and insert the styled trigger after it. */
function _tofuDdEnhanceOne(select) {
  if (!select || select._tofuDdEnhanced) {
    /* Already enhanced — just resync the visible value (options may have
     * been rebuilt underneath by a re-render). */
    if (select && select._tofuDdWrap) _tofuDdSyncTrigger(select._tofuDdWrap, select);
    return;
  }
  var wrap = document.createElement('span');
  wrap.className = 'tofu-dd';
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);
  select.classList.add('tofu-dd-native');
  select.setAttribute('aria-hidden', 'false'); // keep it in the a11y tree
  select.tabIndex = -1; // trigger takes keyboard focus instead

  var trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'tofu-dd-trigger';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  if (select.disabled) trigger.disabled = true;
  var value = document.createElement('span');
  value.className = 'tofu-dd-value';
  trigger.appendChild(value);
  var arrow = document.createElement('span');
  arrow.className = 'tofu-dd-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  trigger.appendChild(arrow);
  trigger.addEventListener('click', function(event) {
    event.preventDefault();
    event.stopPropagation();
    _tofuDdOpen(wrap);
  });
  trigger.addEventListener('keydown', function(event) {
    if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      _tofuDdOpen(wrap);
    }
  });
  wrap.appendChild(trigger);

  select._tofuDdEnhanced = true;
  select._tofuDdWrap = wrap;
  _tofuDdSyncTrigger(wrap, select);
}

/* Re-entrant entry point: enhance every opted-in select under root. Called
 * after each settings re-render; default root is the whole document so
 * callers don't have to thread the node through. The trigger and menu use
 * direct listeners (not data-tofu-action) because the menu is body-anchored
 * and ephemeral; only the underlying select keeps its declarative change
 * action, which is the contract existing handlers rely on. */
function _tofuDdEnhanceAll(root) {
  var scope = root || document;
  var selects = scope.querySelectorAll('select[data-tofu-dd], select.tofu-dd-native');
  Array.from(selects).forEach(_tofuDdEnhanceOne);
}

runtimeScope.TofuDropdown = { enhance: _tofuDdEnhanceAll, close: _tofuDdClose };
