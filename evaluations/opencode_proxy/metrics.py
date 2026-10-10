"""Per-trial metrics reconciliation for the OpenCode benchmark proxy."""

from __future__ import annotations

from typing import Any

from evaluations.codex_kimi_proxy.codex_contract import (
    CodexContractError,
    _metrics_rows,
    validate_trial_token,
)


def _usage_free_upstream_failure(row: dict[str, Any]) -> bool:
    """One tagged upstream failure that carries no billable usage.

    The OpenCode CLI retries failed upstream requests internally; each failed
    attempt appears as a tagged proxy row with an empty/zero usage block and
    no finish reasons (observed in tb21 v11: ``status="upstream_error"`` after
    ~150ms, same requestDigest retried). Such rows cannot break token
    reconciliation — there is nothing to account for on either side — so they
    stay auditable but must not invalidate the whole trial. Rows carrying any
    usage, finish reasons, a client disconnect, or an invalid-trial flag are
    accounting-relevant and remain strictly invalidating.
    """
    if row.get("status") in (None, "completed"):
        return False
    if row.get("clientDisconnected") is True or row.get("invalidTrial"):
        return False
    if row.get("finishReasons"):
        return False
    usage = row.get("usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        return False
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if int(usage.get(key) or 0):
            return False
    return True


def validate_opencode_proxy_metrics(
    path: str,
    *,
    expected_trial_token: str,
) -> dict[str, Any]:
    """Reconcile one trial's tagged proxy rows and aggregate exact usage."""

    token = validate_trial_token(expected_trial_token)
    records = [
        row for row in _metrics_rows(path)
        if row.get("trialToken") == token
    ]
    if not records:
        raise CodexContractError(
            "opencode proxy metrics do not contain the requested trial")
    completions = [row for row in records
                   if row.get("event") == "chatCompletion"]
    violations = [row for row in records
                  if row.get("event") in {"invalidTrialHeader",
                                          "modelPinViolation"}]
    upstream_failures = [row for row in completions
                         if _usage_free_upstream_failure(row)]
    accounted = [row for row in completions
                 if not _usage_free_upstream_failure(row)]
    upstream_calls = sum(int(row.get("upstreamCalls") or 0)
                         for row in completions)
    usage_totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
    }
    for row in completions:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        usage_totals["completion_tokens"] += int(
            usage.get("completion_tokens") or 0)
        usage_totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            usage_totals["reasoning_tokens"] += int(
                details.get("reasoning_tokens") or 0)
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            usage_totals["cache_read_tokens"] += int(
                prompt_details.get("cached_tokens") or 0)
        usage_totals["cache_read_tokens"] += int(
            usage.get("cache_read_tokens") or 0)
    valid = bool(
        accounted
        and not violations
        and upstream_calls == len(completions)
        and not any(row.get("invalidTrial") for row in completions)
        and all(
            row.get("status") == "completed"
            and row.get("clientDisconnected") is not True
            for row in accounted
        )
    )
    return {
        "valid": valid,
        "chatCompletions": len(completions),
        "accountedChatCompletions": len(accounted),
        "upstreamFailures": len(upstream_failures),
        "upstreamFailureStatuses": sorted(
            {str(row.get("status")) for row in upstream_failures}),
        "upstreamCalls": upstream_calls,
        "violations": len(violations),
        "usage": usage_totals,
        "proxyCpuNs": sum(int(row.get("proxyCpuNs") or 0)
                          for row in completions),
        "rawWallNs": sum(int(row.get("rawWallNs") or 0)
                         for row in completions),
        "upstreamWallNs": sum(int(row.get("upstreamWallNs") or 0)
                              for row in completions),
        "model": next(
            (str(row.get("model")) for row in completions if row.get("model")),
            "",
        ),
    }


__all__ = ["validate_opencode_proxy_metrics"]
