"""Bounded prepared-image prewarm for rootless-qemu benchmark runs.

Cold prepared-image caches were the dominant environment-start failure in
tb21 v11: a cache miss builds the task image inside a throwaway VM and can
exceed harbor's outer environment-start cap, killing the trial before the
agent runs. Preparing every pinned task's image up front turns trial-time
environment starts into ~2s cache hits.

The module reconstructs the exact ``PreparedImageSpec`` the trial-time
environment builds (same base disk, payload ISO, loaded image reference and
checksums — memory/cpu/timeout fields do not enter the recipe digest), so a
prewarmed entry is guaranteed to be the entry the trial looks up.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from rootless_vm.image_cache import PreparedImageCache, PreparedImageSpec
from rootless_vm.qemu import QemuRuntime

from .artifacts import atomic_write_json
from .constants import BenchmarkDefinition
from .rootless_qemu import RootlessQemuSettings, load_image_store_index

_MAX_CONCURRENCY = 4


def _store_entry_for_task(index: dict[str, Any], task_name: str) -> dict[str, Any]:
    images = index.get("images") or {}
    short = task_name.rsplit("/", 1)[-1]
    for key, entry in images.items():
        if not isinstance(entry, dict):
            continue
        tasks = entry.get("tasks")
        if isinstance(tasks, list) and task_name in {str(t) for t in tasks}:
            return entry
        if str(entry.get("task") or "") == short:
            return entry
    raise ValueError(f"image store has no entry for pinned task: {task_name}")


def prewarm_prepared_images(
    settings: RootlessQemuSettings,
    definition: BenchmarkDefinition,
    task_names: tuple[str, ...],
    *,
    cache_root: Path,
    concurrency: int = 2,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    """Prepare every pinned task's image once; return a bounded receipt.

    Cache hits return in seconds, so re-running the prewarm is cheap and the
    operation is idempotent. Failures are reported per task instead of
    aborting the batch: a task that cannot be prewarmed is rebuilt at trial
    time under the lifted environment-start budget.
    """
    if not task_names:
        raise ValueError("task_names must not be empty")
    if len(set(task_names)) != len(task_names):
        raise ValueError("task_names must be unique")
    if not 1 <= concurrency <= _MAX_CONCURRENCY:
        raise ValueError(f"prewarm concurrency must be in 1..{_MAX_CONCURRENCY}")
    resolved = settings.validate(
        definition, runtime_probe=False, required_tasks=task_names)
    store, index = load_image_store_index(
        settings.image_store, definition, required_tasks=task_names)
    runtime = QemuRuntime.discover(settings.qemu_path, settings.qemu_img_path)
    base_disk = Path(str(resolved["base_disk"]))
    base_disk_sha256 = str(resolved["base_disk_sha256"])

    specs: dict[str, PreparedImageSpec] = {}
    for task_name in task_names:
        entry = _store_entry_for_task(index, task_name)
        payload_iso = store / str(entry["iso"])
        specs[task_name] = PreparedImageSpec(
            runtime=runtime,
            cache_root=Path(cache_root),
            base_disk=base_disk,
            payload_iso=payload_iso,
            task_image=str(entry["loaded_image_reference"]),
            python_runtime_image=None,
            build_context=None,
            expected_base_disk_sha256=base_disk_sha256,
            expected_payload_iso_sha256=str(entry["sha256"]),
            memory_mib=2048,
            cpus=2,
            boot_timeout_sec=360.0,
            prepare_timeout_sec=7200.0,
        )

    progress: dict[str, Any] = {
        "schema": "tofu.prepared-prewarm/v1",
        "cacheRoot": str(cache_root),
        "taskCount": len(task_names),
        "completed": 0,
        "failed": 0,
        "tasks": {},
    }

    def _record() -> None:
        if progress_path is not None:
            atomic_write_json(progress_path, progress)

    def _prepare_one(task_name: str) -> dict[str, Any]:
        started = time.monotonic()
        cache = PreparedImageCache(specs[task_name])
        result = cache.prepare()
        return {
            "recipeDigest": cache.recipe.digest,
            "cacheHit": bool(result.cache_hit),
            "elapsedSec": round(time.monotonic() - started, 3),
        }

    _record()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_prepare_one, task_name): task_name
            for task_name in task_names
        }
        for future in as_completed(futures):
            task_name = futures[future]
            try:
                row = future.result()
                row["ok"] = True
            except Exception as exc:  # noqa: BLE001 — bounded receipt, no abort
                row = {"ok": False, "errorType": type(exc).__name__,
                       "error": str(exc)[:500]}
                progress["failed"] += 1
            progress["tasks"][task_name] = row
            progress["completed"] += 1
            _record()
    progress["ok"] = progress["failed"] == 0
    _record()
    return progress


__all__ = ["prewarm_prepared_images"]
