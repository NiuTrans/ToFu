"""Per-round swarm/peer/steer inbox drain (pt_03f4cdf1 slice 11).

Extracted from ``run_task``'s per-round body (~L585 in _run.py) so the
main loop no longer carries this ~180-line block inline. Byte-identical
behaviour to the pre-extraction form — the tests in
``test_lib_orchestrator_swarm_inbox_wire_parity.py`` pin the seam.

Owned by run_task (called every round just before tool-list resolution).
The three lanes have subtly different semantics documented alongside
each drain call:

  * Swarm items — sub-agent completions and other model-facing
    notifications drained from ``swarm_key_for(task)``. Delivered
    IMMEDIATELY (chip emitted + ``mark_delivered`` persisted here) so a
    mid-turn restart does not re-inject the same <swarm-update>s.
  * Peer items — Pillar #6 peer messages drained from the possibly-
    different ``_peer_drain_key`` (a VU sub-task runs with convId='' and
    carries the parent conv in that key). Delivery is DEFERRED to the
    post-LLM flush in run_task: only after the LLM call returns is the
    chip emitted AND the durable message_queue row deleted — an atomic
    "chip-shown ⟺ model-consumed ⟺ durable-deleted" step that keeps
    delivery exactly-once across an abort.
  * Steer items — human interjections drained from the swarm key with
    the same deferred-confirm discipline as peer. Salvaged back to the
    durable queue by finalize on abort.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def drain_and_inject_inbox(
    *,
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    round_num: int,
    tid: str,
) -> None:
    """Drain swarm/peer/steer inboxes and inject as ONE coalesced user
    message before the next LLM call.

    Mutates ``messages`` (appends the coalesced user message when any
    lane produced payloads) and ``task`` (SWARM_INBOX_INJECT event +
    ``_inboxInjects`` sidecar for immediate delivery;
    ``_peer_inject_pending`` / ``_steer_inject_pending`` stashes for the
    deferred post-LLM flush). Never raises — a drain failure logs an
    error and the task continues without notifications.

    Refuses to drain when the previous message is an unmatched
    assistant tool_call (the tool_call ↔ tool_result pair MUST close
    before another role can speak).
    """
    try:
        _last_msg = messages[-1] if messages else None
        _has_unmatched_tool_call = (
            bool(_last_msg)
            and _last_msg.get('role') == 'assistant'
            and _last_msg.get('tool_calls')
        )
        if not _has_unmatched_tool_call:
            from lib.agent_inbox import drain as _drain_inbox
            from lib.swarm.integration import swarm_key_for as _swarm_key_for
            # NOTE: drain with the conversation-scoped SWARM KEY — the
            # inbox is keyed by ``swarm_key_for(task)`` (conv id when
            # present, else task id) so <swarm-update>s enqueued by a
            # PRIOR turn's background agents are still drained on a
            # later "continue" turn of the same conversation. ``tid`` is
            # just the 8-char log prefix.
            _swarm_key = _swarm_key_for(task)
            # ── Peer key can DIFFER from the swarm key ──
            #   A VU sub-task runs with convId='' (swarm key = sub-task
            #   id) but its peer twin lives under the PARENT conv, passed
            #   via ``_peer_drain_key``. And when a DRIVER loop (endpoint)
            #   owns peer delivery at its OWN iteration boundary it sets
            #   ``_peer_driver_owned`` — run_task must then NOT drain peer
            #   here (only swarm), or the two paths would double-drain.
            _peer_owned = bool(task.get('_peer_driver_owned'))
            _peer_key = task.get('_peer_drain_key') or _swarm_key
            # Swarm items (peer-msg AND user-steer excluded — both are
            # drained separately below with their own de-dup / chip
            # semantics; folding them into _swarm_items would render a
            # human steer as a <swarm-update> chip and mark it delivered
            # via the swarm path, which is the wrong lane).
            _swarm_items = _drain_inbox(
                _swarm_key, exclude_modes=['peer-msg', 'user-steer'])
            _peer_items = ([] if _peer_owned
                           else _drain_inbox(_peer_key, modes=['peer-msg']))
            # Human steer messages (the operator interjecting into their
            # own running turn). Keyed on the same conversation swarm key
            # the send route enqueues under. Delivered exactly once via
            # the deferred-confirm flush after the LLM call (mirrors peer).
            _steer_items = _drain_inbox(_swarm_key, modes=['user-steer'])
            _inbox_items = (list(_swarm_items) + list(_peer_items)
                            + list(_steer_items))
            if _inbox_items:
                # Coalesce ALL drained items into a single user
                # message — one message with N <swarm-update>
                # blocks instead of N adjacent user messages.
                # Reasons:
                #   1. Cuts message count → cleaner cache prefix.
                #   2. <swarm-update> is treated as factual data
                #      (not a system reminder), so this is a real
                #      user-role message — no _isMeta flag, no
                #      <system-reminder> wrapper.  Mirrors Claude
                #      Code's <task-notification> approach.
                _payloads = [it.get('value', '') for it in _inbox_items
                             if it.get('value')]
                if _payloads:
                    messages.append({
                        'role':    'user',
                        'content': '\n\n'.join(_payloads),
                    })
                    # Items already partitioned by the two drains above:
                    # ``_swarm_items`` (sub-agent results, carry agent_id)
                    # and ``_peer_items`` (Pillar #6, carry queueId). They
                    # share the ONE coalesced user message but their de-dup
                    # + observability handling differ.
                    _swarm_items = [it for it in _swarm_items if it.get('value')]
                    _peer_items = [it for it in _peer_items if it.get('value')]
                    _steer_items = [it for it in _steer_items if it.get('value')]

                    # Swarm: persist the delivered flag so a restart
                    # mid-turn doesn't re-inject these <swarm-update>s.
                    if _swarm_items:
                        try:
                            from lib.swarm import persistence as _swarm_persist
                            _swarm_persist.mark_delivered(
                                _swarm_key_for(task),
                                [it.get('agent_id', '') for it in _swarm_items
                                 if it.get('agent_id')])
                        except Exception as _mde:
                            logger.debug('[Task %s] swarm mark_delivered failed: %s',
                                         tid, _mde)
                        _swarm_previews = [{
                            'agentId': it.get('agent_id', ''),
                            'text': (it.get('value') or '')[:1200],
                        } for it in _swarm_items]
                        append_event(task, build_event(
                            EventType.SWARM_INBOX_INJECT,
                            roundNum=round_num + 1,
                            count=len(_swarm_items),
                            agentIds=[it.get('agent_id', '')
                                      for it in _swarm_items],
                            # ★ Carry the actual <swarm-update> payloads
                            #   (truncated) so the frontend can render an
                            #   in-timeline ptool-panel row showing exactly
                            #   what the model received — not just a count.
                            previews=_swarm_previews,
                        ))
                        # Display-only sidecar accumulation (shape mirrors
                        # the peer/steer inject records). Persisted by the
                        # sync layer as the underscore field
                        # ``msg['_inboxInjects']`` — NEVER into toolRounds
                        # (that is the wire-replay / prefix-cache source; a
                        # synthetic row there breaks tool-turn continuation
                        # and shifts wire bytes). Frontend rebuilds the
                        # in-timeline chip from this on reload.
                        task.setdefault('_inboxInjects', []).append({
                            'round': round_num + 1,
                            'count': len(_swarm_items),
                            'agentIds': [it.get('agent_id', '')
                                         for it in _swarm_items],
                            'previews': _swarm_previews,
                        })

                    # Peer: the message is now in the in-memory
                    # `messages` list but NOT yet consumed by the model.
                    # The FORWARD-race de-dup (delete the durable row)
                    # and the PEER_INBOX_INJECT arrival chip are BOTH
                    # DEFERRED to just after the LLM call confirms
                    # consumption (see the flush below) — so an abort
                    # before the call leaves the durable row intact for a
                    # later fresh-turn redelivery (never zero-delivered).
                    if _peer_items:
                        # ── DEFERRED confirmed-delivery (never-zero fix) ──
                        # Do NOT emit the PEER_INBOX_INJECT chip NOR
                        # delete the durable message_queue rows here. At
                        # this point the message is only placed in the
                        # IN-MEMORY `messages` list — the model has not
                        # yet consumed it. If the task aborts / crashes
                        # between here and the LLM call, the inbox twin is
                        # already drained (gone) and the in-memory message
                        # dies with the task; deleting the durable row now
                        # would make the message render NOWHERE (zero
                        # delivery), and emitting the chip now would show
                        # a delivery that never happened. Instead stash
                        # the peer items and do BOTH — emit the chip AND
                        # delete the durable rows — only AFTER the LLM
                        # call returns (delivery confirmed), so
                        # chip-shown ⟺ model-consumed ⟺ durable-deleted
                        # is one atomic step. On an abort the durable row
                        # SURVIVES → it is re-dispatched later as a fresh
                        # turn (delivered late, never lost, and rendered
                        # exactly once).
                        task.setdefault(
                            '_peer_inject_pending', []).extend(_peer_items)

                    # Steer: same deferred-confirm discipline as peer.
                    # The human steer is now in the in-memory `messages`
                    # list but the model has not consumed it yet. Do NOT
                    # emit the USER_STEER_INJECT chip here — stash it and
                    # emit AFTER the LLM call confirms consumption. On an
                    # abort before the call the steer is re-routed to the
                    # durable message_queue as a fresh next turn (see the
                    # flush + the finalize salvage), so it is delivered
                    # exactly once — never zero, never double.
                    if _steer_items:
                        task.setdefault(
                            '_steer_inject_pending', []).extend(_steer_items)

                    logger.info(
                        '[Task %s] injected %d inbox item(s) '
                        '(%d swarm, %d peer, %d steer) as 1 user message '
                        'at round %d',
                        tid, len(_payloads), len(_swarm_items),
                        len(_peer_items), len(_steer_items), round_num + 1)
    except Exception as _e:
        logger.error(
            '[Task %s] swarm inbox drain/inject failed at round %d: %s '
            '— continuing without notifications',
            tid, round_num + 1, _e, exc_info=True)
