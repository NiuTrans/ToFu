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
            _MetadataQueryKey, tuple[dict[str, Any], ...]
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
        return self._caller_copy(rows)

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


__all__ = ["ConversationMetadataQuery", "list_conversation_metadata"]
