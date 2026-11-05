"""Request-local route eligibility, credential grants and stable ranking.

Entry points: RouteCandidateCompiler and standalone compile helpers. Selection
indexes are validated once per compiler; primary, deployment-witness and fallback
queries share them. Health is explicit; repository and secret IO stay outside.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import heapq
from typing import Any

from .domain import ModelRef, ModelRoutingError, NativeModelSelection
from .health import RouteHealthRegistry
from .selection_resolution import _indexes, _resolve_model_ref


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


def _credential_rejection(
    credential: Mapping[str, Any],
    *,
    connection_id: str,
    model_ref: ModelRef | None,
    wire_model_id: str | None = None,
) -> str | None:
    authorization = credential["authorization"]
    if connection_id not in authorization["connection_ids"]:
        return "credential_connection_denied"
    if model_ref is None:
        return None
    for grant in authorization["models"]:
        if grant["creator_id"] != model_ref.creator_id:
            continue
        if grant["model_id"] != model_ref.model_id:
            continue
        if wire_model_id and wire_model_id in grant.get("excluded_wire_model_ids", ()):
            return "credential_wire_denied"
        return None
    return "credential_model_denied"


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

    def resolve_model_id(self, model_id: str) -> ModelRef:
        """Resolve one exact logical ID using this request's validated authority.

        A string selector cannot choose between creators. Keep that ambiguity
        explicit rather than interpreting display names or provider wire IDs.
        """
        resolved=None
        for reference in self._indexes['models']:
            if reference.model_id!=model_id:
                continue
            if resolved is not None:
                raise ModelRoutingError('logical model ID has multiple creator identities',
                    kind='model_identity_ambiguous')
            resolved=reference
        if resolved is None:
            raise ModelRoutingError('logical model ID is not registered',kind='model_not_found')
        return resolved

    def compile_deployments(
        self,
        selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
    ) -> list[RouteCandidate]:
        """Secret-free eligibility projection, one authorization witness per deployment.

        Controls need to know whether a deployment is accessible, not rank all
        of its credential alternatives. Stop after the first authorized grant
        to avoid materializing a deployment × credential product. Execution
        continues to use ``compile`` with full health and credential scoring.
        """
        return _compile_candidates(
            self._indexes, selection, policy=policy,
            one_authorization_witness=True,
        )

    def compile(
        self,
        selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
        health: RouteHealthRegistry | None = None,
        excluded_deployment_ids: Sequence[str] = (),
        excluded_connection_ids: Sequence[str] = (),
        excluded_credential_ids: Sequence[str] = (),
        rejections: dict[str, int] | None = None,
        max_candidates: int | None = None,
    ) -> list[RouteCandidate]:
        return _compile_candidates(
            self._indexes,
            selection,
            policy=policy,
            health=health,
            excluded_deployment_ids=excluded_deployment_ids,
            excluded_connection_ids=excluded_connection_ids,
            excluded_credential_ids=excluded_credential_ids,
            rejections=rejections, max_candidates=max_candidates,
        )


    def compile_fallbacks(
        self,
        failed_selection: NativeModelSelection,
        *,
        policy: RoutePolicy | None = None,
        health: RouteHealthRegistry | None = None,
        original_provider_id: str = "",
        excluded_models: Sequence[ModelRef] = (),
        max_candidates: int | None = None,
    ) -> list[RouteCandidate]:
        """Choose quality-ranked model degradation after one model fully fails."""
        policy = policy or RoutePolicy()
        idx = self._indexes
        excluded = set(excluded_models)
        if failed_selection.model is not None:
            excluded.add(_resolve_model_ref(idx, failed_selection.model) or failed_selection.model)
        def eligible_candidates():
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
                yield from _iter_candidates(idx, selection, policy=policy, health=health)
        return _rank_candidates(eligible_candidates(), max_candidates=max_candidates, key=lambda candidate: (
            0 if original_provider_id and candidate.provider_id == original_provider_id else 1,
            -float((candidate.model or {}).get("quality_rank") or 0.0),
            candidate.score,
        ))


def _compile_candidates(
    idx: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
    rejections: dict[str, int] | None = None,
    one_authorization_witness: bool = False,
    max_candidates: int | None = None,
) -> list[RouteCandidate]:
    return _rank_candidates(_iter_candidates(
        idx, selection, policy=policy, health=health,
        excluded_deployment_ids=excluded_deployment_ids,
        excluded_connection_ids=excluded_connection_ids,
        excluded_credential_ids=excluded_credential_ids,
        rejections=rejections, one_authorization_witness=one_authorization_witness,
    ), max_candidates=max_candidates, key=lambda candidate: candidate.score)


def _rank_candidates(candidates: Iterable[RouteCandidate], *, max_candidates, key):
    if max_candidates is None:
        return sorted(candidates, key=key)
    if (isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
            or max_candidates < 1):
        raise ModelRoutingError('candidate limit must be a positive integer',
                                kind='model_route_resource_budget_invalid',
                                field='max_candidates')
    # nsmallest is stable on equal keys, matching sorted(... )[:limit], while
    # retaining only the ranked prefix during the deployment × credential scan.
    return heapq.nsmallest(max_candidates, candidates, key=key)


def _iter_candidates(
    idx: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
    rejections: dict[str, int] | None = None,
    one_authorization_witness: bool = False,
) -> Iterator[RouteCandidate]:
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
        resolved = _resolve_model_ref(idx, selection.model)
        if resolved is None:
            raise ModelRoutingError(
                "official model is not registered", kind="model_not_found")
        target_model = resolved
        target_offerings = idx["offerings_by_model"].get(resolved, [])
    else:
        raise ModelRoutingError("model selection is empty")

    def reject(reason: str) -> None:
        # Fixed reason codes only: no credential, endpoint or caller content.
        if rejections is not None:
            rejections[reason] = rejections.get(reason, 0) + 1

    if not target_offerings:
        reject("offering_missing")
    for offering in target_offerings:
        from .registration import model_registration_ready
        bound_model = idx['models'].get(ModelRef.from_value(offering['model'])) if offering.get('model') else None
        if bound_model is None:
            reject('model_identity_pending')
            continue
        if bound_model is not None and not model_registration_ready(bound_model):
            reject('model_registration_incomplete')
            continue
        if not offering["enabled"]:
            reject("offering_disabled")
            continue
        if offering.get("stale"):
            reject("offering_stale")
            continue
        if not policy.required_capabilities.issubset(set(offering["capabilities"])):
            reject("capabilities_missing")
            continue
        if policy.required_context > offering["context_window"]:
            reject("context_insufficient")
            continue
        if not _pricing_allowed(offering, policy):
            reject("pricing_policy_denied")
            continue
        access = idx["accesses"][offering["provider_access_id"]]
        provider = idx["providers"][access["provider_id"]]
        if not access["enabled"]:
            reject("provider_access_disabled")
            continue
        if selection.provider_offering is not None and provider["provider_id"] != selection.provider_offering.provider_id:
            continue
        if not idx["credentials_by_access"][access["provider_access_id"]]:
            reject("credential_missing")
        for deployment in idx["deployments_by_offering"][offering["offering_id"]]:
            if deployment["deployment_id"] in excluded_deployments:
                reject("deployment_excluded")
                continue
            # Pending identities are reachable only through an explicit
            # provider+offering selection, never automatic official routing.
            if (
                offering["identity_state"] == "pending_identity"
                and selection.provider_offering is None
            ):
                continue
            connection = idx["connections"][deployment["connection_id"]]
            if deployment.get('registration_issues'):
                reject('model_registration_incomplete')
                continue
            if not connection["enabled"]:
                reject("connection_disabled")
                continue
            if connection["connection_id"] in excluded_connections:
                reject("connection_excluded")
                continue
            if policy.required_protocols and connection["protocol"] not in policy.required_protocols:
                reject("protocol_mismatch")
                continue
            for credential in idx["credentials_by_access"][access["provider_access_id"]]:
                if not credential["enabled"]:
                    reject("credential_disabled")
                    continue
                if credential["credential_id"] in excluded_credentials:
                    reject("credential_excluded")
                    continue
                credential_rejection = _credential_rejection(
                    credential,
                    connection_id=connection["connection_id"],
                    model_ref=target_model if offering["identity_state"] == "confirmed" else None,
                    wire_model_id=deployment["wire_model_id"],
                )
                if credential_rejection:
                    reject(credential_rejection)
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
                    reject("health_unavailable")
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
                    int(offering.get("priority", 100)),
                    health_penalty,
                    cache_rank,
                    int(connection.get("priority", 100)),
                    int(deployment.get("priority", 100)),
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
                yield RouteCandidate(
                    model=partial.model,
                    provider=provider,
                    provider_access=access,
                    offering=offering,
                    deployment=deployment,
                    connection=connection,
                    credential=credential,
                    score=score,
                    selection_reasons=tuple(reasons),
                )

                if one_authorization_witness:
                    break


def compile_candidates(
    document: Mapping[str, Any],
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    excluded_deployment_ids: Sequence[str] = (),
    excluded_connection_ids: Sequence[str] = (),
    excluded_credential_ids: Sequence[str] = (),
    rejections: dict[str, int] | None = None,
    max_candidates: int | None = None,
) -> list[RouteCandidate]:
    """Compile bounded runtime candidates; no configured Route entity exists."""
    return RouteCandidateCompiler(document).compile(
        selection,
        policy=policy,
        health=health,
        excluded_deployment_ids=excluded_deployment_ids,
        excluded_connection_ids=excluded_connection_ids,
        excluded_credential_ids=excluded_credential_ids,
        rejections=rejections, max_candidates=max_candidates,
    )


def compile_model_fallback_candidates(
    document: Mapping[str, Any],
    failed_selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    original_provider_id: str = "",
    excluded_models: Sequence[ModelRef] = (),
    max_candidates: int | None = None,
) -> list[RouteCandidate]:
    """Choose quality-ranked model degradation after one model fully fails."""
    return RouteCandidateCompiler(document).compile_fallbacks(
        failed_selection, policy=policy, health=health,
        original_provider_id=original_provider_id, excluded_models=excluded_models,
        max_candidates=max_candidates,
    )
