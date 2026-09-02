"""Freshness-preserving burst query for authoritative conversation snapshots.

Responsibility
--------------
Collapse equivalent owner-scoped snapshot arrivals onto one authority read and
projection.  Request-local delivery hints are applied in separate top-level
envelopes while the nested authority projection remains read-only during HTTP
serialization.  Flights close before reads and retain no TTL result.

Entry points
------------
``ConversationSnapshotQuery.read`` serves one snapshot request.
``ConversationSnapshotQuery.snapshot`` exposes bounded coordination counters.

Dependencies
------------
The injected loader owns repository access and contract validation.  Active
flights reuse the launch-probed Sidecar RPC capacity and create no workers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime_guards import resolve_resource_budget

from lib.burst_read_coalescer import BurstReadCoalescer
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
class _SnapshotQueryKey:
    user_id: int
    conversation_id: str


class ConversationSnapshotQuery:
    """Share one pre-read authority projection and isolate every participant."""

    def __init__(
        self,
        loader: Callable[[str, int], dict[str, Any]],
        *,
        max_active_gathers: int = _DEFAULT_ACTIVE_GATHERS,
        gather_seconds: float = _DEFAULT_GATHER_SECONDS,
        wait_for_arrivals: Callable[[float], None] = time.sleep,
    ) -> None:
        self._loader = loader
        self._max_active_gathers = max_active_gathers
        self._coalescer = BurstReadCoalescer[
            _SnapshotQueryKey, dict[str, Any]
        ](
            max_active_gathers=max_active_gathers,
            gather_seconds=gather_seconds,
            wait_for_arrivals=wait_for_arrivals,
            observe_bypass=self._observe_bypass,
        )

    def _observe_bypass(self, bypassed: int) -> None:
        if bypassed & (bypassed - 1) == 0:
            logger.warning(
                "[ConversationSnapshot] gather capacity=%d bypassed_total=%d",
                self._max_active_gathers,
                bypassed,
            )

    def read(
        self,
        conversation_id: str,
        user_id: int,
        *,
        push_withheld: bool,
    ) -> dict[str, Any]:
        """Return a request-local envelope with the current delivery hint."""
        key = _SnapshotQueryKey(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        authority = self._coalescer.run(
            key,
            lambda: self._loader(conversation_id, user_id),
        )
        # API response construction and JSON serialization only read nested
        # authority values.  A shallow envelope isolates the sole request-local
        # field without multiplying a multi-megabyte projection in CPU/RAM.
        response = dict(authority)
        # The base value was contract-validated with a boolean false.  Replacing
        # that field with another constructed boolean preserves the schema
        # without traversing the full projection once per HTTP participant.
        response["pushWithheld"] = bool(push_withheld)
        return response

    def snapshot(self) -> dict[str, int]:
        """Expose bounded coordination counters for diagnostics and tests."""
        snapshot = self._coalescer.snapshot()
        snapshot["backingSnapshots"] = snapshot.pop("backingReads")
        return snapshot


__all__ = ["ConversationSnapshotQuery"]
