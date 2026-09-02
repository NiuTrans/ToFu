"""Owner-bound repository for the recent-project navigation history."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from lib.identity import require_user_id


class RecentProjectRepository:
    """Expose recent-project operations without leaking storage keys or SQL."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.owner_user_id = require_user_id(
            owner_user_id, context="recent project owner")
        self._client_factory = client_factory

    def _client(self, *, write: bool = False):
        if self._client_factory is not None:
            return self._client_factory(write=write)
        from lib.storage import get_storage_client

        return get_storage_client(write=write)

    def list(self) -> list[dict]:
        rows = self._client().query(
            "project.recent.list", {"user_id": self.owner_user_id})
        return [dict(row) for row in rows] if isinstance(rows, list) else []

    def touch(self, project_path: str) -> dict:
        path = str(project_path or "").strip()
        if not path:
            raise ValueError("project_path is required")
        result = self._client(write=True).command(
            "project.recent.touch",
            {
                "user_id": self.owner_user_id,
                "project_path": path,
                "last_used": int(time.time()),
            },
            f"project.recent.touch:{uuid.uuid4().hex}",
        )
        return dict(result) if isinstance(result, dict) else {}

    def clear(self) -> int:
        result = self._client(write=True).command(
            "project.recent.clear",
            {"user_id": self.owner_user_id},
            f"project.recent.clear:{uuid.uuid4().hex}",
        )
        return int((result or {}).get("deleted") or 0)


__all__ = ["RecentProjectRepository"]
