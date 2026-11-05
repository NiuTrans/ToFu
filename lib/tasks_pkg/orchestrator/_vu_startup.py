"""Synthetic-user startup phase adapter for the shared task invocation.

Entry point: make_vu_phase binds the role flag once. Common project preparation
and external-edit detection live in _startup; this adapter only publishes the
role-specific phase events through the canonical task event owner.
"""
from __future__ import annotations

from typing import Any

from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _vu_phase(
    task: dict[str, Any],
    detail: str,
    *,
    vu_startup: bool,
    detail_key: str | None = None,
    detail_args: dict | None = None,
) -> None:
    """Emit a PHASE event during the VU sub-task's startup window.

    The VU sub-task carries ``_vu_event_transform`` (the event-transform
    seam), so any PHASE emitted here is wrapped as
    ``autopilot_vu_event`` and lands in the synthetic-user bubble on BOTH
    the carrier's own stream and the parent's. The
    pre-stream prep window (tool assembly → tool-history rebuild → system-
    context injection → FUSE memory/project prefetch) is otherwise SILENT
    for up to tens of seconds on a large conversation (measured 2.9–4.7s
    typical, ~26s on a 3000-event conv), leaving the bubble on a vague
    placeholder. Naming each real sub-step keeps the display honest.

    Gated on ``vu_startup`` so the ordinary non-VU startup path
    stays byte-identical (no new events).

    Args:
        task: The live task dict — passed to ``append_event`` as-is.
        detail: Short user-visible description of the current sub-step.
        vu_startup: True IFF this is a VU sub-task's startup window
            (``bool(task.get('_vu_subtask'))`` at the call site).
    """
    if not vu_startup:
        return
    try:
        fields: dict[str, Any] = {'detail': detail}
        if detail_key:
            fields['detailKey'] = detail_key
        if detail_args:
            fields['detailArgs'] = detail_args
        append_event(task, build_phase(Phase.WORKING, **fields))
    except Exception as _e:
        _tid = (task.get('id') or '')[:8]
        logger.debug('[Task %s] vu startup phase emit failed: %s', _tid, _e)


def make_vu_phase(task: dict[str, Any]):
    """Bind the VU-startup flag + task into the closure-style phase emitter.

    Extracted 2026-08-01 ( slice 37) from ``run_task``'s inline
    attribution + closure block. The captured ``task`` + ``_vu_startup``
    are stable across the whole invocation (no rebind), so binding them
    once at factory time is semantically identical to the inline
    closure — and the loop's closure-style call sites (``_vu_phase(x)``)
    are preserved unchanged. The returned closure is internally named
    ``_bound`` so the module-level ``_vu_phase`` stays resolvable through
    the module namespace at call time (an inner ``def _vu_phase`` would
    shadow it across the whole factory scope — def is an assignment).
    """
    _vu_startup = bool(task.get('_vu_subtask'))

    def _bound(detail, *, detail_key=None, detail_args=None):
        _vu_phase(
            task,
            detail,
            vu_startup=_vu_startup,
            detail_key=detail_key,
            detail_args=detail_args,
        )

    return _bound
