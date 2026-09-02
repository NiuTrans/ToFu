/* ===== migrated source: orchestration-popup-menu.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-popup-menu.js — shared Studio popup-menu behavior

   Owns visibility/ARIA synchronization, trigger focus restoration and the
   Arrow/Home/End/Escape keyboard model for both template and stored-flow
   menus. Dynamic stored rows are discovered at interaction time.

   MUST load before orchestration-shell.js and orchestration-workspace.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPopupMenuController(options) {
  options = options || {};
  var doc = options.document || document;
  var registered = [];

  function _element(id) {
    return id ? doc.getElementById(id) : null;
  }

  function _items(menu) {
    if (!menu) return [];
    return Array.prototype.filter.call(
      menu.querySelectorAll('[role="menuitem"]:not([disabled])'),
      function (item) {
        return item.style.display !== 'none'
          && item.getAttribute('aria-hidden') !== 'true';
      }
    );
  }

  function syncItems(menuId, preferred) {
    var menu = _element(menuId);
    var items = _items(menu);
    var active = doc.activeElement;
    var current = items.indexOf(preferred) >= 0 ? preferred
      : items.indexOf(active) >= 0 ? active
        : items.filter(function (item) { return item.tabIndex === 0; })[0]
          || items[0] || null;
    if (menu) {
      Array.prototype.forEach.call(
        menu.querySelectorAll('[role="menuitem"]'), function (item) {
          item.tabIndex = item === current ? 0 : -1;
        }
      );
    }
    return current;
  }

  function isOpen(menuId) {
    var menu = _element(menuId);
    return !!menu && menu.style.display !== 'none';
  }

  function setOpen(menuId, triggerId, open, opts) {
    opts = opts || {};
    var menu = _element(menuId);
    var trigger = _element(triggerId);
    var active = doc.activeElement;
    if (menu) menu.style.display = open ? 'block' : 'none';
    if (open) syncItems(menuId);
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open && opts.restoreFocus !== false && menu && trigger
        && (opts.restoreFocus === true || menu.contains(active))
        && typeof trigger.focus === 'function') {
      trigger.focus();
    }
    return !!open;
  }

  function focusEdge(menuId, last) {
    var items = _items(_element(menuId));
    var item = items[last ? items.length - 1 : 0];
    syncItems(menuId, item);
    if (item && typeof item.focus === 'function') item.focus();
    return item || null;
  }

  function _remember(binding) {
    var exists = registered.some(function (item) {
      return item.menuId === binding.menuId;
    });
    if (!exists) registered.push(binding);
  }

  function _close(bindings, opts) {
    var closed = false;
    (bindings || []).forEach(function (binding) {
      if (!isOpen(binding.menuId)) return;
      closed = true;
      setOpen(binding.menuId, binding.triggerId, false, opts);
    });
    return closed;
  }

  function closeAll(opts) {
    return _close(registered, opts);
  }

  function _requestOpen(binding, last) {
    var pending = isOpen(binding.menuId)
      ? true : (typeof binding.open === 'function' ? binding.open() : false);
    return Promise.resolve(pending).then(function () {
      if (!isOpen(binding.menuId)) return null;
      return focusEdge(binding.menuId, last);
    });
  }

  function _triggerKey(event, binding) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      _requestOpen(binding, event.key === 'ArrowUp');
      return;
    }
    if (event.key === 'Escape' && isOpen(binding.menuId)) {
      event.preventDefault();
      event.stopPropagation();
      setOpen(binding.menuId, binding.triggerId, false);
    }
  }

  function _menuKey(event, binding) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setOpen(binding.menuId, binding.triggerId, false);
      return;
    }
    if (event.key === 'Tab') {
      setOpen(binding.menuId, binding.triggerId, false);
      return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(event.key) === -1) {
      return;
    }
    var menu = _element(binding.menuId);
    var items = _items(menu);
    if (!items.length) return;
    event.preventDefault();
    var current = items.indexOf(doc.activeElement);
    var index;
    if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = items.length - 1;
    else if (event.key === 'ArrowDown') index = (current + 1) % items.length;
    else index = (current <= 0 ? items.length : current) - 1;
    syncItems(binding.menuId, items[index]);
    items[index].focus();
  }

  function bind(boundary, bindings) {
    (bindings || []).forEach(function (binding) {
      _remember(binding);
      var trigger = boundary && boundary.querySelector('#' + binding.triggerId);
      var menu = boundary && boundary.querySelector('#' + binding.menuId);
      if (trigger) trigger.addEventListener('keydown', function (event) {
        _triggerKey(event, binding);
      });
      if (menu) menu.addEventListener('keydown', function (event) {
        _menuKey(event, binding);
      });
      if (menu) menu.addEventListener('focusin', function (event) {
        var item = event.target && event.target.closest
          ? event.target.closest('[role="menuitem"]') : null;
        if (item && menu.contains(item)) syncItems(binding.menuId, item);
      });
      syncItems(binding.menuId);
    });
    if (boundary) boundary.addEventListener('click', function (event) {
      var target = event.target;
      var insidePopup = (bindings || []).some(function (binding) {
        var trigger = _element(binding.triggerId);
        var menu = _element(binding.menuId);
        return !!target && ((trigger && trigger.contains(target))
          || (menu && menu.contains(target)));
      });
      if (!insidePopup) _close(bindings, {restoreFocus: false});
    });
  }

  return {
    bind: bind,
    closeAll: closeAll,
    focusEdge: focusEdge,
    isOpen: isOpen,
    setOpen: setOpen,
    syncItems: syncItems,
  };
}

