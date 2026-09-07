/* ===== migrated source: orchestration-inspector-interaction.js ===== */
/* Inspector section memory, local event binding and field validity feedback. */

function createOrchestrationInspectorInteraction(options) {
  options = options || {};
  var disclosureState = options.disclosureState
    || createOrchestrationDisclosureState();
  var focus = options.focusController
    || createOrchestrationInspectorFocusController(options);
  var validity = options.fieldValidity
    || createOrchestrationFieldValidity();
  var fieldErrorSerial = 0;

  function _fieldFailureText(result) {
    result = result && typeof result === 'object' ? result : {};
    var labels = {
      'field.max_length': 'orch.field.errorMaxLength',
      'field.max_items': 'orch.field.errorMaxItems',
      'field.max_item_length': 'orch.field.errorMaxItemLength',
      'field.type.integer': 'orch.field.errorNumber',
      'field.contract.unsupported': 'orch.field.errorUnsupported',
      'max-length': 'orch.field.errorMaxLength',
      'max-items': 'orch.field.errorMaxItems',
      'max-item-length': 'orch.field.errorMaxItemLength',
      'invalid-number': 'orch.field.errorNumber',
      'unsupported-contract': 'orch.field.errorUnsupported',
    };
    var key = labels[result.code] || labels[result.reason]
      || 'orch.field.errorInvalid';
    return typeof options.translate === 'function'
      ? options.translate(key, { n: result.limit }) : key;
  }

  function _setFieldValidity(field, result) {
    var accepted = result && typeof result === 'object'
      ? result.ok !== false : result !== false;
    var errorId = field.getAttribute('data-orch-field-error-id');
    var error = errorId ? _document().getElementById(errorId) : null;
    if (accepted) {
      validity.setLocal(field, true);
      if (error) { error.hidden = true; error.textContent = ''; }
      return true;
    }
    if (!error) {
      error = _document().createElement('small');
      error.id = 'orchFieldError-' + (++fieldErrorSerial);
      error.className = 'orch-fld-error';
      error.setAttribute('role', 'alert');
      (field.closest('.orch-fld') || field.parentNode).appendChild(error);
      field.setAttribute('data-orch-field-error-id', error.id);
    }
    validity.setLocal(field, false, error.id,
      String(result.code || result.reason || ''));
    error.textContent = _fieldFailureText(result);
    error.hidden = false;
    return false;
  }

  function _document() { return options.document || document; }

  function scope(context) {
    context = context || {};
    var workspace = typeof options.workspaceToken === 'function'
      ? options.workspaceToken() : '';
    var subject = context.node
      ? 'node:' + context.node.id
      : (context.edge ? 'edge:' + context.edge.id : 'none');
    return orchestrationScrollScope([workspace || 'root', subject]);
  }

  function setMobileOpen(element, open) {
    if (typeof options.isMobile !== 'function' || !options.isMobile()) return;
    if (typeof options.setMobileOpen === 'function') {
      options.setMobileOpen(!!open);
    }
  }

  function _restoreAndBindSections(element, context) {
    disclosureState.bind(element, scope(context), {
      selector: 'details[data-orch-section-key]',
      attribute: 'data-orch-section-key',
    });
  }

  function _bindClose(element) {
    Array.prototype.forEach.call(
      element.querySelectorAll('.orch-inspector-close'), function (button) {
        button.addEventListener('click', function () {
          if (typeof options.closeMobile === 'function') options.closeMobile();
        });
      }
    );
  }

  function _bindEdge(element, edge) {
    Array.prototype.forEach.call(
      element.querySelectorAll('.orch-edge-binding'), function (select) {
        select.addEventListener('change', function () {
          if (typeof options.bindEdgeInput === 'function') {
            options.bindEdgeInput(
              edge.to, Number(select.getAttribute('data-input-index')),
              select.value
            );
          }
        });
      }
    );
    var reverse = element.querySelector(
      '[data-orch-inspector-action="reverse-edge"]');
    var remove = element.querySelector(
      '[data-orch-inspector-action="delete-edge"]');
    if (reverse) reverse.addEventListener('click', function () {
      if (typeof options.reverseEdge === 'function') {
        options.reverseEdge(edge.id);
      }
    });
    if (remove) remove.addEventListener('click', function () {
      if (typeof options.deleteEdge === 'function') options.deleteEdge(edge.id);
    });
  }

  function _bindFields(element, node) {
    Array.prototype.forEach.call(
      element.querySelectorAll('[data-orch-param-key]'), function (field) {
        var eventName = field.tagName === 'SELECT'
          || (field.tagName === 'INPUT' && field.type === 'checkbox')
          ? 'change' : 'input';
        field.addEventListener(eventName, function () {
          if (typeof options.setParam !== 'function') return;
          var value = field.type === 'checkbox' ? field.checked : field.value;
          var args = [node.id,
            field.getAttribute('data-orch-param-key') || '', value,
            field.getAttribute('data-orch-param-kind') || '',
            eventName === 'input'];
          var result = typeof options.setParamResult === 'function'
            ? options.setParamResult.apply(null, args)
            : options.setParam.apply(null, args);
          _setFieldValidity(field, result);
        });
      }
    );
  }

  function _bindNode(element, node) {
    _bindFields(element, node);
    if (typeof options.bindIoSection === 'function') {
      options.bindIoSection(element, node.id);
    }
    var enter = element.querySelector(
      '[data-orch-inspector-action="enter-group"]');
    var remove = element.querySelector(
      '[data-orch-inspector-action="delete-node"]');
    if (enter) enter.addEventListener('click', function () {
      if (typeof options.enterGroup === 'function') options.enterGroup(node.id);
    });
    if (remove) remove.addEventListener('click', function () {
      if (typeof options.deleteNode === 'function') options.deleteNode(node.id);
    });
  }

  function bind(element, context) {
    context = context || {};
    _restoreAndBindSections(element, context);
    _bindClose(element);
    if (context.edge) {
      _bindEdge(element, context.edge);
      return;
    }
    if (context.node) _bindNode(element, context.node);
  }

  return {
    bind: bind,
    captureFocus: focus.capture,
    restoreFocus: focus.restore,
    focusDiagnostic: focus.focusDiagnostic,
    scope: scope,
    setMobileOpen: setMobileOpen,
  };
}

