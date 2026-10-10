"""Pure inline-subflow macro expansion."""

from __future__ import annotations

import copy
from typing import Any

from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._layout import layout_definition
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration._subflow_contract import MAX_SUBFLOW_DEPTH


def expand_subflows(
    definition: dict,
    *,
    resolver: Any = None,
    _depth: int = 0,
) -> dict:
    """Flatten inline subflow nodes into a detached parent graph.

    Isolated subflows remain black boxes for a nested executor. Embedded
    definitions are used directly; referenced definitions use the optional
    resolver. Every inlined child ID is namespaced by its parent node ID.
    """
    if _depth > MAX_SUBFLOW_DEPTH:
        raise ValueError(
            f'subflow nesting exceeds MAX_SUBFLOW_DEPTH ({MAX_SUBFLOW_DEPTH})'
        )

    nodes = definition.get('nodes') or []
    edges = definition.get('edges') or []
    if not any(isinstance(node, dict) and node.get('type') == 'subflow'
               and resolve_node_runtime_param(
                   node, 'scope') == 'inline' for node in nodes):
        return copy.deepcopy(definition)

    out_nodes: list[dict] = []
    out_edges: list[dict] = [dict(edge) for edge in edges
                             if isinstance(edge, dict)]

    for node in nodes:
        if not isinstance(node, dict) or node.get('type') != 'subflow':
            out_nodes.append(copy.deepcopy(node))
            continue
        if resolve_node_runtime_param(node, 'scope') == 'isolated':
            out_nodes.append(copy.deepcopy(node))
            continue

        subflow_id = node.get('id')
        params = node.get('params') or {}
        child = params.get('definition')
        if child is None:
            ref = params.get('ref')
            if not (resolver and ref):
                raise ValueError(
                    f'subflow {subflow_id!r} has a ref {ref!r} but no '
                    'resolver was supplied to expand it'
                )
            child = resolver(ref)
            if not isinstance(child, dict):
                raise ValueError(
                    f'subflow {subflow_id!r} ref {ref!r} did not resolve '
                    'to a definition'
                )
        child = expand_subflows(
            child, resolver=resolver, _depth=_depth + 1,
        )

        child_nodes = [candidate for candidate in child.get('nodes') or []
                       if isinstance(candidate, dict)]
        child_edges = [candidate for candidate in child.get('edges') or []
                       if isinstance(candidate, dict)]
        prefix = f'{subflow_id}/'

        def prefixed(child_id: str) -> str:
            return prefix + child_id

        child_starts = {candidate['id'] for candidate in child_nodes
                        if candidate.get('kind') == 'start'}
        child_stops = {candidate['id'] for candidate in child_nodes
                       if candidate.get('kind') == 'stop'}
        entries = [edge['to'] for edge in child_edges
                   if edge.get('from') in child_starts]
        exits = [edge['from'] for edge in child_edges
                 if edge.get('to') in child_stops]

        for child_node in child_nodes:
            if child_node.get('id') in child_starts | child_stops:
                continue
            spliced = copy.deepcopy(child_node)
            spliced['id'] = prefixed(child_node['id'])
            out_nodes.append(spliced)

        for child_edge in child_edges:
            source, target = child_edge.get('from'), child_edge.get('to')
            if source in child_starts or target in child_stops \
                    or source in child_stops or target in child_starts:
                continue
            out_edges.append({
                'from': prefixed(source),
                'to': prefixed(target),
            })

        rewired: list[dict] = []
        for edge in out_edges:
            if edge.get('to') == subflow_id:
                rewired.extend({'from': edge['from'], 'to': prefixed(entry)}
                               for entry in entries)
            elif edge.get('from') == subflow_id:
                rewired.extend({'from': prefixed(exit_id), 'to': edge['to']}
                               for exit_id in exits)
            else:
                rewired.append(edge)
        out_edges = rewired

    result = {
        'schema': definition.get('schema', SCHEMA_ID),
        'name': definition.get('name', ''),
        'nodes': out_nodes,
        'edges': out_edges,
    }
    layout_definition(result)
    return result
