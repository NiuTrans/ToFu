"""Conservative identity matching for external model-score datasets.

This module owns only name normalization and matching.  It never knows about
providers, wire identifiers, routes, or credentials.  External score sources
must prefer a missing score over attaching one model's benchmark to another:
matching is exact-identity only, so a variant the source never benchmarked
(dated snapshot, turbo/highspeed spin, regional supply alias) gets no score
rather than inheriting the base model's result.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ._creator_families import FAMILY_BRAND_KEYS


def normalize_name(value: Any) -> str:
    """Return a lowercase alphanumeric identity projection."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _creator_identities(creator: str) -> set[str]:
    """Expand a canonical creator id into its family brand identities.

    External datasets label creators by brand ("Kimi", "Qwen") while the
    canonical catalog uses the family id ("moonshot", "alibaba"); the brand
    table is the offline authority that bridges the two.
    """
    identities = {creator} - {""}
    for family, brands in FAMILY_BRAND_KEYS.items():
        keys = {normalize_name(family)} | {
            key for key in (normalize_name(brand) for brand in brands) if key
        }
        keys.discard("")
        if creator in keys:
            identities |= keys
    return identities


def match_model(
    model: Mapping[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match one canonical Model to one external row without guessing.

    Only an exact normalized model-id/display-name/leaf-name match scores;
    a prefix or substring relation never does, because the external source
    benchmarked exactly one model, not its family.  Ambiguity (more than one
    row claiming the same identity) produces no result.
    """
    model_id = model.get("model_id")
    names = {
        normalize_name(model_id),
        normalize_name(model.get("display_name")),
    }
    if isinstance(model_id, str) and "/" in model_id:
        names.add(normalize_name(model_id.rsplit("/", 1)[-1]))
    names.discard("")
    if not names:
        return None
    identities = _creator_identities(normalize_name(model.get("creator_id")))
    if identities and any(row.get("_creator_keys") for row in rows):
        rows = [
            row for row in rows
            if any(
                creator_key == identity
                or creator_key.startswith(identity)
                or identity.startswith(creator_key)
                for creator_key in set(row.get("_creator_keys") or ())
                for identity in identities
            )
        ]
    exact = [row for row in rows if set(row.get("_keys") or ()) & names]
    if len(exact) == 1:
        return exact[0]
    return None


__all__ = ["match_model", "normalize_name"]
