/* ===== migrated source: orchestration-diagnostic-target.js ===== */
/* Pure backend JSON-Pointer → Studio navigation target projection. */

function _decodeOrchestrationDiagnosticPointer(path) {
  if (path === '') return [];
  if (typeof path !== 'string' || path.charAt(0) !== '/') return null;
  var tokens = path.slice(1).split('/');
  if (tokens.some(function (token) { return /~(?:[^01]|$)/.test(token); })) {
    return null;
  }
  return tokens.map(function (token) {
    return token.replace(/~1/g, '/').replace(/~0/g, '~'); });
}

function _orchestrationDiagnosticIndex(token, values) {
  if (!/^(0|[1-9]\d*)$/.test(String(token == null ? '' : token))) return -1;
  var index = Number(token);
  return Number.isSafeInteger(index) && index < values.length ? index : -1;
}

function _orchestrationDiagnosticNodeIdentity(nodes, index) {
  var node = nodes[index];
  var id = node && typeof node.id === 'string' ? node.id : '';
  var count = id ? nodes.filter(function (candidate) {
    return candidate && candidate.id === id;
  }).length : 0;
  return { node: node, id: id, navigable: count === 1 };
}

function _orchestrationDiagnosticField(tokens) {
  if (!tokens.length) return null;
  if (tokens[0] === 'name' || tokens[0] === 'role') {
    return { kind: 'param', key: tokens[0] };
  }
  if (tokens[0] !== 'params') return null;
  if (tokens[1] !== 'io') {
    return tokens[1] ? { kind: 'param', key: tokens[1] } : null;
  }
  var side = tokens[2];
  if (side !== 'inputs' && side !== 'outputs') {
    return { kind: 'io-section' };
  }
  var indexToken = tokens[3];
  var canonicalIndex = /^(0|[1-9]\d*)$/.test(
    String(indexToken == null ? '' : indexToken));
  var index = canonicalIndex ? Number(indexToken) : NaN;
  var key = tokens[4];
  if (!Number.isSafeInteger(index) || !key) {
    return { kind: 'io-section', side: side };
  }
  return { kind: 'io', side: side, index: index, key: key };
}

function resolveOrchestrationDiagnosticTarget(diagnostic, definition) {
  var tokens = _decodeOrchestrationDiagnosticPointer(
    diagnostic && diagnostic.path || '');
  if (!tokens) return null;
  var cursor = definition && typeof definition === 'object' ? definition : {};
  var groups = [];
  var offset = 0;

  while (tokens[offset] === 'nodes') {
    var nodes = Array.isArray(cursor.nodes) ? cursor.nodes : [];
    if (tokens[offset + 1] == null) {
      return { kind: 'document', groups: groups, field: null,
        path: diagnostic && diagnostic.path || '' };
    }
    var nodeIndex = _orchestrationDiagnosticIndex(tokens[offset + 1], nodes);
    if (nodeIndex < 0) return null;
    var identity = _orchestrationDiagnosticNodeIdentity(nodes, nodeIndex);
    var node = identity.node;
    var rest = tokens.slice(offset + 2);
    if (rest[0] === 'params' && rest[1] === 'definition'
        && node && node.params && node.params.definition
        && typeof node.params.definition === 'object' && rest.length > 2) {
      if (!identity.navigable) return {
        kind: 'node', id: identity.id, index: nodeIndex, groups: groups,
        navigable: false, field: null,
        path: diagnostic && diagnostic.path || '',
      };
      groups.push(identity.id);
      cursor = node.params.definition;
      offset += 4;
      continue;
    }
    return {
      kind: 'node', id: identity.id, index: nodeIndex, groups: groups,
      navigable: identity.navigable,
      field: identity.navigable ? _orchestrationDiagnosticField(rest) : null,
      path: diagnostic && diagnostic.path || '',
    };
  }

  if (tokens[offset] === 'edges') {
    var edges = Array.isArray(cursor.edges) ? cursor.edges : [];
    if (tokens[offset + 1] == null) {
      return { kind: 'document', groups: groups, field: null,
        path: diagnostic && diagnostic.path || '' };
    }
    var edgeIndex = _orchestrationDiagnosticIndex(tokens[offset + 1], edges);
    if (edgeIndex < 0) return null;
    return {
      kind: 'edge', index: edgeIndex, groups: groups,
      path: diagnostic && diagnostic.path || '',
    };
  }
  return {
    kind: 'document', groups: groups,
    field: tokens[offset] === 'name' ? { kind: 'document-name' } : null,
    path: diagnostic && diagnostic.path || '',
  };
}

function orchestrationDiagnosticTargetLabel(target, definition, translate) {
  var tr = typeof translate === 'function' ? translate : function (key) {
    return key;
  };
  if (!target) return tr('orch.issues.flowTarget');
  if (target.kind === 'edge') {
    return tr('orch.issues.edgeTarget', { n: target.index + 1 });
  }
  if (target.kind === 'document') return tr('orch.issues.flowTarget');
  var cursor = definition || {};
  target.groups.forEach(function (groupId) {
    var group = (cursor.nodes || []).filter(function (node) {
      return node.id === groupId;
    })[0];
    cursor = group && group.params && group.params.definition || {};
  });
  var node = (cursor.nodes || [])[target.index] || {};
  var label = node.name || node.id || tr('orch.issues.nodeTarget', {
    n: target.index + 1,
  });
  var field = target.field && target.field.key;
  return field ? label + ' · ' + field : label;
}

