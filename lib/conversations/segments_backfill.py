"""lib/conversations/segments_backfill.py — the ONE segments-fill primitive.

Segment-timeline delivery (epic pt_cb8f98b0cb9b47fb) has two consumers that
must fill a segment-less assistant message from the backend-authoritative
``task_results.segments`` row (the thin persisted render form —
id/name/input/result, exactly what ``renderSegmentTimelineHTML`` consumes),
keyed on the message's ``_taskId``:

  1. The GET-path BACKSTOP (``routes/conversations.py`` — display-only, enriches
     the served payload, never writes back).
  2. The one-shot BACKFILL migration
     (``tests/_migrate_backfill_segments_from_task_results.py`` — persists the
     filled segments into the ``messages`` column so recovery survives
     ``task_results`` retention/cleanup).

Both share the SAME fill semantics — which task ids need filling, and the
segment-shape guard (list, non-empty, ``!= 'null'``). Per the conv-OOM lesson
("a write-path fix is incomplete without a backfill that REUSES, not copies,
the write-path logic"), that shared core lives HERE so the migration can never
drift from the route. The two callers differ ONLY in how they fetch the
``task_results`` rows (a live pooled ``db`` cursor vs the async facade) and what
they do with the result (serve vs persist), so the fetch and the persist stay
in the callers; this module owns the pure planning + splicing.

Pure: no Flask, no DB handle of its own, no LLM. The caller supplies the
already-fetched ``task_id -> segments-JSON`` mapping.
"""

from __future__ import annotations

import json
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def collect_taskids_needing_segments(messages: list[Any]) -> dict[str, list[dict]]:
    """Map ``_taskId`` -> [assistant message dicts] for messages that LACK
    segments but carry a ``_taskId``.

    Returns an empty dict when nothing needs filling (so a caller can early-out
    without a DB round-trip). Non-assistant / already-segmented / task-less
    messages are skipped.
    """
    need: dict[str, list[dict]] = {}
    if not isinstance(messages, list):
        return need
    for m in messages:
        if not isinstance(m, dict) or m.get('role') != 'assistant':
            continue
        if m.get('segments'):
            continue
        tid = m.get('_taskId')
        if tid:
            need.setdefault(tid, []).append(m)
    return need


def parse_segments_json(seg_raw: Any) -> list | None:
    """Coerce a stored ``task_results.segments`` value to a non-empty list, or
    ``None`` when it is absent / ``'null'`` / unparseable / not a non-empty
    list. Centralizes the shape guard both callers apply.
    """
    if not seg_raw or seg_raw == 'null':
        return None
    if isinstance(seg_raw, list):
        return seg_raw if seg_raw else None
    if isinstance(seg_raw, str):
        try:
            segs = json.loads(seg_raw)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[segments-fill] segments JSON parse failed: %s', e)
            return None
        if isinstance(segs, list) and segs:
            return segs
    return None


def fill_messages_with_segments(need: dict[str, list[dict]],
                                segments_by_taskid: dict[str, Any]) -> int:
    """Splice segments into the messages collected by
    ``collect_taskids_needing_segments``.

    Args:
        need: the ``_taskId -> [message dicts]`` mapping to fill (mutated in
            place — segments are assigned onto the message dicts).
        segments_by_taskid: ``task_id -> raw segments value`` (JSON text, or an
            already-decoded list) fetched by the caller from
            ``task_results.segments``.

    Returns the number of messages that received segments. Only fills a message
    still lacking segments (idempotent — a second pass on an already-filled
    message is a no-op).
    """
    filled = 0
    for tid, raw in segments_by_taskid.items():
        segs = parse_segments_json(raw)
        if segs is None:
            continue
        for m in need.get(tid, ()):
            if not m.get('segments'):
                m['segments'] = segs
                filled += 1
    return filled
