"""Validated authority indexes and logical/provider-scoped selector resolution.

Entry points: resolve_compatible_model; indexes are shared by one request's
candidate compiler. Depends on the v2 domain and canonical model identity;
no repository, credential resolution or process-wide cache is involved.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.model_catalog._creator_identity import canonical_key

from .domain import ModelRef, ModelRoutingError, NativeModelSelection, ProviderOfferingRef, normalize_document


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
        "models_by_canonical": _models_by_canonical(normalized),
    }


def _models_by_canonical(
    normalized: Mapping[str, Any],
) -> dict[str, list[ModelRef]]:
    index: dict[str, list[ModelRef]] = {}
    for row in normalized["models"]:
        key = canonical_key(row["model_id"])
        if key:
            index.setdefault(key, []).append(
                ModelRef(row["creator_id"], row["model_id"]))
    return index


def _resolve_model_ref(
    idx: Mapping[str, Any], ref: ModelRef,
) -> ModelRef | None:
    """Resolve a model ref, tolerating provider respellings absorbed by the
    trained-model merge (old pins keep routing to the survivor)."""
    if ref in idx["models"]:
        return ref
    matches = [
        candidate
        for candidate in idx["models_by_canonical"].get(
            canonical_key(ref.model_id), ())
        if candidate.creator_id == ref.creator_id
    ]
    return matches[0] if len(matches) == 1 else None


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
    if not matches:
        # Provider respellings (relay SKUs, snapshots) absorbed by the
        # trained-model merge still resolve to the surviving row.
        matches = [
            ref
            for ref in idx["models_by_canonical"].get(canonical_key(text), ())
            if not creator or ref.creator_id == creator
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
