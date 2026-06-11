"""lib/orchestration_endpoint_adapter.py — FlowExecutor → endpoint UI bridge.

The frontend renders endpoint mode from a specific message schema:

    assistant(planner, _isEndpointPlanner, _epPlannerIteration=N)
    assistant(worker,  _epIteration=N)
    user(critic, _isEndpointReview, _epNextPhase='worker'|'planner', _epApproved)

When the eventual cutover routes endpoint mode through
:class:`lib.orchestration_engine.FlowExecutor`, the engine emits its OWN
vocabulary (``step_start`` / ``step_complete`` / ``loop_iteration`` /
``replan`` / ``zero_deliverable_guard`` …). This adapter is the pure,
stateful translator between the two — so the cutover can drive the
existing UI with ZERO frontend changes, behind a feature flag.

It is intentionally a standalone, dependency-light class with no I/O: feed
it engine events via :meth:`on_event`, collect endpoint-shaped message
dicts from :meth:`drain`. Tests exercise it directly; the live wiring
(SSE emission, DB persistence) stays in the route/orchestrator layer.

This module does NOT perform the cutover — it makes it *possible* and
*testable* without touching the battle-tested ``lib/tasks_pkg/endpoint.py``
path. Nothing imports it yet except its test.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from lib.log import get_logger

logger = get_logger(__name__)


# Engine role → endpoint phase classification.
_VERIFIER_ROLES = frozenset({'critic', 'reviewer'})
_PLANNER_ROLES = frozenset({'planner'})


class EndpointEventAdapter:
    """Translate FlowExecutor events into endpoint-mode message dicts.

    Usage::

        adapter = EndpointEventAdapter(emit=task_append_event)
        executor = FlowExecutor(defn, on_event=adapter.on_event)
        executor.run(...)
        messages = adapter.messages   # endpoint-shaped, ready to persist

    Parameters
    ----------
    emit : callable(dict), optional
        If supplied, each translated endpoint message is also forwarded
        live (e.g. to ``TaskRuntime.append_event``). The adapter always
        accumulates them in ``self.messages`` regardless.
    """

    def __init__(self, emit: Callable | None = None):
        self._emit = emit
        self.messages: list[dict] = []
        self._iteration = 0           # worker iteration counter
        self._planner_iteration = 0   # planner (re)plan counter
        self._next_phase = 'worker'   # phase the upcoming critic points to
        self._pending_replan = False  # a replan event arrived; next planner is a re-plan

    # ── engine event sink ───────────────────────────────────────────

    def on_event(self, ev: dict):
        """Consume one FlowExecutor event; may produce an endpoint message."""
        etype = ev.get('type')
        try:
            handler = getattr(self, f'_on_{etype}', None)
            if handler:
                handler(ev)
        except Exception as e:
            logger.debug('[EndpointAdapter] handler %s failed: %s', etype, e)

    def drain(self) -> list[dict]:
        """Return accumulated endpoint messages and clear the buffer."""
        out = self.messages
        self.messages = []
        return out

    # ── per-event handlers ───────────────────────────────────────────

    def _on_replan(self, ev: dict):
        # The next planner turn is a re-plan; mark it so the planner message
        # carries the higher _epPlannerIteration and the critic that caused
        # it was already tagged _epNextPhase='planner'.
        self._pending_replan = True

    def _on_step_complete(self, ev: dict):
        role = ev.get('role') or ''
        out = ev.get('preview') or ''
        if role in _PLANNER_ROLES:
            self._planner_iteration += 1
            self._push({
                'role': 'assistant',
                'content': out,
                'timestamp': _now(),
                '_isEndpointPlanner': True,
                '_epPlannerIteration': self._planner_iteration,
            })
            self._pending_replan = False
        elif role in _VERIFIER_ROLES:
            # Determine the next phase from the verdict text the engine saw.
            # The engine already classified it; we re-derive a light label
            # from the preview so the UI shows the right placeholder.
            next_phase = self._derive_next_phase(out)
            self._next_phase = next_phase
            self._push({
                'role': 'user',
                'content': out,
                'timestamp': _now(),
                '_isEndpointReview': True,
                '_epIteration': self._iteration,
                '_epApproved': next_phase == 'stop',
                '_epNextPhase': next_phase,
            })
        else:
            # A worker (producer) turn.
            self._iteration += 1
            self._push({
                'role': 'assistant',
                'content': out,
                'timestamp': _now(),
                '_epIteration': self._iteration,
                '_epStateChangingCount': ev.get('state_changing', 0),
            })

    def _on_zero_deliverable_guard(self, ev: dict):
        # Mirror endpoint's synthetic critic row so the UI shows the guard.
        self._push({
            'role': 'user',
            'content': ('⚠️ Zero-deliverable guard: the worker produced no '
                        'state-changing actions; injecting an execute-now '
                        'directive.'),
            'timestamp': _now(),
            '_isEndpointReview': True,
            '_epIteration': self._iteration,
            '_epApproved': False,
            '_epNextPhase': 'worker',
            '_isSyntheticCritic': True,
        })

    # ── helpers ──────────────────────────────────────────────────────

    def _derive_next_phase(self, text: str) -> str:
        """Light verdict label for UI placeholder selection.

        The authoritative classification happens in the engine
        (_classify_verdict); here we only need the coarse phase to pick
        the next placeholder. Replan is signalled out-of-band via the
        ``replan`` event (``_pending_replan``).
        """
        if self._pending_replan:
            return 'planner'
        low = (text or '').lower()
        if '[verdict: stop]' in low or 'verdict: stop' in low:
            # STOP with unresolved markers is overridden by the engine; if a
            # replan/worker iteration follows we'll have seen those events.
            return 'stop'
        if 'continue_planner' in low:
            return 'planner'
        return 'worker'

    def _push(self, msg: dict):
        self.messages.append(msg)
        if self._emit:
            try:
                self._emit(msg)
            except Exception as e:
                logger.debug('[EndpointAdapter] emit failed: %s', e)


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


__all__ = ['EndpointEventAdapter']
