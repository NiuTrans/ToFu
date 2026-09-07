"""Dry-run compiler for validated orchestration definitions.

The compiler projects a graph into the ordered preview consumed by Studio. It
does not construct a runtime executor or agent, and shares adjacency semantics
with execution through :class:`lib.orchestration_graph.GraphNavigator`.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration._subflow_expansion import expand_subflows
from lib.orchestration._validate import validate_definition
from lib.orchestration_graph import GraphNavigator

logger = get_logger(__name__)


def compile_plan(definition: dict) -> dict:
    """Describe execution order without running agents."""
    verdict = validate_definition(definition)
    if not verdict['ok']:
        return {'ok': False, 'steps': [], 'error': '; '.join(verdict['errors'])}

    try:
        definition = expand_subflows(definition)
    except ValueError as error:
        logger.debug('[FlowPlan] subflow expansion failed: %s', error)
        return {'ok': False, 'steps': [], 'error': f'subflow: {error}'}

    nodes = {node['id']: node for node in definition.get('nodes', [])}
    navigator = GraphNavigator.from_edges(nodes, definition.get('edges', []))
    current = navigator.find_start()
    steps: list[dict] = []
    seen: set[str] = set()
    guard = 0

    while current and guard < len(nodes) * 3:
        guard += 1
        if current in seen:
            steps.append({'node_id': current, 'action': 'loop-back'})
            break
        seen.add(current)
        node = nodes.get(current)
        if not node:
            break
        if node.get('type') == 'role':
            steps.append({
                'node_id': current,
                'role': node.get('role'),
                'action': 'run-agent',
            })
        elif node.get('type') == 'subflow':
            steps.append({
                'node_id': current,
                'role': node.get('role'),
                'action': 'run-subflow',
                'scope': resolve_node_runtime_param(node, 'scope'),
            })
        elif node.get('kind') == 'artifact':
            steps.append({
                'node_id': current,
                'kind': 'artifact',
                'action': 'declare-deliverable',
                'path': resolve_node_runtime_param(node, 'path'),
            })
        elif node.get('kind') == 'human':
            mode = resolve_node_runtime_param(node, 'mode')
            steps.append({
                'node_id': current,
                'kind': 'human',
                'action': f'human-{mode}',
            })
        else:
            steps.append({
                'node_id': current,
                'kind': node.get('kind'),
                'action': node.get('kind'),
            })
        if node.get('kind') == 'stop':
            break
        current = navigator.single_next(current)

    return {'ok': True, 'steps': steps, 'error': None}


__all__ = ['compile_plan']
