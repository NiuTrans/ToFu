from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json, create_run_dir, make_run_id, utc_now, validate_run_id
from .constants import FRAMEWORK_VERSION, OFFICIAL_DATASET, SWEBENCH_VERSION
from .process import run_streaming


def load_predictions(path: Path) -> list[dict[str, str]]:
    path = path.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        rows = []
        for instance_id, prediction in value.items():
            if not isinstance(prediction, dict):
                raise ValueError(f"prediction for {instance_id!r} is not an object")
            rows.append({"instance_id": instance_id, **prediction})
    elif isinstance(value, list):
        rows = value
    else:
        raise ValueError("predictions must be a JSON list, object map, or JSONL")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"prediction #{index} is not an object")
        required = ("instance_id", "model_name_or_path", "model_patch")
        missing = [key for key in required if key not in row]
        if missing:
            raise ValueError(f"prediction #{index} is missing {missing}")
        wrong_types = [key for key in required if not isinstance(row[key], str)]
        if wrong_types:
            raise ValueError(f"prediction #{index} fields must be strings: {wrong_types}")
        item = {key: row[key] for key in required}
        if not item["instance_id"].strip() or not item["model_name_or_path"].strip():
            raise ValueError(f"prediction #{index} has an empty instance or model name")
        key = (item["model_name_or_path"], item["instance_id"])
        if key in seen:
            raise ValueError(f"duplicate prediction for model/task {key}")
        seen.add(key)
        normalized.append(item)
    if not normalized:
        raise ValueError("predictions file is empty")
    return normalized


def group_predictions(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["model_name_or_path"]].append(row)
    return dict(groups)


def normalized_predictions_sha256(rows: list[dict[str, str]]) -> str:
    canonical = sorted(
        rows,
        key=lambda row: (row["model_name_or_path"], row["instance_id"]),
    )
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _model_key(model: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-.")[:48] or "model"
    digest = hashlib.sha256(model.encode()).hexdigest()[:10]
    return f"{slug}-{digest}"


def _write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def grade_predictions(
    predictions_path: Path,
    *,
    backend: str,
    output_root: Path,
    run_id: str | None = None,
    workers: int = 4,
    timeout: int = 1800,
    dry_run: bool = False,
) -> tuple[int, Path]:
    if backend not in {"modal", "docker"}:
        raise ValueError("official grading backend must be 'modal' or 'docker'")
    if workers < 1 or timeout < 1:
        raise ValueError("workers and timeout must be positive")
    rows = load_predictions(predictions_path)
    groups = group_predictions(rows)
    resolved_run_id = validate_run_id(run_id) if run_id else make_run_id("official")
    run_dir = create_run_dir(output_root, resolved_run_id)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "framework_version": FRAMEWORK_VERSION,
        "kind": "official-patch-evaluation",
        "status": "prepared" if dry_run else "running",
        "run_id": resolved_run_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "dataset": OFFICIAL_DATASET,
        "swebench_version": SWEBENCH_VERSION,
        "project_revision": _git_revision(),
        "normalized_predictions_sha256": normalized_predictions_sha256(rows),
        "backend": backend,
        "prediction_count": len(rows),
        "models": list(groups),
        "groups": {},
    }
    manifest_path = run_dir / "manifest.json"
    overall = 0
    for model, model_rows in groups.items():
        key = _model_key(model)
        model_dir = run_dir / "models" / key
        model_dir.mkdir(parents=True, mode=0o700)
        prediction_copy = model_dir / "predictions.jsonl"
        _write_jsonl(prediction_copy, model_rows)
        backend_run_id = f"{resolved_run_id}-{hashlib.sha256(model.encode()).hexdigest()[:8]}"
        command = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            OFFICIAL_DATASET,
            "--split",
            "test",
            "--predictions_path",
            str(prediction_copy),
            "--run_id",
            backend_run_id,
            "--timeout",
            str(timeout),
            "--report_dir",
            str(model_dir / "reports"),
            "--instance_ids",
            *[row["instance_id"] for row in model_rows],
        ]
        if backend == "modal":
            command.extend(["--modal", "true"])
        else:
            command.extend(["--max_workers", str(workers)])
        manifest["groups"][key] = {
            "model": model,
            "instances": len(model_rows),
            "backend_run_id": backend_run_id,
            "status": "prepared" if dry_run else "running",
            "command": command,
        }
        atomic_write_json(manifest_path, manifest)
        if dry_run:
            continue
        exit_code = run_streaming(
            command,
            cwd=model_dir,
            log_path=model_dir / "launcher.log",
        )
        manifest["groups"][key]["exit_code"] = exit_code
        manifest["groups"][key]["status"] = "succeeded" if exit_code == 0 else "failed"
        manifest["updated_at"] = utc_now()
        atomic_write_json(manifest_path, manifest)
        if exit_code:
            overall = exit_code
    manifest["status"] = "prepared" if dry_run else ("succeeded" if overall == 0 else "failed")
    manifest["exit_code"] = overall
    manifest["updated_at"] = utc_now()
    atomic_write_json(manifest_path, manifest)
    if not dry_run and overall == 0:
        from .audit import audit_run

        audit = audit_run(run_dir)
        atomic_write_json(run_dir / "audit.json", audit)
        if not audit["ok"]:
            overall = 3
            manifest["status"] = "audit_failed"
            manifest["exit_code"] = overall
            manifest["updated_at"] = utc_now()
            atomic_write_json(manifest_path, manifest)
    return overall, run_dir
