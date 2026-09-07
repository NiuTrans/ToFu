/* ===== migrated source: orchestration-authoring-policy-validation.js ===== */
/* Focused semantic validators for backend-owned Studio editor policies. */
function _validateFieldSpecList(fields, path, missing) {
  if (!Array.isArray(fields)) { missing.push(path); return; }
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.fieldSpec;
  fields.forEach(function (field, index) {
    var fieldPath = path + '.' + index;
    if (!_orchestrationContractRecord(field)) {
      missing.push(fieldPath); return;
    }
    metadata.requiredStringFields.forEach(function (name) {
      _orchestrationRequireString(field[name], fieldPath + '.' + name, missing);
    });
    metadata.positiveIntegerFields.forEach(function (name) {
      if (field[name] != null) _orchestrationRequirePositiveInteger(
        field[name], fieldPath + '.' + name, missing);
    });
    metadata.arrayFields.forEach(function (name) {
      _orchestrationRequireOptional(
        field[name], fieldPath + '.' + name, missing, Array.isArray);
    });
    metadata.objectFields.forEach(function (name) {
      _orchestrationRequireOptional(field[name], fieldPath + '.' + name,
        missing, _orchestrationContractRecord);
    });
    _validateFieldSpecOptions(field, fieldPath, missing, metadata);
  });
}

function _validateFieldSpecRegistry(section, path, missing) {
  Object.keys(section).forEach(function (name) {
    _validateFieldSpecList(section[name], path + '.' + name, missing);
  });
}

function _validateDefinitionWriteAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract(
    'definitionWriteContract');
  _orchestrationRequireStringFields(section,
    ORCHESTRATION_AUTHORING_VALIDATION_METADATA.definitionWriteContract
      .requiredStringFields, 'definitionWriteContract', missing);
  _orchestrationRequireArray(section.operations,
    'definitionWriteContract.operations', missing, defaults.operations);
  _orchestrationRequirePositiveInteger(section.conflictStatus,
    'definitionWriteContract.conflictStatus', missing);
  if (section.conflictFields != null) {
    _orchestrationRequireFieldSpecs(section.conflictFields, {
      format: 'string', reason: 'string', operation: 'string',
      expectedUpdatedAt: 'non_negative_integer',
      currentUpdatedAt: 'non_negative_integer',
    }, 'definitionWriteContract.conflictFields', missing);
    var fields = _orchestrationContractRecord(section.conflictFields)
      ? section.conflictFields : {};
    if (fields.format && fields.format.name !== 'format') {
      missing.push('definitionWriteContract.conflictFields');
    }
  }
}

function _validatePersonaAuthoringSection(section, missing) {
  var fields = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .personas.requiredStringFields;
  Object.keys(section).forEach(function (role) {
    var persona = section[role];
    if (!_orchestrationContractRecord(persona)) {
      missing.push('personas.' + role); return;
    }
    fields.forEach(function (field) {
      _orchestrationRequireString(persona[field],
        'personas.' + role + '.' + field, missing);
    });
  });
}

function _validateDefaultEmitsAuthoringSection(section, missing) {
  Object.keys(section).forEach(function (role) {
    _orchestrationRequireString(section[role],
      'defaultEmits.' + role, missing);
  });
}

function _validateAuthoringFieldKinds(body, missing) {
  var kinds = Array.isArray(body.kinds) ? body.kinds : [];
  var valueKinds = _orchestrationContractRecord(body.fieldValueContract)
    && _orchestrationContractRecord(body.fieldValueContract.kinds)
    ? Object.keys(body.fieldValueContract.kinds) : [];
  if (kinds.some(function (kind, index) {
    return typeof kind !== 'string' || !kind
      || kinds.indexOf(kind) !== index || valueKinds.indexOf(kind) < 0;
  }) || valueKinds.some(function (kind) { return kinds.indexOf(kind) < 0; })) {
    missing.push('kinds');
  }
  function validateList(fields, path) {
    if (!Array.isArray(fields)) return;
    fields.forEach(function (field, index) {
      if (_orchestrationContractRecord(field) && typeof field.kind === 'string'
          && field.kind && kinds.indexOf(field.kind) < 0) {
        missing.push(path + '.' + index + '.kind');
      }
    });
  }
  validateList(body.generic, 'generic');
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA.fieldSpecRegistrySections
    .forEach(function (sectionName) {
    var section = body[sectionName];
    if (!_orchestrationContractRecord(section)) return;
    Object.keys(section).forEach(function (name) {
      validateList(section[name], sectionName + '.' + name);
    });
    });
}
