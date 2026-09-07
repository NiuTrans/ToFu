"""Bounded tool-result evidence and sparse model-delivery contracts.

``ToolResultEnvelopeV2`` is the internal semantic/evidence record used by
budgeting, replay guards, diagnostics, and evaluation.  ``to_model_text`` is
the only model projection: complete text successes become plain text, complete
structured values retain only their semantic value, and partial/errors carry
only actionable non-empty fields.  ``split_tool_result_delivery`` keeps the
small evidence sidecar out of ``role='tool'`` content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal, Mapping


ToolResultStatus = Literal["ok", "partial", "error"]
ToolResultRecoveryKind = Literal["source"]

TOOL_RESULT_CONTRACT_VERSION = "tofu.tool-result/v2"
TOOL_RESULT_EVIDENCE_CONTRACT_VERSION = "tofu.tool-result-evidence/v1"


def _compact_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _model_projection(value: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(model_text, projection_kind)`` for one internal envelope."""
    status = str(value.get("status") or "ok")
    summary = str(value.get("summary") or "")
    raw_items = value.get("items")
    items = list(raw_items) if isinstance(raw_items, list) else []

    if status == "ok" and not bool(value.get("truncated")):
        if not items:
            return summary, "text"
        if summary == "Tool returned a structured result." and len(items) == 1:
            return _compact_json(items[0]), "json_value"
        if summary == f"Tool returned {len(items)} items.":
            return _compact_json(items), "json_value"
        payload: dict[str, Any] = {}
        if summary:
            payload["summary"] = summary
        if items:
            payload["items"] = items
        return _compact_json(payload), "summary_items"

    if status == "error":
        error = value.get("error")
        error = error if isinstance(error, Mapping) else {}
        payload = {"status": "error"}
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        next_action = str(error.get("next_action") or "").strip()
        if code:
            payload["code"] = code
        if message:
            payload["message"] = message
        if isinstance(error.get("retryable"), bool):
            payload["retryable"] = error["retryable"]
        if next_action:
            payload["nextAction"] = next_action
        if summary and summary not in {code, message}:
            payload["summary"] = summary
        return _compact_json(payload), "error"

    payload = {"status": "partial"}
    if summary:
        payload["summary"] = summary
    if items:
        payload["items"] = items
    artifact_ref = str(value.get("artifactRef") or "")
    cursor = str(value.get("cursor") or "")
    if artifact_ref:
        payload["artifactRef"] = artifact_ref
    if cursor:
        payload["cursor"] = cursor
    recovery = value.get("recovery")
    if isinstance(recovery, Mapping):
        payload["recovery"] = dict(recovery)
    if bool(value.get("truncated")):
        payload["truncated"] = True
    return _compact_json(payload), "partial"


@dataclass(frozen=True)
class ToolResultDelivery:
    """One model-visible result plus its non-semantic internal evidence."""

    model_text: str
    evidence: Mapping[str, Any] | None = None


def _evidence_projection(
    value: Mapping[str, Any], *, model_text: str, envelope_text: str,
    projection_kind: str,
) -> dict[str, Any]:
    freshness = value.get("freshness")
    freshness = freshness if isinstance(freshness, Mapping) else {}
    evidence: dict[str, Any] = {
        "contractVersion": TOOL_RESULT_EVIDENCE_CONTRACT_VERSION,
        "resultContractVersion": TOOL_RESULT_CONTRACT_VERSION,
        "status": str(value.get("status") or "ok"),
        "projectionKind": projection_kind,
        "truncated": bool(value.get("truncated")),
        "rawBytes": _nonnegative_int(value.get("rawBytes")),
        "visibleBytes": len(model_text.encode("utf-8", errors="replace")),
        "envelopeBytes": len(envelope_text.encode("utf-8", errors="replace")),
        "evidenceId": str(value.get("evidenceId") or ""),
    }
    artifact_ref = str(value.get("artifactRef") or "")
    cursor = str(value.get("cursor") or "")
    if artifact_ref:
        evidence["artifactRef"] = artifact_ref
    if cursor:
        evidence["cursor"] = cursor
    observed_at_ms = _nonnegative_int(freshness.get("observedAtMs"))
    world_version = str(freshness.get("worldVersion") or "")
    if observed_at_ms or world_version:
        evidence["freshness"] = {
            "observedAtMs": observed_at_ms,
            "worldVersion": world_version,
        }
    error = value.get("error")
    if isinstance(error, Mapping):
        evidence["error"] = dict(error)
    return evidence


