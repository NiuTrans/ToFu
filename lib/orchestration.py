"""lib/orchestration.py — Orchestration definition schema + validator.

An *orchestration definition* is the declarative graph a user authors in
the frontend Orchestration Studio (``static/js/orchestration.js``). It
describes a topology of ROLE agents and CONTROL nodes wired by directed
edges — an endpoint-style loop, a fan-out/synthesize flow, etc.

This module is the **contract seam**: it owns the schema constants and a
pure ``validate_definition()`` that both the REST store
(``routes/api_v1/orchestrations.py``) and the future execution engine
import. Keeping validation here (not in the route) means the engine
validates with the exact same rules the authoring API enforced.

The definition is intentionally NOT executed here. Per CLAUDE.md the
frontend authors JSON; the backend stores + validates it now, and a
swarm-backed interpreter will consume it later.

Schema (``tofu.orchestration/v1``)::

    {
      "schema": "tofu.orchestration/v1",
      "name":   "Endpoint Loop",
      "nodes": [
        {"id": "planner1", "type": "role", "role": "planner",
         "name": "Planner", "pos": {"x": 1, "y": 2}, "params": {...}},
        {"id": "loop1", "type": "control", "kind": "loop",
         "pos": {...}, "params": {"max_iterations": 10, ...}}
      ],
      "edges": [{"from": "planner1", "to": "loop1"}]
    }
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

SCHEMA_ID = 'tofu.orchestration/v1'

#: Role names the executor will eventually map to swarm ``AGENT_ROLES``
#: (lib/swarm/registry.py) plus the endpoint-style conceptual roles
#: (planner / worker / critic) and composition roles (synthesizer /
#: router). Unknown roles are a *warning*, not an error — the studio is
#: an authoring surface and roles may be user-defined before the engine
#: learns them.
KNOWN_ROLES = frozenset({
    # swarm AGENT_ROLES
    'researcher', 'coder', 'analyst', 'browser', 'reviewer', 'writer', 'general',
    # endpoint-style + composition roles
    'planner', 'worker', 'critic', 'synthesizer', 'router',
})

#: Control-node kinds and whether at most one may exist per definition.
CONTROL_KINDS = {
    'start':    {'single': True},
    'stop':     {'single': True},
    'loop':     {'single': False},
    'parallel': {'single': False},
    'barrier':  {'single': False},
    'branch':   {'single': False},
    'artifact': {'single': False},
    'human':    {'single': False},
}

#: Valid artifact 'format' hints (mirror the studio inspector dropdown).
VALID_ARTIFACT_FORMATS = frozenset({'file', 'report', 'dataset', 'code', 'image'})

#: Human-in-the-loop gate modes (mirror the studio inspector dropdown):
#: ``approve`` blocks for an approve/reject decision, ``input`` blocks to
#: collect a free-text answer that is appended to the flow context, and
#: ``notify`` is non-blocking — it just surfaces a message to the user.
VALID_HUMAN_MODES = frozenset({'approve', 'input', 'notify'})

MAX_ARTIFACT_PATH_LEN = 512

VALID_TIERS = frozenset({'light', 'standard', 'heavy'})
VALID_ISOLATION = frozenset({'fresh-context', 'shared-context'})

MAX_NAME_LEN = 120
MAX_NODES = 200
MAX_OBJECTIVE_LEN = 4000


def validate_definition(defn: Any) -> dict[str, Any]:
    """Validate an orchestration definition.

    Pure function — no I/O, no mutation of the input. Returns a verdict
    dict so both the REST layer and the engine can decide what to do.

    Args:
        defn: The candidate definition (already JSON-parsed).

    Returns:
        ``{'ok': bool, 'errors': [str], 'warnings': [str]}``. ``ok`` is
        True iff ``errors`` is empty; ``warnings`` never block.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(defn, dict):
        return {'ok': False, 'errors': ['definition must be a JSON object'],
                'warnings': []}

    schema = defn.get('schema')
    if schema != SCHEMA_ID:
        warnings.append(f'unexpected schema {schema!r} (expected {SCHEMA_ID!r})')

    name = defn.get('name', '')
    if not isinstance(name, str) or not name.strip():
        errors.append('name is required and must be a non-empty string')
    elif len(name) > MAX_NAME_LEN:
        errors.append(f'name exceeds {MAX_NAME_LEN} chars')

    nodes = defn.get('nodes')
    edges = defn.get('edges')
    if not isinstance(nodes, list):
        errors.append('nodes must be an array')
        nodes = []
    if not isinstance(edges, list):
        errors.append('edges must be an array')
        edges = []

    if len(nodes) > MAX_NODES:
        errors.append(f'too many nodes ({len(nodes)} > {MAX_NODES})')

    ids: set[str] = set()
    kind_counts: dict[str, int] = {}
    role_count = 0

    for i, node in enumerate(nodes):
        where = f'node[{i}]'
        if not isinstance(node, dict):
            errors.append(f'{where} must be an object')
            continue
        nid = node.get('id')
        if not isinstance(nid, str) or not nid:
            errors.append(f'{where} missing string id')
            continue
        where = f'node {nid!r}'
        if nid in ids:
            errors.append(f'duplicate node id {nid!r}')
        ids.add(nid)

        ntype = node.get('type')
        params = node.get('params') or {}
        if not isinstance(params, dict):
            errors.append(f'{where} params must be an object')
            params = {}

        if ntype == 'role':
            role_count += 1
            role = node.get('role')
            if not isinstance(role, str) or not role:
                errors.append(f'{where} role node missing role')
            elif role not in KNOWN_ROLES:
                warnings.append(f'{where} unknown role {role!r} (engine may '
                                'not map it until registered)')
            tier = params.get('tier')
            if tier is not None and tier not in VALID_TIERS:
                errors.append(f'{where} invalid tier {tier!r}')
            iso = params.get('isolation')
            if iso is not None and iso not in VALID_ISOLATION:
                errors.append(f'{where} invalid isolation {iso!r}')
            obj = params.get('objective')
            if isinstance(obj, str) and len(obj) > MAX_OBJECTIVE_LEN:
                errors.append(f'{where} objective exceeds {MAX_OBJECTIVE_LEN} chars')
        elif ntype == 'control':
            kind = node.get('kind')
            if kind not in CONTROL_KINDS:
                errors.append(f'{where} invalid control kind {kind!r}')
            else:
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
                if kind == 'artifact':
                    path = params.get('path')
                    if path is not None and not isinstance(path, str):
                        errors.append(f'{where} artifact path must be a string')
                    elif isinstance(path, str) and len(path) > MAX_ARTIFACT_PATH_LEN:
                        errors.append(f'{where} artifact path exceeds '
                                      f'{MAX_ARTIFACT_PATH_LEN} chars')
                    fmt = params.get('format')
                    if fmt is not None and fmt not in VALID_ARTIFACT_FORMATS:
                        warnings.append(f'{where} unknown artifact format {fmt!r}')
                    if not (isinstance(path, str) and path.strip()):
                        warnings.append(f'{where} artifact has no path — it will be '
                                        'recorded but unnamed')
                elif kind == 'human':
                    mode = params.get('mode')
                    if mode is not None and mode not in VALID_HUMAN_MODES:
                        errors.append(f'{where} invalid human mode {mode!r}')
                    prompt = params.get('prompt')
                    if isinstance(prompt, str) and len(prompt) > MAX_OBJECTIVE_LEN:
                        errors.append(f'{where} prompt exceeds {MAX_OBJECTIVE_LEN} chars')
        else:
            errors.append(f'{where} invalid type {ntype!r} (expected '
                          "'role' or 'control')")

    # Single-instance control nodes.
    for kind, cfg in CONTROL_KINDS.items():
        if cfg['single'] and kind_counts.get(kind, 0) > 1:
            errors.append(f'at most one {kind!r} node allowed '
                          f'(found {kind_counts[kind]})')

    # Edge validation.
    seen_edges: set[tuple[str, str]] = set()
    id_to_node = {n.get('id'): n for n in nodes if isinstance(n, dict)}
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f'edge[{i}] must be an object')
            continue
        src = edge.get('from')
        dst = edge.get('to')
        if src not in ids:
            errors.append(f'edge[{i}] from {src!r} references unknown node')
        if dst not in ids:
            errors.append(f'edge[{i}] to {dst!r} references unknown node')
        if src == dst:
            errors.append(f'edge[{i}] self-loop on {src!r}')
        if (src, dst) in seen_edges:
            warnings.append(f'duplicate edge {src!r}→{dst!r}')
        seen_edges.add((src, dst))
        # A Start node has no input; a Stop node has no output.
        sn = id_to_node.get(src)
        dn = id_to_node.get(dst)
        if dn and dn.get('kind') == 'start':
            errors.append(f'edge[{i}] targets a start node (start has no input)')
        if sn and sn.get('kind') == 'stop':
            errors.append(f'edge[{i}] leaves a stop node (stop has no output)')

    # Structural soft-guidance (warnings only — a draft may be incomplete).
    if nodes:
        if kind_counts.get('start', 0) == 0:
            warnings.append('no start node — the engine will not know where to begin')
        if kind_counts.get('stop', 0) == 0:
            warnings.append('no stop node — the flow has no defined terminal')
        if role_count == 0:
            warnings.append('no agent nodes — the flow does no work')

    return {'ok': not errors, 'errors': errors, 'warnings': warnings}


