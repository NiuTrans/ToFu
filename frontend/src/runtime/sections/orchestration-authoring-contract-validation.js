/* ===== migrated source: orchestration-authoring-contract-validation.js ===== */
/* Compose leaf validators into the backend authoring catalogue gate. */


var ORCHESTRATION_AUTHORING_SECTION_VALIDATORS = Object.freeze({
  roles: function (section, missing) {
    _validateFieldSpecRegistry(section, 'roles', missing);
  },
  controlSchemas: function (section, missing) {
    _validateFieldSpecRegistry(section, 'controlSchemas', missing);
  },
  personas: _validatePersonaAuthoringSection,
  defaultEmits: _validateDefaultEmitsAuthoringSection,
  inspectionContract: _validateInspectionAuthoringSection,
  definitionListContract: _validateDefinitionListAuthoringSection,
  definitionEntryContract: _validateDefinitionEntryAuthoringSection,
  executionOptions: _validateExecutionAuthoringSection,
  nodeDefaults: _validateNodeDefaultsAuthoringSection,
  fieldValueContract: _validateFieldValueAuthoringSection,
  definitionWriteContract: _validateDefinitionWriteAuthoringSection,
  ioContract: _validateIoAuthoringSection,
});


function _orchestrationAuthoringContractProblems(body) {
  if (!body || typeof body !== 'object') return ['contract'];
  var missing = [];
  ORCHESTRATION_AUTHORING_OBJECT_SECTIONS.forEach(function (field) {
    var section = body[field];
    if (!_orchestrationContractRecord(section)) {
      missing.push(field);
      return;
    }
    // Runtime-section validators are owned by the lazy typed orchestration
    // domain. They do not exist while the main ESM graph is evaluating, so a
    // top-level Object.assign would either ReferenceError at boot or freeze an
    // empty snapshot forever. Resolve them when the authoring contract is
    // actually read, after the domain loader has installed its owner.
    var runtimeValidators = ORCHESTRATION_RUNTIME_SECTION_VALIDATORS || {};
    var validate = runtimeValidators[field]
      || ORCHESTRATION_AUTHORING_SECTION_VALIDATORS[field];
    if (validate) validate(section, missing);
  });
  if (!_orchestrationContractRecord(body.controls)) missing.push('controls');
  ['roleNames', 'generic', 'kinds'].forEach(function (field) {
    if (!Array.isArray(body[field]) || !body[field].length) missing.push(field);
  });
  _validateFieldSpecList(body.generic, 'generic', missing);
  _validateAuthoringFieldKinds(body, missing);
  _validateAuthoringCatalogueLinks(body, missing);
  _orchestrationRequireString(body.schema, 'schema', missing);
  _validateOrchestrationContractSectionRegistry(
    body.contractSections, missing);

  Object.keys(ORCHESTRATION_AUTHORING_WIRE_SECTIONS).forEach(function (field) {
    var nested = body[field];
    if (_orchestrationContractRecord(nested)) {
      var wire = inspectOrchestrationWireFormat(
        ORCHESTRATION_AUTHORING_WIRE_SECTIONS[field], nested);
      if (!wire.supported) {
        missing.push(field + '.' + (wire.identityField || 'format'));
      }
    }
  });
  return missing;
}
runtimeScope._orchestrationAuthoringContractProblems =
  _orchestrationAuthoringContractProblems;
if (typeof orchestrationRegistry !== 'undefined') {
  orchestrationRegistry._orchestrationAuthoringContractProblems =
    _orchestrationAuthoringContractProblems;
}

