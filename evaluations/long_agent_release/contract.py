"""Fail-closed contract for the five external frozen task packs.

The task payloads contain hidden oracles and simulator state, so they live in
an explicit external directory and are never copied into the public benchmark
manifest.  The compiler reads owner-provided paths, verifies every content
digest, and projects only immutable task identities and hashes.
"""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lib.benchmark_contract import RELEASE_TASK_MATRIX_V2


FROZEN_TASK_PACK_VERSION = "tofu-frozen-task-pack/v1"
FROZEN_TASK_VERSION = "tofu-frozen-task/v1"


class FrozenTaskPackError(ValueError):
    """A frozen dataset asset is missing, mutable, or malformed."""


@dataclass(frozen=True)
class FrozenPackSpec:
    family: str
    dataset: str
    task_count: int
    validate_shape: Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class FrozenTask:
    task_id: str
    family: str
    dataset: str
    sha256: str
    oracle_type: str
    tags: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class FrozenTaskPack:
    family: str
    dataset: str
    world_version: str
    backend_id: str
    backend_sha256: str
    manifest_sha256: str
    tasks: tuple[FrozenTask, ...]


def _positive_int(value: Any, field: str, *, minimum: int = 1,
                  maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FrozenTaskPackError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise FrozenTaskPackError(f"{field} must be <= {maximum}")
    return value


def _nonempty_strings(value: Any, field: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum \
            or any(not isinstance(item, str) or not item.strip()
                   for item in value):
        raise FrozenTaskPackError(
            f"{field} must contain at least {minimum} non-empty strings")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise FrozenTaskPackError(f"{field} must not contain duplicates")
    return normalized


def _validate_integrated(shape: dict[str, Any]) -> None:
    _nonempty_strings(shape.get("toolNames"), "shape.toolNames", minimum=2)
    _positive_int(shape.get("dependencyEdges"), "shape.dependencyEdges")
    _positive_int(shape.get("expectedResultBytes"),
                  "shape.expectedResultBytes")


def _validate_continuity(shape: dict[str, Any]) -> None:
    _positive_int(shape.get("turns"), "shape.turns", minimum=20, maximum=80)
    _positive_int(shape.get("hiddenFactCount"), "shape.hiddenFactCount")
    _positive_int(shape.get("constraintChangeCount"),
                  "shape.constraintChangeCount")


def _validate_research(shape: dict[str, Any]) -> None:
    _positive_int(shape.get("sourceCount"), "shape.sourceCount", minimum=2)
    if shape.get("frozenSources") is not True:
        raise FrozenTaskPackError("shape.frozenSources must be true")
    if shape.get("requiresCitations") is not True:
        raise FrozenTaskPackError("shape.requiresCitations must be true")


def _validate_writing(shape: dict[str, Any]) -> None:
    _positive_int(shape.get("revisionStages"),
                  "shape.revisionStages", minimum=4)
    _positive_int(shape.get("factCount"), "shape.factCount")
    _positive_int(shape.get("formatConstraintCount"),
                  "shape.formatConstraintCount")
    _positive_int(shape.get("styleConstraintCount"),
                  "shape.styleConstraintCount")


_FAULT_INJECTIONS = frozenset({
    "429", "partial_sse", "timeout", "duplicate_call_id",
    "empty_call_id", "tool_failure", "compaction_boundary",
    "storage_failure", "stale_world_state", "approval_denied",
    "process_recovery",
})


def _validate_fault(shape: dict[str, Any]) -> None:
    injections = _nonempty_strings(
        shape.get("injections"), "shape.injections")
    unknown = sorted(set(injections) - _FAULT_INJECTIONS)
    if unknown:
        raise FrozenTaskPackError(
            f"shape.injections contains unsupported faults: {unknown}")
    if shape.get("requiresRecoveryOracle") is not True:
        raise FrozenTaskPackError(
            "shape.requiresRecoveryOracle must be true")


def _spec(family: str, dataset: str,
          validator: Callable[[dict[str, Any]], None]) -> FrozenPackSpec:
    return FrozenPackSpec(
        family=family,
        dataset=dataset,
        task_count=RELEASE_TASK_MATRIX_V2[(family, dataset)],
        validate_shape=validator,
    )


CUSTOM_PACK_SPECS = (
    _spec("integrated_multi_tool", "frozen-integrated-tools",
          _validate_integrated),
    _spec("long_continuity", "frozen-continuity", _validate_continuity),
    _spec("frozen_research", "frozen-source-packs", _validate_research),
    _spec("long_writing", "frozen-writing", _validate_writing),
    _spec("fault_recovery", "frozen-fault-recovery", _validate_fault),
)


def _sha256(value: Any) -> str:
    text = str(value or "").lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FrozenTaskPackError("expected a lowercase SHA-256 digest")
    return text


def _read_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FrozenTaskPackError(f"{label} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FrozenTaskPackError(f"cannot read {label}: {path}") from exc


def _load_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise FrozenTaskPackError(
                    f"{label} contains duplicate key {key!r}")
            value[key] = item
        return value

    def invalid_constant(value):
        raise FrozenTaskPackError(
            f"{label} contains non-finite number {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=object_pairs,
            parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenTaskPackError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FrozenTaskPackError(f"{label} must be a JSON object")
    return value


def _private_root(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise FrozenTaskPackError(
            "frozen task-pack root must not be a symlink")
    try:
        root = expanded.resolve(strict=True)
    except OSError as exc:
        raise FrozenTaskPackError(f"frozen task-pack root is missing: {path}") from exc
    if not root.is_dir() or root.is_symlink():
        raise FrozenTaskPackError("frozen task-pack root must be a directory")
    mode = stat.S_IMODE(root.stat().st_mode)
    if mode & 0o077:
        raise FrozenTaskPackError(
            f"frozen task-pack root must be private (0700), got {mode:04o}")
    return root


def _resolve_child(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise FrozenTaskPackError("task path must be a non-empty string")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise FrozenTaskPackError("task path escapes its frozen pack")
    unresolved = root / candidate
    cursor = unresolved
    while cursor != root:
        if cursor.is_symlink():
            raise FrozenTaskPackError("task path traverses a symlink")
        cursor = cursor.parent
    try:
        resolved = unresolved.resolve(strict=True)
    except OSError as exc:
        raise FrozenTaskPackError(f"task file is missing: {relative}") from exc
    if root not in resolved.parents or resolved.is_symlink():
        raise FrozenTaskPackError("task path escapes its frozen pack")
    return resolved


def _nonempty_definition(value: Any, field: str) -> None:
    if value in (None, "", [], {}):
        raise FrozenTaskPackError(f"{field} must contain a frozen definition")


def _validate_task(value: dict[str, Any], *, spec: FrozenPackSpec,
                   expected_id: str, path: Path, digest: str) -> FrozenTask:
    if value.get("contractVersion") != FROZEN_TASK_VERSION:
        raise FrozenTaskPackError(f"task {expected_id} version mismatch")
    expected = {
        "taskId": expected_id,
        "family": spec.family,
        "dataset": spec.dataset,
    }
    for field, required in expected.items():
        if value.get(field) != required:
            raise FrozenTaskPackError(
                f"task {expected_id} {field} mismatch")
    if not isinstance(value.get("instructions"), str) \
            or not value["instructions"].strip():
        raise FrozenTaskPackError(
            f"task {expected_id} requires non-empty instructions")
    oracle = value.get("oracle")
    simulator = value.get("simulator")
    permissions = value.get("permissions")
    shape = value.get("shape")
    if not isinstance(oracle, dict) or not str(oracle.get("type") or ""):
        raise FrozenTaskPackError(f"task {expected_id} oracle is invalid")
    _nonempty_definition(oracle.get("definition"), "oracle.definition")
    if not isinstance(simulator, dict) or not str(simulator.get("type") or ""):
        raise FrozenTaskPackError(f"task {expected_id} simulator is invalid")
    _nonempty_definition(simulator.get("definition"), "simulator.definition")
    if not isinstance(permissions, dict) \
            or not str(permissions.get("profile") or ""):
        raise FrozenTaskPackError(f"task {expected_id} permissions are invalid")
    if not isinstance(shape, dict):
        raise FrozenTaskPackError(f"task {expected_id} shape is invalid")
    spec.validate_shape(shape)
    tags = tuple(_nonempty_strings(value.get("tags"), "task.tags"))
    return FrozenTask(
        task_id=expected_id,
        family=spec.family,
        dataset=spec.dataset,
        sha256=digest,
        oracle_type=str(oracle["type"]),
        tags=tags,
        path=path,
    )


def load_frozen_task_pack(
    manifest_path: Path, spec: FrozenPackSpec,
) -> FrozenTaskPack:
    """Load one pack, verifying cardinality, paths, hashes, and task shapes."""
    root = _private_root(manifest_path.parent)
    resolved_manifest = _resolve_child(root, manifest_path.name)
    raw_manifest = _read_bytes(resolved_manifest, "pack manifest")
    manifest = _load_json_bytes(raw_manifest, "pack manifest")
    expected = {
        "contractVersion": FROZEN_TASK_PACK_VERSION,
        "family": spec.family,
        "dataset": spec.dataset,
        "taskCount": spec.task_count,
        "frozen": True,
    }
    for field, required in expected.items():
        if manifest.get(field) != required:
            raise FrozenTaskPackError(
                f"pack {spec.dataset} {field} mismatch")
    world_version = str(manifest.get("worldVersion") or "")
    if not world_version:
        raise FrozenTaskPackError(f"pack {spec.dataset} requires worldVersion")
    backend = manifest.get("backend")
    if not isinstance(backend, dict) or not str(backend.get("id") or ""):
        raise FrozenTaskPackError(f"pack {spec.dataset} backend is invalid")
    backend_sha = _sha256(backend.get("sha256"))
    rows = manifest.get("tasks")
    if not isinstance(rows, list) or len(rows) != spec.task_count:
        raise FrozenTaskPackError(
            f"pack {spec.dataset} requires exactly {spec.task_count} tasks")
    task_ids = [row.get("taskId") if isinstance(row, dict) else None
                for row in rows]
    if task_ids != sorted(task_ids, key=lambda value: str(value or "")):
        raise FrozenTaskPackError(
            f"pack {spec.dataset} tasks must be sorted by taskId")

    tasks: list[FrozenTask] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    observed_tags: set[str] = set()
    required_prefix = f"{spec.dataset}/"
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FrozenTaskPackError(
                f"pack {spec.dataset} task row {index} is not an object")
        task_id = str(row.get("taskId") or "")
        if not task_id.startswith(required_prefix) or task_id in seen_ids:
            raise FrozenTaskPackError(
                f"pack {spec.dataset} task identity is invalid: {task_id!r}")
        path = _resolve_child(root, row.get("path"))
        if path in seen_paths:
            raise FrozenTaskPackError(
                f"pack {spec.dataset} repeats task path: {path.name}")
        expected_sha = _sha256(row.get("sha256"))
        raw_task = _read_bytes(path, f"task {task_id}")
        actual_sha = hashlib.sha256(raw_task).hexdigest()
        if actual_sha != expected_sha:
            raise FrozenTaskPackError(
                f"task {task_id} SHA-256 mismatch")
        task = _validate_task(
            _load_json_bytes(raw_task, f"task {task_id}"),
            spec=spec, expected_id=task_id, path=path, digest=actual_sha)
        tasks.append(task)
        observed_tags.update(task.tags)
        seen_ids.add(task_id)
        seen_paths.add(path)

    required_tags = set(_nonempty_strings(
        manifest.get("requiredTags"), "pack.requiredTags"))
    missing_tags = sorted(required_tags - observed_tags)
    if missing_tags:
        raise FrozenTaskPackError(
            f"pack {spec.dataset} does not cover required tags: {missing_tags}")
    return FrozenTaskPack(
        family=spec.family,
        dataset=spec.dataset,
        world_version=world_version,
        backend_id=str(backend["id"]),
        backend_sha256=backend_sha,
        manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        tasks=tuple(tasks),
    )


def load_all_custom_packs(root: Path) -> tuple[FrozenTaskPack, ...]:
    """Load all five required packs from ``ROOT/<dataset>/pack.json``."""
    private_root = _private_root(root)
    packs = []
    for spec in CUSTOM_PACK_SPECS:
        packs.append(load_frozen_task_pack(
            private_root / spec.dataset / "pack.json", spec))
    return tuple(packs)


__all__ = [
    "CUSTOM_PACK_SPECS", "FROZEN_TASK_PACK_VERSION", "FROZEN_TASK_VERSION",
    "FrozenPackSpec", "FrozenTask", "FrozenTaskPack",
    "FrozenTaskPackError", "load_all_custom_packs",
    "load_frozen_task_pack",
]
