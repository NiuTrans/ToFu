"""Typed result contract for orchestration agent-runner ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias

from lib.log import get_logger
from lib.orchestration_tool_usage import (
    OrchestrationToolUsage,
    classify_orchestration_tool_usage,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrchestrationModelRoute:
    """Selected-to-resolved model decision for one orchestration leaf."""

    selected_model: str = ''
    resolved_model: str = ''
    role: str = ''
    tier: str = ''
    kind: str = 'selected'

    @property
    def switched(self) -> bool:
        """Whether role routing changed the model selected by the user."""
        return bool(
            self.selected_model
            and self.resolved_model
            and self.selected_model != self.resolved_model
        )

    def to_projection(self) -> dict[str, str]:
        """Return the public camelCase projection carried by events/Turns."""
        return {
            'selectedModel': self.selected_model,
            'resolvedModel': self.resolved_model,
            'role': self.role,
            'tier': self.tier,
            'kind': self.kind,
        }


@dataclass(frozen=True)
class OrchestrationAgentResult:
    """One normalized leaf-agent result consumed by ``FlowExecutor``."""

    output: str = ''
    status: str = 'completed'
    error: str = ''
    thinking: str = ''
    model_route: OrchestrationModelRoute = field(
        default_factory=OrchestrationModelRoute)
    tool_usage: OrchestrationToolUsage = field(
        default_factory=OrchestrationToolUsage)
    # Bounded display rows (args brief, result preview, error) recorded by the
    # SubAgent substrate — one dict per dispatched call, already capped by the
    # swarm presentation budget. The chat adapter projects these into the
    # settled turn's toolRounds so goal-mode turns show what their tools did.
    tool_log: tuple = ()


AgentRunnerResultLike: TypeAlias = (
    OrchestrationAgentResult | Mapping[str, Any] | None
)


class OrchestrationAgentRunnerPort(Protocol):
    """Structural callable port used by the graph interpreter."""

    def __call__(
        self,
        node: dict,
        context: str,
        iteration: int,
    ) -> AgentRunnerResultLike: ...


def _tool_log_rows(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(row) for row in value if isinstance(row, Mapping))


def _text(value: Any) -> str:
    if value is None:
        return ''
    try:
        return str(value)
    except Exception as exc:
        logger.debug('[Orchestration] result value is not stringable (%s): %s',
                     type(value).__name__, exc)
        return ''


def normalize_orchestration_model_route(value: Any) -> OrchestrationModelRoute:
    """Normalize one trusted runner route without accepting arbitrary shape."""
    if isinstance(value, OrchestrationModelRoute):
        return value
    if not isinstance(value, Mapping):
        return OrchestrationModelRoute()
    selected_model = _text(
        value.get('selectedModel') or value.get('selected_model'))
    resolved_model = _text(
        value.get('resolvedModel') or value.get('resolved_model'))
    kind = _text(value.get('kind'))
    if kind not in {'selected', 'role_tier'}:
        kind = 'role_tier' if (
            selected_model and resolved_model
            and selected_model != resolved_model
        ) else 'selected'
    return OrchestrationModelRoute(
        selected_model=selected_model,
        resolved_model=resolved_model,
        role=_text(value.get('role')),
        tier=_text(value.get('tier')),
        kind=kind,
    )


def normalize_orchestration_agent_result(
    value: AgentRunnerResultLike | Any,
) -> OrchestrationAgentResult:
    """Normalize typed and legacy Mapping results at one compatibility seam.

    ``None`` retains the historic empty-success behavior used by minimal test
    and extension runners. Any other unsupported top-level shape becomes an
    explicit failed node instead of crashing later on a scattered ``.get``.
    """
    if isinstance(value, OrchestrationAgentResult):
        return value
    if value is None:
        return OrchestrationAgentResult()
    if not isinstance(value, Mapping):
        return OrchestrationAgentResult(
            status='failed',
            error='invalid agent runner result: expected a mapping or '
                  f'OrchestrationAgentResult, got {type(value).__name__}',
        )
    return OrchestrationAgentResult(
        output=_text(value.get('output')),
        status=_text(value.get('status')) or 'completed',
        error=_text(value.get('error')),
        thinking=_text(value.get('thinking')),
        model_route=normalize_orchestration_model_route(
            value.get('modelRoute') or value.get('model_route')),
        tool_usage=classify_orchestration_tool_usage(value),
        tool_log=_tool_log_rows(
            value.get('tool_log') or value.get('toolLog')),
    )


__all__ = [
    'AgentRunnerResultLike',
    'OrchestrationAgentResult',
    'OrchestrationModelRoute',
    'OrchestrationAgentRunnerPort',
    'normalize_orchestration_agent_result',
    'normalize_orchestration_model_route',
]
