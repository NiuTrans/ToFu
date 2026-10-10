"""Read-only decoding for persisted task-result metadata.

Conversation transcript writes live exclusively in the Turn/Attempt command
and lifecycle services. This module intentionally has no conversation writer.
"""

from __future__ import annotations

import json

from lib.log import get_logger


logger = get_logger(__name__)


def extract_db_meta(row) -> dict:
    """Decode the metadata document stored with one task-result row."""
    if not row["metadata"]:
        return {}
    try:
        return json.loads(row["metadata"])
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "[Chat] Failed to parse task metadata JSON (task_id=%s): %s",
            row["task_id"],
            exc,
            exc_info=True,
        )
        return {}


__all__ = ["extract_db_meta"]
