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

from lib.model_info import context_profile

from .domain import ModelRoutingError


_DEFAULT_CONTEXT_WINDOW = 32_768


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
    return sorted(capabilities or {"text"})


def _context_window(row: Mapping[str, Any], provider_id: str, model_id: str) -> int:
    declared = row.get("context_window")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    known = context_profile(model_id, provider_id).get("window")
    if isinstance(known, int) and known > 0:
        return known
    return _DEFAULT_CONTEXT_WINDOW


def build_discovered_provider_bundle(
    *,
    provider_id: str,
    display_name: str,
    brand: str,
    base_url: str,
    models: Sequence[Mapping[str, Any]],
    protocol: str = "openai",
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
    for index, raw in enumerate(models):
        if not isinstance(raw, Mapping):
            continue
        model_id = _identifier(
            raw.get("model_id"), field=f"models[{index}].model_id")
        if model_id in normalized_models:
            continue
        rpm = raw.get("rpm")
        normalized_models[model_id] = {
            "capabilities": _capabilities(raw.get("capabilities")),
            "context_window": _context_window(
                raw, normalized_provider_id, model_id),
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
            "context_window": facts["context_window"],
            "priority": priority,
        })
        deployments.append({
            "deployment_id": _stable_id(
                "deployment", normalized_provider_id, normalized_url, model_id),
            "offering_id": offering_id,
            "connection_id": connection_id,
            "wire_model_id": model_id,
            "enabled": True,
            "identity_confidence": "pending",
            "probe_status": "passed",
            "priority": priority,
        })

    rpm_values = [
        facts["rpm"] for facts in normalized_models.values()
        if facts["rpm"] is not None
    ]
    return {
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


__all__ = ["build_discovered_provider_bundle", "discovered_provider_id"]
