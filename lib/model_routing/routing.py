"""Pure v2 candidate compilation, compatibility resolution, and snapshots."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .domain import (
    MAX_ROUTE_SNAPSHOT_BYTES,
    ModelRef,
    ModelRoutingError,
    NativeModelSelection,
    ProviderOfferingRef,
    normalize_document,
)
from .health import RouteHealthRegistry


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    required_capabilities: frozenset[str] = frozenset({"text"})
    required_context: int = 1
    required_protocols: frozenset[str] = frozenset()
    max_input_price: float | None = None
    max_output_price: float | None = None
    price_currency: str = "USD"
    cache_affinity_connection_id: str = ""


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    model: dict[str, Any] | None
    provider: dict[str, Any]
    provider_access: dict[str, Any]
    offering: dict[str, Any]
    deployment: dict[str, Any]
    connection: dict[str, Any]
    credential: dict[str, Any]
    score: tuple[Any, ...]
    selection_reasons: tuple[str, ...] = ()

    @property
    def provider_id(self) -> str:
        return str(self.provider["provider_id"])


def _indexes(document: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_document(document)
    deployments_by_offering: dict[str, list[dict[str, Any]]] = {
        str(offering["offering_id"]): []
        for offering in normalized["offerings"]
    }
    offerings_by_model: dict[ModelRef, list[dict[str, Any]]] = {}
    for offering in normalized["offerings"]:
        model = offering.get("model")
        if isinstance(model, Mapping):
            offerings_by_model.setdefault(
                ModelRef.from_value(model), []).append(offering)
    for deployment in normalized["deployments"]:
        deployments_by_offering[deployment["offering_id"]].append(deployment)
    credentials_by_access: dict[str, list[dict[str, Any]]] = {
        str(access["provider_access_id"]): []
        for access in normalized["provider_accesses"]
    }
    for credential in normalized["credentials"]:
        credentials_by_access[credential["provider_access_id"]].append(credential)
    return {
        "document": normalized,
        "models": {
            ModelRef(row["creator_id"], row["model_id"]): row
            for row in normalized["models"]
        },
        "providers": {row["provider_id"]: row for row in normalized["providers"]},
        "accesses": {
            row["provider_access_id"]: row
            for row in normalized["provider_accesses"]
        },
        "connections": {
            row["connection_id"]: row for row in normalized["connections"]
        },
        "offerings": {row["offering_id"]: row for row in normalized["offerings"]},
        "offerings_by_model": offerings_by_model,
        "deployments_by_offering": deployments_by_offering,
        "credentials_by_access": credentials_by_access,
    }


def resolve_compatible_model(
    document: Mapping[str, Any],
    model_id: str,
    *,
    creator_id: str = "",
    preferred_provider_id: str = "",
) -> NativeModelSelection:
    """Resolve an OpenAI/Anthropic string plus Tofu creator/provider hints."""
    text = str(model_id or "").strip()
    if not text:
        raise ModelRoutingError("model is required", field="model")
    if "@" in text:
        raise ModelRoutingError(
            "model@provider selectors were removed; use Tofu routing fields",
            kind="legacy_model_selector_removed",
            field="model",
        )
    idx = _indexes(document)
    preferred = str(preferred_provider_id or "").strip()
    creator = str(creator_id or "").strip()

    if preferred:
        access_ids = {
            access_id for access_id, access in idx["accesses"].items()
            if access["provider_id"] == preferred
        }
        wire_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for deployment in idx["document"]["deployments"]:
            offering = idx["offerings"][deployment["offering_id"]]
            if (
                deployment["wire_model_id"] == text
                and offering["provider_access_id"] in access_ids
            ):
                wire_matches.append((offering, deployment))
        if len(wire_matches) > 1:
            raise ModelRoutingError(
                f"wire model {text!r} is ambiguous within provider {preferred!r}",
                kind="model_selector_ambiguous",
                candidates=[
                    {"provider_id": preferred, "offering_id": offering["offering_id"]}
                    for offering, _deployment in wire_matches
                ],
            )
        if len(wire_matches) == 1:
            offering, _deployment = wire_matches[0]
            if offering["identity_state"] == "pending_identity":
                return NativeModelSelection(
                    None,
                    ProviderOfferingRef(preferred, offering["offering_id"]),
                    preferred,
                )
            ref = ModelRef.from_value(offering["model"])
            return NativeModelSelection(ref, None, preferred)

    matches = [
        ref for ref in idx["models"]
        if ref.model_id == text and (not creator or ref.creator_id == creator)
    ]
    if len(matches) == 1:
        return NativeModelSelection(matches[0], None, preferred)
    candidates = [ref.public_dict() for ref in sorted(matches)]
    if not matches:
        raise ModelRoutingError(
            f"model {text!r} is not registered",
            kind="model_not_found",
            field="model",
        )
    raise ModelRoutingError(
        f"model {text!r} is ambiguous; send tofu.creator_id",
        kind="model_selector_ambiguous",
        field="model",
        candidates=candidates,
    )


def _pricing_allowed(offering: Mapping[str, Any], policy: RoutePolicy) -> bool:
    if policy.max_input_price is None and policy.max_output_price is None:
        return True
    pricing = offering.get("actual_pricing")
    if not isinstance(pricing, Mapping):
        return False
    if str(pricing.get("currency") or "USD").upper() != policy.price_currency.upper():
        return False
    if (
        policy.max_input_price is not None
        and float(pricing.get("input") or 0.0) > policy.max_input_price
    ):
        return False
    if (
        policy.max_output_price is not None
        and float(pricing.get("output") or 0.0) > policy.max_output_price
    ):
        return False
    return True


def _credential_authorized(
    credential: Mapping[str, Any],
    *,
    connection_id: str,
    model_ref: ModelRef | None,
) -> bool:
    authorization = credential["authorization"]
    if connection_id not in authorization["connection_ids"]:
        return False
    if model_ref is None:
        return True
    return model_ref.public_dict() in authorization["models"]


class RouteCandidateCompiler:
    """Request-scoped compiler that validates/indexes one authority once.

    Capability catalogs and fallback scans evaluate multiple selections from
    the same revision. Re-normalizing and deep-copying the complete aggregate
    for every Offering made that bounded query quadratic in aggregate size.
    This owner retains only one request-local normalized document and indexes;
    it is never a cross-owner or cross-revision cache.
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._indexes = _indexes(document)

    def compile(
        self,
        selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
        health: RouteHealthRegistry | None = None,
        excluded_deployment_ids: Sequence[str] = (),
        excluded_connection_ids: Sequence[str] = (),
        excluded_credential_ids: Sequence[str] = (),
    ) -> list[RouteCandidate]:
        return _compile_candidates(
            self._indexes,
            selection,
            policy=policy,
            health=health,
            excluded_deployment_ids=excluded_deployment_ids,
            excluded_connection_ids=excluded_connection_ids,
            excluded_credential_ids=excluded_credential_ids,
        )


