"""Bind computed v2 candidates into the existing request execution engine."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import secrets
from typing import Callable, ContextManager, Mapping
from types import MappingProxyType

from lib.llm_dispatch.ephemeral import (
    EphemeralSlotHandle,
    dispose_ephemeral_slots,
    prepare_ephemeral_slot,
    publish_ephemeral_slots,
)
from lib.llm_dispatch.slot import key_stats_namespace
from lib.provider.provider_headers import sanitise_extra_headers

from .credential_envelope import decode_credential_envelope
from .domain import ModelRoutingError, NativeModelSelection
from .health import RouteHealthRegistry
from .inference import candidate_inference_metadata
from .repository import OwnerBoundary, RepositoryPort, StoredAuthority
from .routing import (
    RouteCandidate,
    RoutePolicy,
    RouteSnapshotBuilder,
    RouteCandidateCompiler,
)


def decode_credential_secret(
    plaintext: str,
    *,
    kind: str,
) -> tuple[str, str, dict[str, str]]:
    """Decode an encrypted secret envelope, accepting raw API-key drafts."""
    value = str(plaintext or "")
    decoded = decode_credential_envelope(value)
    if decoded is not None:
        raw_headers = decoded.get("extra_headers") or {}
        if not isinstance(raw_headers, Mapping):
            raise ModelRoutingError(
                "credential secret extra_headers is invalid",
                kind="credential_secret_invalid",
            )
        clean_headers, header_error = sanitise_extra_headers(dict(raw_headers))
        if header_error:
            raise ModelRoutingError(
                header_error,
                kind="credential_secret_invalid",
                field="credential.extra_headers",
            )
        return (
            str(decoded.get("api_key") or ""),
            str(decoded.get("oauth") or ""),
            clean_headers,
        )
    if kind == "oauth":
        return "", value.removeprefix("oauth:"), {}
    return value, "", {}


@dataclass(slots=True)
class RoutedSlotGroup:
    pin_id: str
    handles: list[EphemeralSlotHandle]
    candidates: list[RouteCandidate]
    disposed: bool = False
    route_identity: Mapping | None = None

    @property
    def primary(self) -> EphemeralSlotHandle:
        return self.handles[0]


def candidate_route_identity(candidate: RouteCandidate, revision: int) -> dict:
    """Stable secret-free witness shared by planner and request materializer."""
    return {
        'revision': revision, 'providerId': candidate.provider_id,
        'providerAccessId': candidate.provider_access['provider_access_id'],
        'offeringId': candidate.offering['offering_id'],
        'deploymentId': candidate.deployment['deployment_id'],
        'connectionId': candidate.connection['connection_id'],
        'credentialId': candidate.credential['credential_id'],
    }


def candidate_key_stats_identity(candidate: RouteCandidate,
                                 boundary: OwnerBoundary) -> tuple[str, str, str]:
    """``(namespace, key name, model)`` the slot minted for *candidate* reports.

    Equal to that slot's ``key_stats_provider_id()``, ``key_stats_key_name()``
    and wire ``model`` (see ``_prepare_and_publish_group`` and
    ``prepare_ephemeral_slot``), so a planner reads the billing stops and
    overrides that dispatch admission reads for it.
    """
    return (key_stats_namespace(boundary.owner_user_id, candidate.provider_id),
            candidate.credential['credential_id'],
            candidate.deployment['wire_model_id'].strip())


def issued_route_identity(candidate: RouteCandidate, boundary: OwnerBoundary, revision: int) -> dict:
    """Secret-free snapshot binding a native transition to an admitted route."""
    return {
        **boundary.payload(), "revision": revision,
        "providerId": candidate.provider_id,
        "providerAccessId": candidate.provider_access["provider_access_id"],
        "offeringId": candidate.offering["offering_id"],
        "deploymentId": candidate.deployment["deployment_id"],
        "connectionId": candidate.connection["connection_id"],
        "credentialId": candidate.credential["credential_id"],
    }


def _select_route_candidates(
    authority: StoredAuthority,
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None,
    health: RouteHealthRegistry | None,
    max_candidates: int | None,
    expected_route_identity: Mapping | None,
) -> list[RouteCandidate]:
    """Plan one authority snapshot; release its indexes before secret materialization."""
    rejections: dict[str, int] = {}
    compiler = RouteCandidateCompiler(authority.document)
    primary_candidates = compiler.compile(
        selection, policy=policy, health=health,
        rejections=rejections, max_candidates=(None
            if expected_route_identity is not None else max_candidates))
    if expected_route_identity is not None:
        primary_candidates = [candidate for candidate in primary_candidates
            if candidate_route_identity(candidate, authority.revision) == dict(expected_route_identity)]
        if not primary_candidates:
            raise ModelRoutingError('planned route changed', kind='model_route_changed')
    if not primary_candidates and rejections.get('model_registration_incomplete'):
        raise ModelRoutingError(
            '模型资料待补全或待验证，请在模型设置中查看缺项 / Model registration is incomplete; inspect missing metadata in Model settings',
            kind='model_registration_incomplete')
    if not primary_candidates and rejections.get('model_identity_pending'):
        raise ModelRoutingError(
            '服务商模型尚未绑定已注册的官方模型 / Model identity is pending: no registered official Model is bound to this provider model',
            kind='model_identity_pending')
    if not primary_candidates:
        raise ModelRoutingError(
            "no authorized, healthy Deployment satisfies the request"
            + ("; rejected: " + ", ".join(
                f"{reason}={count}" for reason, count in sorted(rejections.items())
            ) if rejections else ""),
            kind="model_route_unavailable",
        )
    candidates = list(primary_candidates)
    # Provider-scoped pending identities cannot cross their service boundary.
    # Official models may degrade only after every candidate for the requested
    # identity, preserving the compiler's preferred-provider stability first.
    if (expected_route_identity is None and selection.model is not None
            and (max_candidates is None or len(candidates) < max_candidates)):
        fallbacks = compiler.compile_fallbacks(
            selection,
            policy=policy,
            health=health,
            original_provider_id=(selection.preferred_provider_id
                                  or primary_candidates[0].provider_id),
            max_candidates=(None if max_candidates is None else max_candidates - len(candidates)),
        )
        emitted = {
            (candidate.deployment["deployment_id"],
             candidate.credential["credential_id"])
            for candidate in candidates
        }
        for candidate in fallbacks:
            identity = (
                candidate.deployment["deployment_id"],
                candidate.credential["credential_id"],
            )
            if identity in emitted:
                continue
            emitted.add(identity)
            candidates.append(candidate)
    if max_candidates is not None:
        candidates = candidates[:max_candidates]

    if expected_route_identity is not None:
        # Summary preflight is secret-free. Reject changed/revoked witnesses
        # before resolving plaintext, rather than silently minting another route.
        if (len(candidates) != 1 or candidate_route_identity(candidates[0], authority.revision)
                != dict(expected_route_identity)):
            raise ModelRoutingError('planned route changed', kind='model_route_changed')
    return candidates


def mint_routed_slot_group(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    health: RouteHealthRegistry | None = None,
    owner_tag: str = "",
    max_candidates: int | None = None,

    expected_route_identity: Mapping | None = None,
    observe_stage: Callable[[str], ContextManager] | None = None,
) -> RoutedSlotGroup:
    """Plan one owner snapshot, then mint and publish its whole route group.

    ``observe_stage(name)`` optionally times the ``route`` (authority read
    and candidate planning) and ``mint`` (credential resolution, slot
    preparation and the single publication) stages for the caller.
    """
    if max_candidates is not None and (isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int) or max_candidates < 1):
        raise ModelRoutingError(
            "max_candidates must be a positive integer or None",
            kind="model_route_resource_budget_invalid",
            field="max_candidates",
        )
    stage = observe_stage or (lambda _name: nullcontext())
    with stage("route"):
        authority = repository.get(boundary)
        candidates = _select_route_candidates(
            authority, selection, policy=policy, health=health,
            max_candidates=max_candidates,
            expected_route_identity=expected_route_identity,
        )
    pin_id = f"model-route:{boundary.owner_user_id}:{secrets.token_hex(8)}"
    handles: list[EphemeralSlotHandle] = []
    runnable_candidates: list[RouteCandidate] = []
    initialization_errors: list[ValueError] = []
    # One admitted authority/owner snapshot may expand a credential into many
    # model/deployment slots. Resolve/decode it once within this request only;
    # a global plaintext cache would outlive revocation and owner boundaries.
    resolved_secrets: dict[
        tuple[str, str], tuple[str, str, dict[str, str]] | ModelRoutingError
    ] = {}
    # Endpoint checks (egress DNS validation, reachability probe) run once per
    # distinct base URL of this group, never across requests.
    endpoint_verdicts: dict = {}
    try:
        with stage("mint"):
            _prepare_and_publish_group(
                repository, boundary, selection, candidates, pin_id=pin_id,
                owner_tag=owner_tag, handles=handles,
                runnable_candidates=runnable_candidates,
                initialization_errors=initialization_errors,
                resolved_secrets=resolved_secrets,
                endpoint_verdicts=endpoint_verdicts)
        witness = MappingProxyType(issued_route_identity(
            runnable_candidates[0], boundary, authority.revision))
        return RoutedSlotGroup(pin_id, handles, runnable_candidates, route_identity=witness)
    except BaseException as creation_error:
        try:
            dispose_routed_slot_group(
                RoutedSlotGroup(pin_id, handles, runnable_candidates))
        except BaseException as cleanup_error:
            # Preserve construction/cancellation as the primary failure while
            # retaining cleanup evidence, after attempting every sibling.
            raise creation_error from cleanup_error
        raise


def _prepare_and_publish_group(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    selection: NativeModelSelection,
    candidates: list[RouteCandidate],
    *,
    pin_id: str,
    owner_tag: str,
    handles: list[EphemeralSlotHandle],
    runnable_candidates: list[RouteCandidate],
    initialization_errors: list,
    resolved_secrets: dict,
    endpoint_verdicts: dict,
) -> None:
    """Prepare one slot per runnable candidate, then publish them together.

    Appends into the caller's lists so its cleanup owns every prepared
    handle even when this raises. Route metadata and rank are final before
    publication; one dispatcher-lock acquisition publishes the group.
    """
    for candidate in candidates:
        credential = candidate.credential
        try:
            secret_key = (credential["secret_reference"], credential["kind"])
            decoded = resolved_secrets.get(secret_key)
            if decoded is None:
                try:
                    secret = repository.resolve_secret(
                        boundary, credential["secret_reference"]
                    ) if credential["secret_reference"] else ""
                    decoded = decode_credential_secret(secret, kind=credential["kind"])
                except ModelRoutingError as exc:
                    if exc.kind not in {
                        'credential_secret_missing', 'credential_secret_invalid',
                    }:
                        raise
                    decoded = exc
                resolved_secrets[secret_key] = decoded
            if isinstance(decoded, ModelRoutingError):
                raise decoded
            api_key, oauth, secret_headers = decoded
        except ModelRoutingError as exc:
            if exc.kind not in {
                'credential_secret_missing', 'credential_secret_invalid',
            }:
                raise
            # A known bad credential cannot mask a later authorized path.
            # Storage availability and owner-boundary failures still abort.
            initialization_errors.append(exc)
            continue
        logical_model = (
            candidate.model["model_id"] if candidate.model is not None
            else candidate.offering["pending_model_id"]
        )
        try:
            handle = prepare_ephemeral_slot(
                base_url=candidate.connection["base_url"],
                api_key=api_key,
                model_id=logical_model,
                **candidate_inference_metadata(candidate),
                wire_model_id=candidate.deployment["wire_model_id"],
                owner=owner_tag or f"owner:{boundary.owner_user_id}",
                extra_headers={
                    **(candidate.connection.get("extra_headers") or {}),
                    **(candidate.deployment.get("extra_headers") or {}),
                    **secret_headers,
                },
                capabilities=set(candidate.offering["capabilities"]),
                protocol=candidate.connection["protocol"],
                oauth=oauth,
                adapter=dict(candidate.connection.get("adapter") or {}),
                routing_owner_user_id=boundary.owner_user_id,
                routing_tenant_id=boundary.tenant_id,
                provider_pin_id=pin_id,
                endpoint_verdicts=endpoint_verdicts,
            )
        except ValueError as exc:
            # One stale/unreachable Connection must not mask later
            # authorized Deployments in the same bounded route group.
            # Nothing is published yet: preparation validates endpoint and
            # model identity before anything is registered.
            initialization_errors.append(exc)
            continue
        handles.append(handle)
        runnable_candidates.append(candidate)

    if not handles:
        if initialization_errors and isinstance(initialization_errors[0], ModelRoutingError):
            raise initialization_errors[0]
        detail = str(initialization_errors[0]) if initialization_errors else (
            'no candidate could be initialized')
        raise ModelRoutingError(
            detail, kind='model_route_endpoint_invalid')

    for rank, (handle, candidate) in enumerate(
            zip(handles, runnable_candidates)):
        credential = candidate.credential
        slot = handle.slot
        slot.routing_provider_id = candidate.provider_id
        slot.route_offering_id = candidate.offering["offering_id"]
        slot.route_deployment_id = candidate.deployment["deployment_id"]
        slot.route_connection_id = candidate.connection["connection_id"]
        slot.route_credential_id = credential["credential_id"]
        from .serving_locality import candidate_is_local_inference
        slot.local_inference = candidate_is_local_inference(candidate)
        slot.max_output_tokens = int(
            candidate.deployment.get("max_output_tokens") or 0)
        snapshot_builder = RouteSnapshotBuilder(selection)
        if initialization_errors:
            snapshot_builder.record_degradation(
                f'{len(initialization_errors)} route candidate(s) could '
                'not be initialized')
        # A candidate is not an attempted route. Dispatch builds ancestry
        # from actual invocations, including retries within this group.
        snapshot_builder.record_transition(
            source=None, target=candidate,
            reason="computed_v2_route", kind="initial")
        slot.route_snapshot = snapshot_builder.finalize(candidate)
        # A 1 ms score offset loses to the scorer's random jitter, cost,
        # and inflight penalties. Keep the compiler order explicit.
        slot.route_rank = rank
        pricing = candidate.offering.get("actual_pricing") or {}
        slot.cost_per_1k_tokens = (
            float(pricing.get("input") or 0.0)
            + float(pricing.get("output") or 0.0)
        ) / 2000.0
    publish_ephemeral_slots(handles)


def dispose_routed_slot_group(group: RoutedSlotGroup | None) -> bool:
    if group is None or group.disposed:
        return False
    try:
        # One dispatcher-lock acquisition withdraws every sibling by identity;
        # every sibling's exemption release is attempted even if one fails.
        disposed = any(dispose_ephemeral_slots(group.handles))
    finally:
        # Request-scoped plaintext must not survive in a retained handle,
        # including when detachment failed and the group stays retryable.
        for handle in group.handles:
            handle.slot.api_key = ''
            handle.slot.extra_headers = {}
    group.disposed = True
    return disposed


__all__ = [
    "RoutedSlotGroup",
    "decode_credential_secret",
    "dispose_routed_slot_group",
    "mint_routed_slot_group",
]
