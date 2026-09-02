/* ===== migrated source: orchestration-authoring-default-validation.js ===== */
/* Validate backend-authored authoring and runtime node defaults. */
function _validateNodeRuntimeDefaultsAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .runtimeSections.nodeRuntimeDefaults;

  function validateRecord(record, path, fields) {
    if (!_orchestrationContractRecord(record)) {
      missing.push(path); return;
    }
    _orchestrationRequireStringFields(record,
      fields.requiredStringFields, path, missing);
    fields.requiredPositiveIntegerFields.forEach(function (field) {
      _orchestrationRequirePositiveInteger(
        record[field], path + '.' + field, missing);
    });
  }

  metadata.requiredObjectFields.forEach(function (field) {
    if (!_orchestrationContractRecord(section[field])) {
      missing.push('nodeRuntimeDefaults.' + field);
    }
  });
  validateRecord(section.role, 'nodeRuntimeDefaults.role', metadata.role);
  validateRecord(section.subflow,
    'nodeRuntimeDefaults.subflow', metadata.subflow);
  Object.keys(metadata.controls).forEach(function (kind) {
    validateRecord((section.controls || {})[kind],
      'nodeRuntimeDefaults.controls.' + kind, metadata.controls[kind]);
  });
}
function _validateAuthoringNodeDefaultAxes(body, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.nodeDefaults;
  var options = body.executionOptions || {};
  var defaults = body.nodeDefaults || {};
  function validate(record, path, axes) {
    if (!_orchestrationContractRecord(record)) return;
    Object.keys(axes || {}).forEach(function (field) {
      var choices = options[axes[field]];
      if (!Array.isArray(choices) || choices.indexOf(record[field]) < 0)
        missing.push(path + '.' + field);
    });
  }

  validate(defaults.genericRole, 'nodeDefaults.genericRole',
    metadata.roleExecutionAxes);
  Object.keys(defaults.roles || {}).forEach(function (role) {
    validate(defaults.roles[role], 'nodeDefaults.roles.' + role,
      metadata.roleExecutionAxes);
  });
  validate(defaults.subflow, 'nodeDefaults.subflow',
    metadata.subflowExecutionAxes);
  var runtimeDefaults = body.nodeRuntimeDefaults || {};
  var runtimeMetadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .runtimeSections.nodeRuntimeDefaults;
  validate(runtimeDefaults.role, 'nodeRuntimeDefaults.role',
    runtimeMetadata.roleExecutionAxes);
  validate(runtimeDefaults.subflow, 'nodeRuntimeDefaults.subflow',
    runtimeMetadata.subflowExecutionAxes);
}

