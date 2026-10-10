"""Focused one-of-many routing boundary for orchestration graphs.

The graph interpreter decides when a branch control is entered.  This module
owns candidate projection, optional classifier execution, deterministic
fallback and the canonical ``branch_pick`` event.  Agent lifecycle details
remain behind the injected classifier port.
"""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Protocol

from lib.log import get_logger


logger = get_logger(__name__)


class OrchestrationBranchNavigatorPort(Protocol):
    def node_label(self, node_id: str) -> str: ...


class OrchestrationBranchRuntime:
    """Select one successor for a branch control node."""

    @staticmethod
    def _classifier_choice(
        verdict: str, labels: dict[str, str],
    ) -> str | None:
        """Return one unambiguous label mention from classifier output.

        Labels are matched on word boundaries, then shorter matches wholly
        contained by a longer match are discarded. This accepts a harmless
        preamble such as ``I choose Writer`` while preventing ``A`` from
        matching ``Data`` and ``Review`` from stealing ``Security Review``.
        If distinct options are mentioned, the verdict is ambiguous and the
        caller retains deterministic first-edge fallback.
        """
        text = str(verdict or '').casefold()
        matches: list[tuple[str, int, int]] = []
        for target, raw_label in labels.items():
            label = str(raw_label or '').strip().casefold()
            if not label:
                continue
            pattern = rf'(?<!\w){re.escape(label)}(?!\w)'
            matches.extend(
                (target, match.start(), match.end())
                for match in re.finditer(pattern, text)
            )
        maximal = [
            candidate for candidate in matches
            if not any(
                other[1] <= candidate[1]
                and other[2] >= candidate[2]
                and (other[2] - other[1]) > (candidate[2] - candidate[1])
                for other in matches
            )
        ]
        targets = {target for target, _start, _end in maximal}
        return next(iter(targets)) if len(targets) == 1 else None

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
            classifier_choice = self._classifier_choice(
                self._run_classifier(classifier, context), labels)
            if classifier_choice is not None:
                chosen, how = classifier_choice, 'classifier'

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
