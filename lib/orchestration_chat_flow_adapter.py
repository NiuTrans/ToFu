"""lib/orchestration_chat_flow_adapter.py — FlowExecutor → chat UI bridge.

The frontend renders a flow run from a specific message schema AND a
specific live SSE event sequence:

    Messages (DB / reload):
      assistant(planner, _isFlowPlanner, _flowPlannerIteration=N)
      assistant(worker,  _flowIteration=N)
      user(critic, _isFlowReview, _flowNextPhase='worker'|'planner', _flowApproved)

    Live SSE (streaming UI):
      flow_iteration(phase=planning|working|reviewing, iteration=N)  ← opens the bubble
      delta(content=… | thinking=…)                                  ← fills it live
      flow_planner_done(content=…)                                   ← finalizes planner
      flow_critic_msg(iteration, content, next_phase)                ← finalizes critic

The engine (:class:`lib.orchestration_engine.FlowExecutor`) emits its OWN
vocabulary (``step_start`` / ``step_delta`` / ``step_complete`` /
``loop_iteration`` / ``replan`` / ``zero_deliverable_guard`` …). This adapter
is the stateful translator between the two. Explicit ``flowProjection`` /
``turnRole`` / ``emits`` metadata lets the frontend render goal-mode
(autopilot graph) and generic-flow semantics without inferring meaning from
transport names.

Two output channels, deliberately separated:

* ``on_stream(sse_event)`` — LIVE SSE events for the streaming UI. Emitted as
  the turn unfolds: a ``flow_iteration`` when a node STARTS (so the
  bubble exists before any token), ``delta`` events per streamed chunk, and a
  finalizing ``flow_planner_done`` / ``flow_critic_msg`` when the node
  COMPLETES.
* ``emit(message_dict)`` — flow-shaped MESSAGE dicts for DB persistence /
  reload parity. Fired once per completed turn (``self.messages`` accumulates
  the same dicts).

Either may be ``None`` (tests drive ``on_event`` directly and read
``self.messages``). New messages use only canonical ``_isFlow*`` / ``_flow*``
markers; the historical spelling is consumed by
``lib.orchestration_message_compat`` at read boundaries.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from lib.log import get_logger
from lib.orchestration._execution_projection import _PLANNER_ROLES
from lib.orchestration_chat_flow_projection import (
    flow_emits_for_role,
    project_flow_next_phase,
    project_flow_phase_event,
    project_flow_turn_metadata,
)

logger = get_logger(__name__)


class FlowEventAdapter:
    """Translate FlowExecutor events into chat messages + live SSE.

    Usage::

        adapter = FlowEventAdapter(emit=db_sync, on_stream=task_append_event)
        executor = FlowExecutor(defn, on_event=adapter.on_event)
        executor.run(...)
        messages = adapter.messages   # flow-shaped, ready to persist

    Parameters
    ----------
    emit : callable(dict), optional
        Called once per COMPLETED turn with the flow-shaped message dict
        (DB persistence). The adapter always accumulates them in
        ``self.messages`` regardless.
    on_stream : callable(dict), optional
        Called with each LIVE SSE event (``flow_iteration`` / ``delta`` /
        ``flow_planner_done`` / ``flow_critic_msg``) as the turn
        unfolds, so the streaming UI renders tokens live.
    """

    def __init__(self, emit: Callable | None = None,
                 on_stream: Callable | None = None,
                 *, vu_run_id: str = '', vu_flow: bool = False,
                 projection: str = 'critic'):
        self._emit = emit
        self._on_stream = on_stream
        # Goal-mode (virtual_user) context: a run id to anchor VU turns to (so
        # they group as one goal-mode run, parity with the live path) and a
        # flag marking this flow as a VU graph (so a synthetic guard row is
        # stamped as a VU turn, not a critic review).
        self._vu_run_id = vu_run_id or ''
        self._vu_flow = bool(vu_flow)
        self._projection = (
            projection if projection in ('critic', 'autopilot', 'flow')
            else 'flow')
        # Stable identity for the in-flight virtual-user turn. The frontend
        # creates the live bubble at step_start and the DB message is emitted
        # at step_complete; both sides must name the same turn.
        self._vu_msg_id = ''
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
            logger.debug('[FlowAdapter] handler %s failed: %s', etype, e)

    def drain(self) -> list[dict]:
        """Return accumulated flow messages and clear the buffer."""
        out = self.messages
        self.messages = []
        return out

    # ── per-event handlers ───────────────────────────────────────────

    def _on_replan(self, ev: dict):
        # The next planner turn is a re-plan; mark it so the planner message
        # carries the higher _flowPlannerIteration and the critic that caused
        # it was already tagged _flowNextPhase='planner'.
        self._pending_replan = True

    def _on_step_start(self, ev: dict):
        """A node began executing — open the matching live bubble.

        Emits the ``flow_iteration`` the frontend keys off to stand up
        (or transition to) the right streaming bubble BEFORE any token
        arrives, so deltas have somewhere to land. Producer iterations are
        counted HERE (at start) so the iteration number is stable across the
        start event, the deltas, and the eventual completed message.
        """
        role = ev.get('role') or ''
        emits = ev.get('emits') or self._derive_emits(role)
        self._cur_role = role
        if role == 'virtual_user':
            self._vu_msg_id = uuid.uuid4().hex

        turn_meta = self._turn_meta(role, emits)

        if role in _PLANNER_ROLES:
            self._stream({'type': 'flow_iteration', 'iteration': 0,
                          'phase': 'planning', **turn_meta})
        elif emits == 'user':
            # Verifier (critic / reviewer / virtual_user) — its turn lands on
            # the user side; the frontend's 'reviewing' branch finalizes the
            # worker bubble and creates the critic bubble.
            self._stream({'type': 'flow_iteration',
                          'iteration': self._iteration, 'phase': 'reviewing',
                          **turn_meta})
        else:
            # Assistant-side producer (worker / specialist) — count the turn.
            self._iteration += 1
            self._stream({'type': 'flow_iteration',
                          'iteration': self._iteration, 'phase': 'working',
                          **turn_meta})

    def _on_step_delta(self, ev: dict):
        """Stream one content/thinking chunk into the current bubble."""
        chunk = ev.get('chunk') or ''
        if not chunk:
            return
        if ev.get('kind') == 'thinking':
            self._stream({'type': 'delta', 'thinking': chunk,
                          **self._turn_meta(
                              ev.get('role') or self._cur_role,
                              ev.get('emits') or '')})
        else:
            self._stream({'type': 'delta', 'content': chunk,
                          **self._turn_meta(
                              ev.get('role') or self._cur_role,
                              ev.get('emits') or '')})

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
        self._stream(project_flow_phase_event(ev))

    def _on_step_complete(self, ev: dict):
        role = ev.get('role') or ''
        # Prefer the FULL turn output; fall back to the 200-char preview only
        # when running against an un-upgraded engine that omits it. Using the
        # preview as message content truncated every turn to 200 chars.
        out = ev.get('output')
        if out is None:
            out = ev.get('preview') or ''
        # Full streamed reasoning for this node (emitted by the engine's
        # default SubAgent runner). Carried onto the finalized message AND
        # the finalizing SSE events so the thinking block survives finalize +
            # DB sync + reload.
        thinking = ev.get('thinking') or ''
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
                'thinking': thinking,
                'timestamp': _now(),
                '_isFlowPlanner': True,
                '_flowPlannerIteration': self._planner_iteration,
            })
            self._pending_replan = False
            # Finalize the planner bubble live.
            self._stream({'type': 'flow_planner_done', 'content': out,
                          'thinking': thinking})
        elif emits == 'user':
            # A "user-side" turn — a critic verdict (critic-loop graph) OR a
            # virtual user reply (goal mode). They render on the user side
            # but carry DIFFERENT markers, and the difference is LOAD-BEARING
            # for the context builder: a critic review is display-only
            # (skipped by _transform_messages — its feedback reaches the
            # worker via the engine's _pending_feedback, not the message
            # history), whereas a VU reply is a REAL user turn that MUST
            # survive into context or the next worker is starved of the
            # "keep going / here's the checklist" instruction. Stamp them
            # apart (_mark_user_side).
            next_phase = self._derive_next_phase(out)
            self._next_phase = next_phase
            # Standalone goal mode treats TASK_DONE as control, not as words
            # the synthetic user said: no DB row is appended and the eager
            # live placeholder is removed. Preserve that contract for a graph
            # virtual_user instead of persisting a visible sentinel turn.
            if role == 'virtual_user':
                from lib.agent_verdict import classify_verdict, strip_machine_tokens
                verdict = classify_verdict(
                    out, verifier_role='virtual_user', loose_fallback=True)
                if verdict.get('phase') == 'stop':
                    self._stream({
                        'type': 'flow_critic_msg',
                        'iteration': self._iteration,
                        'content': '',
                        'thinking': '',
                        'next_phase': 'stop',
                        'discard': True,
                        **self._turn_meta(role, emits),
                    })
                    self._vu_msg_id = ''
                    return
                out = strip_machine_tokens(out)
                if not out.strip():
                    self._stream({
                        'type': 'flow_critic_msg',
                        'iteration': self._iteration,
                        'content': '',
                        'thinking': '',
                        'next_phase': 'worker',
                        'discard': True,
                        **self._turn_meta(role, emits),
                    })
                    self._vu_msg_id = ''
                    return
            msg = {
                'role': 'user',
                'content': out,
                'thinking': thinking,
                'timestamp': _now(),
            }
            self._mark_user_side(msg, role, next_phase=next_phase)
            self._push(msg)
            # Finalize the critic/VU bubble live.
            self._stream({'type': 'flow_critic_msg',
                          'iteration': self._iteration, 'content': out,
                          'thinking': thinking, 'next_phase': next_phase,
                          **self._turn_meta(role, emits)})
            if role == 'virtual_user':
                self._vu_msg_id = ''
        else:
            # An assistant-side producer turn (worker / specialist). The
            # iteration was already counted at step_start; the worker bubble
            # is finalized by the NEXT iteration / complete event, so no
            # finalize SSE is emitted here.
            self._push({
                'role': 'assistant',
                'content': out,
                'thinking': thinking,
                'timestamp': _now(),
                '_flowIteration': self._iteration,
                '_flowStateChangingCount': ev.get('state_changing', 0),
            })

    def _mark_user_side(self, msg: dict, role: str, *, next_phase: str,
                        synthetic: bool = False) -> None:
        """Stamp a user-side turn with the CORRECT lane markers.

        ``virtual_user`` (goal mode) → ``_isVirtualUser`` (+ a routable
        ``_msgId`` / optional ``_autopilotRunId``), mirroring the live
        goal-mode path (lib/tasks_pkg/autopilot.py). Crucially these rows
        carry NO Flow-review marker, so ``_transform_messages`` KEEPS them and
        the VU instruction reaches the model. ``critic`` / ``reviewer``
        → ``_isFlowReview`` display-only markers (skipped by the context
        builder). A synthetic guard row follows the flow's kind
        (``self._vu_flow``).
        """
        is_vu = role == 'virtual_user' or (synthetic and self._vu_flow)
        if is_vu:
            from lib.turn_initiation import (
                INITIATOR_AUTOPILOT,
                stamp_initiator,
            )

            msg['_isVirtualUser'] = True
            stamp_initiator(msg, INITIATOR_AUTOPILOT)
            msg['_msgId'] = self._vu_msg_id or uuid.uuid4().hex
            if self._vu_run_id:
                msg['_autopilotRunId'] = self._vu_run_id
        else:
            msg['_isFlowReview'] = True
            msg['_flowIteration'] = self._iteration
            msg['_flowApproved'] = next_phase == 'stop'
            msg['_flowNextPhase'] = next_phase

    _derive_emits = staticmethod(flow_emits_for_role)

    def _turn_meta(self, role: str, emits: str) -> dict:
        """Wire metadata that keeps live and persisted turn identity equal."""
        return project_flow_turn_metadata(
            role,
            emits,
            projection=self._projection,
            vu_msg_id=self._vu_msg_id,
            vu_run_id=self._vu_run_id,
        )

    def _on_zero_deliverable_guard(self, ev: dict):
        if self._projection == 'autopilot':
            # The real VU turn has already told the worker what remains. The
            # engine directive is control-plane feedback for the next worker,
            # not a second synthetic user message. Standalone goal mode never
            # persists such a duplicate row, so keep it off the transcript.
            return
        # Mirror the synthetic critic row so the UI shows the guard.
        content = ('⚠️ Zero-deliverable guard: the worker produced no '
                   'state-changing actions; injecting an execute-now '
                   'directive.')
        # Open + finalize a synthetic critic bubble live (no deltas).
        self._stream({'type': 'flow_iteration',
                      'iteration': self._iteration, 'phase': 'reviewing'})
        guard_msg = {
            'role': 'user',
            'content': content,
            'timestamp': _now(),
            '_isSyntheticCritic': True,
        }
        self._mark_user_side(guard_msg, '', next_phase='worker', synthetic=True)
        self._push(guard_msg)
        self._stream({'type': 'flow_critic_msg',
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
        return project_flow_next_phase(
            text,
            pending_replan=self._pending_replan,
        )

    def _push(self, msg: dict):
        self.messages.append(msg)
        if self._emit:
            try:
                self._emit(msg)
            except Exception as e:
                logger.debug('[FlowAdapter] emit failed: %s', e)

    def _stream(self, ev: dict):
        if self._on_stream:
            try:
                self._on_stream(ev)
            except Exception as e:
                logger.debug('[FlowAdapter] on_stream failed: %s', e)


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


__all__ = ['FlowEventAdapter']
