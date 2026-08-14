"""Pure topology queries for flattened orchestration graphs.

This module has no runtime state, locks, agent execution or persistence.  It
owns the graph-navigation boundary used by :class:`FlowExecutor`: adjacency
queries, entry selection, loop partitioning and parallel-branch convergence.
"""

from __future__ import annotations

from collections import deque


class FlowExecutionError(Exception):
    """Raised for structural problems discovered at execution time."""


class GraphNavigator:
    """Read-only topology queries over a flattened orchestration definition."""

    def __init__(
        self,
        nodes: dict[str, dict],
        fwd: dict[str, list[str]],
        rev: dict[str, list[str]],
    ):
        self.nodes = nodes
        self.fwd = fwd
        self.rev = rev

    @classmethod
    def from_edges(
        cls,
        nodes: dict[str, dict],
        edges: list[dict],
    ) -> GraphNavigator:
        """Build ordered forward/reverse adjacency from validated graph edges."""
        forward: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        reverse: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for edge in edges:
            source, target = edge.get('from'), edge.get('to')
            if source in nodes and target in nodes:
                forward[source].append(target)
                reverse[target].append(source)
        return cls(nodes, forward, reverse)

    def node_label(self, node_id: str) -> str:
        node = self.nodes.get(node_id) or {}
        return (
            node.get('name') or node.get('role') or node.get('kind') or node_id
        )

    def single_next(self, node_id: str) -> str | None:
        next_nodes = self.fwd.get(node_id, [])
        return next_nodes[0] if next_nodes else None

    def find_start(self) -> str:
        for node_id, node in self.nodes.items():
            if node.get('kind') == 'start':
                return node_id
        for node_id in self.nodes:
            if not self.rev.get(node_id):
                return node_id
        raise FlowExecutionError('no start node and no source node')

    def loop_parts(self, loop_id: str) -> tuple[str | None, str | None]:
        """Return the loop body entry and its non-cyclic exit successor."""
        successors = list(self.fwd.get(loop_id, []))
        body, exit_node = None, None
        for successor in successors:
            if self.can_reach(successor, loop_id, avoid=loop_id):
                body = body or successor
            else:
                exit_node = exit_node or successor
        if body is None and successors:
            body = successors[0]
        if exit_node is None:
            exit_node = next(
                (successor for successor in successors if successor != body),
                None,
            )
        return body, exit_node

    def find_loop_planner(
        self,
        loop_id: str,
        body_entry: str | None,
    ) -> str | None:
        """Return a role predecessor outside the loop body, when present."""
        for predecessor in self.rev.get(loop_id, []):
            node = self.nodes.get(predecessor) or {}
            if node.get('type') != 'role':
                continue
            if body_entry and self.can_reach(
                body_entry,
                predecessor,
                avoid=loop_id,
            ):
                continue
            return predecessor
        return None

    def find_common_barrier(self, branches: list[str]) -> str | None:
        """Return the nearest barrier reachable from every branch."""
        if not branches:
            return None
        reach_sets = [self.reachable(branch) for branch in branches]
        common = set.intersection(*reach_sets) if reach_sets else set()
        barriers = [
            node_id for node_id in common
            if self.nodes[node_id].get('kind') == 'barrier'
        ]
        candidates = barriers or list(common)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda node_id: self.distance(branches[0], node_id),
        )

    def reachable(self, start: str) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(self.fwd.get(node_id, []))
        return seen

    def can_reach(self, start: str, target: str, *, avoid: str = '') -> bool:
        seen, stack = set(), [start]
        while stack:
            node_id = stack.pop()
            if node_id == target:
                return True
            if node_id in seen or node_id == avoid and node_id != start:
                continue
            seen.add(node_id)
            for successor in self.fwd.get(node_id, []):
                if successor == target:
                    return True
                if successor != avoid:
                    stack.append(successor)
        return False

    def distance(self, start: str, target: str) -> int:
        queue = deque([(start, 0)])
        seen = {start}
        while queue:
            node_id, distance = queue.popleft()
            if node_id == target:
                return distance
            for successor in self.fwd.get(node_id, []):
                if successor not in seen:
                    seen.add(successor)
                    queue.append((successor, distance + 1))
        return 1 << 30


__all__ = ['FlowExecutionError', 'GraphNavigator']
