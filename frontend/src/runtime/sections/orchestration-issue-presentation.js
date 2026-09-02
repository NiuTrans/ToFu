/* ===== migrated source: orchestration-issue-presentation.js ===== */
/* Safe, stateless issue-panel DOM projection. */

function createOrchestrationIssuePresentation(options) {
  options = options || {};
  var doc = options.document || document;

  function tr(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function resolve(diagnostic, definition) {
    var projector = typeof options.resolveTarget === 'function'
      ? options.resolveTarget : resolveOrchestrationDiagnosticTarget;
    return projector(diagnostic, definition);
  }
  function targetLabel(target, definition) {
    var projector = typeof options.targetLabel === 'function'
      ? options.targetLabel : orchestrationDiagnosticTargetLabel;
    return projector(target, definition, tr);
  }
  function element(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }
  function snapshot(state) {
    state = state || {};
    return Object.freeze({
      validation: String(state.validation || 'unknown'),
      errors: Object.freeze(Array.isArray(state.errors) ? state.errors.slice() : []),
      warnings: Object.freeze(
        Array.isArray(state.warnings) ? state.warnings.slice() : []),
      diagnostics: Object.freeze(Array.isArray(state.diagnostics)
        ? state.diagnostics.map(function (item) {
            return Object.freeze(Object.assign({}, item || {}));
          }) : []),
      contract: state.contract && typeof state.contract === 'object'
        ? Object.freeze(Object.assign({}, state.contract)) : null,
    });
  }
  function render(panel, current, definition) {
    if (!panel || !current) return false;
    panel.replaceChildren();
    var header = element('div', 'orch-issues-head');
    header.appendChild(element('strong', '', tr('orch.issues.title')));
    header.appendChild(element(
      'span', 'orch-issues-count',
      tr('orch.issues.counts', {
        errors: current.errors.length, warnings: current.warnings.length,
      })
    ));
    var closeButton = element('button', 'orch-issues-close', '×');
    closeButton.type = 'button';
    closeButton.setAttribute('data-orch-issues-close', '');
    closeButton.setAttribute('aria-label', tr('orch.tip.close'));
    header.appendChild(closeButton);
    panel.appendChild(header);

    if (!current.diagnostics.length) {
      var key = current.validation === 'valid'
        ? 'orch.issues.valid' : 'orch.issues.pending';
      panel.appendChild(element('div', 'orch-issues-empty', tr(key, {
        projection: current.contract && current.contract.projection || 'flow',
        nodes: current.contract && current.contract.nodes || 0,
      })));
      return true;
    }
    var list = element('div', 'orch-issues-list');
    current.diagnostics.forEach(function (diagnostic, index) {
      var severity = diagnostic.severity === 'warning' ? 'warning' : 'error';
      var button = element('button', 'orch-issue-item is-' + severity);
      button.type = 'button';
      button.setAttribute('data-orch-issue-index', String(index));
      button.appendChild(element('span', 'orch-issue-dot', ''));
      var copy = element('span', 'orch-issue-copy');
      var message = element(
        'span', 'orch-issue-message', String(diagnostic.message || ''));
      message.id = 'orchIssueMessage-' + index;
      button.setAttribute('data-orch-issue-message-id', message.id);
      copy.appendChild(message);
      copy.appendChild(element(
        'span', 'orch-issue-target',
        targetLabel(resolve(diagnostic, definition), definition)));
      button.appendChild(copy);
      list.appendChild(button);
    });
    panel.appendChild(list);
    return true;
  }

  return Object.freeze({ render: render, snapshot: snapshot });
}

