"""CAS mutations for one owner-scoped, system-managed ProviderAccess bundle.

Autodiscovery, managed local serving, OAuth and desktop adapters discover
transport facts outside Settings.  This module gives those producers one
bounded mutation boundary: replace only their Provider-owned resources,
preserve user policy fields, and keep plaintext credentials behind the
repository secret channel.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from lib.log import get_logger

from .domain import ModelRoutingError, normalize_document
from .repository import OwnerBoundary, RepositoryPort, StoredAuthority


logger = get_logger(__name__)

_MAX_COMMIT_ATTEMPTS = 4
_BUNDLE_COLLECTIONS = (
    "providers",
    "provider_accesses",
    "connections",
    "credentials",
    "offerings",
    "deployments",
)


@dataclass(frozen=True, slots=True)
class ManagedProviderMutation:
    """Result of one idempotent managed-provider aggregate mutation."""

    authority: StoredAuthority
    provider_id: str
    changed: bool


def connection_urls(document: Mapping[str, Any]) -> dict[str, list[str]]:
    """Project configured v2 Connection URLs by Provider."""

    access_to_provider = {
        row["provider_access_id"]: row["provider_id"]
        for row in document.get("provider_accesses", [])
        if isinstance(row, Mapping)
        and isinstance(row.get("provider_access_id"), str)
        and isinstance(row.get("provider_id"), str)
    }
    result: dict[str, list[str]] = {}
    for row in document.get("connections", []):
        if not isinstance(row, Mapping):
            continue
        provider_id = access_to_provider.get(row.get("provider_access_id"))
        base_url = row.get("base_url")
        if provider_id and isinstance(base_url, str) and base_url.strip():
            result.setdefault(provider_id, []).append(base_url.strip())
    return {
        provider_id: sorted(set(urls))
        for provider_id, urls in result.items()
    }


def remove_provider_resources(
    document: Mapping[str, Any], provider_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Return a copy without one Provider and its transitively owned rows."""

    result = copy.deepcopy(dict(document))
    access_ids = {
        row["provider_access_id"]
        for row in result["provider_accesses"]
        if row["provider_id"] == provider_id
    }
    connection_ids = {
        row["connection_id"]
        for row in result["connections"]
        if row["provider_access_id"] in access_ids
    }
    offering_ids = {
        row["offering_id"]
        for row in result["offerings"]
        if row["provider_access_id"] in access_ids
    }
    secret_references = sorted({
        row["secret_reference"]
        for row in result["credentials"]
        if row["provider_access_id"] in access_ids
        and row.get("secret_reference")
    })
    result["providers"] = [
        row for row in result["providers"]
        if row["provider_id"] != provider_id
    ]
    result["provider_accesses"] = [
        row for row in result["provider_accesses"]
        if row["provider_access_id"] not in access_ids
    ]
    result["connections"] = [
        row for row in result["connections"]
        if row["connection_id"] not in connection_ids
    ]
    result["credentials"] = [
        row for row in result["credentials"]
        if row["provider_access_id"] not in access_ids
    ]
    result["offerings"] = [
        row for row in result["offerings"]
        if row["offering_id"] not in offering_ids
    ]
    result["deployments"] = [
        row for row in result["deployments"]
        if row["offering_id"] not in offering_ids
        and row["connection_id"] not in connection_ids
    ]
    return result, secret_references


