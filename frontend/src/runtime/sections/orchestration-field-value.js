/* ===== migrated source: orchestration-field-value.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-field-value.js — pure FieldSpec draft-value codec

   Owns the browser codec for the versioned field-value contract. Inspector
   renderers declare only a FieldSpec kind; every mutation surface uses this
   codec to produce the backend wire shape. No DOM or graph dependency.

   MUST load before orchestration-node-editor.js.
   ═══════════════════════════════════════════════════════════════════ */


function orchestrationFieldValueContract(contractSource) {
  return orchestrationDirectContract(contractSource);
}


function _orchestrationFieldValuePolicy(contract, kind) {
  if (!contract) return null;
  if (!orchestrationWireContractSpec('field-value', contract).supported) {
    return false;
  }
  var policy = contract.kinds && contract.kinds[kind];
  return policy && typeof policy === 'object' ? policy : false;
}


function _orchestrationFieldWireSupported(kind, policy) {
  if (!policy) return policy === null;
  var expected = {
    text: 'string', textarea: 'string', select: 'declared option',
    list: 'array<string>', int: 'integer', bool: 'boolean',
  }[kind];
  return !!expected && policy.wire === expected;
}


function _orchestrationFieldPositiveLimit(spec, key) {
  var value = spec && typeof spec === 'object' ? Number(spec[key]) : 0;
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}


var _ORCHESTRATION_FIELD_FAILURES = {
  unsupportedContract: [
    'unsupported-contract', 'field.contract.unsupported'],
  invalidNumber: ['invalid-number', 'field.type.integer'],
  invalidBoolean: ['invalid-boolean', 'field.type.boolean'],
  maxLength: ['max-length', 'field.max_length'],
  maxItems: ['max-items', 'field.max_items'],
  maxItemLength: ['max-item-length', 'field.max_item_length'],
};


function _orchestrationFieldFailure(
  contract, failure, present, value, limit
) {
  var policy = _ORCHESTRATION_FIELD_FAILURES[failure];
  var published = contract && contract.failureCodes;
  var code = published && typeof published[failure] === 'string'
    && published[failure] ? published[failure] : policy[1];
  var result = {
    ok: false, present: !!present, value: value,
    reason: policy[0], code: code,
  };
  if (limit != null) result.limit = limit;
  return result;
}


function normalizeOrchestrationFieldDraftValue(
  kind, rawValue, spec, contractSource
) {
  kind = String(kind || (typeof rawValue === 'boolean' ? 'bool' : 'text'));
  var contract = orchestrationFieldValueContract(contractSource);
  var policy = _orchestrationFieldValuePolicy(contract, kind);
  if (!_orchestrationFieldWireSupported(kind, policy)) {
    return _orchestrationFieldFailure(
      contract, 'unsupportedContract', false, null);
  }

  if (kind === 'list') {
    var source = Array.isArray(rawValue) ? rawValue
      : !policy || policy.editor === 'newline'
        ? String(rawValue == null ? '' : rawValue).split('\n') : null;
    if (!source) {
      return _orchestrationFieldFailure(
        contract, 'unsupportedContract', false, null);
    }
    var items = source
      .map(function (item) {
        item = String(item == null ? '' : item);
        return !policy || policy.trimItems === true ? item.trim() : item;
      })
      .filter(function (item) {
        return !policy || policy.dropEmptyItems === true ? !!item : true;
      });
    var maxItems = _orchestrationFieldPositiveLimit(spec, 'maxItems');
    if (maxItems != null && items.length > maxItems) {
      return _orchestrationFieldFailure(
        contract, 'maxItems', true, items, maxItems);
    }
    var maxItemLength = _orchestrationFieldPositiveLimit(
      spec, 'maxItemLength');
    if (maxItemLength != null && items.some(function (item) {
      return item.length > maxItemLength;
    })) {
      return _orchestrationFieldFailure(
        contract, 'maxItemLength', true, items, maxItemLength);
    }
    return { ok: true, present: items.length > 0, value: items };
  }

  if (rawValue == null || rawValue === '') {
    if (contract && contract.optionalEmpty !== 'omit') {
      return _orchestrationFieldFailure(
        contract, 'unsupportedContract', false, null);
    }
    return { ok: true, present: false, value: null };
  }

  if (kind === 'int') {
    var numeric = Number(rawValue);
    // Finite but non-integral values remain in the draft so the shared
    // backend inspection can explain min/max/integer violations. NaN and
    // Infinity cannot cross JSON faithfully (both collapse to null), so they
    // are rejected before they can silently erase a parameter.
    if (!Number.isFinite(numeric)) {
      return _orchestrationFieldFailure(
        contract, 'invalidNumber', false, null);
    }
    return { ok: true, present: true, value: numeric };
  }

  if (kind === 'bool') {
    if (typeof rawValue !== 'boolean') {
      return _orchestrationFieldFailure(
        contract, 'invalidBoolean', false, null);
    }
    return { ok: true, present: true, value: rawValue };
  }

  var maxLength = _orchestrationFieldPositiveLimit(spec, 'maxLength');
  if (maxLength != null && typeof rawValue === 'string'
      && rawValue.length > maxLength) {
    return _orchestrationFieldFailure(
      contract, 'maxLength', true, rawValue, maxLength);
  }

  return { ok: true, present: true, value: rawValue };
}


function orchestrationFieldDraftValuesEqual(left, right) {
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)
        || left.length !== right.length) return false;
    return left.every(function (value, index) { return value === right[index]; });
  }
  return left === right;
}

