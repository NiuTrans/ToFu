"""Owner-scoped local endpoint registration for model-routing v2.

This module is the sole bridge from a discovered OpenAI-compatible loopback
endpoint into the canonical routing aggregate.  Auto-discovery and managed
local serving both supply endpoint facts here; neither constructs a legacy
``server_config.providers`` row or edits storage directly.

Registration is a bounded revision-CAS update.  Entity identifiers are stable
for one Provider and wire model, so repeating discovery replaces facts instead
of growing the aggregate.  Discovered model names remain provider-scoped
``pending_identity`` Offerings until an independent identity authority confirms
an official Creator/Model reference.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from lib.model_routing.offering_metadata import discovery_context_budget

from .domain import ModelRoutingError

from .inference import profile_from_metadata
from .managed_provider import (
    ManagedProviderMutation,
    connection_urls,
    delete_managed_provider,
    replace_managed_provider,
)
from .repository import OwnerBoundary, RepositoryPort


LocalProviderMutation = ManagedProviderMutation


def _stable_id(kind: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x00".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:24]
    return f"local-{kind}-{digest}"


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ModelRoutingError(
            f"{field} must be a string",
            kind="local_provider_invalid",
            field=field,
        )
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ModelRoutingError(
            f"{field} must be 1..256 printable characters",
            kind="local_provider_invalid",
            field=field,
        )
    return normalized


def _capabilities(value: object) -> list[str]:
    raw = value if isinstance(value, list) else []
    result: set[str] = set()
    for item in raw[:64]:
        if not isinstance(item, str):
            continue
        capability = item.strip()
        if (
            capability
            and len(capability) <= 256
            and not any(ord(character) < 32 for character in capability)
        ):
            result.add(capability)
    # Discovery should already classify every row.  The fallback keeps a
    # minimally useful pending model when a standards-compliant /models server
    # returns IDs only.
    return sorted(result if isinstance(value, list) else {"text"})


def _normalized_models(
    models: Sequence[Mapping[str, Any]], *, provider_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
        raise ModelRoutingError(
            "local provider models must be an array",
            kind="local_provider_invalid",
            field="models",
        )
    from .catalog_identity import inherit_discovered_metadata
    declared = inherit_discovered_metadata(
        [row for row in models if isinstance(row, Mapping)], [])
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(declared):
        if not isinstance(raw, Mapping):
            continue
        model_id = _identifier(raw.get("model_id"), field=f"models[{index}].model_id")
        if model_id in by_id:
            continue
        by_id[model_id] = {
            "model_id": model_id,
            "capabilities": _capabilities(raw.get("capabilities")),
            "inherited_model_fields": raw["inherited_model_fields"],

            "inference_profile": profile_from_metadata(raw, source="configured"),
            "context_window": discovery_context_budget(raw),
            "rpm": (
                int(raw["rpm"])
                if isinstance(raw.get("rpm"), int)
                and not isinstance(raw.get("rpm"), bool)
                and int(raw["rpm"]) >= 0
                else None
            ),
        }
    if not by_id:
        raise ModelRoutingError(
            "local endpoint did not expose a valid model identity",
            kind="local_provider_models_empty",
            field="models",
        )
    return [by_id[model_id] for model_id in sorted(by_id)]


def build_local_provider_bundle(
    *,
    provider_id: str,
    display_name: str,
    base_url: str,
    models: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build one complete, secret-free ProviderAccess aggregate fragment."""

    normalized_provider_id = _identifier(provider_id, field="provider_id")
    normalized_name = _identifier(display_name, field="display_name")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ModelRoutingError(
            "local provider base_url is required",
            kind="local_provider_invalid",
            field="base_url",
        )
    normalized_url = base_url.strip().rstrip("/")
    if len(normalized_url) > 2048:
        raise ModelRoutingError(
            "local provider base_url exceeds 2048 characters",
            kind="local_provider_invalid",
            field="base_url",
        )
    normalized_models = _normalized_models(
        models, provider_id=normalized_provider_id)

    access_id = _stable_id("access", normalized_provider_id)
    connection_id = _stable_id("connection", normalized_provider_id, normalized_url)
    credential_id = _stable_id("credential", normalized_provider_id)
    offerings: list[dict[str, Any]] = []
    deployments: list[dict[str, Any]] = []
    for priority, model in enumerate(normalized_models):
        offering_id = _stable_id(
            "offering", normalized_provider_id, model["model_id"])
        offerings.append({
            "offering_id": offering_id,
            "provider_access_id": access_id,
            "identity_state": "pending_identity",
            "pending_model_id": model["model_id"],
            "enabled": True,
            "stale": False,
            "capabilities": model["capabilities"],
            "inherited_model_fields": model["inherited_model_fields"],

            "inference_profile": model["inference_profile"],
            "context_window": model["context_window"],
            "priority": priority,
        })
        deployments.append({
            "deployment_id": _stable_id(
                "deployment", normalized_provider_id, model["model_id"], normalized_url),
            "offering_id": offering_id,
            "connection_id": connection_id,
            "wire_model_id": model["model_id"],
            "identity_confidence": "pending",
            "probe_status": "unprobed",
            "priority": priority,
        })

    rpm_values = [model["rpm"] for model in normalized_models if model["rpm"] is not None]
    return {
        "providers": [{
            "provider_id": normalized_provider_id,
            "name": normalized_name,
            "scope": "owner",
            "brand": "local",
        }],
        "provider_accesses": [{
            "provider_access_id": access_id,
            "provider_id": normalized_provider_id,
            "enabled": True,
            "display_name": normalized_name,
            "quota_policy": ({"rpm": min(rpm_values)} if rpm_values else {}),
        }],
        "connections": [{
            "connection_id": connection_id,
            "provider_access_id": access_id,
            "base_url": normalized_url,
            "protocol": "local",
            "enabled": True,
            "priority": 0,
            "extra_headers": {},
        }],
        "credentials": [{
            "credential_id": credential_id,
            "provider_access_id": access_id,
            "kind": "local_identity",
            "secret_reference": "",
            "key_hint": "",
            "enabled": True,
            "authorization": {
                "connection_ids": [connection_id],
                "models": [],
            },
            "quota_policy": {},
        }],
        "offerings": offerings,
        "deployments": deployments,
    }


def upsert_local_provider(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    *,
    provider_id: str,
    display_name: str,
    base_url: str,
    models: Sequence[Mapping[str, Any]],
    require_unclaimed_connection: bool = False,
) -> LocalProviderMutation:
    """Create or replace one deterministic local ProviderAccess bundle."""

    bundle = build_local_provider_bundle(
        provider_id=provider_id,
        display_name=display_name,
        base_url=base_url,
        models=models,
    )

    return replace_managed_provider(
        repository,
        boundary,
        provider_id=provider_id,
        bundle=bundle,
        require_unclaimed_connection=require_unclaimed_connection,
    )


def delete_local_provider(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    *,
    provider_id: str,
) -> LocalProviderMutation:
    """Delete one provider and only its owned access resources."""

    normalized_provider_id = _identifier(provider_id, field="provider_id")
    return delete_managed_provider(
        repository,
        boundary,
        provider_id=normalized_provider_id,
    )


__all__ = [
    "LocalProviderMutation",
    "build_local_provider_bundle",
    "connection_urls",
    "delete_local_provider",
    "upsert_local_provider",
]
