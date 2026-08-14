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
class OrchestrationAgentResult:
    """One normalized leaf-agent result consumed by ``FlowExecutor``."""

    output: str = ''
    status: str = 'completed'
    error: str = ''
    thinking: str = ''
    tool_usage: OrchestrationToolUsage = field(
        default_factory=OrchestrationToolUsage)


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


def _text(value: Any) -> str:
    if value is None:
        return ''
    try:
        return str(value)
    except Exception as exc:
        logger.debug('[Orchestration] result value is not stringable (%s): %s',
                     type(value).__name__, exc)
        return ''


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
        tool_usage=classify_orchestration_tool_usage(value),
    )


__all__ = [
    'AgentRunnerResultLike',
    'OrchestrationAgentResult',
    'OrchestrationAgentRunnerPort',
    'normalize_orchestration_agent_result',
]
