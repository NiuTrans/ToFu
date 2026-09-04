"""Conservative identity matching for external model-score datasets.

This module owns only name normalization and matching.  It never knows about
providers, wire identifiers, routes, or credentials.  External score sources
must prefer a missing score over attaching one model's benchmark to another.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


def normalize_name(value: Any) -> str:
    """Return a lowercase alphanumeric identity projection."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def match_model(
    model: Mapping[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match one canonical Model to one external row without guessing.

    Exact normalized model-id/display-name matches win.  A prefix match is
    accepted only for names of at least five characters, and equal-specificity
    ambiguity produces no result.
    """
    names = {
        normalize_name(model.get("model_id")),
        normalize_name(model.get("display_name")),
    } - {""}
    if not names:
        return None
    creator = normalize_name(model.get("creator_id"))
    if creator and any(row.get("_creator_keys") for row in rows):
        rows = [
            row for row in rows
            if any(
                creator_key == creator
                or creator_key.startswith(creator)
                or creator.startswith(creator_key)
                for creator_key in set(row.get("_creator_keys") or ())
            )
        ]
    exact = [row for row in rows if set(row.get("_keys") or ()) & names]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        for key in set(row.get("_keys") or ()):
            if len(key) >= 5 and any(
                key.startswith(name) or name.startswith(key) for name in names
            ):
                candidates.append((len(key), row))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if (
        len(candidates) > 1
        and candidates[0][0] == candidates[1][0]
        and candidates[0][1] is not candidates[1][1]
    ):
        return None
    return candidates[0][1]


__all__ = ["match_model", "normalize_name"]
