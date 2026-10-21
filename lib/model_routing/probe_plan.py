"""Shared owner-scoped matrix planning for HTTP and scheduled probes."""
import copy
from collections.abc import Mapping
from typing import Any
from .repository import ModelRoutingRepository, OwnerBoundary
from .domain import ModelRoutingError


def provider_bundle(document: Mapping[str, Any], provider_id: str) -> dict[str, Any] | None:
    provider = next(
        (row for row in document["providers"] if row["provider_id"] == provider_id),
        None,
    )
    if provider is None:
        return None
    accesses = [
        row for row in document["provider_accesses"]
        if row["provider_id"] == provider_id
    ]
    access_ids = {row["provider_access_id"] for row in accesses}
    connections = [
        row for row in document["connections"]
        if row["provider_access_id"] in access_ids
    ]
    connection_ids = {row["connection_id"] for row in connections}
    credentials = [
        row for row in document["credentials"]
        if row["provider_access_id"] in access_ids
    ]
    offerings = [
        row for row in document["offerings"]
        if row["provider_access_id"] in access_ids
    ]
    offering_ids = {row["offering_id"] for row in offerings}
    deployments = [
        row for row in document["deployments"]
        if row["offering_id"] in offering_ids
        and row["connection_id"] in connection_ids
    ]
    return {
        "provider": copy.deepcopy(provider),
        "provider_access": copy.deepcopy(accesses[0]) if accesses else None,
        "connections": copy.deepcopy(connections),
        "credentials": copy.deepcopy(credentials),
        "offerings": copy.deepcopy(offerings),
        "deployments": copy.deepcopy(deployments),
    }


