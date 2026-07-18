"""lib/orchestration/_validate.py — Schema constants + the pure validator.

Owns the top-level schema id, the control-node kind table, the artifact /
human-gate + structural caps, and the pure :func:`validate_definition`
that both the REST store and the execution engine share. Also owns the
brief-rendering (:func:`render_role_brief`) and opening-phase
classification (:func:`first_executed_role` / :func:`initial_phase_for_flow`)
helpers.

See :mod:`lib.orchestration` for the package overview.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.orchestration._io import (
    _coerce_list,
    _validate_node_io,
)
from lib.orchestration._roles import (
    KNOWN_ROLES,
    MAX_OBJECTIVE_LEN,
    VALID_EMITS,
    VALID_ISOLATION,
    VALID_SCOPES,
    VALID_TIERS,
    _validate_role_params,
    resolve_emits,
    role_param_schema,
)

logger = get_logger(__name__)

SCHEMA_ID = 'tofu.orchestration/v1'

#: Nesting cap for subflow expansion — defense against pathological or
#: (via a ref resolver) self-referential nesting.
MAX_SUBFLOW_DEPTH = 5

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

MAX_NAME_LEN = 120
MAX_NODES = 200


def render_role_brief(node: dict) -> str:
    """Compose a role node's structured params into a delegation brief.

    This is the bridge from the authoring layer to the execution layer: the
    engine fills ``SubTaskSpec.objective`` with this rendered text (the swarm
    stays dumb — it still just wraps the result in ``## Your Task``).

    Back-compat invariant: a node whose only meaningful param is ``objective``
    (no other structured fields set) returns **exactly** ``objective`` —
    byte-identical to the pre-structured-params behavior — so every existing
    definition, ``build_endpoint_definition`` and ``build_autopilot_definition``
    render unchanged.

    Composition rule: the ``objective`` field renders as a bare lead paragraph
    (no heading); every other set field renders as a ``### <heading>`` section.
    List fields become ``- item`` bullets; bool fields render only when true;
    select fields render their stored value. Empty/unset fields are omitted.
    Section order follows the role's schema order. Pure; never raises.
    """
    params = node.get('params') or {}
    role = node.get('role') or ''
    schema = role_param_schema(role)

    lead = ''
    sections: list[str] = []
    for spec in schema:
        key = spec.get('key')
        kind = spec.get('kind')
        val = params.get(key)
        if key == 'objective':
            lead = (val or '').strip() if isinstance(val, str) else ''
            continue
        heading = spec.get('heading') or key
        if kind == 'list':
            items = _coerce_list(val)
            if items:
                body = '\n'.join(f'- {it}' for it in items)
                sections.append(f'### {heading}\n{body}')
        elif kind == 'bool':
            if val is True:
                sections.append(f'### {heading}\nYes.')
        elif kind in ('text', 'textarea', 'select'):
            s = (val or '').strip() if isinstance(val, str) else ''
            if s:
                sections.append(f'### {heading}\n{s}')
        elif kind == 'int':
            if isinstance(val, int):
                sections.append(f'### {heading}\n{val}')

    if not sections:
        return lead
    parts = ([lead] if lead else []) + sections
    return '\n\n'.join(parts)


#: Engine role → the endpoint UI phase (and streaming-bubble role) it maps to.
#: A planner node opens the loop with a Planner bubble; a verifier (critic /
#: reviewer / virtual_user) lands on the user side ("reviewing"); every other
#: producer role streams as a Worker. Mirrors ``EndpointEventAdapter``'s
#: role/emits classification so the bubble the FRONTEND creates up front
#: matches the first message the adapter will actually emit.
_PLANNER_ROLES = frozenset({'planner'})


def first_executed_role(defn: dict) -> dict | None:
    """Return the first ROLE node the engine would run, or ``None``.

    Walks the graph from the start node following single ``from→to`` edges,
    skipping control nodes (start / loop / parallel / barrier / branch /
    artifact), and returns the first ``type == 'role'`` (or ``subflow``) node
    encountered. This is a static, side-effect-free preview of "what bubble
    comes first" — used to pick the initial chat phase so a plannerless flow
    (e.g. autopilot: worker→vu) never shows a hanging Planner placeholder.

    Pure; never raises. Returns ``None`` for a graph with no reachable role.
    """
    if not isinstance(defn, dict):
        return None
    nodes = {n.get('id'): n for n in defn.get('nodes') or []
             if isinstance(n, dict) and n.get('id')}
    fwd: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in defn.get('edges') or []:
        if not isinstance(e, dict):
            continue
        s, d = e.get('from'), e.get('to')
        if s in nodes and d in fwd:
            fwd[s].append(d)
    # Locate start (explicit start kind, else a source node).
    start = None
    for nid, n in nodes.items():
        if n.get('kind') == 'start':
            start = nid
            break
    if start is None:
        rev_targets = {d for outs in fwd.values() for d in outs}
        for nid in nodes:
            if nid not in rev_targets:
                start = nid
                break
    if start is None:
        return None
    seen: set[str] = set()
    cur = start
    while cur and cur not in seen:
        seen.add(cur)
        n = nodes.get(cur) or {}
        if n.get('type') in ('role', 'subflow'):
            return n
        nxt = fwd.get(cur) or []
        cur = nxt[0] if nxt else None
    return None


def initial_phase_for_flow(defn: dict) -> str:
    """Classify a flow's opening chat phase from its first role node.

    Returns one of ``'planning'`` | ``'reviewing'`` | ``'working'`` — the
    same vocabulary ``routes/chat.py`` ships as ``endpointPhase`` and the
    frontend maps to the planner / critic / worker streaming bubble. A flow
    that opens on a ``planner`` role → ``'planning'``; one that opens on a
    verifier (its first turn lands user-side) → ``'reviewing'``; everything
    else (the common worker-first / autopilot case) → ``'working'``.

    Pure; never raises. Defaults to ``'working'`` when no role is found.
    """
    node = first_executed_role(defn)
    if not node:
        return 'working'
    role = node.get('role') or ''
    if role in _PLANNER_ROLES:
        return 'planning'
    if resolve_emits(node) == 'user':
        return 'reviewing'
    return 'working'


def _validate_subflow_node(node: dict, where: str, params: dict,
                           errors: list, warnings: list,
                           depth: int, seen_refs: frozenset[str]) -> None:
    """Validate a ``subflow`` node (a "big role" composed of small roles).

    A subflow node embeds (``params.definition``) or references
    (``params.ref`` — a stored orchestration id) a complete child
    ``tofu.orchestration/v1`` definition. To the parent it is one node with
    its own ``role`` label + ``emits``; internally it is a self-contained
    flow with its own start/stop and context organisation. Validation
    recurses into an embedded definition (bounded by
    :data:`MAX_SUBFLOW_DEPTH`) and detects ref cycles. A bare ``ref`` is NOT
    resolved here (the validator is pure / I/O-free) — the engine resolves +
    re-validates it at expansion time; we only guard against a subflow
    referencing an ancestor (direct self-include).
    """
    emits = params.get('emits')
    if emits is not None and emits not in VALID_EMITS:
        errors.append(f'{where} invalid emits {emits!r} '
                      f'(expected one of {sorted(VALID_EMITS)})')

    scope = params.get('scope')
    if scope is not None and scope not in VALID_SCOPES:
        errors.append(f'{where} invalid scope {scope!r} '
                      f'(expected one of {sorted(VALID_SCOPES)})')

    child = params.get('definition')
    ref = params.get('ref')
    if child is None and ref is None:
        errors.append(f'{where} subflow needs params.definition (embedded) '
                      'or params.ref (stored id)')
        return

    if ref is not None:
        if not isinstance(ref, str) or not ref:
            errors.append(f'{where} subflow ref must be a non-empty string')
        elif ref in seen_refs:
            errors.append(f'{where} subflow ref {ref!r} is recursive '
                          '(references an ancestor flow)')
        return  # embedded definition (if any) is validated below; ref is opaque here

    if depth + 1 > MAX_SUBFLOW_DEPTH:
        errors.append(f'{where} subflow nesting exceeds MAX_SUBFLOW_DEPTH '
                      f'({MAX_SUBFLOW_DEPTH})')
        return

    sub = validate_definition(child, _depth=depth + 1, _seen_refs=seen_refs)
    for e in sub['errors']:
        errors.append(f'{where} subflow: {e}')
    for w in sub['warnings']:
        warnings.append(f'{where} subflow: {w}')


def validate_definition(defn: Any, *, _depth: int = 0,
                        _seen_refs: frozenset[str] = frozenset()) -> dict[str, Any]:
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
            emits = params.get('emits')
            if emits is not None and emits not in VALID_EMITS:
                errors.append(f'{where} invalid emits {emits!r} '
                              f'(expected one of {sorted(VALID_EMITS)})')
            _validate_role_params(role if isinstance(role, str) else '',
                                  where, params, errors, warnings)
        elif ntype == 'subflow':
            role_count += 1
            _validate_subflow_node(node, where, params, errors, warnings,
                                   _depth, _seen_refs)
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
                          "'role', 'subflow' or 'control')")

    # Single-instance control nodes.
    for kind, cfg in CONTROL_KINDS.items():
        if cfg['single'] and kind_counts.get(kind, 0) > 1:
            errors.append(f'at most one {kind!r} node allowed '
                          f'(found {kind_counts[kind]})')

    id_to_node = {n.get('id'): n for n in nodes if isinstance(n, dict)}

    # Typed I/O contract — validated in a second pass so an input ``from``
    # ref may point at a node declared later in the array.
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = node.get('id')
        if not isinstance(nid, str) or not nid:
            continue
        nparams = node.get('params') or {}
        if isinstance(nparams, dict):
            _validate_node_io(node, f'node {nid!r}', nparams, ids,
                              id_to_node, errors, warnings)

    # Edge validation.
    seen_edges: set[tuple[str, str]] = set()
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
