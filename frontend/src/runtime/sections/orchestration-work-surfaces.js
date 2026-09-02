/* ===== migrated source: orchestration-work-surfaces.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-work-surfaces.js — exclusive Studio work surfaces

   Coordinates Composer and Run through one open/close/isOpen port. The
   admitOpen hook also provides the shared handoff contract for
   other exclusive surfaces. Rail layout, DOM projection, focus restoration
   and feature state stay in their focused controllers.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationWorkSurfaceController(options) {
  options = options || {};
  var names = Array.isArray(options.names)
    ? options.names.slice() : ['composer', 'run'];
  var dismissOrder = Array.isArray(options.dismissOrder)
    ? options.dismissOrder.slice() : names.slice().reverse();

  function _surface(name) {
    if (names.indexOf(name) < 0) return null;
    var candidate = options.surfaces && options.surfaces[name];
    return typeof candidate === 'function' ? candidate() : candidate;
  }

  function _isOpen(surface) {
    return !!(surface && typeof surface.isOpen === 'function'
      && surface.isOpen());
  }

  function isOpen(name) {
    return _isOpen(_surface(name));
  }

  function close(name) {
    var surface = _surface(name);
    if (!surface || typeof surface.close !== 'function') return false;
    if (!_isOpen(surface)) return true;
    surface.close();
    return !_isOpen(surface);
  }

  function open(name) {
    var target = _surface(name);
    if (!target || typeof target.open !== 'function') return false;
    var otherOpen = names.some(function (other) {
      return other !== name && isOpen(other);
    });
    if (_isOpen(target) && !otherOpen) return true;
    if (typeof options.admitOpen === 'function'
        && options.admitOpen(name) === false) return false;
    for (var i = 0; i < names.length; i++) {
      var other = names[i];
      if (other !== name && isOpen(other) && !close(other)) return false;
    }
    if (!_isOpen(target)) target.open();
    return _isOpen(target);
  }

  function toggle(name) {
    if (isOpen(name)) { close(name); return isOpen(name); }
    return open(name);
  }

  function dismiss() {
    for (var i = 0; i < dismissOrder.length; i++) {
      if (isOpen(dismissOrder[i])) {
        return close(dismissOrder[i]);
      }
    }
    return false;
  }

  function active() {
    for (var i = 0; i < names.length; i++) {
      if (isOpen(names[i])) return names[i];
    }
    return null;
  }

  return Object.freeze({
    active: active,
    isOpen: isOpen,
    open: open,
    close: close,
    toggle: toggle,
    dismiss: dismiss,
  });
}

