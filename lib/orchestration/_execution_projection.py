"""Pure definition-to-execution presentation projections.

Role brief rendering and opening chat-phase classification bridge authored
graphs to execution/UI adapters. They perform no validation, persistence or
I/O and therefore live outside the whole-definition validator.
"""

from __future__ import annotations

from lib.orchestration.io_values import _coerce_list
from lib.orchestration._role_specs import role_param_schema
from lib.orchestration._runtime_params import resolve_node_runtime_param


def render_role_brief(node: dict) -> str:
    """Compose a role node's structured params into a delegation brief.

    A node whose only meaningful param is ``objective`` returns that value
    byte-identically. Other populated fields become ordered Markdown sections
    derived from the same backend FieldSpecs used by authoring and validation.
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
            value = (val or '').strip() if isinstance(val, str) else ''
            if value:
                sections.append(f'### {heading}\n{value}')
        elif kind == 'int' and isinstance(val, int):
            sections.append(f'### {heading}\n{val}')

    if not sections:
        return lead
    return '\n\n'.join(([lead] if lead else []) + sections)


#: Engine role → endpoint UI phase classification. Shared with the live event
#: adapter so the initial bubble and emitted role can never classify planners
#: from separate vocabularies.
_PLANNER_ROLES = frozenset({'planner'})


def first_executed_role(defn: dict) -> dict | None:
    """Return the first reachable role/subflow node, or ``None``.

    Walks from the explicit start node (or first source node) through the first
    outgoing edge of each control node. Pure and cycle-safe.
    """
    if not isinstance(defn, dict):
        return None
    nodes = {node.get('id'): node for node in defn.get('nodes') or []
             if isinstance(node, dict) and node.get('id')}
    forward: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in defn.get('edges') or []:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get('from'), edge.get('to')
        if source in nodes and target in forward:
            forward[source].append(target)

    start = next((node_id for node_id, node in nodes.items()
                  if node.get('kind') == 'start'), None)
    if start is None:
        targets = {target for outputs in forward.values() for target in outputs}
        start = next((node_id for node_id in nodes if node_id not in targets), None)
    if start is None:
        return None

    seen: set[str] = set()
    current = start
    while current and current not in seen:
        seen.add(current)
        node = nodes.get(current) or {}
        if node.get('type') in ('role', 'subflow'):
            return node
        outputs = forward.get(current) or []
        current = outputs[0] if outputs else None
    return None


def initial_phase_for_flow(defn: dict) -> str:
    """Classify a flow opening as planning, reviewing or working."""
    node = first_executed_role(defn)
    if not node:
        return 'working'
    if (node.get('role') or '') in _PLANNER_ROLES:
        return 'planning'
    if resolve_node_runtime_param(node, 'emits') == 'user':
        return 'reviewing'
    return 'working'
