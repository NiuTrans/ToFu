"""Shared progress/no-progress ledger for every agent runner.

Progress is structural: a changed world version, new evidence, or successful
verification resets the streak. Non-empty assistant prose is never proof of
progress or completion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


def _call_signature(tool_calls: Iterable[Any]) -> str:
    normalized: list[dict[str, Any]] = []
    for call in tool_calls or ():
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        function = function if isinstance(function, dict) else call
        arguments = function.get("arguments", call.get("arguments", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                pass
        normalized.append({
            "name": str(function.get("name") or call.get("name") or ""),
            "arguments": arguments,
        })
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ProgressLedgerV2:
    """Bounded in-memory detector keyed by calls, world state, and evidence."""

    maximum_evidence_ids: int = 2048
    last_call_signature: str = ""
    last_world_version: str = ""
    no_progress_streak: int = 0
    observed_evidence_ids: set[str] = field(default_factory=set)
    last_nonretryable_failure_signature: str = ""
    nonretryable_failure_streak: int = 0

    def observe(self, tool_calls: Iterable[Any], *, world_version: str = "",
                evidence_ids: Iterable[str] = (),
                verification: str = "") -> dict[str, Any]:
        signature = _call_signature(tool_calls)
        incoming = {str(value) for value in evidence_ids if str(value)}
        new_evidence = sorted(incoming - self.observed_evidence_ids)
        world = str(world_version or "")
        verified = str(verification or "").lower() in {
            "passed", "success", "verified", "ok"}
        same_calls = bool(signature and signature == self.last_call_signature)
        same_world = world == self.last_world_version
        stalled = same_calls and same_world and not new_evidence and not verified
        self.no_progress_streak = self.no_progress_streak + 1 if stalled else 0
        self.last_call_signature = signature
        self.last_world_version = world
        if new_evidence:
            self.observed_evidence_ids.update(new_evidence)
            if len(self.observed_evidence_ids) > self.maximum_evidence_ids:
                self.observed_evidence_ids = set(sorted(
                    self.observed_evidence_ids)[-self.maximum_evidence_ids:])
        return {
            "contractVersion": "tofu.progress-ledger/v2",
            "progress": not stalled,
            "reason": (
                "verification_passed" if verified else
                "new_evidence" if new_evidence else
                "world_changed" if not same_world else
                "calls_changed" if not same_calls else
                "same_calls_world_no_evidence"),
            "noProgressStreak": self.no_progress_streak,
            "newEvidenceIds": new_evidence,
            "worldVersion": world,
            "callSignature": signature[:24],
        }

    def observe_nonretryable_failures(
        self,
        failure_signatures: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Count rounds where every tool ended in the same terminal failure.

        Call arguments are deliberately absent: a model that changes a tab ID,
        path, or selector cannot turn the same explicit ``retryable=false``
        capability denial into progress. Unknown, legacy, mixed-success, and
        retryable result rounds pass an empty iterable and reset the streak.
        """
        normalized = sorted({
            str(value).strip() for value in failure_signatures
            if str(value).strip()
        })
        payload = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":"))
        signature = (
            hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if normalized else ""
        )
        same_failure = bool(
            signature and signature == self.last_nonretryable_failure_signature)
        if not signature:
            self.nonretryable_failure_streak = 0
        elif same_failure:
            self.nonretryable_failure_streak += 1
        else:
            self.nonretryable_failure_streak = 1
        self.last_nonretryable_failure_signature = signature
        return {
            "contractVersion": "tofu.progress-ledger/v2",
            "progress": not signature,
            "reason": (
                "round_not_terminal_failure" if not signature else
                "same_nonretryable_failure" if same_failure else
                "nonretryable_failure_changed"
            ),
            "nonretryableFailureStreak": self.nonretryable_failure_streak,
            "failureSignature": signature[:24],
        }


__all__ = ["ProgressLedgerV2"]
