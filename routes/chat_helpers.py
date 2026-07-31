"""routes/chat_helpers.py — pure utilities extracted from routes/chat.py.

**Extraction context** (board epic ``pt_04686ac6054a451e``, slice 1 of N):
``routes/chat.py`` is being decomposed. This module holds the FIRST slice —
five functions with three defining properties that make the move
wire-safe:

  1. **Pure**: no module-level mutable state (no ``_send_abort_marker``-like
     dicts), no ``routes/chat.py``-defined globals read at call time.
  2. **No route decorators**: every function here is called *by* handlers,
     never IS a handler. No BP registration moves with the split.
  3. **Import-lightweight**: only stdlib + ``orjson`` + ``lib.log``. Zero
     circular-import risk against ``routes/chat.py`` or its callers.

``routes/chat.py`` keeps every name importable via a re-export line at the
top (``from routes.chat_helpers import *``-equivalent, but explicit); every
external ``from routes.chat import _dumps_yielding`` etc. keeps working
unchanged. Wire-parity guarded by ``tests/test_routes_chat_wire_parity.py``.

Later slices will move the state-carrying helpers
(``_send_abort_marker`` + ``_mark_conv_aborted`` / ``_was_aborted_after``
as a bundle; ``_truncate_conv_history``; the 300-line ``_start_task_for_conv``)
plus the fat handlers themselves into a proper ``routes/chat/`` sub-package
once the seam behaviour is proven safe by this first slice.
"""

from __future__ import annotations

import json

import orjson

from lib.log import get_logger

logger = get_logger(__name__)


def _dumps_yielding(obj) -> str:
    """Serialize a (potentially multi-MB) SSE snapshot off the event loop.

    Background: the C accelerator behind ``json.dumps`` holds the GIL for the
    *entire* call and never releases it mid-encode, so wrapping plain
    ``json.dumps`` in ``asyncio.to_thread`` does NOT free the loop — a 10 MB
    conversation snapshot still stalls ``accept()`` for ~40 ms (the wedge
    behind the 15000 incident).

    ``orjson.dumps`` encodes the same 10 MB in ~5 ms — fast enough that the
    loop stall drops to ~4 ms even though it, too, holds the GIL; the encode
    is simply over before it matters, and it also tames the pathological
    "one huge string field" shape that ``iterencode`` (one atomic chunk)
    cannot. It is the primary path.

    orjson rejects a handful of inputs the stdlib tolerates (notably non-str
    dict keys → ``JSONEncodeError``/``TypeError``). For those rare snapshots
    we fall back to ``JSONEncoder.iterencode``, which yields to the
    interpreter between chunks so the loop can still breathe.

    The two encoders differ only in item separators (orjson is compact:
    ``,``/``:`` vs stdlib ``, ``/``: ``); both are valid JSON the frontend
    parses identically.
    """
    try:
        return orjson.dumps(obj).decode('utf-8')
    except (TypeError, ValueError) as e:
        logger.warning('[Chat] orjson snapshot encode failed (%s); '
                       'falling back to stdlib iterencode', e)
        return ''.join(json.JSONEncoder(ensure_ascii=False).iterencode(obj))


def _running_checkpoint_verdict(sharded: bool):
    """Decide how to report a DB checkpoint with status='running' whose task is
    ABSENT from this replica's memory (Epic C §4.1 / §6.4).

    Returns ``(effective_status, reconnect_hint)``:
      * sharded (redis, multi-replica): ``('running', True)`` — the task is
        (probably) alive on another replica; the client re-routes via taskId
        affinity. NO cross-replica liveness probe, NO DB flip to interrupted.
      * single-process (inproc): ``('interrupted', False)`` — absent genuinely
        means the server crashed mid-task; keep the crash-recovery behaviour
        byte-identical to before Epic C.
    """
    if sharded:
        return ('running', True)
    return ('interrupted', False)