def split_tool_result_delivery(content: Any) -> ToolResultDelivery:
    """Split an internal V2 envelope from the sparse text sent to the model.

    Legacy/non-envelope values pass through byte-for-byte and have no sidecar.
    """
    envelope_text = (
        content if isinstance(content, str) else _compact_json(content))
    try:
        value = json.loads(envelope_text)
    except (TypeError, ValueError):
        return ToolResultDelivery(model_text=envelope_text)
    if not isinstance(value, dict) \
            or value.get("contractVersion") != TOOL_RESULT_CONTRACT_VERSION:
        return ToolResultDelivery(model_text=envelope_text)
    model_text, projection_kind = _model_projection(value)
    return ToolResultDelivery(
        model_text=model_text,
        evidence=_evidence_projection(
            value, model_text=model_text, envelope_text=envelope_text,
            projection_kind=projection_kind),
    )


def model_text_from_tool_result(content: Any) -> str:
    """Return exactly the text that belongs in a model tool-result message."""
    return split_tool_result_delivery(content).model_text


def sparse_result_items(value: Any) -> list[Any] | None:
    """Return the items of a V2 envelope or its sparse model projection.

    ``_model_projection``'s ``summary_items`` kind intentionally drops
    ``contractVersion``, and that sparse text (``{"summary", "items"}``) is
    what tool rounds persist as ``toolContent``. Readers recovering a
    structured payload from persisted content must accept BOTH shapes —
    gating on the marker alone loses every sparse recording. Returns
    ``None`` when *value* is not one of the two envelope shapes (e.g. a bare
    payload that carries its own fields at the top level, or an envelope of
    a different contract).
    """
    if not isinstance(value, dict):
        return None
    items = value.get("items")
    if not isinstance(items, list):
        return None
    marker = value.get("contractVersion")
    if marker == TOOL_RESULT_CONTRACT_VERSION:
        return items
    if marker is None and isinstance(value.get("summary"), str):
        return items
    return None


@dataclass(frozen=True)
class ToolResultErrorV2:
    code: str
    retryable: bool
    next_action: str
    message: str = ""


@dataclass(frozen=True)
class ToolResultRecoveryV2:
    """Bounded machine-readable way to reconstruct omitted source evidence."""

    kind: ToolResultRecoveryKind
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind != "source":
            raise ValueError("ToolResultRecoveryV2.kind must be source")
        if not str(self.tool or "").strip():
            raise ValueError("ToolResultRecoveryV2.tool is required")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("ToolResultRecoveryV2.arguments must be an object")


