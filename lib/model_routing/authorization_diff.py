"""Authorization-diff semantics shared by the access-matrix UI, apply
recommendations, and daily probe automation.

The UI mirrors this module inside ``access_matrix.js`` — keep the two in
lock-step. Everything here is pure function of
``(credentials, deployments, probe_evidence)``; no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lib.model_routing.wire_grants import (
    grant_allows_wire,
    grant_for,
    grant_key,
)

__all__ = [
    "ProposedAuthorization",
    "apply_proposed_authorizations",
    "build_cell_states",
    "derive_cell_state",
    "derive_proposed_authorizations",
    "project_count",
]

def _display_state(evidence: Mapping[str, Any] | None) -> str:
    if not evidence:
        return "unprobed"
    state = str(evidence.get("state") or "")
    if state == "ok":
        return "ok"
    if state == "stale-ok":
        return "stale-ok"
    if state in ("recommend-disable", "confirmed-disabled"):
        return "recommend-disable"
    return "pending"


def derive_cell_state(
    *,
    credential: Mapping[str, Any],
    wire_model_id: str,
    model_ref: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One matrix cell — a (credential × wire request name) pair. ``enabled``
    is derived from the model grant minus the wire exclusion; nothing
    per-cell is persisted.
    """
    grants = list((credential.get("authorization") or {}).get("models") or [])
    granted = grant_for(grants, model_ref)
    probe = _display_state(evidence)
    enabled = grant_allows_wire(granted, wire_model_id)
    return {
        "credential_id": credential.get("credential_id"),
        "wire_model_id": wire_model_id,
        "model_ref": dict(model_ref),
        "probe": probe,
        "enabled": enabled,
        "recommend_disable": probe == "recommend-disable",
        "recommend_enable": probe == "ok" and not enabled,
    }


def build_cell_states(
    *,
    credentials: Sequence[Mapping[str, Any]],
    deployments_by_model: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    evidence_by_wire: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """All cells, one per (credential × deployment wire) pair.
    ``deployments_by_model`` is keyed by ``(creator_id, model_id)`` of the
    deployment's offering's model; ``evidence_by_wire`` by wire id.
    """
    cells: list[dict[str, Any]] = []
    for credential in credentials:
        for ref_key, deployments in deployments_by_model.items():
            ref = {"creator_id": ref_key[0], "model_id": ref_key[1]}
            for deployment in deployments:
                wire = str(deployment.get("wire_model_id") or "")
                if not wire:
                    continue
                cells.append(derive_cell_state(
                    credential=credential,
                    wire_model_id=wire,
                    model_ref=ref,
                    evidence=evidence_by_wire.get(wire),
                ))
    return cells


# --- apply-recommendations projection -----------------------------------


@dataclass
class ProposedAuthorization:
    """The authorization.models a credential would have after applying the
    visible recommendations, plus whether that differs from today's."""

    credential_id: str
    models: list[dict[str, Any]] = field(default_factory=list)
    changed: bool = False


def _group_cells(cells: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    by_credential: dict[str, list[Mapping[str, Any]]] = {}
    for cell in cells:
        by_credential.setdefault(str(cell["credential_id"]), []).append(cell)
    return by_credential


def derive_proposed_authorizations(
    *,
    cells: Sequence[Mapping[str, Any]],
    current_grants_by_credential: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, ProposedAuthorization]:
    """Fold the visible cell states into a proposed authorization.models per
    credential. Symmetric: recommend-disable cells add exclusions (or drop the
    grant when nothing would remain); recommend-enable cells (probe ok but
    disabled) mint grants or lift exclusions.
    """
    out: dict[str, ProposedAuthorization] = {}
    for credential_id, cred_cells in _group_cells(cells).items():
        current = list(current_grants_by_credential.get(credential_id) or [])
        current_by_key = {grant_key(g): dict(g) for g in current}
        wires_by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for cell in cred_cells:
            wires_by_key.setdefault(grant_key(cell["model_ref"]), []).append(cell)

        proposed: list[dict[str, Any]] = []
        changed = False
        for key, existing in current_by_key.items():
            cred_wire_cells = wires_by_key.get(key, [])
            old_excluded = {str(w) for w in existing.get("excluded_wire_model_ids") or ()}
            excluded = set(old_excluded)
            for cell in cred_wire_cells:
                wire = str(cell["wire_model_id"])
                if cell["recommend_disable"]:
                    excluded.add(wire)
                elif cell["recommend_enable"]:
                    excluded.discard(wire)
            if excluded != old_excluded:
                changed = True
            # Drop the grant entirely only when every visible wire of this
            # model for this credential is recommend-disable.
            if cred_wire_cells and all(c["recommend_disable"] for c in cred_wire_cells):
                changed = True
                continue
            grant: dict[str, Any] = {"creator_id": key[0], "model_id": key[1]}
            if excluded:
                grant["excluded_wire_model_ids"] = sorted(excluded)
            proposed.append(grant)

        # Mint grants for recommend-enable cells whose model the credential
        # does not grant at all (zero reachable cells today).
        for key, cred_wire_cells in wires_by_key.items():
            if key in current_by_key:
                continue
            if not any(c["recommend_enable"] for c in cred_wire_cells):
                continue
            excluded = sorted(
                str(c["wire_model_id"]) for c in cred_wire_cells
                if c["recommend_disable"]
            )
            minted: dict[str, Any] = {"creator_id": key[0], "model_id": key[1]}
            if excluded:
                minted["excluded_wire_model_ids"] = excluded
            proposed.append(minted)
            changed = True

        out[credential_id] = ProposedAuthorization(
            credential_id=credential_id,
            models=proposed,
            changed=changed,
        )
    return out


def project_count(proposals: Mapping[str, ProposedAuthorization]) -> int:
    """Number of credentials whose authorization.models would change."""
    return sum(1 for p in proposals.values() if p.changed)


def apply_proposed_authorizations(
    *,
    credentials: Sequence[Mapping[str, Any]],
    proposals: Mapping[str, ProposedAuthorization],
) -> list[dict[str, Any]]:
    """Replace authorization.models for every changed credential; untouched
    credentials keep their grants. Returns full credential dicts (shallow
    copies) so the caller can diff/store them.
    """
    updated: list[dict[str, Any]] = []
    for credential in credentials:
        cid = str(credential.get("credential_id"))
        proposal = proposals.get(cid)
        if proposal is None or not proposal.changed:
            continue
        new_credential = dict(credential)
        authorization = dict(new_credential.get("authorization") or {})
        authorization["models"] = [dict(m) for m in proposal.models]
        new_credential["authorization"] = authorization
        updated.append(new_credential)
    return updated
