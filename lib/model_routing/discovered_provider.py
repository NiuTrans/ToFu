"""Compile one authenticated provider probe into a model-routing v2 draft.

The network discovery engine reports transport facts in its historical model
row shape.  This module is the sole translation boundary into an owner-scoped
ProviderAccess bundle.  Discovered names remain pending identities; a probe
proves that a wire deployment exists, not who created the model.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from lib.model_routing.offering_metadata import discovery_context_budget

from .domain import ModelRoutingError

from .inference import profile_from_metadata




def _stable_id(kind: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x00".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:24]
    return f"byo-{kind}-{digest}"


def discovered_provider_id(brand: object, base_url: object) -> str:
    """Return a stable endpoint identity without exposing the URL in an ID."""

    brand_text = "".join(
        character if character.isalnum() else "-"
        for character in str(brand or "generic").strip().lower()
    ).strip("-")[:32] or "generic"
    url = str(base_url or "").strip().rstrip("/")
    return f"byo-{brand_text}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ModelRoutingError(
            f"{field} must be a string",
            kind="discovered_provider_invalid",
            field=field,
        )
    result = value.strip()
    if (
        not result
        or len(result) > 256
        or any(ord(character) < 32 for character in result)
    ):
        raise ModelRoutingError(
            f"{field} must be 1..256 printable characters",
            kind="discovered_provider_invalid",
            field=field,
        )
    return result


def _capabilities(value: object) -> list[str]:
    raw = value if isinstance(value, list) else []
    capabilities = {
        item.strip()
        for item in raw[:64]
        if isinstance(item, str)
        and item.strip()
        and len(item.strip()) <= 256
        and not any(ord(character) < 32 for character in item.strip())
    }
    return sorted(capabilities if isinstance(value, list) else {"text"})


def build_discovered_provider_bundle(
    *,
    provider_id: str,
    display_name: str,
    brand: str,
    base_url: str,
    models: Sequence[Mapping[str, Any]],
    protocol: str = "openai",
    official_directory: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a secret-free ProviderAccess creation payload from probe facts."""

    normalized_provider_id = _identifier(provider_id, field="provider_id")
    normalized_name = _identifier(display_name, field="display_name")
    normalized_brand = _identifier(brand or "generic", field="brand")
    normalized_url = _identifier(base_url, field="base_url").rstrip("/")
    normalized_protocol = _identifier(protocol, field="protocol")
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
        raise ModelRoutingError(
            "models must be an array",
            kind="discovered_provider_invalid",
            field="models",
        )

    normalized_models: dict[str, dict[str, Any]] = {}
    from .catalog_identity import inherit_discovered_metadata
    declared = inherit_discovered_metadata(
        [row for row in models if isinstance(row, Mapping)], official_directory)
    for index, raw in enumerate(declared):
        if not isinstance(raw, Mapping):
            continue
        model_id = _identifier(
            raw.get("model_id"), field=f"models[{index}].model_id")
        if model_id in normalized_models:
            continue
        rpm = raw.get("rpm")
        normalized_models[model_id] = {
            "capabilities": _capabilities(raw.get("capabilities")),
            "inherited_model_fields": raw['inherited_model_fields'],

            "inference_profile": profile_from_metadata(raw, source="provider_catalog"),
            "context_window": discovery_context_budget(raw),
            "rpm": (
                int(rpm)
                if isinstance(rpm, int) and not isinstance(rpm, bool) and rpm >= 0
                else None
            ),
        }
    if not normalized_models:
        raise ModelRoutingError(
            "provider probe did not expose a valid model identity",
            kind="discovered_provider_models_empty",
            field="models",
        )

    access_id = _stable_id("access", normalized_provider_id)
    connection_id = _stable_id(
        "connection", normalized_provider_id, normalized_url)
    credential_id = _stable_id("credential", normalized_provider_id)
    offerings: list[dict[str, Any]] = []
    deployments: list[dict[str, Any]] = []
    for priority, (model_id, facts) in enumerate(sorted(normalized_models.items())):
        offering_id = _stable_id("offering", normalized_provider_id, model_id)
        offerings.append({
            "offering_id": offering_id,
            "provider_access_id": access_id,
            "identity_state": "pending_identity",
            "pending_model_id": model_id,
            "enabled": True,
            "stale": False,
            "capabilities": facts["capabilities"],
            "inherited_model_fields": facts['inherited_model_fields'],

            "inference_profile": facts["inference_profile"],
            "context_window": facts["context_window"],
            "priority": priority,
        })
        deployments.append({
            "deployment_id": _stable_id(
                "deployment", normalized_provider_id, normalized_url, model_id),
            "offering_id": offering_id,
            "connection_id": connection_id,
            "wire_model_id": model_id,
            # Listing is account catalog evidence, not an inference verdict.
            "identity_confidence": "pending",
            "probe_status": "unprobed",
            "priority": priority,
        })

    rpm_values = [
        facts["rpm"] for facts in normalized_models.values()
        if facts["rpm"] is not None
    ]
    bundle = {
        "provider": {
            "provider_id": normalized_provider_id,
            "name": normalized_name,
            "scope": "owner",
            "brand": normalized_brand,
        },
        "provider_access": {
            "provider_access_id": access_id,
            "provider_id": normalized_provider_id,
            "enabled": True,
            "display_name": normalized_name,
            "quota_policy": ({"rpm": min(rpm_values)} if rpm_values else {}),
        },
        "connections": [{
            "connection_id": connection_id,
            "provider_access_id": access_id,
            "base_url": normalized_url,
            "protocol": normalized_protocol,
            "enabled": True,
            "priority": 0,
            "extra_headers": {},
        }],
        "credentials": [{
            "credential_id": credential_id,
            "provider_access_id": access_id,
            "kind": "api_key",
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
        "creators": [],
        "models": [],
    }
    from .catalog_identity import reconcile_catalog_identities
    from copy import deepcopy
    document = {**bundle, 'models': deepcopy(list(official_directory))}
    reconcile_catalog_identities(document, access_id=access_id)
    used = {(r['model']['creator_id'], r['model']['model_id'])
            for r in offerings if r.get('model')}
    bundle['models'] = [r for r in document['models']
                        if (r['creator_id'], r['model_id']) in used]
    bundle['creators'] = [{'creator_id': creator, 'name': creator}
                          for creator in sorted({ref[0] for ref in used})]
    return bundle


__all__ = ["build_discovered_provider_bundle", "discovered_provider_id"]
