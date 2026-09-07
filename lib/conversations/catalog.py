"""Bounded burst coalescing for owner-scoped conversation metadata reads.

Responsibility
--------------
Collapse metadata requests that arrive together onto one repository read.  A
short gather window closes *before* the backing read starts, so a request that
arrives after query execution began always starts a fresh read.  No result is
cached after the participating callers complete.

Entry points
------------
``list_conversation_metadata`` serves the process-wide application query.
``list_conversation_metadata_page`` serves the bounded HTTP catalog query.
``ConversationMetadataQuery`` provides an isolated, injectable test seam.

Dependencies
------------
Storage access stays behind ``ConversationRepository``.  The active gather
registry is process-local, owner-keyed, and capped by the launch-probed storage
RPC budget; it creates no executor or background thread of its own.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime_guards import resolve_resource_budget

from lib.burst_read_coalescer import BurstReadCoalescer
from lib.conversations.repository import ConversationRepository
from lib.log import get_logger


_MAX_ACTIVE_GATHERS = 256
_DEFAULT_ACTIVE_GATHERS = resolve_resource_budget(
    "TOFU_STORAGE_RPC_CAPACITY",
    maximum=_MAX_ACTIVE_GATHERS,
)
_DEFAULT_GATHER_SECONDS = 0.008
_MAX_GATHER_SECONDS = 0.100
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _MetadataQueryKey:
    user_id: int
    limit: int
    order_by: str
    settings_keys: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _MetadataPageQueryKey:
    user_id: int
    limit: int
    folder_id: str | None
    before_updated_at: int | None
    before_id: str
    settings_keys: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _MetadataPageValue:
    items: tuple[dict[str, Any], ...]
    total_count: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class ConversationMetadataPage:
    """Independent caller-owned metadata page returned to HTTP projections."""

    items: list[dict[str, Any]]
    total_count: int
    has_more: bool


def _query_key(
    *,
    user_id: int,
    limit: int,
    order_by: str,
    settings_keys: list[str] | tuple[str, ...] | None,
) -> _MetadataQueryKey:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 <= limit <= 10_000
    ):
        raise ValueError("limit must be between 0 and 10000")
    if not isinstance(order_by, str) or not order_by.strip():
        raise ValueError("order_by is required")
    normalized_keys = None
    if settings_keys is not None:
        if not all(isinstance(key, str) and key.strip() for key in settings_keys):
            raise ValueError("settings_keys must contain non-empty strings")
        normalized_keys = tuple(key.strip() for key in settings_keys)
    return _MetadataQueryKey(
        user_id=user_id,
        limit=limit,
        order_by=order_by.strip(),
        settings_keys=normalized_keys,
    )


def _page_query_key(
    *,
    user_id: int,
    limit: int,
    folder_id: str | None,
    before_updated_at: int | None,
    before_id: str,
    settings_keys: list[str] | tuple[str, ...] | None,
) -> _MetadataPageQueryKey:
    base = _query_key(
        user_id=user_id,
        limit=limit,
        order_by="updated_at_desc",
        settings_keys=settings_keys,
    )
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    normalized_folder_id = None
    if folder_id is not None:
        if (
            not isinstance(folder_id, str)
            or not folder_id.strip()
            or len(folder_id) > 512
        ):
            raise ValueError("folder_id must be a non-empty bounded string")
        normalized_folder_id = folder_id.strip()
    if before_updated_at is not None and (
        isinstance(before_updated_at, bool)
        or not isinstance(before_updated_at, int)
        or before_updated_at < 0
    ):
        raise ValueError("before_updated_at must be a non-negative integer")
    if not isinstance(before_id, str) or len(before_id) > 256:
        raise ValueError("before_id must be a bounded string")
    if before_updated_at is None and before_id:
        raise ValueError("before_id requires before_updated_at")
    return _MetadataPageQueryKey(
        user_id=base.user_id,
        limit=base.limit,
        folder_id=normalized_folder_id,
        before_updated_at=before_updated_at,
        before_id=before_id,
        settings_keys=base.settings_keys,
    )


class ConversationMetadataQuery:
    """Share one post-arrival metadata read without retaining stale results."""

    def __init__(
        self,
        repository_factory: Callable[
            [], ConversationRepository
        ] = ConversationRepository,
        *,
        max_active_gathers: int = _DEFAULT_ACTIVE_GATHERS,
        gather_seconds: float = _DEFAULT_GATHER_SECONDS,
        wait_for_arrivals: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(max_active_gathers, bool)
            or not isinstance(max_active_gathers, int)
            or not 1 <= max_active_gathers <= _MAX_ACTIVE_GATHERS
        ):
            raise ValueError(
                f"max_active_gathers must be between 1 and {_MAX_ACTIVE_GATHERS}"
            )
        if not 0 <= float(gather_seconds) <= _MAX_GATHER_SECONDS:
            raise ValueError(
                f"gather_seconds must be between 0 and {_MAX_GATHER_SECONDS}"
            )
        self._repository_factory = repository_factory
        self._max_active_gathers = max_active_gathers
        self._coalescer = BurstReadCoalescer[
            _MetadataQueryKey | _MetadataPageQueryKey,
            tuple[dict[str, Any], ...] | _MetadataPageValue,
        ](
            max_active_gathers=max_active_gathers,
            gather_seconds=gather_seconds,
            wait_for_arrivals=wait_for_arrivals,
            observe_bypass=self._observe_bypass,
        )

    def _observe_bypass(self, bypassed: int) -> None:
        if bypassed & (bypassed - 1) == 0:
            logger.warning(
                "[ConversationCatalog] gather capacity=%d bypassed_total=%d",
                self._max_active_gathers,
                bypassed,
            )

    def _load(self, key: _MetadataQueryKey) -> tuple[dict[str, Any], ...]:
        snapshots = self._repository_factory().list(
            user_id=key.user_id,
            order_by=key.order_by,
            limit=key.limit,
            include_messages=False,
            settings_keys=key.settings_keys,
        )
        return tuple(copy.deepcopy(snapshot.metadata) for snapshot in snapshots)

    def _load_page(self, key: _MetadataPageQueryKey) -> _MetadataPageValue:
        page = self._repository_factory().list_catalog_page(
            user_id=key.user_id,
            limit=key.limit,
            folder_id=key.folder_id,
            before_updated_at=key.before_updated_at,
            before_id=key.before_id,
            settings_keys=key.settings_keys,
        )
        return _MetadataPageValue(
            items=tuple(
                copy.deepcopy(snapshot.metadata) for snapshot in page.items
            ),
            total_count=page.total_count,
            has_more=page.has_more,
        )

    @staticmethod
    def _caller_copy(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        return [copy.deepcopy(row) for row in rows]

    def list_metadata(
        self,
        *,
        user_id: int,
        limit: int = 10_000,
        order_by: str = "updated_at_desc",
        settings_keys: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a fresh owner-scoped projection, joining only arrivals."""
        key = _query_key(
            user_id=user_id,
            limit=limit,
            order_by=order_by,
            settings_keys=settings_keys,
        )
        rows = self._coalescer.run(key, lambda: self._load(key))
        if not isinstance(rows, tuple):
            raise RuntimeError("conversation metadata query returned a page")
        return self._caller_copy(rows)

    def list_metadata_page(
        self,
        *,
        user_id: int,
        limit: int,
        folder_id: str | None = None,
        before_updated_at: int | None = None,
        before_id: str = "",
        settings_keys: list[str] | tuple[str, ...] | None = None,
    ) -> ConversationMetadataPage:
        """Return a bounded page while sharing only equivalent arrivals."""
        key = _page_query_key(
            user_id=user_id,
            limit=limit,
            folder_id=folder_id,
            before_updated_at=before_updated_at,
            before_id=before_id,
            settings_keys=settings_keys,
        )
        page = self._coalescer.run(key, lambda: self._load_page(key))
        if not isinstance(page, _MetadataPageValue):
            raise RuntimeError("conversation metadata page returned rows")
        return ConversationMetadataPage(
            items=[copy.deepcopy(item) for item in page.items],
            total_count=page.total_count,
            has_more=page.has_more,
        )

    def snapshot(self) -> dict[str, int]:
        """Expose bounded coordination counters for diagnostics and tests."""
        snapshot = self._coalescer.snapshot()
        snapshot["backingQueries"] = snapshot.pop("backingReads")
        return snapshot


_METADATA_QUERY = ConversationMetadataQuery()


def list_conversation_metadata(
    *,
    user_id: int,
    limit: int = 10_000,
    order_by: str = "updated_at_desc",
    settings_keys: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Serve the process-wide owner-scoped metadata query."""
    return _METADATA_QUERY.list_metadata(
        user_id=user_id,
        limit=limit,
        order_by=order_by,
        settings_keys=settings_keys,
    )


def list_conversation_metadata_page(
    *,
    user_id: int,
    limit: int,
    folder_id: str | None = None,
    before_updated_at: int | None = None,
    before_id: str = "",
    settings_keys: list[str] | tuple[str, ...] | None = None,
) -> ConversationMetadataPage:
    """Serve the process-wide bounded owner-scoped metadata page query."""
    return _METADATA_QUERY.list_metadata_page(
        user_id=user_id,
        limit=limit,
        folder_id=folder_id,
        before_updated_at=before_updated_at,
        before_id=before_id,
        settings_keys=settings_keys,
    )


__all__ = [
    "ConversationMetadataPage",
    "ConversationMetadataQuery",
    "list_conversation_metadata",
    "list_conversation_metadata_page",
]
