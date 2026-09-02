/* ===== migrated source: orchestration-graph-workspace.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-workspace.js — nested definition workspace policy

   Owns definition hydration/serialization and pure Group enter/exit/root
   transitions. Topology validation stays in orchestration-graph-topology.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationGraphWorkspace(options) {
  options = options || {};
  var topology = options.topology;
  var schemaId = options.schemaId || orchestrationWireFormat('definition');

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function _projectionFailure(code, path, cause) {
    var result = { ok: false, reason: 'invalid-definition', code: code,
      path: path };
    if (cause) result.cause = cause;
    return result;
  }

  function workspaceFromDefinitionResult(definition, fallbackName) {
    try {
      if (!definition || typeof definition !== 'object'
          || Array.isArray(definition)) {
        return _projectionFailure('definition.type.object', '');
      }
      if (!Array.isArray(definition.nodes)) {
        return _projectionFailure('definition.nodes.type.array', '/nodes');
      }
      if (!Array.isArray(definition.edges)) {
        return _projectionFailure('definition.edges.type.array', '/edges');
      }
      var sourceNodes = definition.nodes;
      var sourceEdges = definition.edges;
      var invalidNode = sourceNodes.findIndex(function (node) {
        return !node || typeof node !== 'object' || Array.isArray(node);
      });
      if (invalidNode >= 0) {
        return _projectionFailure('node.type.object', '/nodes/' + invalidNode);
      }
      var invalidEdge = sourceEdges.findIndex(function (edge) {
        return !edge || typeof edge !== 'object' || Array.isArray(edge);
      });
      if (invalidEdge >= 0) {
        return _projectionFailure('edge.type.object', '/edges/' + invalidEdge);
      }
      return { ok: true, workspace: _workspaceFromDefinition(
        definition, fallbackName) };
    } catch (cause) {
      return _projectionFailure('definition.projection.failed', '', cause);
    }
  }

  function _workspaceFromDefinition(definition, fallbackName) {
    var sourceNodes = definition.nodes;
    var sourceEdges = definition.edges;
    var sequence = 0;
    var needsLayout = false;
    var nodes = sourceNodes.map(function (node) {
      var suffix = /(\d+)$/.exec(node.id || '');
      if (suffix) sequence = Math.max(sequence, parseInt(suffix[1], 10));
      var hasPosition = orchestrationNodeHasPosition(node);
      var position = orchestrationNodePosition(node, { x: 20, y: 20 });
      if (!hasPosition) needsLayout = true;
      return {
        id: node.id,
        type: node.type,
        role: node.role || '',
        kind: node.kind || '',
        x: position.x,
        y: position.y,
        name: node.name || '',
        params: clone(node.params || {}),
      };
    });
    var edges = sourceEdges.map(function (edge) {
      sequence += 1;
      return { id: 'e' + sequence, from: edge.from, to: edge.to };
    });
    return {
      name: definition.name || fallbackName || 'Untitled Flow',
      nodes: nodes,
      edges: edges,
      selected: null,
      sequence: sequence,
      needsLayout: needsLayout,
    };
  }

  function workspaceFromDefinition(definition, fallbackName) {
    var result = workspaceFromDefinitionResult(definition, fallbackName);
    return result.ok ? result.workspace : null;
  }

  function enterGroup(workspace, stack, groupId, fallbackDefinition,
                      fallbackName) {
    var group = topology.findNode(workspace.nodes, groupId);
    if (!group || group.type !== 'subflow') return null;
    var frame = {
      nodes: clone(workspace.nodes),
      edges: clone(workspace.edges),
      sel: workspace.selected,
      seq: workspace.sequence,
      name: workspace.name,
      groupId: groupId,
    };
    var child = group.params && group.params.definition || fallbackDefinition;
    var projected = workspaceFromDefinitionResult(child, fallbackName);
    if (!projected.ok) return null;
    return {
      stack: stack.concat([frame]),
      workspace: projected.workspace,
    };
  }

  function definitionFromState(name, nodes, edges) {
    return {
      schema: schemaId,
      name: name,
      nodes: nodes.map(function (node) {
        return {
          id: node.id,
          type: node.type,
          role: node.role || undefined,
          kind: node.kind || undefined,
          name: node.name || undefined,
          pos: { x: Math.round(node.x), y: Math.round(node.y) },
          params: clone(node.params || {}),
        };
      }),
      edges: edges.map(function (edge) {
        return { from: edge.from, to: edge.to };
      }),
    };
  }

  function exitGroup(workspace, stack) {
    if (!stack.length) return null;
    var childDefinition = definitionFromState(
      workspace.name, workspace.nodes, workspace.edges
    );
    var frame = stack[stack.length - 1];
    var parentNodes = clone(frame.nodes);
    var group = topology.findNode(parentNodes, frame.groupId);
    if (group) {
      group.params = group.params || {};
      group.params.definition = childDefinition;
      delete group.params.ref;
    }
    return {
      stack: stack.slice(0, -1),
      workspace: {
        name: frame.name,
        nodes: parentNodes,
        edges: clone(frame.edges),
        selected: frame.sel,
        sequence: frame.seq,
        needsLayout: false,
      },
    };
  }

  function rootSnapshot(name, nodes, edges, stack) {
    var definition = definitionFromState(name, nodes, edges);
    for (var i = stack.length - 1; i >= 0; i--) {
      var frame = stack[i];
      var parentNodes = clone(frame.nodes);
      var group = topology.findNode(parentNodes, frame.groupId);
      if (group) {
        group.params = group.params || {};
        group.params.definition = definition;
        delete group.params.ref;
      }
      definition = definitionFromState(frame.name, parentNodes, frame.edges);
    }
    return definition;
  }

  return {
    workspaceFromDefinitionResult: workspaceFromDefinitionResult,
    workspaceFromDefinition: workspaceFromDefinition,
    enterGroup: enterGroup,
    exitGroup: exitGroup,
    definitionFromState: definitionFromState,
    rootSnapshot: rootSnapshot,
  };
}

