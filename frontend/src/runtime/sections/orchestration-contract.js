/* ===== migrated source: orchestration-contract.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-contract.js — backend authoring-contract controller

   Owns immutable response adoption and backend/frontend catalogue merge.
   The injected loader owns status-aware transport and rolling fallback. It is
   deliberately DOM-free: orchestration.js receives one onChange callback
   and remains a renderer/editor instead of a transport/schema coordinator.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationAuthoringContractController(options) {
  options = options || {};

  function _clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  var _catalogue = createOrchestrationCatalogueProjection({
    roles: options.roles,
    controls: options.controls,
    translate: options.translate,
  });
  var initialSections = {};
  ORCHESTRATION_AUTHORING_OBJECT_SECTIONS.forEach(function (name) {
    initialSections[name] = options[name];
  });
  var _sections = createOrchestrationContractSectionStore({
    initial: initialSections,
  });
  var _generic = _clone(options.genericRoleSchema || []);
  var _contractSections = _clone(options.contractSections || null);
  var _loader = createOrchestrationContractLoader({
    api: options.api,
    normalizeRead: options.normalizeRead,
    ready: ready,
    apply: apply,
    snapshot: snapshot,
    onError: options.onError,
  });

  function snapshot() {
    var current = _sections.snapshot();
    var catalogue = _catalogue.snapshot();
    current.roleSchemas = current.roles;
    current.roles = catalogue.roles;
    current.controls = catalogue.controls;
    current.genericRoleSchema = _clone(_generic);
    current.executionOptions = current.executionOptions || {};
    current.nodeDefaults = current.nodeDefaults || {};
    current.requestLimits = current.requestLimits || {};
    current.contractSections = _clone(_contractSections);
    current.ready = ready();
    current.settled = settled();
    current.error = _clone(_loader.error());
    return current;
  }

  function ready() {
    var ioReady = !options.ioTools
      || (typeof options.ioTools.getContract === 'function'
          && !!options.ioTools.getContract());
    return _sections.ready() && _generic.length > 0 && ioReady;
  }

  function settled() {
    return _loader.settled();
  }

  function apply(contract) {
    if (!contract || typeof contract !== 'object') return snapshot();
    _sections.adopt(contract);
    _contractSections = _clone(contract.contractSections || null);
    if (Array.isArray(contract.generic) && contract.generic.length) {
      _generic = _clone(contract.generic);
    }
    if (options.ioTools && typeof options.ioTools.setContract === 'function') {
      var ioContract = _sections.get('ioContract');
      if (ioContract) {
        options.ioTools.setContract(ioContract);
      }
    }
    _catalogue.adopt({
      roleNames: contract.roleNames,
      personas: _sections.get('personas'),
      nodeDefaults: _sections.get('nodeDefaults'),
      controls: contract.controls,
    });

    var current = snapshot();
    if (typeof options.onChange === 'function') options.onChange(current);
    return current;
  }

  function load() {
    return _loader.load();
  }

  function roleFields(role) {
    var schemas = _sections.get('roles');
    var fields = schemas && schemas[role];
    return _clone(fields || _generic);
  }

  function controlFields(kind) {
    var schemas = _sections.get('controlSchemas');
    return _clone((schemas && schemas[kind]) || []);
  }

  function fieldSpec(ownerType, ownerName, key) {
    var fields = ownerType === 'role' ? roleFields(ownerName)
      : ownerType === 'control' ? controlFields(ownerName) : [];
    var match = fields.filter(function (spec) {
      return spec && spec.key === key;
    })[0];
    return _clone(match || null);
  }

  function persona(role) {
    var personas = _sections.get('personas');
    return _clone((personas && personas[role]) || null);
  }

  function defaultEmits(role) {
    var defaults = _sections.get('defaultEmits');
    return defaults && defaults[role] ? defaults[role] : '';
  }

  function section(name) { return _sections.get(name); }
  function executionOptions() {
    return section('executionOptions') || {};
  }
  function requestLimits() { return section('requestLimits') || {}; }

  function blankSubflowDefinition() {
    var defaults = _sections.get('nodeDefaults') || {};
    return _clone(defaults.blankSubflow);
  }

  function nodeParams(payload) {
    payload = payload || {};
    var defaults = _sections.get('nodeDefaults') || {};
    if (payload.ptype === 'role') {
      var roleParams = defaults.roles && defaults.roles[payload.role];
      return _clone(roleParams || defaults.genericRole || {});
    }
    if (payload.ptype === 'subflow') {
      var subflow = _clone(defaults.subflow || {});
      subflow.definition = blankSubflowDefinition();
      return subflow;
    }
    var controls = defaults.controls || {};
    return _clone(controls[payload.kind] || {});
  }

  var controller = {
    apply: apply,
    load: load,
    ready: ready,
    settled: settled,
    snapshot: snapshot,
    roleFields: roleFields,
    controlFields: controlFields,
    fieldSpec: fieldSpec,
    persona: persona,
    defaultEmits: defaultEmits,
    section: section,
    executionOptions: executionOptions,
    requestLimits: requestLimits,
    nodeParams: nodeParams,
    blankSubflowDefinition: blankSubflowDefinition,
  };
  ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS.concat(
    Object.keys(ORCHESTRATION_AUTHORING_WIRE_SECTIONS)
  ).forEach(function (name) {
    if (Object.prototype.hasOwnProperty.call(controller, name)) return;
    controller[name] = function () { return section(name); };
  });
  return controller;
}
