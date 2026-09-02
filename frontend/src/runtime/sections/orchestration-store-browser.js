/* ===== migrated source: orchestration-store-browser.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-store-browser.js — saved-flow menu controller

   Owns list request fencing and safe DOM event bindings.
   Saved-flow formatting and HTML projection live in the presentation sibling.
   Load/delete commands remain injected; this module never mutates the draft.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationStoreBrowser(options) {
  options = options || {};
  var popupMenus = options.popupMenus;
  var requestGeneration = 0;
  var presentation = options.presentation
    || createOrchestrationStoreBrowserPresentation(options);
  var menuFocus = createOrchestrationStoreMenuFocusController({
    document: options.document, popupMenus: popupMenus });
  var definitions = options.definitions ||
    createOrchestrationDefinitionRequestClient({ api: options.api });

  function _document() { return options.document || document; }
  function _menu() { return _document().getElementById('orchLoadMenu'); }
  function _isOpen() {
    return !!popupMenus && popupMenus.isOpen('orchLoadMenu');
  }
  function _setOpen(open, opts) {
    return popupMenus ? popupMenus.setOpen(
      'orchLoadMenu', 'orchLoadBtn', open, opts) : false;
  }

  function _message(menu, key, params) {
    menu.innerHTML = presentation.messageHtml(key, params);
  }

  function _runAction(button, action) {
    if (!button || button.disabled || typeof action !== 'function') return null;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    var result;
    try { result = action(); }
    catch (error) { result = Promise.reject(error); }
    return Promise.resolve(result).catch(function (error) {
      reportOrchestrationDiagnostic(options.onError, 'row-action', error);
      return null;
    }).finally(function () {
      // Successful load/delete usually replaces or hides this row. Only
      // restore a control that still belongs to the live menu DOM.
      if (!button.isConnected) return;
      button.disabled = false;
      button.removeAttribute('aria-busy');
    });
  }

  function _renderRows(menu, entries) {
    var currentId = typeof options.currentId === 'function'
      ? options.currentId() : null;
    menu.innerHTML = presentation.rowsHtml(entries, currentId);
    Array.prototype.forEach.call(
      menu.querySelectorAll('[data-load-index]'), function (button) {
        var entry = entries[Number(button.getAttribute('data-load-index'))];
        button.addEventListener('click', function () {
          _runAction(button, function () {
            return typeof options.onLoad === 'function'
              ? options.onLoad(entry.id) : null;
          });
        });
      }
    );
    Array.prototype.forEach.call(
      menu.querySelectorAll('[data-delete-index]'), function (button) {
        var index = Number(button.getAttribute('data-delete-index'));
        var entry = entries[index];
        button.addEventListener('click', function (event) {
          var intent = menuFocus.stage(entry, index, 'delete');
          var action = _runAction(button, function () {
            return typeof options.onDelete === 'function'
              ? options.onDelete(
                entry.id, event,
                entry.definitionVersion == null
                  ? entry.updatedAt : entry.definitionVersion) : null;
          });
          Promise.resolve(action).finally(function () {
            menuFocus.clear(intent);
          });
        });
      }
    );
    menuFocus.render(entries);
  }

  function close(opts) {
    requestGeneration += 1; menuFocus.cancel();
    var menu = _menu();
    if (menu) menu.setAttribute('aria-busy', 'false');
    _setOpen(false, opts);
    return false;
  }

  async function open(forceOpen) {
    var menu = _menu();
    if (!menu) return [];
    if (forceOpen !== true && _isOpen()) {
      close();
      return [];
    }
    var generation = ++requestGeneration;
    menuFocus.remember();
    if (popupMenus) popupMenus.setOpen('orchTplMenu', 'orchTplBtn', false);
    _setOpen(true);
    menu.setAttribute('aria-busy', 'true');
    _message(menu, 'orch.load.loading');
    function stillCurrent() {
      return generation === requestGeneration && _isOpen();
    }
    function settleBusy() {
      if (generation === requestGeneration) {
        menu.setAttribute('aria-busy', 'false');
      }
    }
    if (!definitions.canList()) {
      if (stillCurrent()) {
        _message(menu, 'orch.load.failed', {
          error: typeof options.translate === 'function'
            ? options.translate('orch.api.unavailable')
            : 'orch.api.unavailable',
        });
        menuFocus.finishMessage();
      }
      settleBusy();
      return [];
    }
    var result = await definitions.list();
    if (result.cause) {
      reportOrchestrationDiagnostic(options.onError, 'list', result.cause);
    }
    if (!result.ok) {
      if (stillCurrent()) {
        _message(menu, 'orch.load.failed', {
          error: result.error || (typeof options.translate === 'function'
            ? options.translate(orchestrationRequestFailureKey(result))
            : orchestrationRequestFailureKey(result)),
        });
        menuFocus.finishMessage();
      }
      settleBusy();
      return [];
    }
    var list = result.items;
    if (!stillCurrent()) {
      settleBusy();
      return [];
    }
    // definition-list/v1 is already newest-first. Retain this stable client
    // sort only for rolling servers that still return repository order.
    var entries = list.filter(function (entry) {
      return entry && entry.id != null;
    }).slice();
    if (!result.canonical) {
      entries.sort(function (left, right) {
        return (right.updatedAt || 0) - (left.updatedAt || 0);
      });
    }
    if (!entries.length) {
      _message(menu, 'orch.load.empty');
      settleBusy();
      menuFocus.finishMessage();
      return [];
    }
    _renderRows(menu, entries);
    settleBusy();
    return entries;
  }

  return {
    open: open,
    close: close,
    isOpen: _isOpen,
  };
}

