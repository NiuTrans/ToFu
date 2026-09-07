"""lib/artifacts/events.py — SSE event emission for artifact lifecycle.

The frontend learns about new artifacts via the same task-event channel
it already uses for tool rounds and content deltas.  This avoids a second
SSE stream and gets us free Last-Event-ID replay through ``task_events``.

Event shape (subset of the public schema; the route layer adds the URL):

    {
      "type":      "artifact",
      "id":        "<uuid>",
      "conv_id":   "...",
      "task_id":   "...",
      "msg_id":    "...",
      "source":    "write_file" | "inline_fence" | "inline_doc",
      "format":    "markdown" | "html" | "svg",
      "title":     "report.html",
      "size_bytes": 18543,
      "version":   1,
      "url":       "/api/artifacts/<id>/raw"
    }

Crucially the **content is NOT in this event** — only metadata.  The
frontend fetches the raw bytes lazily when the user clicks "Open".
This keeps the SSE stream small and replayable.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def _build_event(artifact_meta: dict) -> dict:
    """Translate a ``create_artifact`` return value into an SSE payload."""
    from lib.agent_core.events import EventType, build_event
    aid = artifact_meta.get('id', '')
    return build_event(
        EventType.ARTIFACT,
        id=aid,
        conv_id=artifact_meta.get('conv_id', ''),
        task_id=artifact_meta.get('task_id', ''),
        msg_id=artifact_meta.get('msg_id', ''),
        source=artifact_meta.get('source', ''),
        source_ref=artifact_meta.get('source_ref', {}),
        format=artifact_meta.get('format', ''),
        title=artifact_meta.get('title', ''),
        size_bytes=int(artifact_meta.get('size_bytes', 0)),
        version=int(artifact_meta.get('version', 1)),
        parent_id=artifact_meta.get('parent_id', ''),
        pinned=bool(artifact_meta.get('pinned', False)),
        created_at=int(artifact_meta.get('created_at', 0)),
        url=f'/api/artifacts/{aid}/raw' if aid else '',
    )


def emit_artifact_event(task: Any, artifact_meta: dict) -> None:
    """Append an ``artifact`` SSE event to the given task's event log.

    Args:
        task: a task dict from ``lib.tasks_pkg.manager``.  When None or
              missing the events lock, the function logs and returns —
              never raises into the caller's persistence path.
        artifact_meta: the dict returned by ``create_artifact``.
    """
    if not artifact_meta or not artifact_meta.get('id'):
        logger.debug('[Artifacts] emit_artifact_event: empty meta, skipping')
        return
    if task is None:
        logger.debug('[Artifacts] emit_artifact_event: no task, skipping (id=%s)',
                     artifact_meta['id'][:8])
        return

    payload = _build_event(artifact_meta)

    # Late import to avoid circular deps (manager imports may pull
    # parts of this subsystem in the future).
    try:
        from lib.tasks_pkg.manager import append_event
    except Exception as e:
        logger.warning('[Artifacts] cannot import append_event: %s', e)
        return

    try:
        append_event(task, payload)
    except Exception as e:
        logger.warning('[Artifacts] append_event failed for id=%s: %s',
                       artifact_meta['id'][:8], e, exc_info=True)
