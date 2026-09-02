/* ===== migrated source: orchestration-authoring-section-validation.js ===== */
/* Leaf schema checks for backend-owned authoring contract sections. */

function _validateInspectionAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract('inspectionContract');
  if (!Array.isArray(section.diagnosticSeverities)
      || !section.diagnosticSeverities.length) {
    missing.push('inspectionContract.diagnosticSeverities');
  }
  _orchestrationRequireArray(section.diagnosticFields,
    'inspectionContract.diagnosticFields', missing,
    defaults.diagnosticFields);
  _orchestrationRequireString(section.diagnosticPathFormat,
    'inspectionContract.diagnosticPathFormat', missing);
}

function _validateDefinitionListAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract(
    'definitionListContract');
  _orchestrationRequireArray(section.itemFields,
    'definitionListContract.itemFields', missing,
    defaults.itemFields);
  if (!Array.isArray(section.orderBy)) {
    missing.push('definitionListContract.orderBy');
  }
  if (typeof section.definitionIncluded !== 'boolean') {
    missing.push('definitionListContract.definitionIncluded');
  }
}

function _validateDefinitionEntryAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract(
    'definitionEntryContract');
  _orchestrationRequireArray(section.fields,
    'definitionEntryContract.fields', missing, defaults.fields);
  _orchestrationRequireString(section.versionField,
    'definitionEntryContract.versionField', missing);
  if (typeof section.versionField === 'string' && section.versionField
      && Array.isArray(section.fields)
      && section.fields.indexOf(section.versionField) < 0) {
    missing.push('definitionEntryContract.fields.' + section.versionField);
  }
  if (typeof section.inspectionIncludedOnWrite !== 'boolean') {
    missing.push('definitionEntryContract.inspectionIncludedOnWrite');
  }
  if (typeof section.versionRequiredOnWrite !== 'boolean') {
    missing.push('definitionEntryContract.versionRequiredOnWrite');
  }
}

function _validateExecutionAuthoringSection(section, missing) {
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA.executionOptions.arrayFields
    .forEach(function (axis) {
      if (!Array.isArray(section[axis]) || !section[axis].length) {
        missing.push('executionOptions.' + axis);
      }
    });
}

function _validateNodeDefaultsAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.nodeDefaults;
  metadata.objectFields.forEach(function (field) {
    if (!_orchestrationContractRecord(section[field])) {
      missing.push('nodeDefaults.' + field);
    }
  });
  var blank = section.blankSubflow || {};
  metadata.blankSubflowArrayFields.forEach(function (field) {
    if (!Array.isArray(blank[field])) {
      missing.push('nodeDefaults.blankSubflow.' + field);
    }
  });
}

function _validateIoAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.ioContract;
  if (!Array.isArray(section.types) || !section.types.length) {
    missing.push('ioContract.types');
  }
  if (!_orchestrationContractRecord(section.defaultOutput)) {
    missing.push('ioContract.defaultOutput');
  }
  _orchestrationRequirePositiveInteger(section.maxPorts,
    'ioContract.maxPorts', missing);
  _orchestrationRequireString(section.startRef, 'ioContract.startRef', missing);
  if (section.failureCodes != null) {
    if (!_orchestrationContractRecord(section.failureCodes)) {
      missing.push('ioContract.failureCodes');
    } else Object.keys(metadata.failureCodes).forEach(function (name) {
      if (section.failureCodes[name] !== metadata.failureCodes[name]) {
        missing.push('ioContract.failureCodes.' + name);
      }
    });
  }
}

