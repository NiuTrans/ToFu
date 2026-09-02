"""Bounded provider-neutral tool-result and typed-error contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal


ToolResultStatus = Literal["ok", "partial", "error"]


@dataclass(frozen=True)
class ToolResultErrorV2:
    code: str
    retryable: bool
    next_action: str
    message: str = ""


@dataclass(frozen=True)
class ToolResultEnvelopeV2:
    """Small model-visible result; large raw data lives behind artifactRef."""

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
    contract_version: str = field(default="tofu.tool-result/v2", init=False)

    def __post_init__(self) -> None:
        if len(self.items) > 64:
            raise ValueError("ToolResultEnvelopeV2.items may contain at most 64 items")
        if self.status == "error" and self.error is None:
            raise ValueError("error status requires a typed error")
        if self.status != "error" and self.error is not None:
            raise ValueError("typed error requires error status")
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
        return value

    def to_model_text(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))

    def with_visible_bytes(self) -> "ToolResultEnvelopeV2":
        """Return a copy whose self-described encoded size is exact."""
        value = self
        for _ in range(8):
            size = len(value.to_model_text().encode("utf-8"))
            if value.visible_bytes == size:
                return value
            value = replace(value, visible_bytes=size)
        return value

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
    """Parse only a canonical V2 typed error, failing open on legacy text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict) \
            or value.get("contractVersion") != "tofu.tool-result/v2" \
            or value.get("status") != "error":
        return None
    error = value.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("retryable"), bool):
        return None
    code = str(error.get("code") or "").strip()
    if not code:
        return None
    return ToolResultErrorV2(
        code=code[:96],
        retryable=error["retryable"],
        next_action=str(error.get("next_action") or "")[:300],
        message=str(error.get("message") or "")[:500],
    )


def nonretryable_tool_error_code(value: Any) -> str | None:
    """Return the stable code only for an explicit non-retryable V2 error."""
    error = tool_result_error(value)
    return error.code if error is not None and not error.retryable else None


__all__ = [
    "ToolResultEnvelopeV2", "ToolResultErrorV2",
    "nonretryable_tool_error_code", "tool_result_error", "typed_tool_error",
]
