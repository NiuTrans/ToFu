/* ===== migrated source: orchestration-authoring-catalogue-validation.js ===== */
/* Cross-section closure checks for the backend-owned authoring catalogue. */

function _authoringRegistryMatches(names, registry, path, missing) {
  if (!_orchestrationContractRecord(registry)) return;
  names.forEach(function (name) {
    if (typeof name === 'string'
        && !Object.prototype.hasOwnProperty.call(registry, name)) {
      missing.push(path + '.' + name);
    }
  });
  Object.keys(registry).forEach(function (name) {
    if (names.indexOf(name) < 0) missing.push(path + '.' + name);
  });
}

function _authoringSafeNames(value, path, missing) {
  if (!_orchestrationRequireStringVocabulary(value, path, missing)) {
    return false;
  }
  if (value.some(function (name) {
    return !/^[A-Za-z][A-Za-z0-9_]*$/.test(name);
  })) {
    missing.push(path);
    return false;
  }
  return true;
}

function _validateAuthoringRoleLinks(body, missing) {
  if (!_authoringSafeNames(body.roleNames, 'roleNames', missing)) return;
  var roleNames = body.roleNames;
  ['roles', 'personas', 'defaultEmits'].forEach(function (path) {
    _authoringRegistryMatches(roleNames, body[path], path, missing);
  });
  var defaults = body.nodeDefaults && body.nodeDefaults.roles;
  _authoringRegistryMatches(roleNames, defaults, 'nodeDefaults.roles', missing);
  var tiers = body.executionOptions && body.executionOptions.tiers;
  var emits = body.executionOptions && body.executionOptions.emits;
  roleNames.forEach(function (role) {
    var persona = body.personas && body.personas[role];
    var node = defaults && defaults[role];
    var emit = body.defaultEmits && body.defaultEmits[role];
    if (persona && (tiers || []).indexOf(persona.tier) < 0) {
      missing.push('personas.' + role + '.tier');
    }
    if (persona && node && persona.tier !== node.tier) {
      missing.push('nodeDefaults.roles.' + role + '.tier');
    }
    if (typeof emit !== 'string' || (emits || []).indexOf(emit) < 0) {
      missing.push('defaultEmits.' + role);
    }
  });
}

function _validateAuthoringControlLinks(body, missing) {
  if (!_orchestrationContractRecord(body.controls)) return;
  var names = Object.keys(body.controls);
  if (!names.length || names.some(function (name) {
    return !/^[A-Za-z][A-Za-z0-9_]*$/.test(name);
  })) missing.push('controls');
  names.forEach(function (name) {
    var spec = body.controls[name];
    if (!_orchestrationContractRecord(spec)
        || typeof spec.single !== 'boolean') {
      missing.push('controls.' + name + '.single');
    }
  });
  _authoringRegistryMatches(
    names, body.controlSchemas, 'controlSchemas', missing);
  _authoringRegistryMatches(
    names, body.nodeDefaults && body.nodeDefaults.controls,
    'nodeDefaults.controls', missing);
}

function _validateAuthoringIoLinks(body, missing) {
  _authoringSafeNames(body.builtins, 'builtins', missing);
  var contractTypes = body.ioContract && body.ioContract.types;
  var contractValid = _authoringSafeNames(
    contractTypes, 'ioContract.types', missing);
  var output = body.ioContract && body.ioContract.defaultOutput;
  if (!_orchestrationContractRecord(output)) return;
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA.ioContract
    .defaultOutputStringFields.forEach(function (field) {
      _orchestrationRequireString(
        output[field], 'ioContract.defaultOutput.' + field, missing);
      if (contractValid && contractTypes.indexOf(output[field]) < 0) {
        missing.push('ioContract.defaultOutput.' + field);
      }
    });
}

function _validateAuthoringRuntimeLinks(body, missing) {
  var eventTypes = body.eventContract && body.eventContract.types;
  var runStatuses = body.runContract && body.runContract.statuses;
  if (!_orchestrationContractRecord(eventTypes)
      || !Array.isArray(runStatuses)) return;
  Object.keys(eventTypes).forEach(function (type) {
    var runStatus = eventTypes[type] && eventTypes[type].runStatus;
    if (runStatus && runStatuses.indexOf(runStatus) < 0) {
      missing.push('eventContract.types.' + type + '.runStatus');
    }
  });
}

function _validateAuthoringCatalogueLinks(body, missing) {
  _validateAuthoringRoleLinks(body, missing);
  _validateAuthoringControlLinks(body, missing);
  _validateAuthoringNodeDefaultAxes(body, missing);
  _validateAuthoringIoLinks(body, missing);
  _validateAuthoringRuntimeLinks(body, missing);
}