def _compile_candidates(
    idx: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
) -> list[RouteCandidate]:
    policy = policy or RoutePolicy()
    excluded_deployments = set(excluded_deployment_ids)
    excluded_connections = set(excluded_connection_ids)
    excluded_credentials = set(excluded_credential_ids)
    preferred_provider_id = selection.preferred_provider_id

    target_offerings: list[dict[str, Any]] = []
    target_model: ModelRef | None = selection.model
    if selection.provider_offering is not None:
        scoped = selection.provider_offering
        offering = idx["offerings"].get(scoped.offering_id)
        if offering is None:
            raise ModelRoutingError(
                "provider-scoped offering does not exist", kind="offering_not_found")
        access = idx["accesses"][offering["provider_access_id"]]
        if access["provider_id"] != scoped.provider_id:
            raise ModelRoutingError(
                "offering does not belong to the selected provider",
                kind="provider_scope_violation",
            )
        target_offerings = [offering]
        if offering["identity_state"] == "confirmed":
            target_model = ModelRef.from_value(offering["model"])
    elif selection.model is not None:
        if selection.model not in idx["models"]:
            raise ModelRoutingError(
                "official model is not registered", kind="model_not_found")
        target_offerings = idx["offerings_by_model"].get(selection.model, [])
    else:
        raise ModelRoutingError("model selection is empty")

    candidates: list[RouteCandidate] = []
    for offering in target_offerings:
        if not offering["enabled"] or offering.get("stale"):
            continue
        if not policy.required_capabilities.issubset(set(offering["capabilities"])):
            continue
        if policy.required_context > offering["context_window"]:
            continue
        if not _pricing_allowed(offering, policy):
            continue
        access = idx["accesses"][offering["provider_access_id"]]
        provider = idx["providers"][access["provider_id"]]
        if not access["enabled"]:
            continue
        if selection.provider_offering is not None and provider["provider_id"] != selection.provider_offering.provider_id:
            continue
        for deployment in idx["deployments_by_offering"][offering["offering_id"]]:
            if (
                not deployment["enabled"]
                or deployment["probe_status"] != "passed"
                or deployment["deployment_id"] in excluded_deployments
            ):
                continue
            # Pending identities are reachable only through an explicit
            # provider+offering selection, never automatic official routing.
            if (
                offering["identity_state"] == "pending_identity"
                and selection.provider_offering is None
            ):
                continue
            connection = idx["connections"][deployment["connection_id"]]
            if not connection["enabled"] or connection["connection_id"] in excluded_connections:
                continue
            if policy.required_protocols and connection["protocol"] not in policy.required_protocols:
                continue
            for credential in idx["credentials_by_access"][access["provider_access_id"]]:
                if not credential["enabled"] or credential["credential_id"] in excluded_credentials:
                    continue
                if not _credential_authorized(
                    credential,
                    connection_id=connection["connection_id"],
                    model_ref=target_model if offering["identity_state"] == "confirmed" else None,
                ):
                    continue
                partial = RouteCandidate(
                    model=(idx["models"].get(target_model) if target_model is not None else None),
                    provider=provider,
                    provider_access=access,
                    offering=offering,
                    deployment=deployment,
                    connection=connection,
                    credential=credential,
                    score=(),
                )
                unavailable, health_penalty, health_reasons = (
                    health.candidate_state(partial) if health is not None
                    else (False, 0.0, [])
                )
                if unavailable:
                    continue
                pricing = offering.get("actual_pricing") or {}
                price_score = float(pricing.get("input") or 0.0) + float(pricing.get("output") or 0.0)
                preferred_rank = (
                    0 if preferred_provider_id and provider["provider_id"] == preferred_provider_id
                    else 1 if preferred_provider_id else 0
                )
                cache_rank = (
                    0 if policy.cache_affinity_connection_id == connection["connection_id"]
                    else 1
                )
                score = (
                    preferred_rank,
                    int(offering.get("priority") or 100),
                    health_penalty,
                    cache_rank,
                    int(connection.get("priority") or 100),
                    int(deployment.get("priority") or 100),
                    float(connection.get("latency_ms") or offering.get("latency_ms") or 0.0),
                    price_score,
                    provider["provider_id"],
                    deployment["deployment_id"],
                    credential["credential_id"],
                )
                reasons = [
                    "preferred_provider" if preferred_rank == 0 and preferred_provider_id else "eligible_provider",
                    "cache_affinity" if cache_rank == 0 and policy.cache_affinity_connection_id else "score_ranked",
                    *health_reasons,
                ]
                candidates.append(RouteCandidate(
                    model=partial.model,
                    provider=provider,
                    provider_access=access,
                    offering=offering,
                    deployment=deployment,
                    connection=connection,
                    credential=credential,
                    score=score,
                    selection_reasons=tuple(reasons),
                ))
    candidates.sort(key=lambda candidate: candidate.score)
    return candidates


