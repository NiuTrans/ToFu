"""lib/orchestration_endpoint_adapter.py — FlowExecutor → endpoint UI bridge.

The frontend renders endpoint mode from a specific message schema AND a
specific live SSE event sequence:

    Messages (DB / reload):
      assistant(planner, _isEndpointPlanner, _epPlannerIteration=N)
      assistant(worker,  _epIteration=N)
      user(critic, _isEndpointReview, _epNextPhase='worker'|'planner', _epApproved)

    Live SSE (streaming UI):
      endpoint_iteration(phase=planning|working|reviewing, iteration=N)  ← opens the bubble
      delta(content=… | thinking=…)                                       ← fills it live
      endpoint_planner_done(content=…)                                    ← finalizes planner
      endpoint_critic_msg(iteration, content, next_phase)                 ← finalizes critic

The engine (:class:`lib.orchestration_engine.FlowExecutor`) emits its OWN
vocabulary (``step_start`` / ``step_delta`` / ``step_complete`` /
``loop_iteration`` / ``replan`` / ``zero_deliverable_guard`` …). This adapter
is the stateful translator between the two, so endpoint / autopilot / custom
flows drive the existing UI with ZERO frontend changes.

Two output channels, deliberately separated:

* ``on_stream(sse_event)`` — LIVE SSE events for the streaming UI. Emitted as
  the turn unfolds: an ``endpoint_iteration`` when a node STARTS (so the
  bubble exists before any token), ``delta`` events per streamed chunk, and a
  finalizing ``endpoint_planner_done`` / ``endpoint_critic_msg`` when the node
  COMPLETES.
* ``emit(message_dict)`` — endpoint-shaped MESSAGE dicts for DB persistence /
  reload parity. Fired once per completed turn (``self.messages`` accumulates
  the same dicts).

Either may be ``None`` (tests drive ``on_event`` directly and read
``self.messages``).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from lib.log import get_logger

logger = get_logger(__name__)


# Engine role → endpoint phase classification.
_VERIFIER_ROLES = frozenset({'critic', 'reviewer', 'virtual_user'})
_PLANNER_ROLES = frozenset({'planner'})


class EndpointEventAdapter:
    """Translate FlowExecutor events into endpoint-mode messages + live SSE.

    Usage::

        adapter = EndpointEventAdapter(emit=db_sync, on_stream=task_append_event)
        executor = FlowExecutor(defn, on_event=adapter.on_event)
        executor.run(...)
        messages = adapter.messages   # endpoint-shaped, ready to persist

    Parameters
    ----------
    emit : callable(dict), optional
        Called once per COMPLETED turn with the endpoint-shaped message dict
        (DB persistence). The adapter always accumulates them in
        ``self.messages`` regardless.
    on_stream : callable(dict), optional
        Called with each LIVE SSE event (``endpoint_iteration`` / ``delta`` /
        ``endpoint_planner_done`` / ``endpoint_critic_msg``) as the turn
        unfolds, so the streaming UI renders tokens live.
    """

    def __init__(self, emit: Callable | None = None,
                 on_stream: Callable | None = None):
        self._emit = emit
        self._on_stream = on_stream
        self.messages: list[dict] = []
        self._iteration = 0           # worker iteration counter
        self._planner_iteration = 0   # planner (re)plan counter
        self._next_phase = 'worker'   # phase the upcoming critic points to
        self._pending_replan = False  # a replan event arrived; next planner is a re-plan
        # Role of the node whose step_start fired but step_complete hasn't yet
        # (the in-flight turn) — lets a stray delta route even if events race.
        self._cur_role = ''

    # ── engine event sink ───────────────────────────────────────────

    def on_event(self, ev: dict):
        """Consume one FlowExecutor event; may produce messages + SSE events."""
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

    def _on_step_start(self, ev: dict):
        """A node began executing — open the matching live bubble.

        Emits the ``endpoint_iteration`` the frontend keys off to stand up
        (or transition to) the right streaming bubble BEFORE any token
        arrives, so deltas have somewhere to land. Producer iterations are
        counted HERE (at start) so the iteration number is stable across the
        start event, the deltas, and the eventual completed message.
        """
        role = ev.get('role') or ''
        emits = ev.get('emits') or self._derive_emits(role)
        self._cur_role = role

        if role in _PLANNER_ROLES:
            self._stream({'type': 'endpoint_iteration', 'iteration': 0,
                          'phase': 'planning'})
        elif emits == 'user':
            # Verifier (critic / reviewer / virtual_user) — its turn lands on
            # the user side; the frontend's 'reviewing' branch finalizes the
            # worker bubble and creates the critic bubble.
            self._stream({'type': 'endpoint_iteration',
                          'iteration': self._iteration, 'phase': 'reviewing'})
        else:
            # Assistant-side producer (worker / specialist) — count the turn.
            self._iteration += 1
            self._stream({'type': 'endpoint_iteration',
                          'iteration': self._iteration, 'phase': 'working'})

    def _on_step_delta(self, ev: dict):
        """Stream one content/thinking chunk into the current bubble."""
        chunk = ev.get('chunk') or ''
        if not chunk:
            return
        if ev.get('kind') == 'thinking':
            self._stream({'type': 'delta', 'thinking': chunk})
        else:
            self._stream({'type': 'delta', 'content': chunk})

    def _on_step_phase(self, ev: dict):
        """Surface a transient producer status as a wire ``phase`` event.

        The engine emits ``step_phase`` while an assistant-side producer's
        dispatch is in flight ("waiting for model…" / "retrying…" under a
        rate-limited strict_model — the 5-minute first-token stall that used
        to show a bare static pulse). Translated to the registered ``phase``
        event the frontend already renders on the worker bubble (transient UI,
        cleared by the first delta — never a content delta, so it can't
        pollute the turn). Only forwarded for assistant-side producers: a
        verifier (critic / virtual_user) renders user-side and its phase chip
        would land on the wrong bubble, so we skip it there.
        """
        emits = ev.get('emits') or self._derive_emits(ev.get('role') or '')
        if emits == 'user':
            return
        out = {'type': 'phase', 'phase': ev.get('phase') or 'working',
               'detail': ev.get('detail') or ''}
        if ev.get('attempt'):
            out['attempt'] = ev.get('attempt')
        if ev.get('status_code'):
            out['statusCode'] = ev.get('status_code')
        self._stream(out)

    def _on_step_complete(self, ev: dict):
        role = ev.get('role') or ''
        # Prefer the FULL turn output; fall back to the 200-char preview only
        # when running against an un-upgraded engine that omits it. Using the
        # preview as message content truncated every turn to 200 chars.
        out = ev.get('output')
        if out is None:
            out = ev.get('preview') or ''
        # The MESSAGE axis the engine resolved for this node (user|assistant).
        # Older events without it fall back to role-based classification so
        # this adapter keeps working against an un-upgraded engine.
        emits = ev.get('emits') or self._derive_emits(role)
        self._cur_role = ''

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
            # Finalize the planner bubble live.
            self._stream({'type': 'endpoint_planner_done', 'content': out})
        elif emits == 'user':
            # A "user-side" turn — a critic verdict (endpoint) OR a virtual
            # user reply (autopilot). Both render on the user side and carry
            # the review markers the frontend keys off.
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
            # Finalize the critic/VU bubble live.
            self._stream({'type': 'endpoint_critic_msg',
                          'iteration': self._iteration, 'content': out,
                          'next_phase': next_phase})
        else:
            # An assistant-side producer turn (worker / specialist). The
            # iteration was already counted at step_start; the worker bubble
            # is finalized by the NEXT iteration / complete event (matching
            # the live endpoint path), so no finalize SSE is emitted here.
            self._push({
                'role': 'assistant',
                'content': out,
                'timestamp': _now(),
                '_epIteration': self._iteration,
                '_epStateChangingCount': ev.get('state_changing', 0),
            })

    @staticmethod
    def _derive_emits(role: str) -> str:
        """Fallback message-axis derivation for events lacking ``emits``.

        Mirrors lib.orchestration.resolve_emits' role rule so an older engine
        (no ``emits`` in its events) classifies identically to the new one.
        """
        return 'user' if role in _VERIFIER_ROLES else 'assistant'

    def _on_zero_deliverable_guard(self, ev: dict):
        # Mirror endpoint's synthetic critic row so the UI shows the guard.
        content = ('⚠️ Zero-deliverable guard: the worker produced no '
                   'state-changing actions; injecting an execute-now '
                   'directive.')
        # Open + finalize a synthetic critic bubble live (no deltas).
        self._stream({'type': 'endpoint_iteration',
                      'iteration': self._iteration, 'phase': 'reviewing'})
        self._push({
            'role': 'user',
            'content': content,
            'timestamp': _now(),
            '_isEndpointReview': True,
            '_epIteration': self._iteration,
            '_epApproved': False,
            '_epNextPhase': 'worker',
            '_isSyntheticCritic': True,
        })
        self._stream({'type': 'endpoint_critic_msg',
                      'iteration': self._iteration, 'content': content,
                      'next_phase': 'worker', 'synthetic': True})

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
        if '[vu: task_done]' in low:
            return 'stop'
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

    def _stream(self, ev: dict):
        if self._on_stream:
            try:
                self._on_stream(ev)
            except Exception as e:
                logger.debug('[EndpointAdapter] on_stream failed: %s', e)


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


__all__ = ['EndpointEventAdapter']
