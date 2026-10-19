"""Bounded platform worker wrapper around the retained Harbor runner.

The platform SDK owns the outer Worker allocation/lifecycle. Its maintained
POSIX runner executes doctor and Harbor under one deadline. A completed wrapper
can return diagnostic artifacts for a failed benchmark; that is never a pass,
and never a certified score. Provider credentials are not written to receipts.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time
import zipfile

from evaluations.source_identity import EVALUATION_ARTIFACT_DIRECTORY, verify_source_receipt
from .contract import canonical, digest, parse_arguments, validate_request

WORKER_OUTPUT_DIRECTORY = EVALUATION_ARTIFACT_DIRECTORY + "/benchmarks"


def installed_engine_identity(expected_commit: str) -> dict:
    distribution = metadata.distribution("harbor")
    origin = json.loads(distribution.read_text("direct_url.json") or "{}")
    commit = (origin.get("vcs_info") or {}).get("commit_id")
    if commit != expected_commit:
        raise ValueError("installed Harbor does not match its admitted source commit")
    return {"version": distribution.version, "source_commit": commit}


def _dispatch(command: list[str], workspace: Path, seconds: int, output_bytes: int) -> dict:
    from tofu_environments.job_runner import run_job
    return run_job({"command": command, "limits": {"runtime_seconds": seconds,
                     "result_bytes": output_bytes}, "outputs": []}, workspace)


def _doctor_arguments(request: dict) -> list[str]:
    args = parse_arguments(request["arguments"])
    values = ["doctor", "--benchmark", request["benchmark"], "--backend", args.backend,
              "--output-root", WORKER_OUTPUT_DIRECTORY]
    # The owning CLI uses the same rootless configuration options on run/doctor.
    index = 0
    while index < len(request["arguments"]):
        value = request["arguments"][index]
        if value.startswith("--rootless-"):
            values.append(value)
            if "=" not in value:
                index += 1
                values.append(request["arguments"][index])
        index += 1
    return values


def _write_execution_logs(root: Path, phase: str, result: dict) -> None:
    directory = root / WORKER_OUTPUT_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("stdout", "stderr"):
        path = directory / f"platform-{phase}.{name}"
        path.write_text(str(result.get(name) or ""), encoding="utf-8")
        path.chmod(0o600)


def _pack_evidence(root: Path, maximum: int, secrets: list[bytes], deadline: float) -> dict:
    source = root / WORKER_OUTPUT_DIRECTORY
    archive = root / "platform-evidence.zip"
    total, count = 0, 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        if source.exists():
            for current, directories, names in os.walk(source, followlinks=False):
                # Only reconstructible transport/cache is excluded. Entire failed
                # attempts, verifier reports and agent evidence remain included.
                directories[:] = [name for name in directories if name not in
                                  {".runtime", ".runtime-bin", "__pycache__"}]
                for name in directories:
                    if (Path(current) / name).is_symlink():
                        raise ValueError("symlink directory in evaluation evidence")
                for name in sorted(names):
                    path = Path(current) / name
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("nonregular evaluation evidence")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("evidence publication deadline")
                    count += 1
                    if count > 100000:
                        raise ValueError("evaluation evidence exceeds declared budget")
                    overlap = b""
                    with path.open("rb") as stream, bundle.open(path.relative_to(root).as_posix(), "w") as member:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("evidence publication deadline")
                            candidate = overlap + chunk
                            if any(secret in candidate for secret in secrets):
                                raise ValueError("credential in evaluation evidence")
                            total += len(chunk)
                            if total > maximum:
                                raise ValueError("evaluation evidence exceeds declared budget")
                            member.write(chunk)
                            overlap = candidate[-max((len(secret) for secret in secrets), default=1):]
                    if archive.stat().st_size > maximum:
                        raise ValueError("compressed evidence exceeds declared budget")
    archive.chmod(0o600)
    if archive.stat().st_size > maximum:
        raise ValueError("compressed evidence exceeds declared budget")
    checksum = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return {"files": count, "uncompressed_bytes": total, "bytes": archive.stat().st_size,
            "sha256": checksum.hexdigest()}


def run_worker(request: dict, workspace: Path, *, dispatch=_dispatch,
               engine_identity=installed_engine_identity) -> dict:
    root = workspace.resolve(strict=True)
    validate_request(request)
    started = time.monotonic()
    deadline = started + request["limits"]["runtime_seconds"]
    reserve = max(1, request["limits"]["runtime_seconds"] // 10)
    execution_deadline = deadline - reserve
    report = {"version": 1, "run_id": request["run_id"], "request_sha256": digest(request),
              "benchmark": request["benchmark"], "dataset_ref": request["dataset_ref"],
              "benchmark_state": "preflight_failed", "formal_score": None,
              "child_exit_code": None, "failure_type": None, "evidence_complete": False}
    try:
        report["source_identity"] = verify_source_receipt(
            root, root / "payload-source.json", expected_revision=request["runner_revision"])
        report["engine_identity"] = engine_identity(request["harbor_commit"])
        seconds = int(execution_deadline - time.monotonic())
        if seconds < 1:
            raise TimeoutError("execution budget exhausted before preflight")
        preflight = dispatch([sys.executable, "-m", "evaluations", "harbor",
                              *_doctor_arguments(request)], root, seconds,
                              request["limits"]["console_bytes"])
        _write_execution_logs(root, "doctor", preflight)
        report["preflight_exit_code"] = preflight.get("exit_code")
        if preflight.get("state") == "succeeded" and preflight.get("exit_code") == 0:
            seconds = int(execution_deadline - time.monotonic())
            if seconds < 1:
                raise TimeoutError("execution budget exhausted before benchmark")
            arguments = [*request["arguments"], "--run-id", request["run_id"],
                         "--output-root", WORKER_OUTPUT_DIRECTORY, "--source-receipt", "payload-source.json"]
            result = dispatch([sys.executable, "-m", "evaluations", "harbor", "run", *arguments],
                              root, seconds, request["limits"]["console_bytes"])
            _write_execution_logs(root, "harbor", result)
            report["child_exit_code"] = result.get("exit_code")
            report["benchmark_state"] = "completed" if result.get("state") == "succeeded" \
                and result.get("exit_code") == 0 else "benchmark_failed"
    except Exception as exc:
        # Raw provider exception text and credentials stay out of receipts.
        report["failure_type"] = type(exc).__name__
    secrets = [os.environ[name].encode() for name in request["credential_env_names"]
               if os.environ.get(name)]
    try:
        report["evidence"] = _pack_evidence(root, request["limits"]["result_bytes"] - 8192, secrets, deadline)
        report["evidence_complete"] = True
    except Exception as exc:
        report["artifact_failure_type"] = type(exc).__name__
        # No partial or credential-bearing archive is published. Preserve the
        # original failed evidence in the worker for owned platform diagnosis.
        with zipfile.ZipFile(root / "platform-evidence.zip", "w"):
            pass
        (root / "platform-evidence.zip").chmod(0o600)
        report["benchmark_state"] = "evidence_failed"
    report["elapsed_seconds"] = time.monotonic() - started
    output = root / "platform-result.json"
    output.write_bytes(canonical(report))
    output.chmod(0o600)
    return report


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    with args.request.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("worker request exceeds byte budget")
    report = run_worker(json.loads(raw), Path.cwd())
    # This means the wrapper produced its declared diagnostic outputs. The
    # benchmark result is independently recorded, never inferred from this 0.
    print(json.dumps({"run_id": report["run_id"], "benchmark_state": report["benchmark_state"],
                      "evidence_complete": report["evidence_complete"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
