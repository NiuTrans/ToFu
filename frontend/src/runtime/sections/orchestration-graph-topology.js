/* ===== migrated source: orchestration-graph-topology.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-topology.js — pure Studio topology policy

   Owns node lookup and immutable edge/node mutations. It has no wire-format,
   nested-workspace or DOM dependencies, so structural controllers can share
   one small policy surface.
   ═══════════════════════════════════════════════════════════════════ */


function orchestrationConnections(edges, nodeId) {
  var incoming = [];
  var outgoing = [];
  (Array.isArray(edges) ? edges : []).forEach(function (edge) {
    if (!edge || typeof edge !== 'object') return;
    if (edge.to === nodeId) incoming.push(edge);
    if (edge.from === nodeId) outgoing.push(edge);
  });
  return { incoming: incoming, outgoing: outgoing };
}


function createOrchestrationGraphTopology() {
  function findNode(nodes, id) {
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].id === id) return nodes[i];
    }
    return null;
  }

  function connect(nodes, edges, from, to, edgeId) {
    var source = findNode(nodes, from);
    var target = findNode(nodes, to);
    if (!source || !target) {
      return { ok: false, changed: false, reason: 'missing-node', edges: edges };
    }
    if (from === to) {
      return { ok: false, changed: false, reason: 'self-loop', edges: edges };
    }
    if (target.kind === 'start') {
      return { ok: false, changed: false, reason: 'start-input', edges: edges };
    }
    if (source.kind === 'stop') {
      return { ok: false, changed: false, reason: 'stop-output', edges: edges };
    }
    var duplicate = edges.some(function (edge) {
      return edge.from === from && edge.to === to;
    });
    if (duplicate) {
      return { ok: true, changed: false, reason: 'duplicate', edges: edges };
    }
    var resolvedEdgeId = typeof edgeId === 'function' ? edgeId() : edgeId;
    return {
      ok: true,
      changed: true,
      reason: '',
      edges: edges.concat([{ id: resolvedEdgeId, from: from, to: to }]),
    };
  }

  function deleteNode(nodes, edges, id) {
    return {
      nodes: nodes.filter(function (node) { return node.id !== id; }),
      edges: edges.filter(function (edge) {
        return edge.from !== id && edge.to !== id;
      }),
    };
  }

  function deleteEdge(edges, id) {
    return edges.filter(function (edge) { return edge.id !== id; });
  }

  function reverseEdge(nodes, edges, id) {
    var edge = edges.filter(function (candidate) {
      return candidate.id === id;
    })[0];
    if (!edge) {
      return { ok: false, changed: false, reason: 'missing-edge', edges: edges };
    }
    var nextSource = findNode(nodes, edge.to);
    var nextTarget = findNode(nodes, edge.from);
    if (!nextSource || !nextTarget) {
      return { ok: false, changed: false, reason: 'missing-node', edges: edges };
    }
    if (nextSource.kind === 'stop') {
      return { ok: false, changed: false, reason: 'stop-output', edges: edges };
    }
    if (nextTarget.kind === 'start') {
      return { ok: false, changed: false, reason: 'start-input', edges: edges };
    }
    var duplicate = edges.some(function (candidate) {
      return candidate.id !== id && candidate.from === edge.to
        && candidate.to === edge.from;
    });
    if (duplicate) {
      return { ok: false, changed: false, reason: 'duplicate', edges: edges };
    }
    return {
      ok: true,
      changed: true,
      reason: '',
      edges: edges.map(function (candidate) {
        return candidate.id === id
          ? { id: candidate.id, from: candidate.to, to: candidate.from }
          : candidate;
      }),
    };
  }

  return {
    connections: orchestrationConnections,
    findNode: findNode,
    connect: connect,
    deleteNode: deleteNode,
    deleteEdge: deleteEdge,
    reverseEdge: reverseEdge,
  };
}

