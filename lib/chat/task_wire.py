"""Serialization and recovery helpers for the generic task transport.

The module is transport-neutral and import-lightweight. Route adapters use it
for task polling; the model execution layer uses it for persisted event replay.
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


def _warm_resume_serviceable(resume_cursor, base_cursor, next_cursor):
    """Decide whether a warm (in-memory) Last-Event-ID resume is serviceable.

    Returns True iff the next absolute event sequence (``Last-Event-ID + 1``)
    falls inside the retained rolling window ``[base_cursor, next_cursor]``.

    When False the caller MUST fall back to a full state-snapshot
    (a "resync"), exactly as the cold path does. This covers both a client
    that fell behind head eviction and a future/corrupt cursor that would
    otherwise skip new events forever.

    ``resume_cursor`` is the SSE ``Last-Event-ID`` (id of the last RECEIVED
    event). The boundary case ``resume_cursor + 1 == next_cursor`` is
    serviceable: replay is empty and live streaming continues at the exact
    producer boundary.
    """
    from lib.task_replay import sse_resume_serviceable
    return sse_resume_serviceable(
        resume_cursor, base_cursor=base_cursor, next_cursor=next_cursor)


__all__ = [
    '_dumps_yielding',
    '_loads_yielding',
    '_running_checkpoint_verdict',
    '_warm_resume_serviceable',
]
