/* ===== migrated source: orchestration-contract-section-registry-validation.js ===== */
/* Validation for the backend-published authoring/runtime section registry. */

function _validateOrchestrationContractSectionRegistry(
  sectionRegistry, missing
) {
  // Optional for rolling deploys against a pre-registry backend. Once
  // present, the runtime projection must be a safe subset of authoring.
  if (sectionRegistry == null) return;
  var authoring = sectionRegistry.authoring;
  if (!_orchestrationContractRecord(sectionRegistry)
      || !Array.isArray(authoring)
      || !Array.isArray(sectionRegistry.runtime)
      || !sectionRegistry.runtime.length
      || sectionRegistry.runtime.some(function (name) {
        return typeof name !== 'string'
          || !/^[A-Za-z][A-Za-z0-9]*$/.test(name)
          || authoring.indexOf(name) < 0;
      })) {
    missing.push('contractSections');
  }

  var rolling = sectionRegistry.rollingOptionalFields;
  if (rolling == null) return;
  var expected = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .rollingOptionalFields;
  if (!_orchestrationContractRecord(rolling)) {
    missing.push('contractSections.rollingOptionalFields');
    return;
  }
  Object.keys(rolling).forEach(function (sectionName) {
    var path = 'contractSections.rollingOptionalFields.' + sectionName;
    if (!/^[A-Za-z][A-Za-z0-9]*$/.test(sectionName)
        || !Array.isArray(authoring)
        || authoring.indexOf(sectionName) < 0) {
      missing.push(path);
    } else {
      _orchestrationRequireStringVocabulary(
        rolling[sectionName], path, missing);
    }
  });
  Object.keys(expected).forEach(function (sectionName) {
    if (rolling[sectionName] == null) return;
    _orchestrationRequireArray(rolling[sectionName],
      'contractSections.rollingOptionalFields.' + sectionName,
      missing, expected[sectionName]);
  });
}

