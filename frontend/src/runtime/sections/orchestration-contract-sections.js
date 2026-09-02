/* ===== migrated source: orchestration-contract-sections.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-contract-sections.js — detached contract-section store

   The authoring response carries several independently consumed policy
   documents. The authoring-contract validator owns their shared name registry;
   this data-driven store owns immutable adoption and completeness state.

   Pure state only: no DOM, transport or localization dependencies.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationContractSectionStore(options) {
  options = options || {};
  var keys = Array.isArray(options.keys)
    ? options.keys.slice() : ORCHESTRATION_AUTHORING_OBJECT_SECTIONS.slice();
  var required = Array.isArray(options.required)
    ? options.required.slice() : keys.slice();
  var values = Object.create(null);

  function _clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function _record(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function adopt(source) {
    source = source && typeof source === 'object' ? source : {};
    keys.forEach(function (key) {
      if (_record(source[key])) values[key] = _clone(source[key]);
    });
    return snapshot();
  }

  function get(key) {
    return keys.indexOf(key) >= 0 ? _clone(values[key] || null) : null;
  }

  function has(key) {
    return _record(values[key]) && Object.keys(values[key]).length > 0;
  }

  function missing() {
    return required.filter(function (key) { return !has(key); });
  }

  function ready() {
    return missing().length === 0;
  }

  function snapshot() {
    var result = {};
    keys.forEach(function (key) { result[key] = get(key); });
    return result;
  }

  adopt(options.initial);
  return {
    adopt: adopt,
    get: get,
    has: has,
    missing: missing,
    ready: ready,
    snapshot: snapshot,
  };
}

