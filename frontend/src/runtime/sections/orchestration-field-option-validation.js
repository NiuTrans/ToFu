/* ===== migrated source: orchestration-field-option-validation.js ===== */
/* Backend FieldSpec option-shape validation shared by authoring registries. */

function _validateFieldSpecOptions(field, fieldPath, missing, metadata) {
  (field.options || []).forEach(function (option, optionIndex) {
    var optionPath = fieldPath + '.options.' + optionIndex;
    if (!_orchestrationContractRecord(option)) {
      missing.push(optionPath); return;
    }
    metadata.optionRequiredStringFields.forEach(function (name) {
      _orchestrationRequireString(
        option[name], optionPath + '.' + name, missing);
    });
    metadata.optionBooleanFields.forEach(function (name) {
      if (option[name] != null && typeof option[name] !== 'boolean') {
        missing.push(optionPath + '.' + name);
      }
    });
  });
}

