"""Pure authored-graph to chat-presentation projection."""

from __future__ import annotations

from lib.orchestration._subflow_contract import MAX_SUBFLOW_DEPTH


def chat_projection_for_flow(definition: dict) -> str:
    """Return ``autopilot``, ``endpoint`` or generic ``flow`` presentation."""
    roles: set[str] = set()
    seen: set[int] = set()

    def collect(graph: dict, depth: int = 0) -> None:
        if not isinstance(graph, dict) or id(graph) in seen:
            return
        if depth > MAX_SUBFLOW_DEPTH:
            return
        seen.add(id(graph))
        for node in graph.get('nodes') or []:
            if not isinstance(node, dict):
                continue
            if node.get('type') == 'role':
                roles.add(str(node.get('role') or ''))
            child = (node.get('params') or {}).get('definition')
            if isinstance(child, dict):
                collect(child, depth + 1)

    collect(definition)
    if 'virtual_user' in roles:
        return 'autopilot'
    if roles.intersection({'planner', 'critic', 'reviewer'}):
        return 'endpoint'
    return 'flow'
