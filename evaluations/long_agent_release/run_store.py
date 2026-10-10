"""Immutable per-arm evidence store and paired-run completeness audit.

Runners place raw artifacts through :func:`store_run_artifact`, then commit one
validated task record through :func:`record_release_task`.  Task records are
content-bound to the immutable manifest and cannot be replaced.  Final JSONL
is assembled in manifest order only after every oracle is resolved and every
artifact still matches its digest.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from evaluations.codex_kimi_proxy.codex_contract import validate_proxy_metrics
from lib.benchmark_contract import (
    CONTRACT_VERSION_V2,
    BenchmarkContractError,
    public_price_cost_from_usage,
    validate_record,
)


RUN_STORE_CONTRACT = "tofu-long-agent-release-run/v1"
ATTEMPT_LEDGER_CONTRACT = "tofu-long-agent-release-attempt/v1"
_ARTIFACT_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ATTEMPT_FILE = re.compile(r"^([0-9]+)-(started|terminal)\.json$")
_EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_RUNNER_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_RAW_TRAJECTORY = "raw_trajectory"
_PROXY_METRICS = "proxy_metrics"
_FAILED_RUNTIME_EVIDENCE = "failed_attempt_runtime_evidence"
_TOFU_RUNTIME_EVIDENCE_CONTRACT = "tofu.agent-runtime-evidence/v1"


class ReleaseRunError(ValueError):
    """A run artifact is mutable, incomplete, or not bound to its manifest."""


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseRunError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ReleaseRunError(f"JSON contains non-finite number {value}")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseRunError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRunError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseRunError(f"{label} must be a JSON object")
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    try:
        rendered = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReleaseRunError("record is not canonical JSON") from exc
    return (rendered + "\n").encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _private_run_root(path: Path, *, create: bool) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ReleaseRunError("run root must not be a symlink")
    if create:
        expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root = expanded.resolve(strict=True)
    except OSError as exc:
        raise ReleaseRunError(f"run root is missing: {path}") from exc
    if not root.is_dir() or root.is_symlink() or root.stat().st_mode & 0o077:
        raise ReleaseRunError("run root must be a mode-0700 private directory")
    return root


def _private_child_directory(root: Path, name: str, *, create: bool) -> Path:
    path = root / name
    if path.is_symlink():
        raise ReleaseRunError(f"run {name} directory must not be a symlink")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseRunError(f"run {name} directory is missing") from exc
    if resolved.parent != root or not resolved.is_dir() \
            or resolved.stat().st_mode & 0o077:
        raise ReleaseRunError(
            f"run {name} directory must be private and path-confined")
    return resolved


@contextmanager
def _run_lock(root: Path) -> Iterator[None]:
    path = root / ".run.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReleaseRunError("run lock is not a safe regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReleaseRunError("run lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_immutable(path: Path, payload: bytes) -> str:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ReleaseRunError(f"immutable output is not a regular file: {path}")
        if path.read_bytes() == payload:
            return "unchanged"
        raise ReleaseRunError(f"refusing to replace immutable output: {path}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o400)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
    return "created"


def _validate_manifest(manifest: dict[str, Any]) -> None:
    try:
        validate_record(manifest)
    except BenchmarkContractError as exc:
        raise ReleaseRunError(f"invalid v2 manifest: {exc}") from exc
    if manifest.get("contractVersion") != CONTRACT_VERSION_V2 \
            or manifest.get("recordType") != "manifest":
        raise ReleaseRunError("run store requires a tofu-benchmark/v2 manifest")
    if not manifest.get("pairId") or manifest.get("comparisonRole") not in {
            "baseline", "candidate"}:
        raise ReleaseRunError("run manifest must identify its pair and role")


def _manifest(root: Path) -> dict[str, Any]:
    manifest = _load_json(root / "manifest.json", "run manifest")
    _validate_manifest(manifest)
    return manifest


def load_release_record(path: Path) -> dict[str, Any]:
    """Read one strict JSON manifest or task record for CLI ingestion."""
    return _load_json(path.expanduser(), "release record")


def load_release_manifest(run_root: Path) -> dict[str, Any]:
    """Load the validated immutable manifest owned by one release run."""
    root = _private_run_root(run_root, create=False)
    return _manifest(root)


def load_release_task_records(
    run_root: Path, *, require_complete: bool = True,
) -> list[dict[str, Any]]:
    """Load manifest-ordered, fully revalidated task evidence."""

    root = _private_run_root(run_root, create=False)
    audit = audit_release_run(root, require_complete=require_complete)
    if not audit["valid"] or require_complete and not audit["complete"]:
        raise ReleaseRunError(
            "release task records are not complete and valid: "
            + str(audit["errors"])
        )
    manifest = _manifest(root)
    task_root = _private_child_directory(root, "tasks", create=False)
    records: list[dict[str, Any]] = []
    for row in manifest["tasks"]:
        path = task_root / f"{_task_key(str(row['taskId']))}.json"
        if not path.is_file():
            if require_complete:
                raise ReleaseRunError(
                    f"release task record is missing: {row['taskId']}"
                )
            continue
        record = _load_json(path, f"task record {row['taskId']}")
        _validate_task_binding(root, manifest, record)
        records.append(record)
    return records


def initialize_release_run(run_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Create or resume one private run store with an immutable manifest."""
    _validate_manifest(manifest)
    root = _private_run_root(run_root, create=True)
    _private_child_directory(root, "tasks", create=True)
    _private_child_directory(root, "artifacts", create=True)
    _private_child_directory(root, "attempts", create=True)
    with _run_lock(root):
        status = _write_immutable(root / "manifest.json", _canonical_bytes(manifest))
    return {
        "contractVersion": RUN_STORE_CONTRACT,
        "status": status,
        "runId": manifest["runId"],
        "pairId": manifest["pairId"],
        "comparisonRole": manifest["comparisonRole"],
        "expectedTasks": len(manifest["tasks"]),
        "runRoot": str(root),
    }


def _task_key(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _task_rows(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["taskId"]): row for row in manifest["tasks"]}


def _requires_attempt_ledger(manifest: dict[str, Any]) -> bool:
    return (manifest.get("datasetSnapshot") or {}).get("releaseMatrix") is True


def _assert_run_open(root: Path) -> None:
    if (root / "run.jsonl").exists():
        raise ReleaseRunError("finalized release runs are immutable")


def _attempt_task_directory(
    root: Path, task_id: str, *, create: bool,
) -> Path | None:
    if not create and not (root / "attempts").exists():
        return None
    attempts_root = _private_child_directory(root, "attempts", create=create)
    path = attempts_root / _task_key(task_id)
    if not create and not path.exists():
        return None
    return _private_child_directory(
        attempts_root, _task_key(task_id), create=create)


def _attempt_event_path(
    root: Path, task_id: str, attempt_index: int, event_type: str,
) -> Path:
    task_root = _attempt_task_directory(root, task_id, create=True)
    assert task_root is not None
    return task_root / f"{attempt_index:04d}-{event_type}.json"


