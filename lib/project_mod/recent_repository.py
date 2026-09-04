"""Owner-bound repository for the recent-project navigation history."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from lib.identity import require_user_id
from lib.project_recent_contract import (
    PROJECT_RELINK_STORAGE_DEADLINE_SECONDS,
    RECENT_PROJECT_PATH_MAX_CHARS,
    RECENT_PROJECT_TOUCH_BATCH_LIMIT,
)


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

    @staticmethod
    def _path(project_path: object) -> str:
        path = str(project_path or "").strip()
        if not path or len(path) > RECENT_PROJECT_PATH_MAX_CHARS:
            raise ValueError("project_path is required and bounded")
        return path

    def touch(self, project_path: str) -> dict:
        path = self._path(project_path)
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

    def touch_many(self, project_paths: list[str] | tuple[str, ...]) -> int:
        if (
            not isinstance(project_paths, (list, tuple))
            or not project_paths
            or len(project_paths) > RECENT_PROJECT_TOUCH_BATCH_LIMIT
        ):
            raise ValueError("project_paths must be a bounded non-empty list")
        paths: list[str] = []
        for candidate in project_paths:
            path = self._path(candidate)
            if path not in paths:
                paths.append(path)
        result = self._client(write=True).command(
            "project.recent.touch_many",
            {
                "user_id": self.owner_user_id,
                "project_paths": paths,
                "last_used": int(time.time()),
            },
            f"project.recent.touch_many:{uuid.uuid4().hex}",
        )
        return int((result or {}).get("touched") or 0)

    def relink(self, old_path: str, new_path: str) -> dict:
        old = self._path(old_path)
        new = self._path(new_path)
        result = self._client(write=True).command(
            "project.relink",
            {
                "user_id": self.owner_user_id,
                "old_path": old,
                "new_path": new,
            },
            f"project.relink:{uuid.uuid4().hex}",
            priority="maintenance",
            deadline=PROJECT_RELINK_STORAGE_DEADLINE_SECONDS,
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