def _preserve_user_configuration(
    document: Mapping[str, Any],
    provider_id: str,
    bundle: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Carry Settings-owned policy across a refresh of discovered facts."""

    preserved = copy.deepcopy(bundle)
    old_provider = next((
        row for row in document.get("providers", [])
        if row.get("provider_id") == provider_id
    ), None)
    if old_provider is None:
        return preserved

    old_accesses = [
        row for row in document.get("provider_accesses", [])
        if row.get("provider_id") == provider_id
    ]
    old_access = old_accesses[0] if len(old_accesses) == 1 else None
    old_access_id = (
        old_access.get("provider_access_id") if old_access is not None else None)

    preserved["providers"][0]["name"] = old_provider.get(
        "name", preserved["providers"][0]["name"])
    if old_access is None:
        return preserved

    for field in ("enabled", "display_name", "quota_policy"):
        if field in old_access:
            preserved["provider_accesses"][0][field] = copy.deepcopy(
                old_access[field])

    old_connections = {
        str(row.get("base_url") or "").rstrip("/"): row
        for row in document.get("connections", [])
        if row.get("provider_access_id") == old_access_id
    }
    for connection in preserved["connections"]:
        old_connection = old_connections.get(
            str(connection.get("base_url") or "").rstrip("/"))
        if old_connection is None:
            continue
        for field in (
            "enabled", "priority", "extra_headers", "thinking_format",
        ):
            if field in old_connection:
                connection[field] = copy.deepcopy(old_connection[field])

    old_credentials = {
        row.get("credential_id"): row
        for row in document.get("credentials", [])
        if row.get("provider_access_id") == old_access_id
    }
    for credential in preserved["credentials"]:
        old_credential = old_credentials.get(credential.get("credential_id"))
        if old_credential is None:
            continue
        for field in ("enabled", "quota_policy"):
            if field in old_credential:
                credential[field] = copy.deepcopy(old_credential[field])

    old_offerings = {
        (row.get("identity_state"), row.get("pending_model_id"),
         tuple(sorted((row.get("model") or {}).items()))): row
        for row in document.get("offerings", [])
        if row.get("provider_access_id") == old_access_id
    }
    old_offering_ids: set[str] = set()
    for offering in preserved["offerings"]:
        identity = (
            offering.get("identity_state"),
            offering.get("pending_model_id"),
            tuple(sorted((offering.get("model") or {}).items())),
        )
        old_offering = old_offerings.get(identity)
        if old_offering is None:
            continue
        old_offering_ids.add(str(old_offering.get("offering_id") or ""))
        for field in ("enabled", "priority", "actual_pricing"):
            if field in old_offering:
                offering[field] = copy.deepcopy(old_offering[field])

    old_deployments_by_wire = {
        row.get("wire_model_id"): row
        for row in document.get("deployments", [])
        if str(row.get("offering_id") or "") in old_offering_ids
    }
    for deployment in preserved["deployments"]:
        old_deployment = old_deployments_by_wire.get(
            deployment.get("wire_model_id"))
        if old_deployment is None:
            continue
        for field in ("enabled", "priority"):
            if field in old_deployment:
                deployment[field] = copy.deepcopy(old_deployment[field])

    return preserved


def _delete_unreferenced_secrets(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    references: set[str],
    authority: StoredAuthority | None,
) -> None:
    active = {
        row.get("secret_reference")
        for row in (authority.document["credentials"] if authority else [])
        if row.get("secret_reference")
    }
    for reference in sorted(references - active):
        try:
            repository.delete_secret(boundary, reference)
        except Exception as exc:
            # Secret cleanup is reconstructible housekeeping.  The repository's
            # bounded orphan-prune operation remains the durable fallback.
            logger.debug(
                "Managed provider secret cleanup deferred for %s: %s",
                reference,
                exc,
            )


def replace_managed_provider(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    *,
    provider_id: str,
    bundle: Mapping[str, list[Mapping[str, Any]]],
    credential_plaintexts: Mapping[str, str] | None = None,
    require_unclaimed_connection: bool = False,
) -> ManagedProviderMutation:
    """Replace one managed bundle with bounded CAS and secret-safe retries."""

    source_bundle = {
        collection: [copy.deepcopy(dict(row)) for row in bundle.get(collection, [])]
        for collection in _BUNDLE_COLLECTIONS
    }
    plaintexts = dict(credential_plaintexts or {})
    created_secret_metadata: dict[str, dict[str, str]] = {}
    created_references: set[str] = set()
    stale_references: set[str] = set()
    last_authority: StoredAuthority | None = None

    try:
        for _attempt in range(_MAX_COMMIT_ATTEMPTS):
            authority = repository.get(boundary)
            last_authority = authority
            if authority.revision <= 0:
                raise ModelRoutingError(
                    "model-routing v2 authority is not active for this owner",
                    kind="model_routing_authority_inactive",
                )
            if require_unclaimed_connection:
                wanted_urls = {
                    str(row.get("base_url") or "").rstrip("/")
                    for row in source_bundle["connections"]
                }
                for existing_provider_id, urls in connection_urls(
                    authority.document
                ).items():
                    if (
                        existing_provider_id != provider_id
                        and wanted_urls & {url.rstrip("/") for url in urls}
                    ):
                        return ManagedProviderMutation(
                            authority, provider_id, False)

            refreshed = _preserve_user_configuration(
                authority.document, provider_id, source_bundle)
            old_credentials = {
                row.get("credential_id"): row
                for row in authority.document["credentials"]
            }
            for credential in refreshed["credentials"]:
                credential_id = str(credential.get("credential_id") or "")
                if credential_id not in plaintexts:
                    continue
                plaintext = plaintexts[credential_id]
                metadata = created_secret_metadata.get(credential_id)
                old_credential = old_credentials.get(credential_id)
                old_reference = str(
                    (old_credential or {}).get("secret_reference") or "")
                if metadata is None and old_reference:
                    try:
                        if repository.resolve_secret(
                            boundary, old_reference
                        ) == plaintext:
                            metadata = {
                                "secret_reference": old_reference,
                                "key_hint": str(
                                    (old_credential or {}).get("key_hint") or ""),
                            }
                    except ModelRoutingError:
                        metadata = None
                if metadata is None:
                    metadata = repository.put_secret(boundary, plaintext)
                    created_secret_metadata[credential_id] = metadata
                    created_references.add(metadata["secret_reference"])
                credential.update(metadata)

            candidate, removed_references = remove_provider_resources(
                authority.document, provider_id)
            stale_references.update(removed_references)
            for collection, rows in refreshed.items():
                candidate[collection].extend(copy.deepcopy(rows))
            candidate = normalize_document(
                candidate, revision=authority.revision)
            if candidate == authority.document:
                _delete_unreferenced_secrets(
                    repository, boundary, created_references, authority)
                return ManagedProviderMutation(authority, provider_id, False)
            try:
                committed = repository.compare_and_swap(
                    boundary,
                    candidate,
                    expected_revision=authority.revision,
                )
            except ModelRoutingError as exc:
                if exc.kind == "model_routing_revision_conflict":
                    continue
                raise
            _delete_unreferenced_secrets(
                repository,
                boundary,
                stale_references | created_references,
                committed,
            )
            return ManagedProviderMutation(committed, provider_id, True)
        raise ModelRoutingError(
            "model-routing aggregate kept changing during provider update",
            kind="model_routing_revision_conflict",
        )
    except Exception:
        _delete_unreferenced_secrets(
            repository, boundary, created_references, last_authority)
        raise


def delete_managed_provider(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    *,
    provider_id: str,
) -> ManagedProviderMutation:
    """Delete one provider and only its owner-scoped resources."""

    for _attempt in range(_MAX_COMMIT_ATTEMPTS):
        authority = repository.get(boundary)
        if authority.revision <= 0:
            raise ModelRoutingError(
                "model-routing v2 authority is not active for this owner",
                kind="model_routing_authority_inactive",
            )
        candidate, secret_references = remove_provider_resources(
            authority.document, provider_id)
        candidate = normalize_document(candidate, revision=authority.revision)
        if candidate == authority.document:
            return ManagedProviderMutation(authority, provider_id, False)
        try:
            committed = repository.compare_and_swap(
                boundary,
                candidate,
                expected_revision=authority.revision,
            )
        except ModelRoutingError as exc:
            if exc.kind == "model_routing_revision_conflict":
                continue
            raise
        _delete_unreferenced_secrets(
            repository, boundary, set(secret_references), committed)
        return ManagedProviderMutation(committed, provider_id, True)
    raise ModelRoutingError(
        "model-routing aggregate kept changing during provider removal",
        kind="model_routing_revision_conflict",
    )


__all__ = [
    "ManagedProviderMutation",
    "connection_urls",
    "delete_managed_provider",
    "remove_provider_resources",
    "replace_managed_provider",
]