def _attempt_event(
    *, manifest: dict[str, Any], task_id: str, attempt_index: int,
    event_type: str, execution_id: str, runner_kind: str,
    occurred_at: int,
) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION_V2,
        "recordType": "attempt",
        "attemptContractVersion": ATTEMPT_LEDGER_CONTRACT,
        "runId": manifest["runId"],
        "taskId": task_id,
        "attemptIndex": attempt_index,
        "eventType": event_type,
        "executionId": execution_id,
        "runnerKind": runner_kind,
        "occurredAt": occurred_at,
    }


def _validate_attempt_event(
    event: dict[str, Any], *, manifest: dict[str, Any], task_id: str,
    filename: str,
) -> None:
    try:
        validate_record(event)
    except BenchmarkContractError as exc:
        raise ReleaseRunError(f"invalid attempt event: {exc}") from exc
    match = _ATTEMPT_FILE.fullmatch(filename)
    if match is None:
        raise ReleaseRunError(f"unexpected attempt event filename: {filename}")
    attempt_index = event.get("attemptIndex")
    occurred_at = event.get("occurredAt")
    if event.get("contractVersion") != CONTRACT_VERSION_V2 \
            or event.get("recordType") != "attempt" \
            or event.get("attemptContractVersion") != ATTEMPT_LEDGER_CONTRACT:
        raise ReleaseRunError("attempt event contract is invalid")
    if event.get("runId") != manifest["runId"] \
            or event.get("taskId") != task_id:
        raise ReleaseRunError("attempt event is not bound to its manifest task")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) \
            or attempt_index < 1 \
            or attempt_index != int(match.group(1)):
        raise ReleaseRunError("attempt event index does not match its filename")
    if event.get("eventType") != match.group(2):
        raise ReleaseRunError("attempt event type does not match its filename")
    if not _EXECUTION_ID.fullmatch(str(event.get("executionId") or "")):
        raise ReleaseRunError("attempt executionId is invalid")
    if not _RUNNER_KIND.fullmatch(str(event.get("runnerKind") or "")):
        raise ReleaseRunError("attempt runnerKind is invalid")
    if isinstance(occurred_at, bool) or not isinstance(occurred_at, int) \
            or occurred_at < 0:
        raise ReleaseRunError("attempt occurredAt is invalid")
    common_fields = {
        "contractVersion", "recordType", "attemptContractVersion", "runId",
        "taskId", "attemptIndex", "eventType", "executionId", "runnerKind",
        "occurredAt",
    }
    if event["eventType"] == "started":
        if set(event) != common_fields:
            raise ReleaseRunError("attempt start contains unsupported fields")
        if event.get("outcome") is not None:
            raise ReleaseRunError("attempt start cannot contain an outcome")
        return
    outcome = event.get("outcome")
    code = str(event.get("code") or "")
    if outcome == "oracle_ready":
        if set(event) != common_fields | {
                "outcome", "code", "taskRecordSha256",
                "taskStartedAtUnixMs"}:
            raise ReleaseRunError(
                "oracle-ready attempt contains unsupported fields")
        digest = str(event.get("taskRecordSha256") or "")
        if code != "oracle_ready" or len(digest) != 64 \
                or any(char not in "0123456789abcdef" for char in digest):
            raise ReleaseRunError("oracle-ready attempt terminal is invalid")
    elif outcome != "infrastructure_failed" \
            or not _FAILURE_CODE.fullmatch(code):
        raise ReleaseRunError("attempt terminal outcome is invalid")
    else:
        if set(event) != common_fields | {
            "outcome", "code", "modelUsages", "paidToolCostUsd",
            "noPaidCalls", "artifacts", "taskStartedAtUnixMs",
        }:
            raise ReleaseRunError(
                "infrastructure attempt contains unsupported fields")
        usages = event.get("modelUsages")
        artifacts = event.get("artifacts")
        no_paid_calls = event.get("noPaidCalls")
        if not isinstance(usages, list) or any(
                not isinstance(usage, dict) for usage in usages):
            raise ReleaseRunError("attempt failure modelUsages are invalid")
        if not isinstance(artifacts, list) or any(
                not isinstance(artifact, dict) for artifact in artifacts):
            raise ReleaseRunError("attempt failure artifacts are invalid")
        try:
            paid_tool_cost = float(event.get("paidToolCostUsd"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReleaseRunError(
                "attempt failure paidToolCostUsd is invalid") from exc
        if not math.isfinite(paid_tool_cost) or paid_tool_cost < 0 \
                or not isinstance(no_paid_calls, bool):
            raise ReleaseRunError("attempt failure cost evidence is invalid")
        if no_paid_calls and (usages or paid_tool_cost != 0):
            raise ReleaseRunError("noPaidCalls contradicts attempt failure usage")
        if not no_paid_calls and not (usages or artifacts or paid_tool_cost > 0):
            raise ReleaseRunError(
                "post-dispatch attempt failure requires retained evidence")
    task_started_at = event.get("taskStartedAtUnixMs")
    if isinstance(task_started_at, bool) or not isinstance(task_started_at, int) \
            or task_started_at < 0 or task_started_at > occurred_at:
        raise ReleaseRunError("attempt taskStartedAtUnixMs is invalid")


def _attempt_state(
    root: Path, manifest: dict[str, Any], task_id: str,
) -> dict[str, Any]:
    task_root = _attempt_task_directory(root, task_id, create=False)
    starts: dict[int, dict[str, Any]] = {}
    terminals: dict[int, dict[str, Any]] = {}
    if task_root is not None:
        for path in sorted(task_root.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                raise ReleaseRunError("attempt ledger contains a non-regular entry")
            event = _load_json(path, f"attempt event {task_id}/{path.name}")
            _validate_attempt_event(
                event, manifest=manifest, task_id=task_id,
                filename=path.name,
            )
            destination = starts if event["eventType"] == "started" else terminals
            index = int(event["attemptIndex"])
            if index in destination:
                raise ReleaseRunError("attempt ledger contains a duplicate event")
            destination[index] = event
    indices = sorted(starts)
    if indices != list(range(1, len(indices) + 1)):
        raise ReleaseRunError("attempt start indices must be contiguous")
    if set(terminals) - set(starts):
        raise ReleaseRunError("attempt terminal has no matching start")
    maximum = int(manifest["retryRule"]["maxInfrastructureRetries"]) + 1
    if len(indices) > maximum:
        raise ReleaseRunError("attempt ledger exceeds the preregistered retry limit")
    failures: list[dict[str, Any]] = []
    success: dict[str, Any] | None = None
    active: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    for index in indices:
        started = starts[index]
        events.append(started)
        terminal = terminals.get(index)
        if terminal is None:
            if index != indices[-1]:
                raise ReleaseRunError("a later attempt follows an open attempt")
            active = started
            continue
        events.append(terminal)
        if terminal["executionId"] != started["executionId"] \
                or terminal["runnerKind"] != started["runnerKind"] \
                or terminal["occurredAt"] < started["occurredAt"] \
                or terminal["taskStartedAtUnixMs"] < started["occurredAt"]:
            raise ReleaseRunError("attempt terminal does not match its start")
        if terminal["outcome"] == "oracle_ready":
            if success is not None or index != indices[-1]:
                raise ReleaseRunError("oracle-ready attempt must be the final attempt")
            success = terminal
        else:
            failures.append(terminal)
    return {
        "starts": starts,
        "terminals": terminals,
        "events": events,
        "failures": failures,
        "success": success,
        "active": active,
    }


def _failure_retry_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "retryIndex": int(event["attemptIndex"]),
        "failureClass": "infrastructure",
        "code": str(event["code"]),
        "modelUsages": list(event["modelUsages"]),
        "paidToolCostUsd": float(event["paidToolCostUsd"]),
        "noPaidCalls": bool(event["noPaidCalls"]),
        "artifacts": list(event["artifacts"]),
        "taskStartedAtUnixMs": int(event["taskStartedAtUnixMs"]),
    }


def claim_release_task_attempts(
    run_root: Path, *, task_ids: list[str] | tuple[str, ...],
    execution_id: str, runner_kind: str,
) -> dict[str, Any]:
    """Preclaim task attempts before a runner can make a paid call."""

    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    _assert_run_open(root)
    identifiers = [str(task_id) for task_id in task_ids]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ReleaseRunError("attempt claim task IDs must be unique and non-empty")
    unknown = sorted(set(identifiers) - set(_task_rows(manifest)))
    if unknown:
        raise ReleaseRunError(f"attempt claim contains unknown tasks: {unknown[:3]}")
    if not _EXECUTION_ID.fullmatch(str(execution_id or "")):
        raise ReleaseRunError("attempt claim executionId is invalid")
    if not _RUNNER_KIND.fullmatch(str(runner_kind or "")):
        raise ReleaseRunError("attempt claim runnerKind is invalid")
    statuses: list[dict[str, Any]] = []
    occurred_at = int(time.time() * 1000)
    with _run_lock(root):
        states: dict[str, dict[str, Any]] = {}
        for task_id in identifiers:
            state = _attempt_state(root, manifest, task_id)
            states[task_id] = state
            if state["success"] is not None:
                raise ReleaseRunError(f"task is already oracle-ready: {task_id}")
            active = state["active"]
            if active is not None and active["executionId"] != execution_id:
                raise ReleaseRunError(
                    f"task has an open attempt in another execution: {task_id}")
            if active is None and any(
                    row["executionId"] == execution_id
                    for row in state["starts"].values()):
                raise ReleaseRunError(
                    f"executionId cannot be reused for a later attempt: {task_id}")
            next_index = len(state["starts"]) + 1
            maximum = int(
                manifest["retryRule"]["maxInfrastructureRetries"]
            ) + 1
            if active is None and next_index > maximum:
                raise ReleaseRunError(
                    f"task exhausted its preregistered attempts: {task_id}")
        for task_id in identifiers:
            state = states[task_id]
            active = state["active"]
            if active is not None:
                statuses.append({
                    "taskId": task_id,
                    "attemptIndex": active["attemptIndex"],
                    "status": "unchanged",
                    "occurredAt": active["occurredAt"],
                })
                continue
            attempt_index = len(state["starts"]) + 1
            event = _attempt_event(
                manifest=manifest, task_id=task_id,
                attempt_index=attempt_index, event_type="started",
                execution_id=execution_id, runner_kind=runner_kind,
                occurred_at=occurred_at,
            )
            status = _write_immutable(
                _attempt_event_path(
                    root, task_id, attempt_index, "started"),
                _canonical_bytes(event),
            )
            statuses.append({
                "taskId": task_id,
                "attemptIndex": attempt_index,
                "status": status,
                "occurredAt": occurred_at,
            })
    return {
        "contractVersion": ATTEMPT_LEDGER_CONTRACT,
        "runId": manifest["runId"],
        "executionId": execution_id,
        "runnerKind": runner_kind,
        "claims": statuses,
    }


def fail_release_task_attempt(
    run_root: Path, *, task_id: str, execution_id: str, code: str,
    model_usages: list[dict[str, Any]], paid_tool_cost_usd: float,
    artifacts: list[dict[str, Any]], no_paid_calls: bool,
    task_started_at_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Close one active attempt while retaining failed-call evidence."""

    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    _assert_run_open(root)
    task_id = str(task_id)
    if task_id not in _task_rows(manifest):
        raise ReleaseRunError(f"attempt failure task is not in manifest: {task_id}")
    if not _FAILURE_CODE.fullmatch(str(code or "")):
        raise ReleaseRunError("attempt failure code is invalid")
    with _run_lock(root):
        state = _attempt_state(root, manifest, task_id)
        active = state["active"]
        if active is None or active["executionId"] != execution_id:
            raise ReleaseRunError("attempt failure requires its active execution")
        for artifact in artifacts:
            _artifact_path(root, task_id, artifact)
        if task_started_at_unix_ms is None:
            if not no_paid_calls:
                raise ReleaseRunError(
                    "post-dispatch failure requires taskStartedAtUnixMs")
            task_started_at_unix_ms = int(active["occurredAt"])
        event = {
            **_attempt_event(
                manifest=manifest, task_id=task_id,
                attempt_index=int(active["attemptIndex"]),
                event_type="terminal", execution_id=execution_id,
                runner_kind=str(active["runnerKind"]),
                occurred_at=int(time.time() * 1000),
            ),
            "outcome": "infrastructure_failed",
            "code": str(code),
            "modelUsages": list(model_usages),
            "paidToolCostUsd": float(paid_tool_cost_usd),
            "noPaidCalls": no_paid_calls,
            "artifacts": list(artifacts),
            "taskStartedAtUnixMs": task_started_at_unix_ms,
        }
        _validate_attempt_event(
            event, manifest=manifest, task_id=task_id,
            filename=f"{int(active['attemptIndex']):04d}-terminal.json",
        )
        _validate_failed_attempt_artifacts(root, manifest, task_id, event)
        status = _write_immutable(
            _attempt_event_path(
                root, task_id, int(active["attemptIndex"]), "terminal"),
            _canonical_bytes(event),
        )
    return {
        "contractVersion": ATTEMPT_LEDGER_CONTRACT,
        "runId": manifest["runId"],
        "taskId": task_id,
        "attemptIndex": active["attemptIndex"],
        "status": status,
        "outcome": "infrastructure_failed",
    }


def fail_release_execution_before_dispatch(
    run_root: Path, *, execution_id: str, code: str,
) -> dict[str, Any]:
    """Close every claim in an execution proven to have made no paid call."""

    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    _assert_run_open(root)
    if not _EXECUTION_ID.fullmatch(str(execution_id or "")) \
            or not _FAILURE_CODE.fullmatch(str(code or "")):
        raise ReleaseRunError("pre-dispatch execution failure identity is invalid")
    closed: list[dict[str, Any]] = []
    with _run_lock(root):
        active_rows: list[tuple[str, dict[str, Any]]] = []
        for task_id in _task_rows(manifest):
            state = _attempt_state(root, manifest, task_id)
            active = state["active"]
            if active is not None and active["executionId"] == execution_id:
                active_rows.append((task_id, active))
        if not active_rows:
            raise ReleaseRunError(
                "pre-dispatch execution has no active release attempts")
        occurred_at = int(time.time() * 1000)
        for task_id, active in active_rows:
            event = {
                **_attempt_event(
                    manifest=manifest, task_id=task_id,
                    attempt_index=int(active["attemptIndex"]),
                    event_type="terminal", execution_id=execution_id,
                    runner_kind=str(active["runnerKind"]),
                    occurred_at=occurred_at,
                ),
                "outcome": "infrastructure_failed",
                "code": str(code),
                "modelUsages": [],
                "paidToolCostUsd": 0.0,
                "noPaidCalls": True,
                "artifacts": [],
                "taskStartedAtUnixMs": int(active["occurredAt"]),
            }
            status = _write_immutable(
                _attempt_event_path(
                    root, task_id, int(active["attemptIndex"]), "terminal"),
                _canonical_bytes(event),
            )
            closed.append({
                "taskId": task_id,
                "attemptIndex": active["attemptIndex"],
                "status": status,
            })
    return {
        "contractVersion": ATTEMPT_LEDGER_CONTRACT,
        "runId": manifest["runId"],
        "executionId": execution_id,
        "outcome": "infrastructure_failed",
        "noPaidCalls": True,
        "closed": closed,
    }


def release_task_retry_evidence(
    run_root: Path, *, task_id: str,
) -> list[dict[str, Any]]:
    """Return immutable failed-attempt rows for the eventual task record."""

    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    if task_id not in _task_rows(manifest):
        raise ReleaseRunError(f"retry evidence task is not in manifest: {task_id}")
    state = _attempt_state(root, manifest, task_id)
    return [_failure_retry_row(event) for event in state["failures"]]


def validate_release_attempt_execution(
    run_root: Path, *, execution_id: str,
    task_ids: list[str] | tuple[str, ...], allow_oracle_ready: bool = False,
) -> dict[str, Any]:
    """Prove an external execution owns every listed task attempt."""

    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    identifiers = [str(task_id) for task_id in task_ids]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ReleaseRunError("attempt execution task IDs are invalid")
    claims: list[dict[str, Any]] = []
    for task_id in identifiers:
        if task_id not in _task_rows(manifest):
            raise ReleaseRunError(
                f"attempt execution task is not in manifest: {task_id}")
        state = _attempt_state(root, manifest, task_id)
        event = state["active"]
        status = "active"
        if event is None and allow_oracle_ready and state["success"] is not None:
            event = state["starts"][int(state["success"]["attemptIndex"])]
            status = "oracle_ready"
        if event is None or event["executionId"] != execution_id:
            raise ReleaseRunError(
                f"execution does not own the current task attempt: {task_id}")
        claims.append({
            "taskId": task_id,
            "attemptIndex": event["attemptIndex"],
            "occurredAt": event["occurredAt"],
            "status": status,
        })
    return {
        "contractVersion": ATTEMPT_LEDGER_CONTRACT,
        "runId": manifest["runId"],
        "executionId": execution_id,
        "claims": claims,
    }


def store_run_artifact(
    run_root: Path,
    *,
    task_id: str,
    kind: str,
    source: Path,
) -> dict[str, Any]:
    """Copy one artifact into a content-addressed, task-scoped run path."""
    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    if task_id not in _task_rows(manifest):
        raise ReleaseRunError(f"artifact task is not in manifest: {task_id}")
    if not _ARTIFACT_KIND.fullmatch(kind):
        raise ReleaseRunError("artifact kind must be lowercase snake_case")
    source = source.expanduser()
    if source.is_symlink() or not source.is_file():
        raise ReleaseRunError("artifact source must be a regular non-symlink file")
    limits = manifest["artifactLimits"]
    if not 0 < source.stat().st_size <= limits["maximumArtifactBytes"]:
        raise ReleaseRunError("artifact exceeds its preregistered byte limit")
    digest = hashlib.sha256()
    size = 0
    artifacts_root = _private_child_directory(root, "artifacts", create=False)
    task_root = _private_child_directory(
        artifacts_root, _task_key(task_id), create=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{kind}.", suffix=".partial", dir=task_root)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                if size > limits["maximumArtifactBytes"]:
                    raise ReleaseRunError(
                        "artifact exceeds its preregistered byte limit")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.chmod(0o600)
        sha256 = digest.hexdigest()
        destination = task_root / f"{kind}-{sha256}.artifact"
        with _run_lock(root):
            existing_delta = 0 if destination.exists() else size
            task_bytes = sum(
                path.stat().st_size for path in task_root.glob("*.artifact")
                if path.is_file() and not path.is_symlink())
            run_bytes = sum(
                path.stat().st_size
                for path in (root / "artifacts").glob("*/*.artifact")
                if path.is_file() and not path.is_symlink())
            if task_bytes + existing_delta > limits["maximumTaskArtifactBytes"]:
                raise ReleaseRunError(
                    "task artifacts exceed their preregistered byte limit")
            if run_bytes + existing_delta > limits["maximumRunArtifactBytes"]:
                raise ReleaseRunError(
                    "run artifacts exceed their preregistered byte limit")
            if destination.exists():
                if destination.is_symlink() or not destination.is_file() \
                        or destination.stat().st_size != size \
                        or _file_sha256(destination) != sha256:
                    raise ReleaseRunError("content-addressed artifact collision")
            else:
                os.replace(temporary, destination)
                destination.chmod(0o400)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return {
        "kind": kind,
        "path": destination.relative_to(root).as_posix(),
        "sha256": sha256,
        "bytes": size,
    }


def _artifact_path(root: Path, task_id: str, artifact: dict[str, Any]) -> Path:
    kind = artifact.get("kind")
    digest = artifact.get("sha256")
    size = artifact.get("bytes")
    relative = artifact.get("path")
    if not isinstance(kind, str) or not _ARTIFACT_KIND.fullmatch(kind):
        raise ReleaseRunError("task artifact kind is invalid")
    if not isinstance(digest, str) or len(digest) != 64 \
            or any(char not in "0123456789abcdef" for char in digest):
        raise ReleaseRunError("task artifact sha256 is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseRunError("task artifact bytes is invalid")
    expected = (
        Path("artifacts") / _task_key(task_id) /
        f"{kind}-{digest}.artifact"
    )
    if not isinstance(relative, str) or Path(relative) != expected:
        raise ReleaseRunError("task artifact path is not content-addressed")
    path = root / expected
    current = root
    for part in expected.parts:
        current /= part
        if current.is_symlink():
            raise ReleaseRunError("task artifact path traverses a symlink")
    if not path.is_file() or path.stat().st_size != size:
        raise ReleaseRunError("task artifact is missing or has changed size")
    if _file_sha256(path) != digest:
        raise ReleaseRunError("task artifact digest mismatch")
    return path


def _validate_retries(record: dict[str, Any], manifest: dict[str, Any]) -> None:
    retries = record.get("retries") or []
    maximum = int(manifest["retryRule"]["maxInfrastructureRetries"])
    if len(retries) > maximum:
        raise ReleaseRunError("task exceeds preregistered infrastructure retries")
    for index, retry in enumerate(retries, 1):
        if retry.get("retryIndex") != index \
                or retry.get("failureClass") != "infrastructure" \
                or not str(retry.get("code") or ""):
            raise ReleaseRunError(
                "task retries must be ordered, typed infrastructure failures")


def _validate_jsonl_artifact(path: Path, label: str) -> None:
    rows = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(
                    line, object_pairs_hook=_object_pairs,
                    parse_constant=_invalid_constant)
                if not isinstance(value, dict):
                    raise ReleaseRunError(
                        f"{label} line {line_number} must be a JSON object")
                rows += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRunError(f"{label} is not valid UTF-8 JSONL") from exc
    if rows == 0:
        raise ReleaseRunError(f"{label} must contain at least one event")


def _canonical_failed_usage(value: Any, label: str) -> dict[str, int]:
    """Validate one failed-call usage row under the release price contract."""
    if not isinstance(value, dict):
        raise ReleaseRunError(f"{label} must be an object")
    numeric_keys = {
        "prompt_tokens", "input_tokens", "completion_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "cached_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        "reasoning_tokens", "thinking_tokens", "total_tokens",
    }
    for key in numeric_keys:
        if key not in value:
            continue
        amount = value[key]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ReleaseRunError(
                f"{label}.{key} must be a non-negative integer")
    for parent, child in (
        ("prompt_tokens_details", "cached_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    ):
        details = value.get(parent)
        if details is None:
            continue
        if not isinstance(details, dict):
            raise ReleaseRunError(f"{label}.{parent} must be an object")
        if child in details:
            amount = details[child]
            if isinstance(amount, bool) or not isinstance(amount, int) \
                    or amount < 0:
                raise ReleaseRunError(
                    f"{label}.{parent}.{child} must be a non-negative integer")
    from lib.cost import normalize_usage, split_input_tokens

    normalized = normalize_usage(value)
    uncached, total_input = split_input_tokens(value)
    if any(amount < 0 for amount in normalized.values()) \
            or uncached < 0 or total_input < 0:
        raise ReleaseRunError(f"{label} has an invalid cache convention")
    output = int(normalized["output"])
    reasoning = int(normalized["thinking"])
    if output > 0 and reasoning > output:
        raise ReleaseRunError(f"{label} reasoning exceeds output tokens")
    return {
        "prompt_tokens": int(total_input),
        "completion_tokens": output,
        "cache_read_tokens": int(normalized["cache_read"]),
        "cache_write_tokens": int(normalized["cache_write"]),
        "reasoning_tokens": reasoning,
        "total_tokens": int(total_input) + output,
    }


def _failed_attempt_proxy_usages(path: Path) -> list[dict[str, Any]]:
    usages: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                row = json.loads(
                    line, object_pairs_hook=_object_pairs,
                    parse_constant=_invalid_constant,
                )
                if not isinstance(row, dict):
                    raise ReleaseRunError(
                        "failed proxy metrics rows must be objects")
                if row.get("event") != "responsesTranslation":
                    continue
                usage = row.get("usage")
                if not isinstance(usage, dict):
                    raise ReleaseRunError(
                        f"failed proxy metrics line {line_number} lacks usage")
                usages.append(usage)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRunError(
            "failed attempt proxy metrics are not valid JSONL") from exc
    return usages


def _validate_failed_attempt_artifacts(
    root: Path, manifest: dict[str, Any], task_id: str,
    failure: dict[str, Any],
) -> None:
    paths: dict[str, list[Path]] = {}
    for artifact in failure["artifacts"]:
        path = _artifact_path(root, task_id, artifact)
        paths.setdefault(str(artifact.get("kind") or ""), []).append(path)
    if failure["noPaidCalls"]:
        return
    trajectories = paths.get("failed_attempt_trajectory", [])
    if len(trajectories) != 1:
        raise ReleaseRunError(
            "post-dispatch failure requires one failed_attempt_trajectory")
    _validate_jsonl_artifact(
        trajectories[0], "failed_attempt_trajectory artifact")
    if manifest["comparisonRole"] == "candidate":
        runtime_paths = paths.get(_FAILED_RUNTIME_EVIDENCE, [])
        if len(runtime_paths) != 1:
            raise ReleaseRunError(
                "Tofu failed calls require one "
                "failed_attempt_runtime_evidence artifact")
        evidence = _load_json(
            runtime_paths[0], "failed Tofu runtime evidence")
        if evidence.get("contractVersion") \
                != _TOFU_RUNTIME_EVIDENCE_CONTRACT \
                or evidence.get("model") != "kimi-k3":
            raise ReleaseRunError(
                "failed Tofu runtime evidence contract/model is invalid")
        evidence_usages: list[dict[str, Any]] = []
        api_rounds = evidence.get("apiRounds")
        if not isinstance(api_rounds, list):
            raise ReleaseRunError(
                "failed Tofu runtime evidence lacks API rounds")
        canonical_usages: list[dict[str, int]] = []
        for index, api_round in enumerate(api_rounds, 1):
            usage = api_round.get("usage") \
                if isinstance(api_round, dict) else None
            if not isinstance(usage, dict):
                raise ReleaseRunError(
                    f"failed Tofu API round {index} lacks usage")
            evidence_usages.append(usage)
            canonical_usages.append(_canonical_failed_usage(
                usage, f"failed Tofu API round {index} usage"))
        compaction = evidence.get("compactionUsage")
        if compaction not in (None, {}):
            if not isinstance(compaction, dict):
                raise ReleaseRunError(
                    "failed Tofu compaction usage is invalid")
            raw_calls = compaction.get("n_calls", 0)
            if isinstance(raw_calls, bool) or not isinstance(raw_calls, int) \
                    or raw_calls < 0:
                raise ReleaseRunError(
                    "failed Tofu compaction call count is invalid")
            canonical_compaction = _canonical_failed_usage(
                compaction, "failed Tofu compaction usage")
            if raw_calls:
                evidence_usages.append(compaction)
                canonical_usages.append(canonical_compaction)
            elif any(canonical_compaction.values()):
                raise ReleaseRunError(
                    "failed Tofu compaction tokens require a model call")
        aggregate = {
            key: sum(row[key] for row in canonical_usages)
            for key in (
                "prompt_tokens", "completion_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens", "total_tokens",
            )
        }
        if _canonical_failed_usage(
                evidence.get("usage"), "failed Tofu aggregate usage") \
                != aggregate:
            raise ReleaseRunError(
                "failed Tofu aggregate usage differs from billed calls")
        if evidence_usages != failure["modelUsages"]:
            raise ReleaseRunError(
                "failed Tofu model usages differ from runtime evidence")
    if manifest["comparisonRole"] != "baseline" \
            or not failure["modelUsages"]:
        return
    metrics_paths = paths.get("failed_attempt_proxy_metrics", [])
    if len(metrics_paths) != 1:
        raise ReleaseRunError(
            "Codex failed calls require one failed_attempt_proxy_metrics artifact")
    try:
        metrics = validate_proxy_metrics(
            str(metrics_paths[0]),
            expected_request_count=len(failure["modelUsages"]),
            require_trial_token=True,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseRunError(
            "failed Codex proxy metrics are malformed") from exc
    if not metrics["valid"] \
            or _failed_attempt_proxy_usages(metrics_paths[0]) \
            != failure["modelUsages"]:
        raise ReleaseRunError(
            "failed Codex proxy metrics do not prove recorded usage")


def _validate_cost(record: dict[str, Any], manifest: dict[str, Any]) -> None:
    price_card = manifest["priceCard"]
    model_cost = 0.0
    for round_row in record["rounds"]:
        usage = round_row.get("usage")
        if not isinstance(usage, dict):
            raise ReleaseRunError("every model round requires provider usage")
        model_cost += public_price_cost_from_usage(usage, price_card)["costUsd"]
    for retry in record.get("retries") or []:
        usages = retry.get("modelUsages") or []
        if not isinstance(usages, list) or any(
                not isinstance(usage, dict) for usage in usages):
            raise ReleaseRunError("retry modelUsages must be a list of objects")
        for usage in usages:
            model_cost += public_price_cost_from_usage(
                usage, price_card)["costUsd"]
    compaction_cost = 0.0
    for compaction in record["compactions"]:
        usage = compaction.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise ReleaseRunError("compaction usage must be an object")
            compaction_cost += public_price_cost_from_usage(
                usage, price_card)["costUsd"]
    paid_tool_cost = 0.0
    for result in record["toolResults"]:
        value = result.get("paidCostUsd", 0)
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReleaseRunError("tool paidCostUsd is invalid") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise ReleaseRunError("tool paidCostUsd is invalid")
        paid_tool_cost += numeric
    for retry in record.get("retries") or []:
        value = retry.get("paidToolCostUsd", 0)
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReleaseRunError("retry paidToolCostUsd is invalid") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise ReleaseRunError("retry paidToolCostUsd is invalid")
        paid_tool_cost += numeric
    expected = {
        "modelCostUsd": model_cost,
        "compactionCostUsd": compaction_cost,
        "paidToolCostUsd": paid_tool_cost,
        "agentCostUsd": model_cost + compaction_cost + paid_tool_cost,
    }
    for field, value in expected.items():
        try:
            observed = float(record["cost"].get(field))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReleaseRunError(f"task cost.{field} is required") from exc
        if not math.isfinite(observed) or not math.isclose(
                observed, value, rel_tol=1e-9, abs_tol=1e-12):
            raise ReleaseRunError(
                f"task cost.{field} does not match priced evidence")


def _validate_task_binding(
    root: Path,
    manifest: dict[str, Any],
    record: dict[str, Any],
) -> None:
    try:
        validate_record(record)
    except BenchmarkContractError as exc:
        raise ReleaseRunError(f"invalid task record: {exc}") from exc
    if record.get("contractVersion") != CONTRACT_VERSION_V2 \
            or record.get("recordType") != "task":
        raise ReleaseRunError("run store accepts only v2 task records")
    task_id = str(record.get("taskId") or "")
    expected = _task_rows(manifest).get(task_id)
    if expected is None:
        raise ReleaseRunError(f"task is not in manifest: {task_id}")
    bindings = {
        "runId": manifest["runId"],
        "dataset": expected["dataset"],
        "family": expected["family"],
        "agent": manifest["agent"],
        "model": manifest["model"],
        "providerFace": manifest["providerFace"],
        "providerSlotId": manifest["providerSlotId"],
        "thinking": manifest["thinking"],
        "experimentArm": manifest["experimentArm"],
    }
    for field, value in bindings.items():
        if record.get(field) != value:
            raise ReleaseRunError(f"task {field} does not match manifest")
    if not isinstance(record["oracle"].get("passed"), bool):
        raise ReleaseRunError("task is not oracle-ready")
    expected_oracle = expected.get("oracleType")
    if expected_oracle and record["oracle"].get("type") != expected_oracle:
        raise ReleaseRunError("task oracle type does not match manifest")
    if record.get("infrastructureError") is not None:
        raise ReleaseRunError("unresolved infrastructure failure is not a final task")
    if not record["rounds"]:
        raise ReleaseRunError("task requires at least one recorded model round")
    digest = str(record.get("finalOutputDigest") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ReleaseRunError("task finalOutputDigest is required")
    _validate_retries(record, manifest)
    _validate_cost(record, manifest)
    artifacts = record.get("artifacts") or []
    paths: dict[str, list[Path]] = {}
    for artifact in artifacts:
        path = _artifact_path(root, task_id, artifact)
        paths.setdefault(str(artifact["kind"]), []).append(path)
    if len(paths.get(_RAW_TRAJECTORY, [])) != 1:
        raise ReleaseRunError("task requires exactly one raw_trajectory artifact")
    _validate_jsonl_artifact(
        paths[_RAW_TRAJECTORY][0], "raw_trajectory artifact")
    if manifest["comparisonRole"] == "baseline":
        metrics_paths = paths.get(_PROXY_METRICS, [])
        if len(metrics_paths) != 1:
            raise ReleaseRunError("Codex baseline task requires proxy_metrics")
        try:
            metrics = validate_proxy_metrics(
                str(metrics_paths[0]),
                expected_request_count=len(record["rounds"]),
                require_trial_token=True,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseRunError("Codex proxy metrics are malformed") from exc
        if not metrics["valid"]:
            raise ReleaseRunError("Codex proxy metrics invalidate the task")
        latency = record["latency"]
        observed = {
            "translationCpuMs": metrics["translationCpuNs"] / 1_000_000,
            "proxyCpuMs": metrics["proxyCpuNs"] / 1_000_000,
        }
        for field, value in observed.items():
            if not math.isclose(float(latency[field]), value, abs_tol=1.0):
                raise ReleaseRunError(
                    f"task latency.{field} does not match proxy metrics")
    else:
        latency = record["latency"]
        if float(latency["translationCpuMs"]) != 0 \
                or float(latency["proxyCpuMs"]) != 0:
            raise ReleaseRunError(
                "candidate latency cannot subtract Codex proxy overhead")


def _validate_task_attempt_evidence(
    root: Path, manifest: dict[str, Any], record: dict[str, Any],
    state: dict[str, Any],
) -> None:
    expected_retries = [
        _failure_retry_row(event) for event in state["failures"]
    ]
    observed_retries = record.get("retries") or []
    if observed_retries != expected_retries:
        raise ReleaseRunError(
            "task retries do not match the immutable attempt ledger")
    record_artifacts = record.get("artifacts") or []
    for retry in expected_retries:
        failure = state["failures"][int(retry["retryIndex"]) - 1]
        _validate_failed_attempt_artifacts(
            root, manifest, str(record["taskId"]), failure)
        for artifact in retry["artifacts"]:
            if artifact not in record_artifacts:
                raise ReleaseRunError(
                    "failed-attempt artifact is absent from the final task record")
    active = state["active"]
    if active is None and state["success"] is None:
        raise ReleaseRunError("task has no active attempt to settle")
    success_start = (
        int(state["success"]["taskStartedAtUnixMs"])
        if state["success"] is not None
        else _record_success_start(manifest, record, active)
    )
    earliest_start = min(
        [success_start]
        + [int(failure["taskStartedAtUnixMs"])
           for failure in state["failures"]]
    )
    expected_wall = int(record["completedAt"]) - earliest_start
    if expected_wall < 0 or abs(
            float(record["latency"]["rawWallMs"]) - expected_wall) > 1.0:
        raise ReleaseRunError(
            "task oracle-ready latency omits attempt or retry wall time")


def _record_success_start(
    manifest: dict[str, Any], record: dict[str, Any],
    active: dict[str, Any] | None,
) -> int:
    environment = record.get("environment") or {}
    release_attempt = environment.get("releaseAttempt") \
        if isinstance(environment, dict) else None
    value = release_attempt.get("taskStartedAtUnixMs") \
        if isinstance(release_attempt, dict) else None
    if value is None and not _requires_attempt_ledger(manifest):
        value = round(
            int(record["completedAt"])
            - float(record["latency"]["rawWallMs"])
        )
    if isinstance(value, bool) or not isinstance(value, int) \
            or active is None or value < int(active["occurredAt"]) \
            or value > int(record["completedAt"]):
        raise ReleaseRunError(
            "claimed task requires a valid releaseAttempt task start")
    return value


def _close_oracle_ready_attempt(
    *, root: Path, manifest: dict[str, Any], task_id: str,
    task_path: Path, record: dict[str, Any], state: dict[str, Any],
) -> str:
    success = state["success"]
    task_digest = _file_sha256(task_path)
    if success is not None:
        if success.get("taskRecordSha256") != task_digest:
            raise ReleaseRunError(
                "oracle-ready attempt does not bind the immutable task record")
        return "unchanged"
    active = state["active"]
    if active is None:
        raise ReleaseRunError("task has no active attempt to settle")
    task_started_at = _record_success_start(manifest, record, active)
    occurred_at = max(
        int(active["occurredAt"]), int(record.get("completedAt") or 0)
    )
    event = {
        **_attempt_event(
            manifest=manifest, task_id=task_id,
            attempt_index=int(active["attemptIndex"]),
            event_type="terminal", execution_id=str(active["executionId"]),
            runner_kind=str(active["runnerKind"]), occurred_at=occurred_at,
        ),
        "outcome": "oracle_ready",
        "code": "oracle_ready",
        "taskRecordSha256": task_digest,
        "taskStartedAtUnixMs": task_started_at,
    }
    return _write_immutable(
        _attempt_event_path(
            root, task_id, int(active["attemptIndex"]), "terminal"),
        _canonical_bytes(event),
    )


def record_release_task(run_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Commit one manifest-bound task record, idempotently and immutably."""
    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    _assert_run_open(root)
    _validate_task_binding(root, manifest, record)
    task_id = str(record["taskId"])
    task_root = _private_child_directory(root, "tasks", create=False)
    output = task_root / f"{_task_key(task_id)}.json"
    with _run_lock(root):
        state = _attempt_state(root, manifest, task_id)
        has_claim = bool(state["starts"])
        if _requires_attempt_ledger(manifest) and not has_claim:
            raise ReleaseRunError(
                "full release task requires a pre-dispatch attempt claim")
        if has_claim:
            _validate_task_attempt_evidence(root, manifest, record, state)
        status = _write_immutable(output, _canonical_bytes(record))
        attempt_status = (
            _close_oracle_ready_attempt(
                root=root, manifest=manifest, task_id=task_id,
                task_path=output, record=record, state=state,
            )
            if has_claim else "not_recorded"
        )
    return {
        "contractVersion": RUN_STORE_CONTRACT,
        "status": status,
        "attemptStatus": attempt_status,
        "runId": manifest["runId"],
        "taskId": task_id,
    }


def audit_release_attempts(
    run_root: Path, *, require_complete: bool = False,
) -> dict[str, Any]:
    """Audit pre-dispatch claims and terminal outcomes independently."""

    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    expected = _task_rows(manifest)
    attempts_path = root / "attempts"
    observed_entries: dict[str, Path] = {}
    errors: list[str] = []
    if attempts_path.exists():
        try:
            attempts_root = _private_child_directory(
                root, "attempts", create=False)
            observed_entries = {
                path.name: path for path in attempts_root.iterdir()
            }
        except (OSError, ReleaseRunError) as exc:
            errors.append(f"attempt root: {exc}")
    expected_directories = {
        _task_key(task_id): task_id for task_id in expected
    }
    unexpected = sorted(set(observed_entries) - set(expected_directories))
    errors.extend(
        f"unexpected attempt ledger entry: {name}" for name in unexpected
    )
    total_attempts = 0
    failed_attempts = 0
    claimed_tasks = 0
    oracle_ready_tasks = 0
    open_attempts = 0
    missing_claims: list[str] = []
    invalid_tasks: list[str] = []
    for directory, task_id in expected_directories.items():
        if directory not in observed_entries:
            missing_claims.append(task_id)
            continue
        try:
            entry = observed_entries[directory]
            if entry.is_symlink() or not entry.is_dir():
                raise ReleaseRunError(
                    "task attempt entry must be a non-symlink directory")
            state = _attempt_state(root, manifest, task_id)
            claimed_tasks += int(bool(state["starts"]))
            total_attempts += len(state["starts"])
            failed_attempts += len(state["failures"])
            open_attempts += int(state["active"] is not None)
            for failure in state["failures"]:
                _validate_failed_attempt_artifacts(
                    root, manifest, task_id, failure)
            task_path = root / "tasks" / f"{_task_key(task_id)}.json"
            success = state["success"]
            if success is not None:
                if not task_path.is_file() or task_path.is_symlink() \
                        or _file_sha256(task_path) \
                        != success.get("taskRecordSha256"):
                    raise ReleaseRunError(
                        "oracle-ready attempt task record is missing or changed")
                record = _load_json(task_path, f"task record {task_id}")
                _validate_task_attempt_evidence(
                    root, manifest, record, state)
                oracle_ready_tasks += 1
            elif task_path.is_file() and state["active"] is None:
                raise ReleaseRunError(
                    "task record exists without an open or oracle-ready attempt")
        except (OSError, ReleaseRunError) as exc:
            invalid_tasks.append(f"{task_id}: {exc}")
    errors.extend(invalid_tasks)
    required = _requires_attempt_ledger(manifest)
    coverage_complete = (
        oracle_ready_tasks == len(expected)
        and not missing_claims and not invalid_tasks and not unexpected
        and open_attempts == 0
    )
    if require_complete and required:
        if missing_claims:
            errors.append(
                f"missing {len(missing_claims)} pre-dispatch attempt claims")
        if open_attempts:
            errors.append(f"{open_attempts} task attempts remain open")
        if oracle_ready_tasks != len(expected):
            errors.append(
                "attempt ledger does not settle every manifest task oracle")
    return {
        "contractVersion": ATTEMPT_LEDGER_CONTRACT,
        "required": required,
        "valid": not errors,
        "complete": coverage_complete,
        "expectedTasks": len(expected),
        "claimedTasks": claimed_tasks,
        "oracleReadyTasks": oracle_ready_tasks,
        "missingClaimTasks": len(missing_claims),
        "missingTaskIds": missing_claims[:20],
        "openAttempts": open_attempts,
        "totalAttempts": total_attempts,
        "failedAttempts": failed_attempts,
        "invalidTasks": len(invalid_tasks),
        "unexpectedEntries": len(unexpected),
        "errors": errors[:20],
    }


def audit_release_run(run_root: Path, *, require_complete: bool = False) -> dict[str, Any]:
    """Inspect every expected record and artifact without mutating the run."""
    root = _private_run_root(run_root, create=False)
    manifest = _manifest(root)
    expected = _task_rows(manifest)
    task_dir = _private_child_directory(root, "tasks", create=False)
    expected_files = {
        f"{_task_key(task_id)}.json": task_id for task_id in expected
    }
    observed_files = {
        path.name: path for path in task_dir.glob("*.json")
        if path.is_file() and not path.is_symlink()
    }
    unexpected = sorted(set(observed_files) - set(expected_files))
    missing = []
    invalid: list[str] = []
    completed = 0
    for filename, task_id in expected_files.items():
        path = observed_files.get(filename)
        if path is None:
            missing.append(task_id)
            continue
        try:
            record = _load_json(path, f"task record {task_id}")
            if record.get("taskId") != task_id:
                raise ReleaseRunError("task record filename does not match taskId")
            _validate_task_binding(root, manifest, record)
            completed += 1
        except (OSError, ReleaseRunError) as exc:
            invalid.append(f"{task_id}: {exc}")
    attempt_audit = audit_release_attempts(
        root, require_complete=require_complete)
    task_records_complete = not missing and not invalid and not unexpected
    complete = task_records_complete and (
        not attempt_audit["required"] or attempt_audit["complete"]
    )
    errors = list(invalid)
    errors.extend(f"unexpected task record: {name}" for name in unexpected)
    errors.extend(attempt_audit["errors"])
    if require_complete and missing:
        errors.append(f"missing {len(missing)} manifest task records")
    valid = not errors
    return {
        "contractVersion": RUN_STORE_CONTRACT,
        "status": "invalid" if not valid else "complete" if complete else "incomplete",
        "valid": valid,
        "complete": complete,
        "runId": manifest["runId"],
        "pairId": manifest["pairId"],
        "comparisonRole": manifest["comparisonRole"],
        "experimentArm": manifest["experimentArm"],
        "expectedTasks": len(expected),
        "completedTasks": completed,
        "missingTasks": len(missing),
        "missingTaskIds": missing[:20],
        "invalidTasks": len(invalid),
        "unexpectedTaskRecords": len(unexpected),
        "attemptLedger": attempt_audit,
        "errors": errors[:20],
        "finalized": (root / "run.jsonl").is_file(),
    }


def finalize_release_run(run_root: Path) -> dict[str, Any]:
    """Create immutable manifest-ordered JSONL only for a complete run."""
    root = _private_run_root(run_root, create=False)
    with _run_lock(root):
        audit = audit_release_run(root, require_complete=True)
        if not audit["valid"] or not audit["complete"]:
            raise ReleaseRunError(
                f"run is not complete: {audit['completedTasks']}/"
                f"{audit['expectedTasks']}; errors={audit['errors']}")
        manifest = _manifest(root)
        task_root = _private_child_directory(root, "tasks", create=False)
        output = root / "run.jsonl"
        descriptor, temporary = tempfile.mkstemp(
            prefix=".run.", suffix=".jsonl.partial", dir=root)
        sha256 = hashlib.sha256()
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                payload = _canonical_bytes(manifest)
                sha256.update(payload)
                handle.write(payload)
                for row in manifest["tasks"]:
                    task_id = str(row["taskId"])
                    state = _attempt_state(root, manifest, task_id)
                    for event in state["events"]:
                        payload = _canonical_bytes(event)
                        sha256.update(payload)
                        handle.write(payload)
                    path = task_root / f"{_task_key(row['taskId'])}.json"
                    payload = _canonical_bytes(_load_json(path, "task record"))
                    sha256.update(payload)
                    handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            payload_digest = sha256.hexdigest()
            if output.exists():
                if output.is_symlink() or not output.is_file() \
                        or _file_sha256(output) != payload_digest:
                    raise ReleaseRunError("refusing to replace finalized run JSONL")
                status = "unchanged"
            else:
                os.replace(temporary, output)
                output.chmod(0o400)
                status = "created"
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
    return {
        **audit,
        "status": status,
        "finalized": True,
        "jsonl": str(output),
        "jsonlSha256": payload_digest,
    }


def audit_release_pair(
    *,
    baseline_root: Path,
    candidate_root: Path,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Prove two run stores share every frozen fairness control."""
    baseline_path = _private_run_root(baseline_root, create=False)
    candidate_path = _private_run_root(candidate_root, create=False)
    baseline_manifest = _manifest(baseline_path)
    candidate_manifest = _manifest(candidate_path)
    baseline = audit_release_run(
        baseline_path, require_complete=require_complete)
    candidate = audit_release_run(
        candidate_path, require_complete=require_complete)
    errors: list[str] = []
    if baseline_manifest["comparisonRole"] != "baseline":
        errors.append("baseline run does not have comparisonRole=baseline")
    if candidate_manifest["comparisonRole"] != "candidate":
        errors.append("candidate run does not have comparisonRole=candidate")
    if baseline_manifest["runId"] == candidate_manifest["runId"]:
        errors.append("paired run IDs must be distinct")
    controls = (
        "pairId", "model", "providerFace", "providerSlotId", "thinking",
        "toolPermissions",
        "datasetSnapshot", "tasks", "priceCard", "sandbox", "retryRule",
        "artifactLimits", "limits", "comparisonControls", "harness",
    )
    drift = [field for field in controls
             if baseline_manifest.get(field) != candidate_manifest.get(field)]
    if drift:
        errors.append(f"paired fairness controls differ: {drift}")
    if not baseline["valid"]:
        errors.append("baseline run is invalid")
    if not candidate["valid"]:
        errors.append("candidate run is invalid")
    if require_complete and not baseline["complete"]:
        errors.append("baseline run is incomplete")
    if require_complete and not candidate["complete"]:
        errors.append("candidate run is incomplete")
    ready = not errors and baseline["complete"] and candidate["complete"]
    return {
        "contractVersion": RUN_STORE_CONTRACT,
        "status": "ready" if ready else "invalid" if errors else "incomplete",
        "valid": not errors,
        "pairReady": ready,
        "pairId": baseline_manifest.get("pairId"),
        "expectedPairs": len(baseline_manifest["tasks"]),
        "completedPairs": min(
            baseline["completedTasks"], candidate["completedTasks"]),
        "baseline": baseline,
        "candidate": candidate,
        "errors": errors,
        "claim": "paired evidence ready for gate analysis" if ready else "not demonstrated",
    }


__all__ = [
    "ATTEMPT_LEDGER_CONTRACT", "RUN_STORE_CONTRACT", "ReleaseRunError",
    "audit_release_attempts", "audit_release_pair", "audit_release_run",
    "claim_release_task_attempts", "fail_release_task_attempt",
    "fail_release_execution_before_dispatch",
    "finalize_release_run", "initialize_release_run",
    "load_release_manifest", "load_release_record", "load_release_task_records",
    "record_release_task", "release_task_retry_evidence",
    "store_run_artifact", "validate_release_attempt_execution",
]
