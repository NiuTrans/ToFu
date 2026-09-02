"""Build the bounded, machine-readable result attached to a compaction archive.

The continuation summary and the audit result have different consumers.  The
summary is model-visible working state; this receipt is user-visible metadata
for the archive/API/UI.  Keep it small, versioned, and free of transcript or
summary-body duplication so richer inspection never grows the next prompt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math


COMPACTION_RECEIPT_SCHEMA_VERSION = "tofu.compaction-receipt/v1"
MAX_RECEIPT_RECENT_FILES = 8


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _short_text(value, maximum: int = 160) -> str:
    return str(value or "").strip()[:maximum]


def _duration_ms(value) -> int:
    try:
        numeric = float(value or 0)
        return _count(round(numeric)) if math.isfinite(numeric) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def summary_usage_details(usage: Mapping | None) -> dict:
    """Normalize provider-specific summary usage into one finite contract."""
    from lib.cost import normalize_usage, split_input_tokens

    raw = dict(usage) if isinstance(usage, Mapping) else {}
    normalized = normalize_usage(raw)
    _uncached, input_tokens = split_input_tokens(raw)
    output_tokens = _count(normalized.get("output"))
    return {
        "inputTokens": _count(input_tokens),
        "outputTokens": output_tokens,
        "totalTokens": _count(input_tokens) + output_tokens,
        "cacheReadTokens": _count(normalized.get("cache_read")),
        "cacheWriteTokens": _count(normalized.get("cache_write")),
    }


def _economics_details(economics: Mapping | None) -> dict:
    if not isinstance(economics, Mapping):
        return {}
    from lib.cost import normalize_usage

    normalized_usage = normalize_usage(dict(economics))
    payback = economics.get("payback_rounds")
    try:
        payback_value = float(payback)
    except (TypeError, ValueError, OverflowError):
        payback_value = math.inf
    details = {
        "droppedTokens": _count(economics.get("dropped_tokens")),
        "cacheReadTokens": _count(normalized_usage.get("cache_read")),
        "cacheRewriteTokens": _count(economics.get("cache_rewrite_tokens")),
        "summaryCostTokens": _count(economics.get("summary_cost_tokens")),
        # JSON/JSONB rejects Infinity.  ``null`` means there is no finite
        # break-even estimate (normally because per-round savings are zero).
        "paybackRounds": round(payback_value, 3)
        if math.isfinite(payback_value) else None,
        "pricingSource": _short_text(economics.get("pricing_source"), 80),
    }
    try:
        payback_limit = float(economics.get("payback_limit_rounds"))
    except (TypeError, ValueError, OverflowError):
        payback_limit = math.inf
    if math.isfinite(payback_limit) and payback_limit >= 0:
        details["paybackLimitRounds"] = round(payback_limit, 3)
    payback_policy = _short_text(economics.get("payback_policy"), 80)
    if payback_policy:
        details["paybackPolicy"] = payback_policy
    return details


def _recent_files(values: Iterable | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        path = _short_text(value, 512)
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
        if len(result) >= MAX_RECEIPT_RECENT_FILES:
            break
    return result


def build_compaction_receipt(
    *,
    trigger: str,
    status: str,
    strategy: str,
    implementation: str,
    mode: str = "",
    continuation_format: str = "none",
    summary_generated: bool = False,
    summary_text: str = "",
    summary_usage: Mapping | None = None,
    summary_duration_ms: int | float = 0,
    projected_summary_usage_tokens: int = 0,
    summary_rejected: bool = False,
    summary_rejection_reason: str = "",
    summarized_messages: int = 0,
    preserved_turns: int = 0,
    folded_tool_rounds: int = 0,
    objective_anchored: bool = False,
    retained_user_messages: int = 0,
    recent_files: Iterable | None = None,
    turn_diff_included: bool = False,
    economics: Mapping | None = None,
    evidence_retained: Iterable | None = None,
    evidence_lost: Iterable | None = None,
    reconcile_attempts: int = 0,
    stripped_images: int = 0,
    truncated_chars: int = 0,
    dropped_messages: int = 0,
    wire_bytes_before: int = 0,
    wire_bytes_after: int = 0,
    outcome_reason: str = "",
) -> dict:
    """Return one bounded receipt shared by automatic/manual/recovery paths."""
    receipt = {
        "schemaVersion": COMPACTION_RECEIPT_SCHEMA_VERSION,
        "status": _short_text(status, 32) or "pending",
        "trigger": _short_text(trigger, 32) or "force",
        "strategy": _short_text(strategy, 64) or "pending",
        "implementation": _short_text(implementation, 80) or "pending",
        "continuation": {
            "format": _short_text(continuation_format, 64) or "none",
        },
    }
    if mode:
        receipt["mode"] = _short_text(mode, 64)
    if outcome_reason:
        receipt["outcomeReason"] = _short_text(outcome_reason, 200)

    usage = summary_usage_details(summary_usage)
    receipt["summary"] = {
        "generated": bool(summary_generated),
        "accepted": bool(summary_generated and not summary_rejected),
        "chars": len(str(summary_text or "")),
        "durationMs": _duration_ms(summary_duration_ms),
        "usage": usage,
        "projectedUsageTokens": _count(projected_summary_usage_tokens),
    }
    if summary_rejected:
        receipt["summary"]["rejectionReason"] = (
            _short_text(summary_rejection_reason, 160) or "rejected"
        )

    receipt["retention"] = {
        "summarizedMessages": _count(summarized_messages),
        "preservedTurns": _count(preserved_turns),
        "foldedToolRounds": _count(folded_tool_rounds),
        "objectiveAnchored": bool(objective_anchored),
        "retainedUserMessages": _count(retained_user_messages),
        "recentFiles": _recent_files(recent_files),
        "turnDiffIncluded": bool(turn_diff_included),
    }

    economic_details = _economics_details(economics)
    if economic_details:
        receipt["economics"] = economic_details

    retained_count = len(list(evidence_retained or ()))
    lost_count = len(list(evidence_lost or ()))
    if retained_count or lost_count:
        receipt["evidence"] = {
            "retainedCount": retained_count,
            "lostCount": lost_count,
        }
    if reconcile_attempts:
        receipt["execution"] = {
            "reconcileAttempts": _count(reconcile_attempts),
        }
    recovery_values = {
        "strippedImages": _count(stripped_images),
        "truncatedChars": _count(truncated_chars),
        "droppedMessages": _count(dropped_messages),
        "wireBytesBefore": _count(wire_bytes_before),
        "wireBytesAfter": _count(wire_bytes_after),
    }
    if any(recovery_values.values()):
        receipt["recovery"] = recovery_values
    return receipt


def pending_compaction_receipt(trigger: str) -> dict:
    """Receipt written with the pre-compaction snapshot before work settles."""
    return build_compaction_receipt(
        trigger=trigger,
        status="pending",
        strategy="pending",
        implementation="pending",
    )


__all__ = [
    "COMPACTION_RECEIPT_SCHEMA_VERSION",
    "MAX_RECEIPT_RECENT_FILES",
    "build_compaction_receipt",
    "pending_compaction_receipt",
    "summary_usage_details",
]
