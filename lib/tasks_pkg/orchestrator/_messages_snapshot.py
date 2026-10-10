"""Best-effort canonical pre-flight Request Inspector evidence adapter."""
from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence

logger = get_logger(__name__)


def emit_messages_snapshot_event(
    task: dict[str, Any], messages: list[dict[str, Any]], *, tid: str,
    round_num: int, model: str, thinking_enabled: bool, thinking_depth: int,
    preset: str, temperature: float, max_tokens: int, response_format: Any,
    tools: Any, prepared_messages: list[dict[str, Any]] | None = None,
) -> None:
    """Record finalized messages; explicitly label failed-build reconstructions."""
    try:
        ref = begin_round_evidence(task, task.get('_inspectorTaskId') or task['id'], round_num + 1,
                                   task.get('_flow_phase') or '')
        event = build_round_evidence(
            task, messages, ref=ref, kind='request', model=model,
            prepared_messages=prepared_messages, tools=tools,
            params={'maxTokens': max_tokens, 'temperature': temperature,
                    'thinkingEnabled': thinking_enabled,
                    'thinkingDepth': thinking_depth, 'preset': preset,
                    'responseFormat': response_format, 'stream': True})
        append_event(task, event)
    except Exception:
        logger.warning('[Task %s] messages_snapshot failed at round %d model=%s',
                       tid, round_num + 1, model, exc_info=True)
