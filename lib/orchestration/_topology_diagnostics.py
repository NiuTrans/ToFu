"""Pure soft diagnostics for orchestration graph topology.

These checks describe execution hazards without rejecting an authoring draft.
Hard node/edge/schema validation remains in :mod:`._validate`.
"""

from __future__ import annotations

from lib.orchestration._role_axes import VERIFIER_ROLES
from lib.orchestration._runtime_params import resolve_node_runtime_param


def parallel_verdict_channel_issues(
    nodes: list,
    edges: list,
    id_to_node: dict,
) -> list[dict[str, str]]:
    """Warn when a concurrent branch can race on the verdict channel.

    Verifier turns write, and shared-context producers consume, the engine's
    single-valued pending feedback/directive slots. More than one such branch
    in a parallel region makes delivery order-dependent.
    """
    issues: list[dict[str, str]] = []
    index_by_id = {
        node.get('id'): index
        for index, node in enumerate(nodes)
        if isinstance(node, dict) and isinstance(node.get('id'), str)
    }
    forward: dict[str, list[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get('from'), edge.get('to')
        if isinstance(source, str) and isinstance(target, str):
            forward.setdefault(source, []).append(target)

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(forward.get(node_id, []))
        return seen

    for parallel_id, parallel_node in id_to_node.items():
        if not (isinstance(parallel_node, dict)
                and parallel_node.get('type') == 'control'
                and parallel_node.get('kind') == 'parallel'):
            continue
        branches = [node_id for node_id in forward.get(parallel_id, [])
                    if node_id in id_to_node]
        if len(branches) < 2:
            continue

        branch_reach = [reachable(node_id) for node_id in branches]
        common = set.intersection(*branch_reach) if branch_reach else set()
        barriers = {
            node_id for node_id in common
            if (id_to_node.get(node_id) or {}).get('kind') == 'barrier'
        }
        region: set[str] = set()
        for reachable_nodes in branch_reach:
            region |= reachable_nodes
        region -= common
        region.update(branches)
        region -= barriers

        offenders: list[str] = []
        for node_id in region:
            node = id_to_node.get(node_id) or {}
            if node.get('type') != 'role':
                continue
            if (
                (node.get('role') or '') in VERIFIER_ROLES
                or resolve_node_runtime_param(
                    node, 'isolation') == 'shared-context'
            ):
                offenders.append(node_id)
        if offenders:
            message = (
                f"parallel {parallel_id!r} region contains verdict-feeding "
                f"producer(s) {sorted(offenders)!r} (a verifier role or a "
                "shared-context producer) — the single-valued feedback/"
                "directive channel is consumed order-dependently across "
                "concurrent branches. Use fresh-context one-shot agents in a "
                "fan-out, or move the verifier/shared-context node out of the "
                "parallel region."
            )
            index = index_by_id.get(parallel_id)
            issues.append({
                'code': 'topology.parallel.verdict_channel_race',
                'path': f'/nodes/{index}' if index is not None else '/nodes',
                'message': message,
            })
    return issues


def parallel_verdict_channel_warnings(
    nodes: list,
    edges: list,
    id_to_node: dict,
) -> list[str]:
    """Compatibility projection retaining the original string-only API."""
    return [issue['message'] for issue in
            parallel_verdict_channel_issues(nodes, edges, id_to_node)]
