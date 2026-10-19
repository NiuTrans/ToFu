"""Owned platform output interpretation; scheduler success is not a score.

The SDK first proves Worker ownership/exit/resources and collects declared
hash-checked files. This adapter then binds the worker receipt to the frozen
evaluation request and exposes a diagnostic outcome. No new scoring rules.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contract import digest, validate_request


def inspect_collected_outputs(directory: Path, registration: dict) -> dict:
    root = directory.resolve(strict=True)
    report_path, archive = root / "platform-result.json", root / "platform-evidence.zip"
    if report_path.is_symlink() or archive.is_symlink() or not report_path.is_file() or not archive.is_file():
        raise ValueError("declared regular platform outputs required")
    with report_path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("platform result exceeds metadata budget")
    report = json.loads(raw)
    request = registration["request"]
    validate_request(request)
    if not isinstance(report, dict) or type(report.get("version")) is not int or report.get("version") != 1 \
            or report.get("request_sha256") != digest(request) \
            or report.get("run_id") != request["run_id"] \
            or report.get("benchmark") != request["benchmark"] \
            or report.get("dataset_ref") != request["dataset_ref"] \
            or report.get("formal_score") is not None:
        raise ValueError("platform output does not match the registered benchmark")
    state = report.get("benchmark_state")
    if state not in {"preflight_failed", "benchmark_failed", "completed", "evidence_failed"}:
        raise ValueError("unrecognized platform benchmark outcome")
    if type(report.get("evidence_complete")) is not bool:
        raise ValueError("explicit evidence completeness required")
    if report.get("evidence_complete") is True:
        evidence = report.get("evidence")
        if not isinstance(evidence, dict) or archive.stat().st_size != evidence.get("bytes"):
            raise ValueError("platform evidence size mismatch")
        checksum = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != evidence.get("sha256"):
            raise ValueError("platform evidence hash mismatch")
    if state == "completed" and (type(report.get("child_exit_code")) is not int
            or report["child_exit_code"] != 0 or report.get("evidence_complete") is not True):
        raise ValueError("benchmark completion requires successful execution and complete evidence")
    if state == "completed" and ((report.get("source_identity") or {}).get("revision") != request["runner_revision"]
            or (report.get("engine_identity") or {}).get("source_commit") != request["harbor_commit"]):
        raise ValueError("benchmark completion requires pinned source and engine evidence")
    return {"run_id": request["run_id"], "benchmark": request["benchmark"],
            "benchmark_state": state, "child_exit_code": report.get("child_exit_code"),
            "diagnostic_only": True, "formal_score": None,
            "evidence_complete": report.get("evidence_complete") is True,
            "native_audit_required": True}


def collect_from_platform(service, actor, operation_id: str, destination: Path, registration: dict) -> dict:
    if registration.get("scope") != actor.scope or registration.get("project_root") != actor.project_root:
        raise ValueError("platform collection belongs to another actor/project")
    # The SDK's ownership/resources/exit/hash evidence boundary remains first.
    collected = service.collect(actor, operation_id, destination)
    interpretation = inspect_collected_outputs(Path(collected["directory"]), registration)
    return {"collection": collected, "evaluation": interpretation}
