"""Wire-level credential grant semantics shared by routing and probe apply.

A credential grant is one ``{creator_id, model_id}`` ModelRef per official
model. ``excluded_wire_model_ids`` on that grant withholds individual request
names (Deployments) of the model's offerings inside the credential's own
ProviderAccess. Wire ids are access-scoped, so an exclusion can never leak
into another access that happens to use the same upstream id; exclusions for
wires the access no longer deploys are inert.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "grant_exclusions_by_model",
    "grant_allows_wire",
    "grant_for",
    "grant_key",
]


def grant_key(ref: Mapping[str, Any]) -> tuple[str, str]:
    return (str(ref.get("creator_id") or ""), str(ref.get("model_id") or ""))


def grant_for(
    grants: list[Mapping[str, Any]],
    ref: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    creator, model = grant_key(ref)
    for grant in grants:
        if grant_key(grant) == (creator, model):
            return grant
    return None


def grant_allows_wire(
    grant: Mapping[str, Any] | None,
    wire_model_id: str,
) -> bool:
    if grant is None:
        return False
    excluded = {str(w) for w in grant.get("excluded_wire_model_ids") or ()}
    return wire_model_id not in excluded


def grant_exclusions_by_model(
    credentials: list[Mapping[str, Any]],
) -> dict[tuple[str, str], set[str]]:
    """Union of every credential's exclusions, keyed by (creator, model).

    The probe layer never sees credential-scoped grants; a wire excluded for
    one key but granted for another is still covered by the granted key, so
    only wires excluded for ALL keys that grant the model count as covered.
    This returns the union for reporting; apply logic intersects per
    credential via ``grant_allows_wire`` instead.
    """
    out: dict[tuple[str, str], set[str]] = {}
    for credential in credentials:
        for grant in (credential.get("authorization") or {}).get("models") or ():
            out.setdefault(grant_key(grant), set()).update(
                str(w) for w in grant.get("excluded_wire_model_ids") or ())
    return out
