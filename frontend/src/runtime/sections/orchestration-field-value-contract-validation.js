/* ===== migrated source: orchestration-field-value-contract-validation.js ===== */
/* Semantic validation for the backend-owned FieldValue contract. */

function _validateFieldValueAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .fieldValueContract;
  if (section.optionalEmpty !== metadata.optionalEmpty) {
    missing.push('fieldValueContract.optionalEmpty');
  }
  if (section.failureCodes != null) {
    if (!_orchestrationContractRecord(section.failureCodes)) {
      missing.push('fieldValueContract.failureCodes');
    } else Object.keys(metadata.failureCodes).forEach(function (name) {
      if (section.failureCodes[name] !== metadata.failureCodes[name]) {
        missing.push('fieldValueContract.failureCodes.' + name);
      }
    });
  }
  if (!_orchestrationContractRecord(section.kinds)) {
    missing.push('fieldValueContract.kinds'); return;
  }
  metadata.kinds.forEach(function (kind) {
    var spec = section.kinds[kind];
    if (!_orchestrationContractRecord(spec)) {
      missing.push('fieldValueContract.kinds.' + kind);
    } else metadata.kindRequiredStringFields.forEach(function (field) {
      _orchestrationRequireString(spec[field],
        'fieldValueContract.kinds.' + kind + '.' + field, missing);
    });
  });
}

