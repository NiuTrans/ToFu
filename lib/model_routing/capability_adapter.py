"""Owner-scoped routing adapter for non-chat model capabilities.

This module is the single bridge used by image, speech, embedding, and future
non-chat surfaces that need either a public list of runnable Offerings or a
request-scoped dispatcher slot group.  It reads only model-routing v2 and
keeps owner identity explicit at the repository boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .dispatch_adapter import (
    MAX_REQUEST_ROUTE_SLOTS,
    RoutedSlotGroup,
    mint_routed_slot_group,
)
from .domain import (
    ModelRef,
    ModelRoutingError,
    NativeModelSelection,
    ProviderOfferingRef,
)
from .repository import OwnerBoundary, RepositoryPort
from .routing import (
    RouteCandidateCompiler,
    RoutePolicy,
    resolve_compatible_model,
)


# ``local`` is the managed-local OpenAI-compatible transport identity in the
# v2 contract. Dedicated OpenAI endpoint families (embeddings, speech,
# transcription) accept both. Chat additionally accepts the Responses wire.
OPENAI_COMPATIBLE_PROTOCOLS = frozenset({"openai", "local"})
OPENAI_CHAT_COMPATIBLE_PROTOCOLS = frozenset({
    *OPENAI_COMPATIBLE_PROTOCOLS,
    "openai_responses",
})
MAX_CAPABILITY_ROUTE_GROUPS = 16


@dataclass(frozen=True, slots=True)
class CapabilityRoute:
    """One runnable Provider Offering projected without credential material."""

    selection: NativeModelSelection
    provider_id: str
    provider_name: str
    offering_id: str
    model_id: str
    display_name: str
    wire_model_id: str

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "model": self.model_id,
            "display_name": self.display_name,
            "available": True,
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "offering_id": self.offering_id,
        }


def _offering_selection(
    offering: Mapping[str, object],
    *,
    provider_id: str,
) -> NativeModelSelection:
    if offering["identity_state"] == "pending_identity":
        return NativeModelSelection(
            None,
            ProviderOfferingRef(provider_id, str(offering["offering_id"])),
            provider_id,
        )
    return NativeModelSelection(
        ModelRef.from_value(offering["model"]),  # type: ignore[arg-type]
        None,
        provider_id,
    )


def _capability_routes(
    document: Mapping[str, object],
    capability: str,
    *,
    required_protocols: frozenset[str] = frozenset(),
    compiler: RouteCandidateCompiler | None = None,
) -> list[CapabilityRoute]:
    required_capability = str(capability or "").strip()
    if not required_capability:
        raise ModelRoutingError("capability is required", field="capability")

    providers = {
        row["provider_id"]: row
        for row in document["providers"]  # type: ignore[index]
    }
    accesses = {
        row["provider_access_id"]: row
        for row in document["provider_accesses"]  # type: ignore[index]
    }
    models = {
        (row["creator_id"], row["model_id"]): row
        for row in document["models"]  # type: ignore[index]
    }
    policy = RoutePolicy(
        required_capabilities=frozenset({required_capability}),
        required_protocols=required_protocols,
    )
    candidate_compiler = compiler or RouteCandidateCompiler(document)
    routes: list[tuple[tuple, CapabilityRoute]] = []
    for offering in document["offerings"]:  # type: ignore[index]
        access = accesses[offering["provider_access_id"]]
        provider = providers[access["provider_id"]]
        selection = _offering_selection(
            offering, provider_id=str(provider["provider_id"]))
        candidates = candidate_compiler.compile(selection, policy=policy)
        candidate = next((
            row for row in candidates
            if row.offering["offering_id"] == offering["offering_id"]
        ), None)
        if candidate is None:
            continue
        model_id = str(offering.get("pending_model_id") or "")
        display_name = model_id
        model_ref = offering.get("model")
        if isinstance(model_ref, Mapping):
            model_id = str(model_ref.get("model_id") or "")
            model_row = models.get((
                str(model_ref.get("creator_id") or ""), model_id)) or {}
            display_name = str(model_row.get("display_name") or model_id)
        routes.append((candidate.score, CapabilityRoute(
            selection=selection,
            provider_id=str(provider["provider_id"]),
            provider_name=str(
                access.get("display_name") or provider.get("name")
                or provider["provider_id"]),
            offering_id=str(offering["offering_id"]),
            model_id=model_id,
            display_name=display_name,
            wire_model_id=str(candidate.deployment["wire_model_id"]),
        )))
    routes.sort(key=lambda row: (
        row[0], row[1].model_id.casefold(), row[1].provider_id))
    return [row[1] for row in routes]


def list_capability_routes(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    capability: str,
    *,
    required_protocols: frozenset[str] = frozenset(),
) -> list[CapabilityRoute]:
    """Return only enabled, authorized, probe-passed owner routes."""

    return _capability_routes(
        repository.get(boundary).document,
        capability,
        required_protocols=required_protocols,
    )


def list_capability_route_groups(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    requirements: Mapping[str, frozenset[str]],
) -> dict[str, list[CapabilityRoute]]:
    """Evaluate several capabilities from one owner authority revision.

    The query is request-scoped and bounded; no document or authorization
    result survives the call. This is the multi-capability equivalent of
    :func:`list_capability_routes`, used when one UI surface needs adjacent
    endpoint families such as transcription and audio-chat.
    """
    if len(requirements) > MAX_CAPABILITY_ROUTE_GROUPS:
        raise ModelRoutingError(
            f"capability route query exceeds {MAX_CAPABILITY_ROUTE_GROUPS} groups",
            kind="model_routing_resource_budget_exceeded",
        )
    authority = repository.get(boundary)
    compiler = RouteCandidateCompiler(authority.document)
    return {
        capability: _capability_routes(
            authority.document,
            capability,
            required_protocols=required_protocols,
            compiler=compiler,
        )
        for capability, required_protocols in requirements.items()
    }


def mint_capability_slot_group(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    capability: str,
    *,
    prefer_model: str = "",
    preferred_provider_id: str = "",
    required_protocols: frozenset[str] = frozenset(),
    owner_tag: str = "",
    max_candidates: int = MAX_REQUEST_ROUTE_SLOTS,
) -> tuple[str, RoutedSlotGroup]:
    """Resolve one capability model and mint its isolated dispatcher group."""

    authority = repository.get(boundary)
    model_id = str(prefer_model or "").strip()
    provider_id = str(preferred_provider_id or "").strip()
    if model_id:
        selection = resolve_compatible_model(
            authority.document,
            model_id,
            preferred_provider_id=provider_id,
        )
    else:
        routes = _capability_routes(
            authority.document,
            capability,
            required_protocols=required_protocols,
        )
        if not routes:
            raise ModelRoutingError(
                f"no authorized, healthy {capability} Deployment satisfies the request",
                kind="model_route_unavailable",
            )
        selection = routes[0].selection
        model_id = routes[0].model_id
    group = mint_routed_slot_group(
        repository,
        boundary,
        selection,
        policy=RoutePolicy(
            required_capabilities=frozenset({str(capability or "").strip()}),
            required_protocols=required_protocols,
        ),
        owner_tag=owner_tag,
        max_candidates=max_candidates,
    )
    return model_id, group


__all__ = [
    "CapabilityRoute",
    "OPENAI_CHAT_COMPATIBLE_PROTOCOLS",
    "OPENAI_COMPATIBLE_PROTOCOLS",
    "MAX_CAPABILITY_ROUTE_GROUPS",
    "list_capability_route_groups",
    "list_capability_routes",
    "mint_capability_slot_group",
]