def compile_candidates(
    document: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
) -> list[RouteCandidate]:
    """Compile bounded runtime candidates; no configured Route entity exists."""
    return RouteCandidateCompiler(document).compile(
        selection,
        policy=policy,
        health=health,
        excluded_deployment_ids=excluded_deployment_ids,
        excluded_connection_ids=excluded_connection_ids,
        excluded_credential_ids=excluded_credential_ids,
    )


def compile_model_fallback_candidates(
    document: Mapping[str, Any],
    failed_selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    original_provider_id: str = "",
    excluded_models: Sequence[ModelRef] = (),
) -> list[RouteCandidate]:
    """Choose quality-ranked model degradation after one model fully fails."""
    policy = policy or RoutePolicy()
    idx = _indexes(document)
    excluded = set(excluded_models)
    if failed_selection.model is not None:
        excluded.add(failed_selection.model)
    candidates: list[RouteCandidate] = []
    for ref, model in idx["models"].items():
        if ref in excluded:
            continue
        if not policy.required_capabilities.issubset(set(model["capabilities"])):
            continue
        if policy.required_context > model["context_window"]:
            continue
        selection = NativeModelSelection(
            ref,
            None,
            original_provider_id or failed_selection.preferred_provider_id,
        )
        candidates.extend(_compile_candidates(
            idx, selection, policy=policy, health=health))
    candidates.sort(key=lambda candidate: (
        0 if original_provider_id and candidate.provider_id == original_provider_id else 1,
        -float((candidate.model or {}).get("quality_rank") or 0.0),
        candidate.score,
    ))
    return candidates


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


__all__ = [
    "RouteCandidate",
    "RouteCandidateCompiler",
    "RoutePolicy",
    "RouteSnapshotBuilder",
    "compile_candidates",
    "compile_model_fallback_candidates",
    "legacy_route_snapshot",
    "resolve_compatible_model",
]
