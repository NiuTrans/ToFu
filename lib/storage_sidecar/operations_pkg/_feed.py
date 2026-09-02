"""Activity feed and status-stream operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import time
import uuid
from typing import Any


from lib.log import get_logger
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._records import (
    _append_event_row,
)


def _feed_append(session: Session, payload: Mapping[str, Any]) -> Any:
    from lib.task_event_contract import PROJECT_FEED_STREAM_KIND

    project_path = _required_text(payload, "project_path", 4096)
    user_id = _integer(payload, "user_id", minimum=1)
    task_id = f"project-feed:{user_id}:{project_path}"
    session.lock_key("project.feed", f"{user_id}:{project_path}")
    # 1-based sequence: readers use `sequence > since_seq` (default 0), so the
    # first event must be seq 1 — 0-based would silently drop it (matches the
    # legacy project_events 1-based allocate_scoped_sequence).
    current = session.fetch_one(
        "SELECT COALESCE(MAX(sequence),0) AS sequence FROM storage_events WHERE task_id=?",
        (task_id,),
    )
    sequence = int(current["sequence"]) + 1
    now = int(time.time() * 1000)
    event = dict(payload.get("event") or {})
    event.update({"seq": sequence, "ts": now})
    _append_event_row(
        session, {"task_id": task_id, "sequence": sequence, "event": event,
                  "stream_kind": PROJECT_FEED_STREAM_KIND}
    )
    # Retention window comes from the caller (legacy parity: project_feed's
    # _PROJECT_EVENTS_KEEP=500). Hardcoding 200 here both diverged from legacy
    # and made the window unreachable for client-side tests.
    keep = _integer(payload, "keep", default=500, minimum=1)
    if sequence > keep:
        session.execute(
            "DELETE FROM storage_events WHERE task_id=? AND sequence<=?",
            (task_id, sequence - keep),
        )
    return event


def _feed_list(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _integer(payload, "user_id", minimum=1)
    task_id = f"project-feed:{user_id}:{project_path}"
    after = _integer(payload, "since_seq", default=0, minimum=0)
    limit = _integer(payload, "limit", default=100, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT sequence,event_json FROM storage_events WHERE task_id=? AND sequence>? ORDER BY sequence DESC LIMIT ?",
        (task_id, after, limit),
    )
    events = [_load(row["event_json"]) or {} for row in rows]
    return {
        "events": events,
        "maxSeq": max([int(event.get("seq", 0)) for event in events] or [0]),
    }


def _status_append(session: Session, payload: Mapping[str, Any]) -> Any:
    from lib.task_event_contract import PROJECT_STATUS_STREAM_KIND

    project_path = _required_text(payload, "project_path", 4096)
    user_id = _integer(payload, "user_id", minimum=1)
    task_id = f"project-status:{user_id}:{project_path}"
    session.lock_key("project.status", f"{user_id}:{project_path}")
    current = session.fetch_one(
        "SELECT COALESCE(MAX(sequence),-1) AS sequence FROM storage_events WHERE task_id=?",
        (task_id,),
    )
    sequence = int(current["sequence"]) + 1
    now = int(time.time() * 1000)
    event = {
        "seq": sequence,
        "snapshot_id": str(payload.get("snapshot_id") or uuid.uuid4().hex),
        "narrative": str(payload.get("narrative") or ""),
        "pillar_state": payload.get("pillar_state") or {},
        "trigger": str(payload.get("trigger") or "manual"),
        "ts": now,
    }
    _append_event_row(
        session, {"task_id": task_id, "sequence": sequence, "event": event,
                  "stream_kind": PROJECT_STATUS_STREAM_KIND}
    )
    # Legacy parity: project_status._SNAPSHOTS_KEEP=200 (was hardcoded 100).
    keep = _integer(payload, "keep", default=200, minimum=1)
    if sequence > keep:
        session.execute(
            "DELETE FROM storage_events WHERE task_id=? AND sequence<=?",
            (task_id, sequence - keep),
        )
    return event


def _status_list(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _integer(payload, "user_id", minimum=1)
    task_id = f"project-status:{user_id}:{project_path}"
    limit = _integer(payload, "limit", default=30, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT sequence,event_json FROM storage_events WHERE task_id=? ORDER BY sequence DESC LIMIT ?",
        (task_id, limit),
    )
    snapshots = [_load(row["event_json"]) or {} for row in rows]
    return {
        "snapshots": snapshots,
        "maxSeq": int(snapshots[0].get("seq", 0)) if snapshots else 0,
    }
