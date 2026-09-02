/* ===== migrated source: orchestration-panel-resize.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-panel-resize.js — persistent desktop rail sizing

   Owns DOM, storage and pointer/keyboard resizing for the two rails. The
   responsive constraint/preference state lives in the pure width model. Panel
   visibility remains in orchestration-panel-layout.js; mobile sheets remain
   in orchestration-studio.js. Widths are projected only through shell CSS
   variables so graph state and panel content never participate.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPanelResizeController(options) {
  options = options || {};
  var doc = options.document || document;
  var win = options.window || window;
  var compactMedia = typeof win.orchestrationCompactMedia === 'function'
    ? win.orchestrationCompactMedia(win) : null;
  var storageKey = options.storageKey
    || 'tofu.orchestration.panel-widths.v1';
  var minCanvasWidth = Number(options.minCanvasWidth) || 360;
  var handleSpace = 12;
  var specs = {
    palette: {
      handleId: 'orchPaletteResize', property: '--orch-palette-width',
      min: 160, max: 360, normal: 212, compact: 160, direction: 1,
    },
    inspector: {
      handleId: 'orchInspectorResize', property: '--orch-inspector-width',
      min: 260, max: 520, normal: 300, compact: 260, direction: -1,
    },
  };
  var bound = false;
  function _shell() { return doc.querySelector('.orch-shell'); }
  function _body() { return doc.querySelector('.orch-body'); }
  function _handle(name) { return doc.getElementById(specs[name].handleId); }
  function _storage() {
    if (Object.prototype.hasOwnProperty.call(options, 'storage')) {
      return options.storage;
    }
    try { return win.localStorage; } catch (error) { return null; }
  }
  function _compact() {
    return !!(compactMedia && compactMedia.matches);
  }
  function _expanded(name) {
    if (typeof options.isExpanded !== 'function') return true;
    try { return options.isExpanded(name) !== false; }
    catch (error) { return true; }
  }
  var model = options.widthModel || createOrchestrationPanelWidthModel({
    specs: specs,
    minCanvasWidth: minCanvasWidth,
    handleSpace: handleSpace,
    compact: _compact,
    isExpanded: _expanded,
    surfaceWidth: _surfaceWidth,
  });

  function _load() {
    var storage = _storage();
    if (!storage || typeof storage.getItem !== 'function') return {};
    try {
      var value = JSON.parse(storage.getItem(storageKey) || '{}');
      return value && typeof value === 'object' ? value : {};
    } catch (error) { return {}; }
  }

  function _persist() {
    var storage = _storage();
    if (!storage) return;
    var value = model.persisted();
    try {
      if (Object.keys(value).length && typeof storage.setItem === 'function') {
        storage.setItem(storageKey, JSON.stringify(value));
      } else if (typeof storage.removeItem === 'function') {
        storage.removeItem(storageKey);
      }
    } catch (error) { /* Storage is an optional enhancement. */ }
  }

  function _surfaceWidth() {
    var element = _body() || _shell();
    if (!element) return 0;
    var rect = typeof element.getBoundingClientRect === 'function'
      ? element.getBoundingClientRect() : null;
    return Math.max(0, Number(rect && rect.width) || element.clientWidth || 0);
  }

  function _syncHandle(name) {
    var handle = _handle(name), bounds = model.bounds(name);
    if (!handle) return;
    handle.setAttribute('aria-valuemin', String(bounds.min));
    handle.setAttribute('aria-valuemax', String(bounds.max));
    var width = model.snapshot()[name];
    handle.setAttribute('aria-valuenow', String(width));
    handle.setAttribute('aria-valuetext', width + ' px');
  }

  function _project(name, width, notify) {
    var shell = _shell();
    if (!shell) return 0;
    shell.style.setProperty(specs[name].property, width + 'px');
    _syncHandle(name);
    _syncHandle(name === 'palette' ? 'inspector' : 'palette');
    if (notify && typeof options.onChange === 'function') {
      options.onChange(name, width, snapshot());
    }
    return width;
  }

  function snapshot() { return model.snapshot(); }

  function setWidth(name, value, save) {
    if (!specs[name]) return 0;
    var width = _project(name, model.setWidth(name, value), true);
    if (save !== false) _persist();
    return width;
  }

  function reset(name) {
    if (!specs[name]) return 0;
    var width = _project(name, model.reset(name), true);
    _persist();
    return width;
  }

  function sync() {
    var next = model.sync();
    _project('palette', next.palette, false);
    _project('inspector', next.inspector, false);
    return snapshot();
  }

  function _bindHandle(name) {
    var handle = _handle(name);
    if (!handle) return;
    var spec = specs[name];
    handle.addEventListener('pointerdown', function (event) {
      if (typeof event.button === 'number' && event.button !== 0) return;
      event.preventDefault();
      var startX = Number(event.clientX) || 0;
      var startWidth = snapshot()[name];
      model.setWidth(name, startWidth);
      var shell = _shell();
      if (shell) shell.classList.add('orch-panel-resizing');
      if (typeof handle.setPointerCapture === 'function'
          && event.pointerId != null) {
        try { handle.setPointerCapture(event.pointerId); } catch (error) {}
      }
      function move(moveEvent) {
        var delta = ((Number(moveEvent.clientX) || 0) - startX)
          * spec.direction;
        _project(name, model.setWidth(name, startWidth + delta), true);
      }
      var unbindPointer = function () {};
      function finish() {
        unbindPointer();
        if (shell) shell.classList.remove('orch-panel-resizing');
        _persist();
      }
      unbindPointer = bindOrchestrationPointerSession({
        pointerId: event.pointerId, moveTarget: doc, pointerTarget: doc,
        captureTarget: handle, window: win, onMove: move, onEnd: finish,
      });
    });
    handle.addEventListener('keydown', function (event) {
      var bounds = model.bounds(name);
      var step = event.shiftKey ? 32 : 12;
      var value = null;
      var width = snapshot()[name];
      if (event.key === 'ArrowLeft') {
        value = width - step * spec.direction;
      } else if (event.key === 'ArrowRight') {
        value = width + step * spec.direction;
      } else if (event.key === 'Home') {
        value = bounds.min;
      } else if (event.key === 'End') {
        value = bounds.max;
      }
      if (value == null) return;
      event.preventDefault();
      setWidth(name, value);
    });
    handle.addEventListener('dblclick', function () { reset(name); });
  }

  function bind() {
    if (bound || !_shell()) return false;
    bound = true;
    var stored = _load();
    model.hydrate(stored);
    sync();
    _bindHandle('palette');
    _bindHandle('inspector');
    if (win.addEventListener) win.addEventListener('resize', sync);
    return true;
  }

  return {
    bind: bind,
    sync: sync,
    setWidth: setWidth,
    reset: reset,
    snapshot: snapshot,
  };
}

