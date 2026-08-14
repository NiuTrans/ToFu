"""Focused one-of-many routing boundary for orchestration graphs.

The graph interpreter decides when a branch control is entered.  This module
owns candidate projection, optional classifier execution, deterministic
fallback and the canonical ``branch_pick`` event.  Agent lifecycle details
remain behind the injected classifier port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from lib.log import get_logger


logger = get_logger(__name__)


class OrchestrationBranchNavigatorPort(Protocol):
    def node_label(self, node_id: str) -> str: ...


class OrchestrationBranchRuntime:
    """Select one successor for a branch control node."""

    def __init__(
        self,
        *,
        navigator: OrchestrationBranchNavigatorPort,
        nodes: dict[str, dict],
        successors: Callable[[str], list[str]],
        run_classifier: Callable[[dict, str], str],
        emit: Callable[[dict], None],
    ) -> None:
        self._navigator = navigator
        self._nodes = nodes
        self._successors = successors
        self._run_classifier = run_classifier
        self._emit = emit

    def run(self, branch_id: str, context: str) -> str | None:
        """Return the selected successor, falling back to edge order."""
        next_nodes = list(self._successors(branch_id))
        if not next_nodes:
            self._emit({
                'type': 'branch_pick',
                'node_id': branch_id,
                'chosen': None,
                'options': 0,
            })
            return None

        node = self._nodes[branch_id]
        params = node.get('params') or {}
        classifier_role = params.get('classifier')
        chosen = next_nodes[0]
        how = 'first-edge'

        if classifier_role and len(next_nodes) > 1:
            labels = {
                target: self._navigator.node_label(target)
                for target in next_nodes
            }
            prompt = (
                f'{context}\n\n## Routing decision\n'
                'Choose exactly ONE next step by replying with its label.\n'
                'Options: '
                + ', '.join(f'{label!r}' for label in labels.values())
            )
            classifier = {
                'id': f'{branch_id}__classifier',
                'type': 'role',
                'role': classifier_role,
                'name': f'{node.get("name") or "branch"} classifier',
                'params': {
                    'objective': prompt,
                    'tier': params.get('tier') or 'light',
                },
            }
            verdict = self._run_classifier(classifier, context).lower()
            for target, label in labels.items():
                if label and label.lower() in verdict:
                    chosen, how = target, 'classifier'
                    break

        self._emit({
            'type': 'branch_pick',
            'node_id': branch_id,
            'chosen': chosen,
            'options': len(next_nodes),
            'how': how,
        })
        logger.info(
            '[FlowBranch] %s → %s (of %d, %s)',
            branch_id, chosen, len(next_nodes), how,
        )
        return chosen


__all__ = [
    'OrchestrationBranchNavigatorPort',
    'OrchestrationBranchRuntime',
]