def build_endpoint_definition(*, name: str = 'Endpoint Loop',
                              max_iterations: int = 10,
                              verifier: str = 'critic') -> dict:
    """Build the canonical endpoint-mode flow as a definition.

    Expresses Tofu's endpoint mode — Planner → loop[Worker → Critic] → Stop —
    as a ``tofu.orchestration/v1`` graph the :class:`FlowExecutor` can run.
    The Worker is ``shared-context`` so it accumulates its prior attempt +
    the critic's feedback across iterations (the engine reproduces endpoint's
    progress-carryover); the verifier loops back to the loop node.

    This is the single source of truth bridging endpoint mode and the
    declarative engine: a future cutover runs THIS definition instead of the
    bespoke loop in ``lib/tasks_pkg/endpoint.py``. Kept here (not in the
    engine) so it is validated + laid out by the same pure helpers.
    """
    defn = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'planner', 'type': 'role', 'role': 'planner',
             'params': {'objective': 'Rewrite the request into a structured '
                        'brief + checklist for the worker.'}},
            {'id': 'loop', 'type': 'control', 'kind': 'loop',
             'params': {'max_iterations': int(max_iterations),
                        'stop_condition': 'verdict:STOP', 'verifier': verifier}},
            {'id': 'worker', 'type': 'role', 'role': 'worker',
             'params': {'isolation': 'shared-context', 'tier': 'heavy',
                        'objective': 'Execute the plan. Your first tool call '
                        'MUST be state-changing — act, do not just analyze.'}},
            {'id': 'critic', 'type': 'role', 'role': verifier,
             'params': {'objective': 'Review the worker output against the '
                        'checklist. End with [VERDICT: STOP] or '
                        '[VERDICT: CONTINUE_WORKER].'}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'planner'},
            {'from': 'planner', 'to': 'loop'},
            {'from': 'loop', 'to': 'worker'},
            {'from': 'worker', 'to': 'critic'},
            {'from': 'critic', 'to': 'loop'},
            {'from': 'loop', 'to': 'stop'},
        ],
    }
    layout_definition(defn)
    return defn


def layout_definition(defn: dict, *, x_gap: int = 230, y_gap: int = 150,
                      x0: int = 40, y0: int = 30) -> dict:
    """Assign node ``pos`` by graph layering (pure; returns *defn*).

    The frontend is a thin renderer, so position computation lives here.
    Layers are derived by relaxing ``layer[v] = max(layer[u]+1)`` over
    every edge, bounded by ``len(nodes)`` passes so a cycle (e.g. an
    endpoint loop's critic→loop back-edge) can't spin forever. Nodes are
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


__all__ = [
    'SCHEMA_ID', 'KNOWN_ROLES', 'CONTROL_KINDS',
    'VALID_TIERS', 'VALID_ISOLATION', 'VALID_ARTIFACT_FORMATS', 'VALID_HUMAN_MODES',
    'validate_definition',
    'layout_definition', 'build_endpoint_definition',
]