@dataclass(frozen=True)
class ToolResultEnvelopeV2:
    """Bounded internal result record with source-aware recovery evidence."""

    status: ToolResultStatus
    summary: str
    items: tuple[Any, ...] = ()
    artifact_ref: str = ""
    cursor: str = ""
    truncated: bool = False
    raw_bytes: int = 0
    visible_bytes: int = 0
    observed_at_ms: int = 0
    world_version: str = ""
    evidence_id: str = ""
    error: ToolResultErrorV2 | None = None
    recovery: ToolResultRecoveryV2 | None = None
    contract_version: str = field(
        default=TOOL_RESULT_CONTRACT_VERSION, init=False)

    def __post_init__(self) -> None:
        if len(self.items) > 64:
            raise ValueError("ToolResultEnvelopeV2.items may contain at most 64 items")
        if self.status == "error" and self.error is None:
            raise ValueError("error status requires a typed error")
        if self.status != "error" and self.error is not None:
            raise ValueError("typed error requires error status")
        if self.status == "ok" and self.truncated:
            raise ValueError("truncated results must use partial status")
        if self.raw_bytes < 0 or self.visible_bytes < 0:
            raise ValueError("byte counts must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "contractVersion": self.contract_version,
            "status": self.status,
            "summary": self.summary,
            "items": list(self.items),
            "artifactRef": self.artifact_ref,
            "cursor": self.cursor,
            "truncated": self.truncated,
            "rawBytes": self.raw_bytes,
            "visibleBytes": self.visible_bytes,
            "freshness": {
                "observedAtMs": self.observed_at_ms,
                "worldVersion": self.world_version,
            },
            "evidenceId": self.evidence_id,
        }
        value["error"] = asdict(self.error) if self.error else None
        # Preserve the byte-stable legacy envelope when no source recovery is
        # needed. Artifact recovery remains represented by artifactRef/cursor.
        if self.recovery is not None:
            value["recovery"] = asdict(self.recovery)
        return value

    def to_envelope_text(self) -> str:
        """Serialize the complete internal evidence envelope."""
        return _compact_json(self.to_dict())

    def to_model_text(self) -> str:
        """Serialize only the sparse semantic projection visible to the model."""
        return _model_projection(self.to_dict())[0]

    def with_visible_bytes(self) -> "ToolResultEnvelopeV2":
        """Return a copy whose model-visible UTF-8 size is exact."""
        size = len(self.to_model_text().encode("utf-8", errors="replace"))
        return self if self.visible_bytes == size else replace(
            self, visible_bytes=size)

    @classmethod
    def from_legacy(cls, content: Any, *, status: ToolResultStatus = "ok",
                    observed_at_ms: int = 0, world_version: str = "",
                    evidence_id: str = "") -> "ToolResultEnvelopeV2":
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        raw = content.encode("utf-8", errors="replace")
        evidence = evidence_id or "ev_" + hashlib.sha256(raw).hexdigest()[:24]
        return cls(
            status=status,
            summary=content,
            raw_bytes=len(raw),
            visible_bytes=len(raw),
            observed_at_ms=max(0, int(observed_at_ms or 0)),
            world_version=str(world_version or ""),
            evidence_id=evidence,
        ).with_visible_bytes()


def typed_tool_error(code: str, *, retryable: bool, next_action: str,
                     message: str = "") -> ToolResultEnvelopeV2:
    error = ToolResultErrorV2(
        code=str(code or "tool_error")[:96],
        retryable=bool(retryable),
        next_action=str(next_action or "Stop and report the failure.")[:300],
        message=str(message or "")[:500],
    )
    return ToolResultEnvelopeV2(
        status="error", summary=error.message or error.code,
        error=error).with_visible_bytes()


