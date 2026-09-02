/* ===== migrated source: orchestration-layout-contract.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-layout-contract.js — shared display-coordinate policy

   The backend owns graph layout. Studio and Task Mode use this small
   contract only to recognize a complete backend/user layout and to remain
   renderable while a malformed or legacy draft is being repaired.
   ═══════════════════════════════════════════════════════════════════ */


function orchestrationFiniteCoordinate(value) {
  return typeof value === 'number' && Number.isFinite(value);
}


function orchestrationNodeHasPosition(node) {
  var position = node && node.pos;
  return !!position
    && orchestrationFiniteCoordinate(position.x)
    && orchestrationFiniteCoordinate(position.y);
}


function orchestrationNodePosition(node, fallback) {
  var position = node && node.pos || {};
  fallback = fallback || {};
  var fallbackX = orchestrationFiniteCoordinate(fallback.x) ? fallback.x : 20;
  var fallbackY = orchestrationFiniteCoordinate(fallback.y) ? fallback.y : 20;
  return {
    x: orchestrationFiniteCoordinate(position.x) ? position.x : fallbackX,
    y: orchestrationFiniteCoordinate(position.y) ? position.y : fallbackY,
  };
}


function projectOrchestrationLayoutPositions(definition, expectedDefinition) {
  function failure(code, path, cause) {
    var result = { ok: false, reason: 'invalid-layout', code: code, path: path };
    if (cause) result.cause = cause;
    return result;
  }
  try {
    if (!definition || typeof definition !== 'object'
        || Array.isArray(definition)) {
      return failure('definition.type.object', '');
    }
    if (!Array.isArray(definition.nodes)) {
      return failure('definition.nodes.type.array', '/nodes');
    }
    var expected = null;
    if (expectedDefinition !== undefined) {
      if (!expectedDefinition || typeof expectedDefinition !== 'object'
          || Array.isArray(expectedDefinition)
          || !Array.isArray(expectedDefinition.nodes)) {
        return failure('layout.request.definition.invalid', '');
      }
      expected = Object.create(null);
      for (var expectedIndex = 0;
           expectedIndex < expectedDefinition.nodes.length; expectedIndex++) {
        var expectedNode = expectedDefinition.nodes[expectedIndex];
        var expectedId = expectedNode && expectedNode.id;
        if (!expectedNode || typeof expectedNode !== 'object'
            || Array.isArray(expectedNode) || typeof expectedId !== 'string'
            || !expectedId) {
          return failure(
            'layout.request.node.id.required',
            '/nodes/' + expectedIndex + '/id');
        }
        if (Object.prototype.hasOwnProperty.call(expected, expectedId)) {
          return failure(
            'layout.request.node.id.duplicate',
            '/nodes/' + expectedIndex + '/id');
        }
        expected[expectedId] = expectedIndex;
      }
    }
    var positions = Object.create(null);
    var seen = Object.create(null);
    for (var index = 0; index < definition.nodes.length; index++) {
      var node = definition.nodes[index];
      var nodePath = '/nodes/' + index;
      if (!node || typeof node !== 'object' || Array.isArray(node)) {
        return failure('node.type.object', nodePath);
      }
      var id = node.id;
      if (typeof id !== 'string' || !id) {
        return failure('node.id.required', nodePath + '/id');
      }
      if (Object.prototype.hasOwnProperty.call(seen, id)) {
        return failure('node.id.duplicate', nodePath + '/id');
      }
      if (expected
          && !Object.prototype.hasOwnProperty.call(expected, id)) {
        return failure('node.id.unexpected', nodePath + '/id');
      }
      if (!orchestrationNodeHasPosition(node)) {
        return failure('node.pos.finite', nodePath + '/pos');
      }
      seen[id] = true;
      if (expected) delete expected[id];
      positions[id] = { x: node.pos.x, y: node.pos.y };
    }
    if (expected) {
      var missing = Object.keys(expected)[0];
      if (missing !== undefined) {
        return failure(
          'node.id.missing', '/nodes/' + expected[missing] + '/id');
      }
    }
    return { ok: true, positions: positions };
  } catch (cause) {
    return failure('definition.projection.failed', '', cause);
  }
}

