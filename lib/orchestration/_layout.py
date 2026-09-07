"""lib/orchestration/_layout.py — Canvas layout (backend-owned positioning).

The frontend is a thin renderer, so a definition's node coordinates are
computed here by :func:`layout_definition` — BFS layering + a barycenter
crossing-minimization sweep into clean top-down lanes.

See :mod:`lib.orchestration` for the package overview.
"""

from __future__ import annotations

import copy
import math

from lib.log import get_logger

logger = get_logger(__name__)


def has_definition_layout(defn: dict) -> bool:
    """Return whether every node has finite numeric display coordinates."""
    nodes = defn.get('nodes') if isinstance(defn, dict) else None
    if not isinstance(nodes, list):
        return False

    def finite_coordinate(value: object) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError as e:
            # Arbitrarily large JSON integers cannot be projected into a
            # browser coordinate even though Python still considers them ints.
            logger.debug(
                '[OrchestrationLayout] coordinate %r is not browser-finite: %s',
                value, e)
            return False

    return all(
        isinstance(node, dict)
        and isinstance(node.get('pos'), dict)
        and finite_coordinate(node['pos'].get('x'))
        and finite_coordinate(node['pos'].get('y'))
        for node in nodes
    )


def project_definition_layout(defn: dict) -> dict:
    """Return a detached definition, laying it out only when coordinates lack.

    Durable reads use this migration-safe projection for old or inline run
    snapshots. Stored authoring data is never mutated, and an intentional
    complete user layout remains byte-for-byte unchanged.
    """
    projected = copy.deepcopy(defn)
    if not has_definition_layout(projected):
        layout_definition(projected)
    return projected


def layout_definition(defn: dict, *, x_gap: int = 230, y_gap: int = 150,
                      x0: int = 40, y0: int = 30) -> dict:
    """Assign node ``pos`` by graph layering (pure; returns *defn*).

    The frontend is a thin renderer, so position computation lives here.
    Layers are derived by relaxing ``layer[v] = max(layer[u]+1)`` over
    every edge, bounded by ``len(nodes)`` passes so a cycle (e.g. an
    verifier loop's critic→loop back-edge) can't spin forever. Nodes are
    then spread horizontally within their layer.

    Mutates each node's ``pos`` in place and returns the same dict for
    chaining. Nodes already carrying a plausible ``pos`` are repositioned
    too (the LLM rarely supplies good coordinates).
    """
    nodes = defn.get('nodes') or []
    edges = defn.get('edges') or []
    if not nodes:
        return defn

    ids = [n.get('id') for n in nodes if isinstance(n, dict) and n.get('id')]
    id_set = set(ids)
    indeg: dict[str, int] = {nid: 0 for nid in ids}
    adj: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in edges:
        if not isinstance(e, dict):
            continue
        s, d = e.get('from'), e.get('to')
        if s in id_set and d in id_set:
            adj[s].append(d)
            indeg[d] += 1

    # BFS shortest-path layering from the sources. Using BFS (not
    # longest-path relaxation) means each node is assigned the first
    # depth it is reached at and never revisited, so a loop back-edge
    # (e.g. critic→loop) does NOT inflate downstream layers.
    from collections import deque

    seeds = [n.get('id') for n in nodes
             if isinstance(n, dict) and n.get('id')
             and (n.get('kind') == 'start' or indeg.get(n.get('id'), 0) == 0)]
    if not seeds and ids:
        seeds = [ids[0]]

    layer: dict[str, int] = {}
    queue: deque = deque()
    for sid in seeds:
        layer[sid] = 0
        queue.append(sid)
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v not in layer:
                layer[v] = layer[u] + 1
                queue.append(v)

    # Any unreached node (disconnected) → place after the deepest layer.
    max_layer = max(layer.values()) if layer else 0
    for nid in ids:
        layer.setdefault(nid, max_layer + 1)

    # Group by layer.
    by_layer: dict[int, list[str]] = {}
    for nid in ids:
        by_layer.setdefault(layer[nid], []).append(nid)

    # ── Crossing minimization (Sugiyama ordering step) ──
    # Order nodes WITHIN each layer by the barycenter (mean order index)
    # of their neighbors in the adjacent layer, alternating down/up
    # sweeps until it settles. This pulls children directly under their
    # parents, so edges read as mostly-vertical, non-crossing lanes
    # instead of long diagonals. Only the x-order within a layer changes;
    # the layer (y) assigned above is untouched, so layering invariants
    # (loop back-edges stay shallow, orphans/cycles keep their depth) hold.
    undirected: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in edges:
        if not isinstance(e, dict):
            continue
        s, d = e.get('from'), e.get('to')
        if s in id_set and d in id_set and s != d:
            undirected[s].append(d)
            undirected[d].append(s)

    order: dict[str, int] = {}
    for members in by_layer.values():
        for i, nid in enumerate(members):
            order[nid] = i

    layers_sorted = sorted(by_layer)
    for sweep in range(4):
        going_down = sweep % 2 == 0
        seq = layers_sorted if going_down else layers_sorted[::-1]
        for lyr in seq:
            adj_lyr = lyr - 1 if going_down else lyr + 1
            keyed = []
            for nid in by_layer[lyr]:
                refs = [order[v] for v in undirected[nid] if layer.get(v) == adj_lyr]
                # No neighbor in the reference layer → keep current index.
                bary = sum(refs) / len(refs) if refs else float(order[nid])
                keyed.append((bary, order[nid], nid))
            keyed.sort()
            by_layer[lyr] = [nid for _, _, nid in keyed]
            for i, nid in enumerate(by_layer[lyr]):
                order[nid] = i

    # Assign coordinates, centering each layer under the widest one.
    widest = max((len(v) for v in by_layer.values()), default=1)
    id_to_node = {n.get('id'): n for n in nodes if isinstance(n, dict)}
    for lyr in layers_sorted:
        members = by_layer[lyr]
        offset = (widest - len(members)) * x_gap // 2
        for i, nid in enumerate(members):
            node = id_to_node.get(nid)
            if node is not None:
                node['pos'] = {'x': x0 + offset + i * x_gap, 'y': y0 + lyr * y_gap}
    return defn
