/* ===== migrated source: orchestration-inspector-focus.js ===== */
/* Semantic focus continuity for Inspector forms that repaint after edits. */

function createOrchestrationInspectorFocusController(options) {
  options = options || {};
  var validity = options.fieldValidity
    || createOrchestrationFieldValidity();
  var attributes = [
    'data-orch-param-key', 'data-input-index',
    'data-orch-io-action', 'data-orch-io-side', 'data-orch-io-index',
    'data-orch-io-key', 'data-orch-io-preset',
    'data-orch-inspector-action',
  ];

  function _document() { return options.document || document; }

  function capture(root) {
    var active = _document().activeElement;
    if (!root || !active || !root.contains(active)) return null;
    var snapshot = {};
    attributes.forEach(function (name) {
      if (active.hasAttribute(name)) {
        snapshot[name] = active.getAttribute(name) || '';
      }
    });
    return Object.keys(snapshot).length ? snapshot : null;
  }

  function _matching(root, selector, snapshot, ignored) {
    var ignoredNames = Array.isArray(ignored) ? ignored : [ignored || ''];
    return Array.prototype.filter.call(root.querySelectorAll(selector),
      function (control) {
        return attributes.every(function (name) {
          return ignoredNames.indexOf(name) !== -1
            || !Object.prototype.hasOwnProperty.call(
            snapshot, name) || control.getAttribute(name) === snapshot[name];
        });
      });
  }

  function _ioTarget(root, snapshot) {
    var action = snapshot['data-orch-io-action'];
    var controls = _matching(
      root, '[data-orch-io-action]', snapshot,
      action === 'add' ? ['data-orch-io-action']
        : action === 'remove'
          ? ['data-orch-io-action', 'data-orch-io-index'] : []
    );
    if (action === 'add') {
      var additions = controls.filter(function (control) {
        return control.getAttribute('data-orch-io-action') === 'set'
          && control.getAttribute('data-orch-io-key') === 'name';
      });
      return additions[additions.length - 1]
        || _matching(root, '[data-orch-io-action="add"]', snapshot)[0];
    }
    if (action === 'remove') {
      var removals = controls.filter(function (control) {
        return control.getAttribute('data-orch-io-action') === 'remove';
      });
      var oldIndex = Number(snapshot['data-orch-io-index']);
      return removals[Math.min(oldIndex, removals.length - 1)]
        || _matching(root, '[data-orch-io-action="add"]', snapshot,
          ['data-orch-io-action', 'data-orch-io-index'])[0];
    }
    return controls[0] || null;
  }

  function restore(root, snapshot) {
    if (!root || !snapshot) return null;
    var target = null;
    if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-orch-param-key')) {
      target = _matching(root, '[data-orch-param-key]', snapshot)[0];
    } else if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-input-index')) {
      target = _matching(root, '[data-input-index]', snapshot)[0];
    } else if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-orch-io-action')) {
      target = _ioTarget(root, snapshot);
    } else if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-orch-inspector-action')) {
      target = _matching(root, '[data-orch-inspector-action]', snapshot)[0];
    }
    if (target && typeof target.focus === 'function') target.focus();
    return target || null;
  }

  function clearDiagnostic() {
    validity.clearDiagnostics(_document());
  }

  function _diagnosticField(root, target) {
    if (!target || !target.field) return null;
    if (target.field.kind === 'document-name') {
      return _document().getElementById('orchNameInput');
    }
    if (!root) return null;
    if (target.field.kind === 'param') {
      return _matching(root, '[data-orch-param-key]', {
        'data-orch-param-key': target.field.key,
      })[0] || null;
    }
    if (target.field.kind === 'io-section') {
      return _matching(root, '[data-orch-io-action]', {
        'data-orch-io-side': target.field.side || '',
      }, target.field.side ? [] : ['data-orch-io-side'])[0] || null;
    }
    if (target.field.kind !== 'io') return null;
    return _matching(root, '[data-orch-io-action="set"]', {
      'data-orch-io-action': 'set',
      'data-orch-io-side': target.field.side,
      'data-orch-io-index': String(target.field.index),
      'data-orch-io-key': target.field.key,
    })[0] || null;
  }

  function focusDiagnostic(root, target, diagnostic, scrollBehavior,
                           descriptionId) {
    clearDiagnostic();
    var field = _diagnosticField(root, target);
    if (!field) return null;
    var section = typeof field.closest === 'function'
      ? field.closest('details[data-orch-section-key]') : null;
    if (section) section.open = true;
    var severity = diagnostic && diagnostic.severity === 'warning'
      ? 'warning' : 'error';
    validity.setDiagnostic(field, severity, descriptionId);
    if (typeof field.focus === 'function') {
      try { field.focus({ preventScroll: true }); }
      catch (_error) { field.focus(); }
    }
    if (typeof field.scrollIntoView === 'function') {
      field.scrollIntoView({
        block: 'center', behavior: scrollBehavior === 'smooth' ? 'smooth' : 'auto',
      });
    }
    return field;
  }

  return {
    capture: capture,
    restore: restore,
    focusDiagnostic: focusDiagnostic,
    clearDiagnostic: clearDiagnostic,
  };
}

