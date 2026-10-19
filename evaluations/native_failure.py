"""Secret-free native terminal failure receipts for diagnostics and retry admission.

Only fixed error categories and an explicit retryability flag cross this boundary.
Raw messages remain in the private original evidence. Ambiguous runtime failure
is never evidence of a transient provider failure.
"""
from __future__ import annotations


def native_failure_receipt(error: object) -> dict:
    if not isinstance(error, dict):
        return {}
    kind = error.get("kind")
    known = {"prompt_too_long", "internal", "network", "rate_limit",
             "server_error", "timeout", "auth", "invalid_request", "cancelled"}
    receipt = {"kind": kind if isinstance(kind, str) and kind in known else "unknown",
               "retryable": error.get("retryable") is True}
    # This is diagnostic attribution, never a predicate granting a retry.
    raw = error.get("raw")
    if isinstance(raw, str) and "summary_budget_exhausted" in raw:
        receipt["reason"] = "context_compaction_budget_exhausted"
    return receipt


def is_transient_provider_failure(receipt: object) -> bool:
    return isinstance(receipt, dict) and receipt.get("retryable") is True \
        and isinstance(receipt.get("kind"), str) \
        and receipt["kind"] in {"network", "rate_limit", "server_error"}
