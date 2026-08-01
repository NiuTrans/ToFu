# HOT_PATH — called once per stream round to emit the pre-flight
# Request-Inspector messages snapshot for the debug panel.
"""Emit the pre-flight ``MESSAGES_SNAPSHOT`` (kind='request') event.

Extracted 2026-07-31 (pt_03f4cdf1 slice 15) from
``lib/tasks_pkg/orchestrator/_run.py``'s stream loop.

**What it does**
    RIGHT AFTER ``sort_tool_results`` — the messages are now in their
    real outbound ordering, so the debug panel sees the same sequence
    the model will. The helper:

    * Runs ``apply_wire_sanitize`` on an INDEPENDENT copy of
      ``messages`` — build_body re-runs its own copy at request time,
      so a shared mutation would double-sanitize.
    * Strips base64 data URLs from the snapshot via
      ``_strip_base64_for_snapshot`` — keeps the debug event small
      enough to travel over SSE.
    * Builds a ``MESSAGES_SNAPSHOT`` event with **kind='request'** — the
      Request Inspector contract (docs/DEBUG_PANEL_REDESIGN.md §3) says
      this is the ONLY kind='request' emission; the three other
      snapshot sites (post-tool / final / fallback) are kind='state'
      (NOT LLM requests) and stay in _turn.py / _finalize.py.
    * Endpoint-mode tasks (Planner / Worker / Critic) each re-run
      run_task with their own round numbering, so
      ``task['_endpoint_phase']`` is tagged onto the event as ``turn``
      so the Request Inspector can distinguish same-numbered rounds
      across drivers (epic pt_e3dc7198e7e34bb1).

**Best-effort**
    The whole helper is try/except-wrapped: a Request Inspector
    failure must never break the LLM round. That contract is
    load-bearing — an inspector regression turned into "the LLM stops
    working" would be catastrophic. Callers can call this
    unconditionally.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,
    append_event,
)
from lib.tasks_pkg.wire_messages import apply_wire_sanitize


logger = get_logger(__name__)


def emit_messages_snapshot_event(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    tid: str,
    round_num: int,
    model: str,
    thinking_enabled: bool,
    thinking_depth: int,
    preset: str,
    temperature: float,
    max_tokens: int,
    response_format: Any,
    tools: Any,
) -> None:
    """Emit the pre-flight ``MESSAGES_SNAPSHOT`` event for the Request
    Inspector.

    Args:
        task: The live task dict; ``task['convId']`` /
            ``task['provider_id']`` / ``task['_endpoint_phase']`` are
            read; the emitted event is appended onto ``task['events']``
            via ``append_event``.
        messages: The live outbound message list — READ ONLY; the
            helper runs the wire sanitizer on its own copy.

    The ``tools`` argument is the round's assembled tool list from
    ``_assemble_tool_list`` (may be None / empty). Attached to the
    event as ``tools`` ONLY when non-empty — presence is load-bearing
    for the Request Inspector.
    """
    try:
        _wire = apply_wire_sanitize(
            messages, conv_id=task.get('convId', ''),
            provider_id=task.get('provider_id') or '')
        snapshot = _strip_base64_for_snapshot(_wire)
        snap_evt = build_event(
            EventType.MESSAGES_SNAPSHOT,
            # Request Inspector contract (docs/DEBUG_PANEL_REDESIGN.md
            # §3): this is the ONLY kind='request' emission — the
            # payload the model is about to receive. The other three
            # snapshot sites (post-tool / final / fallback) are
            # kind='state' (NOT LLM requests).
            kind='request',
            model=model,
            # Endpoint turns (Planner/Worker/Critic) each re-run
            # run_task with their OWN round numbering — tag the
            # driver's phase so the Request Inspector can tell
            # same-numbered rounds apart (epic pt_e3dc7198e7e34bb1).
            # '' for normal (non-endpoint) tasks.
            turn=task.get('_endpoint_phase') or '',
            params={
                'maxTokens': max_tokens,
                'temperature': temperature,
                'thinkingEnabled': thinking_enabled,
                'thinkingDepth': thinking_depth,
                'preset': preset,
                'responseFormat': response_format,
                'stream': True,
            },
            roundNum=round_num + 1,
            label=f'Round {round_num + 1} 请求前 · {len(snapshot)}条',
            messages=snapshot,
        )
        if tools:
            snap_evt['tools'] = tools
        append_event(task, snap_evt)
    except Exception:
        logger.warning(
            '[Task %s] messages_snapshot failed at round %d model=%s',
            tid, round_num + 1, model, exc_info=True)