def tool_result_error(value: Any) -> ToolResultErrorV2 | None:
    """Parse a canonical envelope or sparse typed model error.

    Unstructured legacy error prose still fails open.  Sparse errors require a
    real boolean ``retryable`` field plus a stable code, so arbitrary text or a
    loosely shaped JSON error cannot trip terminal-failure policy.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict) or value.get("status") != "error":
        return None
    if value.get("contractVersion") == TOOL_RESULT_CONTRACT_VERSION:
        error = value.get("error")
    elif "code" in value and (
            "nextAction" in value or "next_action" in value):
        error = value
    else:
        return None
    if not isinstance(error, dict) or not isinstance(error.get("retryable"), bool):
        return None
    code = str(error.get("code") or "").strip()
    if not code:
        return None
    return ToolResultErrorV2(
        code=code[:96],
        retryable=error["retryable"],
        next_action=str(
            error.get("next_action") or error.get("nextAction") or "")[:300],
        message=str(error.get("message") or "")[:500],
    )


def tool_result_observation(
    content: Any, evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reconstruct one server-side V2 observation without bloating model text.

    Legacy rows may still carry the complete V2 envelope in ``content``.  New
    rows pair sparse model content with ``tofu.tool-result-evidence/v1``.
    """
    content_text = content if isinstance(content, str) else _compact_json(content)
    try:
        parsed = json.loads(content_text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) \
            and parsed.get("contractVersion") == TOOL_RESULT_CONTRACT_VERSION:
        return parsed
    if not isinstance(evidence, Mapping) or evidence.get(
            "contractVersion") != TOOL_RESULT_EVIDENCE_CONTRACT_VERSION:
        return None

    status = str(evidence.get("status") or "ok")
    observation: dict[str, Any] = {
        "contractVersion": TOOL_RESULT_CONTRACT_VERSION,
        "status": status,
        "summary": "",
        "items": [],
        "artifactRef": str(evidence.get("artifactRef") or ""),
        "cursor": str(evidence.get("cursor") or ""),
        "truncated": bool(evidence.get("truncated")),
        "rawBytes": _nonnegative_int(evidence.get("rawBytes")),
        "visibleBytes": _nonnegative_int(evidence.get("visibleBytes")),
        "freshness": (
            dict(evidence.get("freshness"))
            if isinstance(evidence.get("freshness"), Mapping) else {}),
        "evidenceId": str(evidence.get("evidenceId") or ""),
        "error": (
            dict(evidence.get("error"))
            if isinstance(evidence.get("error"), Mapping) else None),
    }
    projection_kind = str(evidence.get("projectionKind") or "text")
    if projection_kind == "text":
        observation["summary"] = content_text
        return observation
    if projection_kind == "json_value":
        if isinstance(parsed, list):
            observation["summary"] = f"Tool returned {len(parsed)} items."
            observation["items"] = parsed
        elif isinstance(parsed, dict):
            observation["summary"] = "Tool returned a structured result."
            observation["items"] = [parsed]
        else:
            observation["summary"] = content_text
        return observation
    if not isinstance(parsed, dict):
        observation["summary"] = content_text
        return observation

    if projection_kind == "error":
        error = {
            "code": str(parsed.get("code") or ""),
            "retryable": parsed.get("retryable"),
            "next_action": str(
                parsed.get("nextAction") or parsed.get("next_action") or ""),
            "message": str(parsed.get("message") or ""),
        }
        if error["code"] and isinstance(error["retryable"], bool):
            observation["error"] = error
        observation["summary"] = str(
            parsed.get("summary") or error["message"] or error["code"])
        return observation

    observation["summary"] = str(parsed.get("summary") or "")
    if isinstance(parsed.get("items"), list):
        observation["items"] = parsed["items"]
    if isinstance(parsed.get("recovery"), dict):
        observation["recovery"] = parsed["recovery"]
    observation["artifactRef"] = str(
        parsed.get("artifactRef") or observation["artifactRef"])
    observation["cursor"] = str(
        parsed.get("cursor") or observation["cursor"])
    return observation


def nonretryable_tool_error_code(value: Any) -> str | None:
    """Return the stable code only for an explicit non-retryable V2 error."""
    error = tool_result_error(value)
    return error.code if error is not None and not error.retryable else None


__all__ = [
    "TOOL_RESULT_CONTRACT_VERSION", "TOOL_RESULT_EVIDENCE_CONTRACT_VERSION",
    "ToolResultDelivery", "ToolResultEnvelopeV2", "ToolResultErrorV2",
    "ToolResultRecoveryV2", "model_text_from_tool_result",
    "nonretryable_tool_error_code", "sparse_result_items",
    "split_tool_result_delivery",
    "tool_result_error", "tool_result_observation", "typed_tool_error",
]