def manual_probe_bundle(
    bundle: Mapping[str, Any], targets: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve draft names only against saved owner-scoped transport identities.

    This is a transient projection, not registration or a routing mutation.
    URLs, credentials, capabilities and identity evidence remain server-owned.
    """
    def invalid(message: str) -> None:
        raise ModelRoutingError(message, kind="provider_probe_invalid_target",
                                field="targets")

    if not isinstance(targets, list) or not 1 <= len(targets) <= 1024:
        invalid("Select between 1 and 1024 request names to detect.")
    offerings = {row["offering_id"]: row for row in bundle["offerings"]}
    connections = {row["connection_id"]: row for row in bundle["connections"]}
    deployments = []
    seen: dict[str, tuple[str, str]] = {}
    for target in targets:
        if not isinstance(target, Mapping) or set(target) != {
            "offering_id", "connection_id", "wire_model_id"
        }:
            invalid("A detection target must specify its model, connection and request name.")
        offering_id = target.get("offering_id")
        connection_id = target.get("connection_id")
        wire = target.get("wire_model_id")
        if not all(isinstance(value, str) for value in (offering_id, connection_id, wire)):
            invalid("Detection target identifiers must be text.")
        wire = wire.strip()
        if not wire or len(wire) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in target["wire_model_id"]):
            invalid("Enter a request name of 1–256 characters without control characters.")
        offering = offerings.get(offering_id)
        connection = connections.get(connection_id)
        if (offering is None or connection is None
                or not offering.get("provider_access_id")
                or offering["provider_access_id"] != connection.get("provider_access_id")):
            invalid("The selected model or connection is no longer available in this provider. Refresh Settings and retry.")
        if not str(connection.get("base_url") or "").strip():
            invalid("Configure a saved connection address before detecting this request name.")
        pair = (offering_id, connection_id)
        if wire in seen:
            if seen[wire] != pair:
                invalid("The same request name cannot select different models or connections in one detection.")
            continue
        seen[wire] = pair
        deployments.append({"offering_id": offering_id, "connection_id": connection_id,
                            "wire_model_id": wire})
    result = copy.deepcopy(bundle)
    result["deployments"] = deployments
    selected = {row["offering_id"] for row in deployments}
    result["offerings"] = [row for row in result["offerings"] if row["offering_id"] in selected]
    return result


def matrix_probe_plan(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    bundle: Mapping[str, Any],
    *, manual: bool = False,
) -> dict[str, Any]:
    """Resolve one ProviderAccess bundle into engine probe inputs.

    Returns engine inputs plus explicit request_contexts (per wire) and
    credential_contexts (per column); these private contexts carry secrets and
    never appear in public probe snapshots.
    Automatic callers skip disabled Offerings and Connections. Explicit manual
    diagnostics include them without enabling, saving or granting anything.
    Every request name is independent of credential grants and historical probe
    evidence. Retired ``Deployment.enabled`` is ignored in both modes.
    ``api_keys[i]`` is the resolved plaintext key for column i (never leaves
    the process; ``public_probe_snapshot`` strips the task's private fields).
    A pure subscription access (every credential decodes to an oauth name)
    switches the task to the engine's oauth mode, which resolves the live
    token per cell instead of probing the stored sentinel.
    """
    connections = [
        row for row in bundle["connections"]
        if (manual or row.get("enabled", True)) and str(row.get("base_url") or "").strip()
    ]
    if not connections:
        raise ModelRoutingError(
            ("Configure a saved connection address before detecting."
             if manual else "provider access has no enabled connection with a base_url"),
            kind="provider_probe_no_connection",
        )
    primary = connections[0]
    connections_by_id = {row["connection_id"]: row for row in connections}
    credentials = list(bundle["credentials"])
    if not credentials:
        raise ModelRoutingError(
            "provider access has no credentials to probe",
            kind="provider_probe_no_credentials",
        )
    api_keys: list[str] = []
    oauth_names: list[str] = []
    credential_contexts: list[dict[str, Any]] = []
    for credential in credentials:
        reference = str(credential.get("secret_reference") or "")
        plaintext = ""
        if reference:
            # A local secret-resolution failure is not remote authorization
            # evidence. Abort the plan before submitting any provider request.
            plaintext = repository.resolve_secret(boundary, reference)
        # Request-scoped: dispatch_adapter must stay out of the boot path.
        from .dispatch_adapter import decode_credential_secret
        api_key, oauth, secret_headers = decode_credential_secret(
            plaintext, kind=str(credential.get("kind") or "api_key"))
        api_keys.append("oauth-managed" if oauth else api_key)
        oauth_names.append(oauth)
        credential_contexts.append({"oauth": oauth, "extra_headers": secret_headers})
    oauth_mode = (
        bool(credentials)
        and all(name and name == oauth_names[0] for name in oauth_names)
    )
    wires_by_offering: dict[str, list[str]] = {}
    request_contexts: dict[str, dict[str, Any]] = {}
    for deployment in bundle["deployments"]:
        wire_id = str(deployment.get("wire_model_id") or "").strip()
        if not wire_id:
            continue
        connection = connections_by_id.get(deployment["connection_id"])
        if connection is not None:
            request_contexts[wire_id] = {
                "base_url": connection["base_url"],
                "protocol": connection.get("protocol") or "openai",
                # The request name's own selectors win over the Connection's,
                # exactly as dispatch merges them; otherwise the matrix would
                # probe a cell with headers the real request never sends.
                "extra_headers": {
                    **dict(connection.get("extra_headers") or {}),
                    **dict(deployment.get("extra_headers") or {}),
                },
                "adapter": dict(connection.get("adapter") or {}),
            }
        rows = wires_by_offering.setdefault(deployment["offering_id"], [])
        if wire_id not in rows:
            rows.append(wire_id)
    models: list[dict[str, Any]] = []
    for offering in bundle["offerings"]:
        if not manual and not offering.get("enabled", True):
            continue
        root_id = str((offering.get("model") or {}).get("model_id")
                      or offering.get("pending_model_id") or "")
        if not root_id:
            continue
        entry: dict[str, Any] = {
            "model_id": root_id,
            "aliases": [],
            "capabilities": list(offering.get("capabilities") or []),
        }
        wire_ids = wires_by_offering.get(offering["offering_id"], [])
        if wire_ids:
            entry["request_ids"] = wire_ids
        else:
            # Discovery before any Deployment exists retains the explicit
            # primary Connection; known wires never fall back to another one.
            request_contexts[root_id] = {
                "base_url": primary["base_url"],
                "protocol": primary.get("protocol") or "openai",
                "extra_headers": dict(primary.get("extra_headers") or {}),
                "adapter": dict(primary.get("adapter") or {}),
            }
        models.append(entry)
    if not models:
        raise ModelRoutingError(
            ("Add a model and request name before detecting."
             if manual else "provider access has no enabled offerings to probe"),
            kind="provider_probe_no_offerings",
        )
    return {
        "provider": {
            "id": str(bundle["provider"]["provider_id"]),
            "base_url": str(primary.get("base_url") or ""),
            "protocol": str(primary.get("protocol") or "openai") or "openai",
            "faces": {},
        },
        "models": models,
        "api_keys": (["oauth-managed"] * len(credentials)) if oauth_mode else api_keys,
        "oauth": oauth_names[0] if oauth_mode else "",
        "request_contexts": request_contexts,
        "credential_contexts": credential_contexts,
        "adapter": dict(primary.get("adapter") or {}),
        "extra_headers": dict(primary.get("extra_headers") or {}),
    }
