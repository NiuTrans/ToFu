/* ===== migrated source: orchestration-diagnostic-index.js ===== */
/* Current-workspace diagnostic summaries derived from canonical targets. */

function createOrchestrationDiagnosticIndex(options) {
  options = options || {};
  var lastDiagnostics = null;
  var lastWorkspace = '';
  var cache = null;

  function _groups() {
    var value = typeof options.workspaceGroups === 'function'
      ? options.workspaceGroups() : [];
    return Array.isArray(value) ? value.filter(Boolean) : [];
  }

  function _summary() {
    return { errors: 0, warnings: 0, total: 0, nested: 0, messages: [] };
  }

  function _add(summary, diagnostic, nested) {
    var severity = diagnostic && diagnostic.severity === 'warning'
      ? 'warnings' : 'errors';
    summary[severity] += 1;
    summary.total += 1;
    if (nested) summary.nested += 1;
    var message = String(diagnostic && diagnostic.message || '');
    if (message && summary.messages.indexOf(message) < 0) {
      summary.messages.push(message);
    }
  }

  function _prefix(prefix, value) {
    if (prefix.length > value.length) return false;
    return prefix.every(function (groupId, index) {
      return value[index] === groupId;
    });
  }

  function _build(diagnostics, groups) {
    var result = {
      document: _summary(),
      nodes: Object.create(null),
      edges: Object.create(null),
    };
    var definition = typeof options.definition === 'function'
      ? options.definition() : null;
    diagnostics.forEach(function (diagnostic) {
      var target = resolveOrchestrationDiagnosticTarget(
        diagnostic, definition);
      if (!target || !_prefix(groups, target.groups || [])) return;
      if (target.groups.length > groups.length) {
        var groupId = target.groups[groups.length];
        result.nodes[groupId] = result.nodes[groupId] || _summary();
        _add(result.nodes[groupId], diagnostic, true);
      } else if (target.kind === 'node' && target.id
                 && target.navigable !== false) {
        result.nodes[target.id] = result.nodes[target.id] || _summary();
        _add(result.nodes[target.id], diagnostic, false);
      } else if (target.kind === 'edge') {
        result.edges[target.index] = result.edges[target.index] || _summary();
        _add(result.edges[target.index], diagnostic, false);
      } else {
        _add(result.document, diagnostic, false);
      }
    });
    return result;
  }

  function snapshot() {
    var diagnostics = typeof options.diagnostics === 'function'
      ? options.diagnostics() : [];
    diagnostics = Array.isArray(diagnostics) ? diagnostics : [];
    var groups = _groups();
    var workspace = groups.join('\u0000');
    if (cache && diagnostics === lastDiagnostics && workspace === lastWorkspace) {
      return cache;
    }
    lastDiagnostics = diagnostics;
    lastWorkspace = workspace;
    cache = _build(diagnostics, groups);
    return cache;
  }

  return {
    snapshot: snapshot,
    node: function (id) { return snapshot().nodes[id] || null; },
    edge: function (index) { return snapshot().edges[index] || null; },
    document: function () { return snapshot().document; },
    invalidate: function () { cache = null; },
  };
}

