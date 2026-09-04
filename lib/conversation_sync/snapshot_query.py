"""Freshness-preserving burst query for authoritative conversation snapshots.

Responsibility
--------------
Collapse equivalent owner-scoped snapshot arrivals onto one authority read and
stable projection.  Callers in that same flight may also share one derived
browser representation. Request-local delivery hints are applied in separate
top-level envelopes while nested values remain read-only during HTTP
serialization. Flights close before reads and retain no TTL result.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import threading
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
_MAX_SHARED_REPRESENTATIONS = 4
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _SnapshotQueryKey:
    user_id: int
    conversation_id: str
    turn_limit: int | None
    include_artifact_hint: bool


@dataclass(slots=True)
class _SharedSnapshotView:
    name: str
    project: Callable[..., dict[str, Any]]
    project_kwargs: tuple[tuple[str, Any], ...]
    value: dict[str, Any]


@dataclass(slots=True)
class _SharedSnapshotAuthority:
    """Own authority and bounded lazy views for one pre-read arrival flight."""

    value: dict[str, Any]
    _view_lock: threading.Lock = field(default_factory=threading.Lock)
    _views: list[_SharedSnapshotView] = field(default_factory=list)

    def representation(
        self,
        name: str,
        project: Callable[..., dict[str, Any]] | None,
        project_kwargs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_kwargs = tuple(sorted((project_kwargs or {}).items()))
        if name == "full":
            if project is not None or normalized_kwargs:
                raise ValueError("The full snapshot representation is authoritative")
            return self.value
        if not name or project is None:
            raise ValueError("A derived snapshot representation requires a projector")

        # Callable identity plus explicit immutable arguments prevent two
        # internal policies from accidentally sharing a name. Arguments let a
        # stable projector retain burst coalescing while deriving owner-scoped
        # URLs; ephemeral per-request closures would defeat that sharing.
        with self._view_lock:
            for existing in self._views:
                if (
                    existing.name == name
                    and existing.project is project
                    and existing.project_kwargs == normalized_kwargs
                ):
                    return existing.value
            projected = project(self.value, **dict(normalized_kwargs))
            if not isinstance(projected, dict):
                raise TypeError("Snapshot projector must return an object")
            if len(self._views) < _MAX_SHARED_REPRESENTATIONS:
                self._views.append(_SharedSnapshotView(
                    name,
                    project,
                    normalized_kwargs,
                    projected,
                ))
            return projected


class ConversationSnapshotQuery:
    """Share one pre-read authority projection and isolate every participant."""

    def __init__(
        self,
        loader: Callable[..., dict[str, Any]],
        *,
        max_active_gathers: int = _DEFAULT_ACTIVE_GATHERS,
        gather_seconds: float = _DEFAULT_GATHER_SECONDS,
        wait_for_arrivals: Callable[[float], None] = time.sleep,
    ) -> None:
        self._loader = loader
        self._max_active_gathers = max_active_gathers
        self._coalescer = BurstReadCoalescer[
            _SnapshotQueryKey, _SharedSnapshotAuthority
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
        representation: str = "full",
        project_representation: (
            Callable[..., dict[str, Any]] | None
        ) = None,
        project_representation_kwargs: Mapping[str, Any] | None = None,
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
    ) -> dict[str, Any]:
        """Return a request-local envelope with a flight-shared nested view."""
        key = _SnapshotQueryKey(
            user_id=user_id,
            conversation_id=conversation_id,
            turn_limit=turn_limit,
            include_artifact_hint=bool(include_artifact_hint),
        )
        def load() -> dict[str, Any]:
            if include_artifact_hint:
                return self._loader(
                    conversation_id,
                    user_id,
                    turn_limit,
                    include_artifact_hint=True,
                )
            if turn_limit is None:
                return self._loader(conversation_id, user_id)
            return self._loader(conversation_id, user_id, turn_limit)

        authority = self._coalescer.run(
            key,
            lambda: _SharedSnapshotAuthority(
                load()
            ),
        )
        shared_view = authority.representation(
            representation,
            project_representation,
            project_representation_kwargs,
        )
        # API response construction and JSON serialization only read nested
        # authority values.  A shallow envelope isolates the sole request-local
        # field without multiplying a multi-megabyte projection in CPU/RAM.
        response = dict(shared_view)
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
