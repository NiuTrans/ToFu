# HOT_PATH — this leaf is called per stream-loop iteration.
"""Post-LLM deferred peer + steer inbox flush.

Extracted 2026-07-28 ( slice 12) from
``lib.tasks_pkg.orchestrator._run.run_task``. The two flushes run
RIGHT AFTER a successful ``_llm_call_with_fallback`` return and
BEFORE the stream loop reads the resolved model onto the task.
They enforce the never-zero-and-never-double delivery invariants:

Peer flush
----------
The LLM call succeeded, so a peer message injected into ``messages``
this round WAS consumed by the model. NOW — atomically — emit the
``PEER_INBOX_INJECT`` chip (the in-timeline arrival marker) AND
delete the durable ``message_queue`` row(s) so ``dispatch_next_queued``
can't later re-dispatch them as a redundant fresh turn. If the task
aborted BEFORE this point, neither happened and the durable row
SURVIVED → it is re-dispatched later as a fresh turn (delivered late,
rendered exactly once — never zero, never double). Runs after a
fallback too (delivery still happened). Best-effort: a delete failure
only risks a rare double-delivery (reverse-race guard still applies),
never a loss.

Steer flush
-----------
Same discipline as the peer flush above: the LLM call succeeded, so
the human steer injected into ``messages`` this round WAS consumed by
the model. Emit the ``USER_STEER_INJECT`` chip now (delivery
confirmed) and accumulate a DISPLAY-ONLY sidecar record on the task
(``task['_userSteerInjects']``) so the sync layer can persist it onto
the assistant message as an underscore field — NEVER into
``toolRounds`` (that is the wire-replay / prefix-cache source; a
synthetic row there breaks tool-turn continuation and shifts wire
bytes). On an abort BEFORE this point the chip is never emitted and
the undelivered steer is salvaged back to the durable
``message_queue`` by finalize (see ``_finalize.py``) → re-dispatched
as a fresh turn, delivered exactly once.

VU sub-task carrier
-------------------
A VU sub-task runs with ``convId=''`` and carries the parent conv in
``task['_peer_drain_key']``. The dedup call MUST prefer that key over
``convId`` — the twin was enqueued under it — or a VU-issued peer
message rerolls as a fresh turn.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event


logger = get_logger(__name__)


def flush_deferred_peer_and_steer(task: dict[str, Any], *,
                                  round_num: int, tid: str) -> None:
    """Emit ``PEER_INBOX_INJECT`` + ``USER_STEER_INJECT`` chips and
    accumulate display-only sidecars for the peer / steer / background-
    command messages that were injected into ``messages`` earlier this round
    and are now confirmed as delivered (the LLM call above returned).

    Parameters
    ----------
    task
        Live task dict; both ``_peer_inject_pending`` and
        ``_steer_inject_pending`` are POPPED here.
    round_num
        Zero-based stream-loop round number; the chips carry
        ``roundNum=round_num + 1`` (the 1-based, human-facing round).
    tid
        8-char task-id prefix used for structured log context.
    """
    # ── Flush DEFERRED peer delivery (never-zero fix) ──
    _peer_inject = task.pop('_peer_inject_pending', None)
    if _peer_inject:
        _peer_previews = [{
            'fromConv': _pit.get('fromConv', ''),
            'text': (_pit.get('peerText')
                     or _pit.get('value') or '')[:1200],
        } for _pit in _peer_inject]
        try:
            append_event(task, build_event(
                EventType.PEER_INBOX_INJECT,
                roundNum=round_num + 1,
                count=len(_peer_inject),
                previews=_peer_previews,
            ))
        except Exception as _pce:
            logger.warning('[Task %s] peer inject chip emit failed: %s',
                           tid, _pce)
        # Display-only sidecar accumulation — persisted by the sync
        # layer as ``msg['_peerInjects']`` (underscore field, NEVER
        # into toolRounds). Delivery is confirmed here, so it is safe
        # to record for the committed-message projection + reload.
        task.setdefault('_peerInjects', []).append({
            'round': round_num + 1,
            'count': len(_peer_inject),
            'previews': _peer_previews,
        })
        # Resolve the peer conv key: a VU sub-task runs with
        # convId='' and carries the parent conv in _peer_drain_key,
        # so dedup the durable rows under that key (the same key the
        # twin was enqueued under), not the empty sub-task convId.
        _conv_dd = (task.get('_peer_drain_key')
                    or task.get('convId', '') or '')
        _dd_ids = [_pit.get('queueId') for _pit in _peer_inject
                   if _pit.get('queueId')]
        if _conv_dd and _dd_ids:
            try:
                from lib.message_queue import dedup_inbox_durable_rows
                from lib.tasks_pkg.manager import task_user_id
                dedup_inbox_durable_rows(
                    _conv_dd, _dd_ids, user_id=int(task_user_id(task)))
            except Exception as _dde:
                logger.warning(
                    '[Task %s] deferred peer de-dup failed (durable '
                    'row may re-deliver once): %s', tid, _dde)

    # ── Flush DEFERRED human-steer delivery (never-zero fix) ──
    _steer_inject = task.pop('_steer_inject_pending', None)
    if _steer_inject:
        _steer_previews = [{
            'text': (_sit.get('value') or '')[:1200],
        } for _sit in _steer_inject]
        # Conversation Sync v3 persists a pending block before the worker is
        # woken. Reuse that exact block identity when model consumption is
        # confirmed so the Surface updates in place instead of appending a
        # second chip. Legacy inbox producers without an identity retain the
        # older one-batch-per-round record.
        _steer_lane = task.setdefault('_userSteerInjects', [])
        _legacy_steer_previews = []
        for _sit, _preview in zip(_steer_inject, _steer_previews):
            _block_id = str(_sit.get('blockId') or '')
            if not _block_id:
                _legacy_steer_previews.append(_preview)
                continue
            _steer_lane.append({
                'blockId': _block_id,
                'commandId': str(_sit.get('injectionId') or ''),
                'round': round_num + 1,
                'count': 1,
                'previews': [_preview],
                'deliveryState': 'delivered',
            })
        if _legacy_steer_previews:
            _steer_lane.append({
                'round': round_num + 1,
                'count': len(_legacy_steer_previews),
                'previews': _legacy_steer_previews,
            })
        try:
            append_event(task, build_event(
                EventType.USER_STEER_INJECT,
                roundNum=round_num + 1,
                count=len(_steer_inject),
                previews=_steer_previews,
            ))
        except Exception as _sce:
            logger.warning('[Task %s] steer inject chip emit failed: %s',
                           tid, _sce)

    # ── Flush DEFERRED background-command delivery (never-zero fix) ──
    # Same discipline as peer: the LLM call succeeded, so the detached
    # run_command completion injected this round WAS consumed. Emit the
    # BACKGROUND_COMMAND_INJECT chip, accumulate the display-only
    # ``task['_bgCommandInjects']`` sidecar, and delete the durable
    # message_queue row(s) by queueId so dispatch_next_queued can't later
    # re-dispatch them as a redundant fresh turn. On an abort before this
    # point the row SURVIVES → fresh-turn dispatch delivers it exactly once.
    _bgcmd_inject = task.pop('_bgcmd_inject_pending', None)
    if _bgcmd_inject:
        _bgcmd_previews = [{
            'commandId': str(_bit.get('commandId') or ''),
            'text': (_bit.get('value') or '')[:1200],
        } for _bit in _bgcmd_inject]
        try:
            append_event(task, build_event(
                EventType.BACKGROUND_COMMAND_INJECT,
                roundNum=round_num + 1,
                count=len(_bgcmd_inject),
                previews=_bgcmd_previews,
            ))
        except Exception as _bce:
            logger.warning('[Task %s] background-command inject chip emit '
                           'failed: %s', tid, _bce)
        task.setdefault('_bgCommandInjects', []).append({
            'round': round_num + 1,
            'count': len(_bgcmd_inject),
            'previews': _bgcmd_previews,
        })
        _bg_conv_dd = task.get('convId', '') or ''
        _bg_dd_ids = [_bit.get('queueId') for _bit in _bgcmd_inject
                      if _bit.get('queueId')]
        if _bg_conv_dd and _bg_dd_ids:
            try:
                from lib.message_queue import dedup_inbox_durable_rows
                from lib.tasks_pkg.manager import task_user_id
                dedup_inbox_durable_rows(
                    _bg_conv_dd, _bg_dd_ids,
                    user_id=int(task_user_id(task)))
            except Exception as _bgde:
                logger.warning(
                    '[Task %s] deferred background-command de-dup failed '
                    '(durable row may re-deliver once): %s', tid, _bgde)
