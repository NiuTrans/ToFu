"""lib/orchestration/_build.py — Canonical flow builders + subflow expansion.

Owns the server-authored reference flows (:func:`build_endpoint_definition`,
:func:`build_autopilot_definition`) — the single source of truth bridging
endpoint / autopilot modes and the declarative engine — and the phase-1
:func:`expand_subflows` macro that flattens inline subflows into the parent
graph.

See :mod:`lib.orchestration` for the package overview.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.orchestration._layout import layout_definition
from lib.orchestration._roles import resolve_scope
from lib.orchestration._validate import MAX_SUBFLOW_DEPTH, SCHEMA_ID

logger = get_logger(__name__)


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


def build_autopilot_definition(*, name: str = 'Autopilot',
                               max_iterations: int = 12,
                               worker: str = 'worker') -> dict:
    """Build the canonical autopilot (virtual-user) flow as a definition.

    Expresses autopilot mode — a ``worker`` that keeps going because a
    ``virtual_user`` auto-replies at every natural stop — as a
    ``tofu.orchestration/v1`` graph:

        start → loop[ worker(assistant) → virtual_user(user) ] → stop

    The virtual user emits a ``user`` turn (the message axis this change
    introduces), and signals completion with ``[VU: TASK_DONE]`` (mapped to
    the loop's STOP verdict). The worker is ``shared-context`` so it
    accumulates the running conversation across turns. This is the single
    source of truth bridging autopilot mode and the declarative engine —
    the sibling of :func:`build_endpoint_definition`.
    """
    defn = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'loop', 'type': 'control', 'kind': 'loop',
             'params': {'max_iterations': int(max_iterations),
                        'stop_condition': 'verdict:STOP', 'verifier': 'virtual_user'}},
            {'id': 'worker', 'type': 'role', 'role': worker,
             'params': {'isolation': 'shared-context', 'tier': 'heavy',
                        'emits': 'assistant',
                        'objective': 'Continue the task. Make concrete '
                        'progress every turn; act, do not just analyze.'}},
            {'id': 'vu', 'type': 'role', 'role': 'virtual_user',
             # The VU PERSONA (the full project-owner driver prompt incl. the
             # mandatory [PROGRESS: resolved=X remaining=Y] hard-signal line)
             # is injected as this node's SYSTEM prompt via the single-sourced
             # registry suffix (AGENT_ROLES['virtual_user'] → VU_ROLE_PROMPT);
             # it does NOT belong in the objective, which is capped at
             # MAX_OBJECTIVE_LEN (4000) and would reject the 4.5k persona. The
             # objective is just the per-flow delegation brief.
             'params': {'emits': 'user', 'tier': 'standard',
                        'objective': 'Stand in for the human and drive the '
                        'task to completion per your virtual-user role. Emit '
                        '[VERDICT: STOP] (or [VU: TASK_DONE]) only when the '
                        'objective is genuinely met.'}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'loop'},
            {'from': 'loop', 'to': 'worker'},
            {'from': 'worker', 'to': 'vu'},
            {'from': 'vu', 'to': 'loop'},
            {'from': 'loop', 'to': 'stop'},
        ],
    }
    layout_definition(defn)
    return defn


def expand_subflows(defn: dict, *, resolver: Any = None, _depth: int = 0) -> dict:
    """Flatten every ``subflow`` node into the parent graph (macro expansion).

    A subflow node is inlined: its embedded child definition's inner nodes
    are spliced into the parent with namespaced ids (``<subflowId>/<childId>``),
    the child's ``start`` / ``stop`` control nodes are dropped, and the
    parent's edges into / out of the subflow node are rewired to the child's
    real entry / exit nodes. Subroutine-inlining semantics.

    This is the phase-1 nesting strategy: the result is a single flat graph
    the existing :class:`FlowExecutor` runs unchanged. Inlining deliberately
    does NOT create a context boundary (inner nodes share the parent
    context).

    Only ``inline``-scoped subflows (the default — see
    :func:`resolve_scope`) are flattened here. An ``isolated`` subflow is the
    true black box: it is left **intact** as a subflow node so the engine can
    run it in its own nested :class:`FlowExecutor` with a fresh context. Its
    embedded child is therefore NOT expanded by the parent — the nested
    executor expands its own inline subflows when it is constructed.

    Args:
        defn: A validated definition (possibly containing subflow nodes).
        resolver: Optional ``callable(ref:str) -> definition|None`` used to
            resolve ``params.ref`` subflows from a store. Embedded
            ``params.definition`` subflows need no resolver.
        _depth: Recursion guard (bounded by :data:`MAX_SUBFLOW_DEPTH`).

    Returns:
        A NEW definition dict (input is not mutated). Positions are recomputed
        via :func:`layout_definition` so the flattened graph lays out cleanly.

    Raises:
        ValueError: on a ref that cannot be resolved, or nesting past the cap.
    """
    import copy

    if _depth > MAX_SUBFLOW_DEPTH:
        raise ValueError(f'subflow nesting exceeds MAX_SUBFLOW_DEPTH ({MAX_SUBFLOW_DEPTH})')

    nodes = defn.get('nodes') or []
    edges = defn.get('edges') or []
    if not any(isinstance(n, dict) and n.get('type') == 'subflow'
               and resolve_scope(n) == 'inline' for n in nodes):
        return copy.deepcopy(defn)

    out_nodes: list[dict] = []
    out_edges: list[dict] = [dict(e) for e in edges if isinstance(e, dict)]

    for node in nodes:
        if not isinstance(node, dict) or node.get('type') != 'subflow':
            out_nodes.append(copy.deepcopy(node))
            continue
        # Isolated subflows are a context boundary, not a macro — leave the
        # node (and its embedded child) intact for the nested executor.
        if resolve_scope(node) == 'isolated':
            out_nodes.append(copy.deepcopy(node))
            continue

        sid = node.get('id')
        params = node.get('params') or {}
        child = params.get('definition')
        if child is None:
            ref = params.get('ref')
            if not (resolver and ref):
                raise ValueError(f'subflow {sid!r} has a ref {ref!r} but no '
                                 'resolver was supplied to expand it')
            child = resolver(ref)
            if not isinstance(child, dict):
                raise ValueError(f'subflow {sid!r} ref {ref!r} did not resolve '
                                 'to a definition')
        # Recursively flatten the child first.
        child = expand_subflows(child, resolver=resolver, _depth=_depth + 1)

        cnodes = [n for n in (child.get('nodes') or []) if isinstance(n, dict)]
        cedges = [e for e in (child.get('edges') or []) if isinstance(e, dict)]
        prefix = f'{sid}/'

        def _pid(cid: str) -> str:
            return prefix + cid

        child_starts = {n['id'] for n in cnodes if n.get('kind') == 'start'}
        child_stops = {n['id'] for n in cnodes if n.get('kind') == 'stop'}

        # Inner entry nodes = successors of a child start; exit nodes =
        # predecessors of a child stop. These become the rewire anchors.
        entries = [e['to'] for e in cedges if e.get('from') in child_starts]
        exits = [e['from'] for e in cedges if e.get('to') in child_stops]

        # Splice inner nodes (minus start/stop), namespaced.
        for cn in cnodes:
            if cn.get('id') in child_starts or cn.get('id') in child_stops:
                continue
            spliced = copy.deepcopy(cn)
            spliced['id'] = _pid(cn['id'])
            out_nodes.append(spliced)

        # Inner edges (minus those touching child start/stop), namespaced.
        for ce in cedges:
            s, d = ce.get('from'), ce.get('to')
            if s in child_starts or d in child_stops or s in child_stops or d in child_starts:
                continue
            out_edges.append({'from': _pid(s), 'to': _pid(d)})

        # Rewire parent edges that touched the subflow node.
        rewired: list[dict] = []
        for e in out_edges:
            if e.get('to') == sid:
                for ent in entries:
                    rewired.append({'from': e['from'], 'to': _pid(ent)})
            elif e.get('from') == sid:
                for ex in exits:
                    rewired.append({'from': _pid(ex), 'to': e['to']})
            else:
                rewired.append(e)
        out_edges = rewired

    result = {
        'schema': defn.get('schema', SCHEMA_ID),
        'name': defn.get('name', ''),
        'nodes': out_nodes,
        'edges': out_edges,
    }
    layout_definition(result)
    return result