def _log_poll_task_id_mismatch(db, conv_id, polled_task_id, db_meta):
    """P0 observability: log the activeTaskId ↔ message _taskId inconsistency
    behind an empty-metadata interrupted poll.

    When a poll serves an ``interrupted`` result whose metadata is EMPTY (no
    finishReason/usage/apiRounds — the finish-bar shows only the model name),
    the underlying cause is almost always an ID desync: the conversation's
    ``settings.activeTaskId`` no longer matches the task the client polled OR
    the trailing assistant message's ``_taskId``. Surfacing that mismatch here
    means the empty finish-bar is diagnosable from ``app.log`` alone, without a
    post-hoc DB query. Best-effort — never raises into the poll response.
    """
    try:
        has_meta = any(db_meta.get(k) for k in ('finishReason', 'usage', 'apiRounds'))
        if has_meta or not conv_id:
            return
        row = db.execute(
            'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return
        try:
            settings = json.loads(row['settings'] or '{}') or {}
        except (json.JSONDecodeError, TypeError) as _e:
            logger.debug('log poll task id mismatch: malformed JSON/unexpected type (%s)', _e)
            settings = {}
        active_task_id = settings.get('activeTaskId')
        reconciled_at = settings.get('_reconciledAt')
        msg_task_id = None
        try:
            messages = json.loads(row['messages'] or '[]')
            for m in reversed(messages):
                if m.get('role') == 'assistant':
                    msg_task_id = m.get('_taskId')
                    break
        except (json.JSONDecodeError, TypeError) as _e:
            logger.debug('log poll task id mismatch: malformed JSON/unexpected type (%s)', _e)
            pass
        logger.warning(
            '[Chat] Poll %s — EMPTY-metadata interrupted result; ID inconsistency: '
            'polled=%s activeTaskId=%s msg_taskId=%s _reconciledAt=%s. '
            'Finish-bar will show only the model name (finishReason/usage/apiRounds never persisted). '
            'This is the interrupted-turn ID desync (empty finish-bar + flicker) class.',
            polled_task_id[:8], polled_task_id[:8],
            (active_task_id[:8] if active_task_id else 'none'),
            (msg_task_id[:8] if msg_task_id else 'none'),
            reconciled_at or 'none')
    except Exception as _e:
        logger.debug('[Chat] poll ID-mismatch probe failed conv=%s: %s',
                     conv_id[:8] if conv_id else '?', _e)


def _loads_yielding(raw):
    """Parse a (potentially multi-MB) JSON snapshot with minimal GIL-hold.

    The mirror of :func:`_dumps_yielding` for the DECODE direction. The
    stdlib ``json.loads`` C accelerator holds the GIL for the whole parse,
    so a multi-MB ``tool_rounds`` blob decoded inside the sync SSE fallback
    generators (``gen_done`` / ``gen_persisted``) stalls the event loop just
    as an on-loop encode would — those generators run each ``next()`` in the
    executor via Quart's ``run_sync_iterable``, but the GIL is still held for
    the whole call so the loop thread is starved regardless (the same trap
    documented for ``to_thread(json.dumps)``).

    ``orjson.loads`` parses the same blob several times faster and releases
    the GIL far sooner, dropping the stall below the danger threshold. It
    accepts ``str`` or ``bytes``. On the rare input orjson rejects we fall
    back to stdlib ``json.loads`` so behaviour is never worse than before.
    """
    try:
        return orjson.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning('[Chat] orjson snapshot parse failed (%s); '
                       'falling back to stdlib json.loads', e)
        return json.loads(raw)


def _warm_resume_serviceable(resume_cursor, n_events):
    """Decide whether a warm (in-memory) Last-Event-ID resume is serviceable.

    Returns True iff ``resume_cursor`` names a position the in-memory event
    buffer can actually replay from — i.e. the next event to send
    (``resume_cursor + 1``) is at or before the current buffer length.

    When False the caller MUST fall back to a full state-snapshot
    (a "resync"), exactly as the cold path does, instead of slicing
    ``events[resume_from:]`` into an empty list. An empty slice on an
    ahead-of-buffer cursor used to leave the warm stream sending nothing
    until the next live event (a silent stall) and mis-index the live loop.
    A cursor that is plausibly behind the buffer (``>= -1``, in range) stays
    serviceable; only an out-of-range-ahead cursor forces resync.

    ``resume_cursor`` is the SSE ``Last-Event-ID`` (id of the last RECEIVED
    event); ``-1``/``0`` etc. are normal early cursors. The boundary case
    ``resume_from == n_events`` IS serviceable (empty replay, then live
    streaming continues from exactly that index).
    """
    if resume_cursor is None or resume_cursor < 0:
        return False  # no/invalid cursor → fresh snapshot (caller's else-branch)
    resume_from = resume_cursor + 1
    return resume_from <= n_events


__all__ = [
    '_dumps_yielding',
    '_loads_yielding',
    '_log_poll_task_id_mismatch',
    '_running_checkpoint_verdict',
    '_warm_resume_serviceable',
]
