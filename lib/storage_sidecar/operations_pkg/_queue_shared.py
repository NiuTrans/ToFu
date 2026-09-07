"""Shared SQL helpers for operations that atomically consume queue intent."""

from __future__ import annotations

from lib.storage_sidecar.adapters.base import Session


def renumber_queue_positions(
    session: Session,
    conversation_id: str,
    user_id: int,
) -> None:
    """Restore the owner-scoped contiguous queue position projection."""
    rows = session.fetch_all(
        "SELECT id FROM storage_queue_items "
        "WHERE conv_id = ? AND user_id = ? "
        "ORDER BY priority, position, id",
        (conversation_id, user_id),
    )
    for position, row in enumerate(rows, 1):
        session.execute(
            "UPDATE storage_queue_items SET position = ? "
            "WHERE id = ? AND user_id = ?",
            (position, row["id"], user_id),
        )


__all__ = ["renumber_queue_positions"]
