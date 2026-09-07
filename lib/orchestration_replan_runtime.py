"""Focused structural re-plan boundary for orchestration loops.

The loop runtime decides *when* a verifier-approved re-plan is required. This
module owns the planner's bounded progress context and immutable brief rewrite,
then invokes the injected role runner. It has no graph-walk or transport logic.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Protocol

from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration_progress import REPLAN_SUMMARY_CHARS


class OrchestrationReplanProgressPort(Protocol):
    def build_replan_summary(
        self,
        transcript: list[dict],
        *,
        verifier_roles: Collection[str],
        limit: int,
    ) -> str: ...


class OrchestrationReplanTranscriptPort(Protocol):
    def snapshot(self) -> list[dict]: ...


class OrchestrationReplanRuntime:
    """Build and run one bounded Planner delta for a structural defect."""

    def __init__(
        self,
        *,
        nodes: dict[str, dict],
        progress: OrchestrationReplanProgressPort,
        transcript: OrchestrationReplanTranscriptPort,
        run_role: Callable[[dict, str], str],
        verifier_roles: Collection[str],
        summary_limit: int = REPLAN_SUMMARY_CHARS,
    ) -> None:
        self._nodes = nodes
        self._progress = progress
        self._transcript = transcript
        self._run_role = run_role
        self._verifier_roles = frozenset(verifier_roles)
        self._summary_limit = max(1, int(summary_limit))

    def progress_summary(self) -> str:
        """Return the canonical bounded producer summary for the next delta."""
        return self._progress.build_replan_summary(
            self._transcript.snapshot(),
            verifier_roles=self._verifier_roles,
            limit=self._summary_limit,
        )

    def run(
        self,
        planner_id: str,
        context: str,
        defect: str | None,
        replan: int,
    ) -> str:
        """Invoke Planner with a defect and progress-aware delta brief."""
        planner_node = dict(self._nodes[planner_id])
        progress = self.progress_summary()
        context_parts = [context] if context else []
        if defect:
            context_parts.append('## Structural plan defect to fix\n' + defect)
        if progress:
            context_parts.append(
                '## Progress so far (do NOT discard — produce a DELTA, '
                'do not regrow the plan)\n' + progress
            )

        params = dict(planner_node.get('params') or {})
        base_brief = render_role_brief(planner_node) or 'Plan the work.'
        params['objective'] = (
            base_brief
            + f'\n\n[RE-PLAN #{replan}] Address the structural defect above '
            'and produce a minimal DELTA to the existing plan — do not '
            'rewrite or grow it.'
        )
        # The structured fields are already folded into ``base_brief``.
        # Retaining them would render the same instructions twice.
        for key in list(params):
            if key not in ('objective', 'tier', 'isolation', 'emits', 'name'):
                params.pop(key, None)
        planner_node['params'] = params
        return self._run_role(planner_node, '\n\n'.join(context_parts))


__all__ = [
    'OrchestrationReplanProgressPort',
    'OrchestrationReplanRuntime',
    'OrchestrationReplanTranscriptPort',
]
