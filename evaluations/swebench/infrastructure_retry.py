"""Bounded, evidence-preserving infrastructure retry for formal Harbor runs.

Harbor's internal retry (``RetryConfig``) replaces a failed attempt in
place, erasing the failed attempt's evidence, so formal runs preregister
``maxRetries=0`` and instead retry infrastructure-classified failures at the
launcher level: the failed trial directory is archived whole (preserving
``exception.txt``, ``agent/``, ``verifier/`` and logs as evidence), and the
job is then resumed so harbor re-dispatches only the missing tasks under
fresh trial names. The comparison protocol preregisters the allowance
(``maxInfrastructureRetries``) and this classified failure set; anything
else fails closed for manual classification.

Failure classes are derived from tb21 v11 evidence:

- ``environment_start_timeout``: harbor's outer environment-build cap fired
  while the rootless-qemu prepared-image cache was still building.
- ``opencode_evidence_reconciliation``: proxy/CLI evidence projection
  refused (upstream retries, CLI death mid-turn).
- ``opencode_proxy_metrics_settle``: tagged proxy metrics were still
  flushing when the short settle window expired.
- ``opencode_cli_nonzero_exit``: the guest CLI crashed; no verifier ran.
- ``tofu_runtime_no_successful_terminal``: the production runtime ended
  without a successful terminal state, with a structured, retryable native
  network/rate_limit/server_error receipt. Context/compaction errors and legacy
  trials without this receipt are never inferred to be transient.

A trial carrying any numeric verifier reward is an agent outcome and is
never retried here.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifacts import atomic_write_json
from evaluations.native_failure import is_transient_provider_failure

ENVIRONMENT_START_TIMEOUT = "environment_start_timeout"
OPENCODE_EVIDENCE_RECONCILIATION = "opencode_evidence_reconciliation"
OPENCODE_PROXY_METRICS_SETTLE = "opencode_proxy_metrics_settle"
OPENCODE_CLI_NONZERO_EXIT = "opencode_cli_nonzero_exit"
TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL = "tofu_runtime_no_successful_terminal"

INFRASTRUCTURE_FAILURE_CLASSES: tuple[str, ...] = (
    ENVIRONMENT_START_TIMEOUT,
    OPENCODE_EVIDENCE_RECONCILIATION,
    OPENCODE_PROXY_METRICS_SETTLE,
    OPENCODE_CLI_NONZERO_EXIT,
    TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL,
)

_MESSAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("trial evidence could not be reconciled", OPENCODE_EVIDENCE_RECONCILIATION),
    ("proxy metrics did not settle", OPENCODE_PROXY_METRICS_SETTLE),
    ("OpenCode CLI exited nonzero", OPENCODE_CLI_NONZERO_EXIT),
    ("did not reach a successful terminal state",
     TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL),
)


@dataclass(frozen=True)
class InfrastructureFailure:
    task_name: str
    trial_name: str
    trial_dir: Path
    failure_class: str
    exception_type: str

    def receipt_row(self, archived_to: Path) -> dict[str, str]:
        return {
            "taskName": self.task_name,
            "trialName": self.trial_name,
            "failureClass": self.failure_class,
            "exceptionType": self.exception_type,
            "archivedTo": str(archived_to),
        }


def classify_infrastructure_failure(result: dict[str, Any]) -> str | None:
    """Classify one trial result; ``None`` means not infrastructure-retryable."""
    verifier = result.get("verifier_result")
    if isinstance(verifier, dict) and verifier.get("rewards"):
        # Structured metrics are outcomes too. Malformed nonempty rewards need
        # evidence repair, never automatic replacement with a fresh attempt.
        return None
    exception = result.get("exception_info")
    if not isinstance(exception, dict):
        return None
    exception_type = str(exception.get("exception_type") or "")
    message = str(exception.get("exception_message") or "")
    if exception_type == "EnvironmentStartTimeoutError":
        return ENVIRONMENT_START_TIMEOUT
    if exception_type != "RuntimeError":
        return None
    for marker, failure_class in _MESSAGE_MARKERS:
        if marker in message:
            if failure_class == TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL:
                agent = result.get("agent_result")
                metadata = agent.get("metadata") if isinstance(agent, dict) else None
                native = metadata.get("tofuKimiEvidence") if isinstance(metadata, dict) else None
                if not isinstance(native, dict):
                    return None
                if not is_transient_provider_failure(native.get("runtimeFailure")):
                    return None
            return failure_class
    return None


def _numeric_reward(result: dict[str, Any]) -> float | None:
    verifier = result.get("verifier_result")
    if not isinstance(verifier, dict):
        return None
    rewards = verifier.get("rewards")
    if not isinstance(rewards, dict):
        return None
    reward = rewards.get("reward")
    return float(reward) if isinstance(reward, (int, float)) else None


def scan_infrastructure_failures(job_dir: Path) -> list[InfrastructureFailure]:
    """Find completed trials whose failure is infrastructure-classified.

    Trial directories without ``result.json`` are skipped: harbor's native
    resume removes and re-dispatches them, so they need no archival.
    Unreadable results and unclassified exceptions stay untouched — they
    fail closed for manual classification instead of being silently retried.
    """
    failures: list[InfrastructureFailure] = []
    for trial_dir in sorted(job_dir.iterdir()):
        if not trial_dir.is_dir():
            continue
        result_path = trial_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _numeric_reward(result) is not None:
            # A scored trial is an agent outcome, never an infrastructure retry.
            continue
        failure_class = classify_infrastructure_failure(result)
        if failure_class is None:
            continue
        exception = result.get("exception_info") or {}
        task_name = str(result.get("task_name") or trial_dir.name)
        failures.append(InfrastructureFailure(
            task_name=task_name,
            trial_name=str(result.get("trial_name") or trial_dir.name),
            trial_dir=trial_dir,
            failure_class=failure_class,
            exception_type=str(exception.get("exception_type") or ""),
        ))
    return failures


def archive_infrastructure_failures(
    failures: list[InfrastructureFailure],
    *,
    archive_root: Path,
    attempt_index: int,
) -> list[dict[str, str]]:
    """Move failed trial directories into the per-attempt archive, intact."""
    if attempt_index < 1:
        raise ValueError("attempt_index is 1-based")
    if not failures:
        return []
    archive_dir = archive_root / f"attempt-{attempt_index}"
    if archive_dir.exists():
        raise FileExistsError(f"archive attempt directory exists: {archive_dir}")
    archive_dir.mkdir(mode=0o700, parents=True)
    receipts: list[dict[str, str]] = []
    for failure in failures:
        destination = archive_dir / failure.trial_dir.name
        if destination.exists():
            raise FileExistsError(f"archive destination exists: {destination}")
        shutil.move(str(failure.trial_dir), str(destination))
        receipts.append(failure.receipt_row(destination))
    return receipts


def infrastructure_retry_cycle(
    run_dir: Path,
    *,
    attempt_index: int,
    max_infrastructure_retries: int,
    resume: Callable[[Path, tuple[str, ...]], int],
) -> dict[str, Any]:
    """Archive infrastructure failures once, then resume the run if any.

    ``resume`` receives the run directory and the archived task names so the
    launcher can prove the retry only re-dispatches archived infrastructure
    failures. Returns a receipt describing the cycle; ``dispatched`` is True
    when a resume was launched. The caller loops this cycle while it keeps
    archiving new failures, up to ``max_infrastructure_retries``.
    """
    run_dir = Path(run_dir)
    if max_infrastructure_retries < 0:
        raise ValueError("max_infrastructure_retries must not be negative")
    if attempt_index < 1 or attempt_index > max_infrastructure_retries:
        raise ValueError(
            f"infrastructure retry attempt {attempt_index} exceeds the "
            f"preregistered allowance {max_infrastructure_retries}")
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (run_dir / 'comparison-registration.json').exists():
        from .comparison import validate_registration
        config = json.loads((run_dir / 'job-config.json').read_text())
        protocol = validate_registration(run_dir, manifest, config)
        if protocol is None or max_infrastructure_retries != protocol.get('maxInfrastructureRetries', 0):
            raise ValueError('infrastructure retry allowance differs from frozen protocol')
    run_id = str(manifest["run_id"])
    job_dir = run_dir / "jobs" / run_id
    failures = scan_infrastructure_failures(job_dir)
    receipt: dict[str, Any] = {
        "schema": "tofu.infrastructure-retry/v1",
        "runId": run_id,
        "attemptIndex": attempt_index,
        "maxInfrastructureRetries": max_infrastructure_retries,
        "classifiedFailures": [
            failure.receipt_row(failure.trial_dir) for failure in failures
        ],
        "archived": [],
        "dispatched": False,
        "exitCode": None,
    }
    if not failures:
        _write_receipt(run_dir, attempt_index, receipt)
        return receipt
    receipts = archive_infrastructure_failures(
        failures,
        archive_root=run_dir / "infrastructure-attempts",
        attempt_index=attempt_index,
    )
    receipt["archived"] = receipts
    receipt["dispatched"] = True
    _write_receipt(run_dir, attempt_index, receipt)
    exit_code = resume(run_dir, tuple(
        failure.task_name for failure in failures))
    receipt["exitCode"] = exit_code
    _write_receipt(run_dir, attempt_index, receipt)
    return receipt


def _write_receipt(run_dir: Path, attempt_index: int, receipt: dict) -> None:
    atomic_write_json(
        run_dir / f"infrastructure-retry-{attempt_index}.json", receipt)


__all__ = [
    "ENVIRONMENT_START_TIMEOUT",
    "INFRASTRUCTURE_FAILURE_CLASSES",
    "InfrastructureFailure",
    "OPENCODE_CLI_NONZERO_EXIT",
    "OPENCODE_EVIDENCE_RECONCILIATION",
    "OPENCODE_PROXY_METRICS_SETTLE",
    "TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL",
    "archive_infrastructure_failures",
    "classify_infrastructure_failure",
    "infrastructure_retry_cycle",
    "scan_infrastructure_failures",
]
