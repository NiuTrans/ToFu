"""Bounded, secret-free route diagnostics and historical route projection.

Entry points: RouteSnapshotBuilder and legacy_route_snapshot. This owner formats
candidate witnesses; it never chooses a route, resolves credentials or executes
an attempt. Actual attempt ancestry remains in llm_dispatch._route_trace.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import time
from typing import Any

from .candidate_compiler import RouteCandidate
from .domain import MAX_ROUTE_SNAPSHOT_BYTES, ModelRoutingError, NativeModelSelection


def _bounded_text(value: object, maximum: int = 256) -> str:
    return str(value or "")[:maximum]


@dataclass(slots=True)
class RouteSnapshotBuilder:
    selection: NativeModelSelection
    transitions: list[dict[str, Any]] = field(default_factory=list)
    degradation_reasons: list[str] = field(default_factory=list)

    def record_transition(
        self,
        *,
        source: RouteCandidate | None,
        target: RouteCandidate,
        reason: str,
        kind: str,
    ) -> None:
        if len(self.transitions) >= 32:
            return
        self.transitions.append({
            "kind": kind if kind in {"provider_failover", "model_fallback", "initial"} else "provider_failover",
            "from": None if source is None else {
                "provider_id": source.provider_id,
                "offering_id": source.offering["offering_id"],
                "deployment_id": source.deployment["deployment_id"],
            },
            "to": {
                "provider_id": target.provider_id,
                "offering_id": target.offering["offering_id"],
                "deployment_id": target.deployment["deployment_id"],
            },
            "reason": _bounded_text(reason),
        })

    def record_degradation(self, reason: str) -> None:
        if len(self.degradation_reasons) < 16:
            self.degradation_reasons.append(_bounded_text(reason))

    def finalize(self, candidate: RouteCandidate) -> dict[str, Any]:
        selected_model: dict[str, str] | None = None
        provider_scoped: dict[str, str] | None = None
        if self.selection.model is not None:
            selected_model = self.selection.model.public_dict()
        if self.selection.provider_offering is not None:
            provider_scoped = self.selection.provider_offering.public_dict()
        actual_model = (
            {
                "creator_id": candidate.model["creator_id"],
                "model_id": candidate.model["model_id"],
            }
            if candidate.model is not None else None
        )
        snapshot: dict[str, Any] = {
            "contract_version": "tofu.route-snapshot/v2",
            "selected_model": selected_model,
            "provider_scoped_selection": provider_scoped,
            "preferred_provider_id": self.selection.preferred_provider_id,
            "actual_model": actual_model,
            "provider_id": candidate.provider_id,
            "offering_id": candidate.offering["offering_id"],
            "deployment_id": candidate.deployment["deployment_id"],
            "connection_id": candidate.connection["connection_id"],
            "credential": {
                "credential_id": candidate.credential["credential_id"],
                "kind": candidate.credential["kind"],
                "key_hint": candidate.credential.get("key_hint", ""),
            },
            "wire_model_id": candidate.deployment["wire_model_id"],
            "transitions": copy.deepcopy(self.transitions),
            "degradation_reasons": list(self.degradation_reasons),
            "recorded_at": time.time(),
        }
        while len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > MAX_ROUTE_SNAPSHOT_BYTES:
            if snapshot["transitions"]:
                snapshot["transitions"].pop(0)
            elif snapshot["degradation_reasons"]:
                snapshot["degradation_reasons"].pop()
            else:
                raise ModelRoutingError(
                    "RouteSnapshot exceeds its resource budget",
                    kind="route_snapshot_resource_budget_exceeded",
                )
        return snapshot


def legacy_route_snapshot(
    *, model_id: str = "", provider_id: str = "", route_id: str = "",
) -> dict[str, Any]:
    """Read projection for historical turns; it never mutates stored data."""
    return {
        "contract_version": "tofu.route-snapshot/v2",
        "legacy": True,
        "selected_model": None,
        "provider_scoped_selection": None,
        "preferred_provider_id": _bounded_text(provider_id),
        "actual_model": (
            {"creator_id": "legacy", "model_id": _bounded_text(model_id)}
            if model_id else None
        ),
        "provider_id": _bounded_text(provider_id),
        "offering_id": "",
        "deployment_id": _bounded_text(route_id),
        "connection_id": "",
        "credential": None,
        "wire_model_id": _bounded_text(model_id),
        "transitions": [],
        "degradation_reasons": ["legacy_turn_projection"],
        "recorded_at": 0.0,
    }
