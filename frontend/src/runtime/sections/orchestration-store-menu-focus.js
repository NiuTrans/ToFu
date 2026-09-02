/* ===== migrated source: orchestration-store-menu-focus.js ===== */
/* Semantic focus continuity for the dynamically repainted saved-flow menu. */

function createOrchestrationStoreMenuFocusController(options) {
  options = options || {};
  var entries = [];
  var intent = null;

  function _document() { return options.document || document; }
  function _menu() { return _document().getElementById('orchLoadMenu'); }

  function _snapshot() {
    var menu = _menu();
    var active = _document().activeElement;
    if (!menu || !active || !menu.contains(active)) return null;
    var attribute = active.hasAttribute('data-delete-index')
      ? 'data-delete-index' : active.hasAttribute('data-load-index')
        ? 'data-load-index' : null;
    if (!attribute) return null;
    var index = Number(active.getAttribute(attribute));
    var entry = entries[index];
    return entry ? {
      action: attribute === 'data-delete-index' ? 'delete' : 'load',
      id: entry.id,
      index: index,
    } : null;
  }

  function remember() {
    intent = _snapshot() || intent;
    return intent;
  }

  function stage(entry, index, action) {
    intent = { action: action, id: entry.id, index: index };
    return intent;
  }

  function clear(token) {
    if (intent === token) intent = null;
  }

  function cancel() { intent = null; }

  function render(nextEntries) {
    var menu = _menu();
    var saved = _snapshot() || intent;
    var target = null;
    intent = null;
    entries = nextEntries.slice();
    if (menu && saved && entries.length) {
      var index = entries.findIndex(function (entry) {
        return entry.id === saved.id;
      });
      var action = saved.action;
      if (index < 0) {
        index = Math.min(saved.index, entries.length - 1);
        action = 'load';
      }
      target = menu.querySelector('[' + (action === 'delete'
        ? 'data-delete-index' : 'data-load-index') + '="' + index + '"]');
    }
    if (options.popupMenus
        && typeof options.popupMenus.syncItems === 'function') {
      options.popupMenus.syncItems('orchLoadMenu', target);
    }
    if (target && options.popupMenus.isOpen('orchLoadMenu')) target.focus();
    return target;
  }

  function finishMessage() {
    var restore = !!intent;
    intent = null;
    entries = [];
    if (options.popupMenus
        && typeof options.popupMenus.syncItems === 'function') {
      options.popupMenus.syncItems('orchLoadMenu');
    }
    if (restore) {
      options.popupMenus.setOpen(
        'orchLoadMenu', 'orchLoadBtn', false, { restoreFocus: true });
    }
    return restore;
  }

  return {
    cancel: cancel,
    clear: clear,
    finishMessage: finishMessage,
    remember: remember,
    render: render,
    stage: stage,
  };
}

