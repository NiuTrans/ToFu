/* ===== migrated source: orchestration-field-validity.js ===== */
/* Multi-source Inspector field validity projected onto accessible DOM state. */

function createOrchestrationFieldValidity() {
  function _has(field, name) {
    return typeof field.hasAttribute === 'function'
      ? field.hasAttribute(name) : field.getAttribute(name) != null;
  }

  function _sync(field) {
    var invalid = _has(field, 'data-orch-local-invalid')
      || field.getAttribute('data-orch-diagnostic-focus') === 'error';
    if (invalid) field.setAttribute('aria-invalid', 'true');
    else field.removeAttribute('aria-invalid');
    return invalid;
  }

  function _description(field, ownerAttribute, descriptionId) {
    var owned = field.getAttribute(ownerAttribute) || '';
    var tokens = String(field.getAttribute('aria-describedby') || '')
      .split(/\s+/).filter(function (token) {
        return token && token !== owned;
      });
    if (descriptionId && tokens.indexOf(descriptionId) < 0) {
      tokens.push(descriptionId);
      field.setAttribute(ownerAttribute, descriptionId);
    } else field.removeAttribute(ownerAttribute);
    if (tokens.length) field.setAttribute('aria-describedby', tokens.join(' '));
    else field.removeAttribute('aria-describedby');
  }

  function setLocal(field, accepted, descriptionId, code) {
    if (!field) return !!accepted;
    if (accepted) {
      field.removeAttribute('data-orch-local-invalid');
      field.removeAttribute('data-orch-local-failure-code');
    } else {
      field.setAttribute('data-orch-local-invalid', 'true');
      if (code) field.setAttribute('data-orch-local-failure-code', code);
      else field.removeAttribute('data-orch-local-failure-code');
    }
    _description(field, 'data-orch-local-description-id',
      accepted ? '' : descriptionId || '');
    _sync(field);
    return !!accepted;
  }

  function setDiagnostic(field, severity, descriptionId) {
    if (!field) return false;
    if (severity) {
      field.setAttribute('data-orch-diagnostic-focus', severity);
    } else field.removeAttribute('data-orch-diagnostic-focus');
    _description(field, 'data-orch-diagnostic-description-id',
      descriptionId || '');
    _sync(field);
    return true;
  }

  function clearDiagnostics(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    Array.prototype.forEach.call(
      root.querySelectorAll('[data-orch-diagnostic-focus]'),
      function (field) { setDiagnostic(field, ''); });
  }

  return Object.freeze({ setLocal: setLocal, setDiagnostic: setDiagnostic,
    clearDiagnostics: clearDiagnostics });
}

