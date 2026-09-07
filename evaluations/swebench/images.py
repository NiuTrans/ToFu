from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import random
import shutil
import subprocess
import tarfile
import threading
import time
import tomllib
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rootless_vm.dockerfile import dockerfile_base_image
from rootless_vm.image_cache import PreparedImageCache, PreparedImageSpec, sha256_file
from rootless_vm.image_store import resolve_image_store
from rootless_vm.qemu import QemuRuntime

from .artifacts import atomic_write_json
from .constants import (
    BenchmarkDefinition,
    swebench_verified_task_digests,
    terminal_bench_21_task_digests,
)


_ASSET_SCHEMA = 1
_INDEX_SCHEMA = 1
_DEFINITIONS_SCHEMA = 1
_PRINT_LOCK = threading.Lock()
_CRANE_VERSION = "v0.21.9"
_CRANE_ARCHIVE_SHA256 = (
    "5c16d8ddb971cb1d5e6ed8b1e743da8224414eeba2c2762d8f1a61b2f095699e"
)
_CRANE_ARCHIVE_URL = (
    "https://github.com/google/go-containerregistry/releases/download/"
    f"{_CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz"
)
_MAX_CRANE_ARCHIVE_BYTES = 128 * 1024 * 1024


def _pinned_task_digests(
    definition: BenchmarkDefinition,
) -> dict[str, str] | None:
    if definition.key == "swebench-verified":
        return swebench_verified_task_digests()
    if definition.key == "terminal-bench-2.1":
        return terminal_bench_21_task_digests()
    return None


def _say(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def _private_dir(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.stat().st_mode & 0o077:
        raise PermissionError(f"{label} must be a private directory: {resolved}")
    return resolved


def _executable(value: str | Path, label: str) -> Path:
    rendered = str(value)
    resolved = shutil.which(rendered)
    if resolved is None:
        candidate = Path(rendered).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = str(candidate.resolve())
    if resolved is None:
        raise ValueError(f"{label} executable not found: {value}")
    return Path(resolved).resolve()


def _download_pinned_crane(tools_root: Path) -> Path:
    """Install a checksum-pinned crane binary without root or host packages."""

    tools_root = _private_dir(tools_root, "rootless tool cache")
    executable = tools_root / f"crane-{_CRANE_VERSION}"
    marker = tools_root / f"crane-{_CRANE_VERSION}.json"
    if executable.is_file() and marker.is_file() and not (
        executable.is_symlink() or marker.is_symlink()
    ):
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        if (
            metadata.get("version") == _CRANE_VERSION
            and metadata.get("archive_sha256") == _CRANE_ARCHIVE_SHA256
            and metadata.get("executable_sha256") == sha256_file(executable)
        ):
            executable.chmod(0o700)
            return executable.resolve()

    token = uuid.uuid4().hex
    archive = tools_root / f".crane.{token}.tar.gz.partial"
    candidate = tools_root / f".crane.{token}.partial"
    try:
        error: Exception | None = None
        for attempt in range(6):
            digest = hashlib.sha256()
            size = 0
            try:
                request = urllib.request.Request(
                    _CRANE_ARCHIVE_URL,
                    headers={"User-Agent": "tofu-rootless-eval/1"},
                )
                with urllib.request.urlopen(request, timeout=120) as response, archive.open(
                    "wb"
                ) as stream:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > _MAX_CRANE_ARCHIVE_BYTES:
                            raise RuntimeError("crane release archive exceeds 128 MiB")
                        digest.update(chunk)
                        stream.write(chunk)
                if digest.hexdigest() != _CRANE_ARCHIVE_SHA256:
                    raise RuntimeError("crane release archive SHA-256 mismatch")
                error = None
                break
            except Exception as exc:
                error = exc
                archive.unlink(missing_ok=True)
                if attempt + 1 < 6:
                    time.sleep(min(30.0, 2.0**attempt) + random.random())
        if error is not None:
            raise RuntimeError(f"could not download pinned crane: {error}") from error

        with tarfile.open(archive, mode="r:gz") as bundle:
            members = [
                member
                for member in bundle.getmembers()
                if member.name.removeprefix("./") == "crane"
            ]
            if len(members) != 1 or not members[0].isreg():
                raise RuntimeError("crane release archive has an unexpected layout")
            member = members[0]
            if not 0 < member.size <= 100 * 1024 * 1024:
                raise RuntimeError("crane executable has an invalid size")
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError("crane executable could not be read")
            with candidate.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        candidate.chmod(0o700)
        executable_sha256 = sha256_file(candidate)
        os.replace(candidate, executable)
        executable.chmod(0o700)
        atomic_write_json(
            marker,
            {
                "version": _CRANE_VERSION,
                "source": _CRANE_ARCHIVE_URL,
                "archive_sha256": _CRANE_ARCHIVE_SHA256,
                "executable_sha256": executable_sha256,
            },
        )
        return executable.resolve()
    finally:
        archive.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def resolve_crane(value: str | Path, tools_root: Path) -> Path:
    if str(value) == "auto":
        return _download_pinned_crane(tools_root)
    return _executable(value, "crane")


def write_payload_iso(payload: Path, output: Path) -> None:
    """Write one tar payload to an ISO without a root-owned system tool."""
    try:
        import pycdlib
    except ImportError as exc:
        raise RuntimeError(
            "pycdlib is required for rootless ISO creation; install the pinned "
            "evaluations/swebench/requirements.txt"
        ) from exc
    image = pycdlib.PyCdlib()
    try:
        image.new(interchange_level=3, joliet=3, rock_ridge="1.09")
        image.add_file(
            str(payload),
            iso_path="/PAYLOAD.TAR;1",
            rr_name="payload.tar",
            joliet_path="/payload.tar",
        )
        image.write(str(output))
    finally:
        image.close()


def _run(command: list[str], *, timeout: float) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"command failed ({result.returncode}): {detail}")
    return result.stdout


def _network_run(command: list[str], *, timeout: float, attempts: int = 6) -> str:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            return _run(command, timeout=timeout)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(min(30.0, 2.0**attempt) + random.random())
    assert error is not None
    raise error


def _repository_without_tag(reference: str) -> str:
    reference = reference.split("@", 1)[0]
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    return reference[:colon] if colon > slash else reference


@dataclass(frozen=True)
class TaskDefinition:
    name: str
    ref: str
    path: Path
    base_image: str
    cpus: int
    memory_mib: int
    dockerfile: bool


async def _download_definitions_async(
    definition: BenchmarkDefinition,
    root: Path,
    *,
    workers: int,
    attempts: int,
) -> None:
    from harbor.registry.client.package import PackageDatasetClient

    client = PackageDatasetClient()
    metadata = await client.get_dataset_metadata(definition.dataset)
    if str(metadata.version) != definition.dataset_source_revision:
        raise ValueError(
            "registry dataset digest changed: "
            f"{metadata.version} != {definition.dataset_source_revision}"
        )
    if len(metadata.task_ids) != definition.task_count:
        raise ValueError(
            f"registry returned {len(metadata.task_ids)} tasks, expected {definition.task_count}"
        )
    pinned_digests = _pinned_task_digests(definition)
    actual_digests = {
        task_id.get_name(): str(task_id.ref) for task_id in metadata.task_ids
    }
    if pinned_digests is not None and actual_digests != pinned_digests:
        raise ValueError(
            f"registry {definition.key} task digests differ from the pinned lock"
        )
    semaphore = asyncio.Semaphore(workers)
    progress_lock = asyncio.Lock()
    completed = 0
    failures: list[tuple[str, str]] = []

    async def one(task_id) -> None:
        nonlocal completed
        async with semaphore:
            for attempt in range(attempts):
                try:
                    await client._task_client.download_tasks([task_id], output_dir=root)
                    async with progress_lock:
                        completed += 1
                        if completed % 25 == 0 or completed == definition.task_count:
                            _say(f"DEFINITIONS {completed}/{definition.task_count}")
                    return
                except Exception as exc:
                    if attempt + 1 == attempts:
                        failures.append(
                            (task_id.get_name(), f"{type(exc).__name__}: {exc}")
                        )
                        return
                    await asyncio.sleep(min(30.0, 2.0**attempt) + random.random())

    await asyncio.gather(*(one(task_id) for task_id in metadata.task_ids))
    if failures:
        rendered = "; ".join(f"{name}: {detail[:200]}" for name, detail in failures[:10])
        raise RuntimeError(f"definition download failed for {len(failures)} tasks: {rendered}")
    rows = []
    for task_id in metadata.task_ids:
        digest = str(task_id.ref).removeprefix("sha256:")
        relative = Path(task_id.get_name()) / digest
        task_path = root / relative
        if not (task_path / "task.toml").is_file():
            raise FileNotFoundError(f"downloaded task is incomplete: {task_path}")
        rows.append(
            {
                "name": task_id.get_name(),
                "ref": str(task_id.ref),
                "path": relative.as_posix(),
            }
        )
    atomic_write_json(
        root / "definitions.json",
        {
            "schema": _DEFINITIONS_SCHEMA,
            "benchmark": definition.key,
            "dataset": definition.dataset,
            "dataset_source_revision": definition.dataset_source_revision,
            "task_count": definition.task_count,
            "tasks": rows,
        },
    )


def prepare_definitions(
    definition: BenchmarkDefinition,
    root: Path,
    *,
    workers: int = 4,
    attempts: int = 6,
) -> list[TaskDefinition]:
    if not 1 <= workers <= 16:
        raise ValueError("definition workers must be between 1 and 16")
    if not 1 <= attempts <= 10:
        raise ValueError("definition attempts must be between 1 and 10")
    root = _private_dir(root, "definition cache")
    if (root / "definitions.json").is_file():
        return load_definitions(definition, root)
    asyncio.run(
        _download_definitions_async(
            definition,
            root,
            workers=workers,
            attempts=attempts,
        )
    )
    return load_definitions(definition, root)


def load_definitions(
    definition: BenchmarkDefinition,
    root: Path,
) -> list[TaskDefinition]:
    root = _private_dir(root, "definition cache")
    manifest_path = root / "definitions.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid definition cache manifest: {manifest_path}") from exc
    expected = {
        "schema": _DEFINITIONS_SCHEMA,
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": definition.task_count,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"definition cache {key} mismatch")
    rows = manifest.get("tasks")
    if not isinstance(rows, list) or len(rows) != definition.task_count:
        raise ValueError("definition cache task cardinality mismatch")
    pinned_digests = _pinned_task_digests(definition)
    if pinned_digests is not None:
        actual_digests: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("definition cache task row is not an object")
            name, ref = row.get("name"), row.get("ref")
            if not isinstance(name, str) or not isinstance(ref, str):
                raise ValueError("definition cache task row has invalid fields")
            if name in actual_digests:
                raise ValueError(f"definition cache task identity mismatch: {name}")
            actual_digests[name] = ref
        if actual_digests != pinned_digests:
            raise ValueError("definition cache task digest lock mismatch")
    tasks: list[TaskDefinition] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("definition cache task row is not an object")
        name, ref, relative = row.get("name"), row.get("ref"), row.get("path")
        if not all(isinstance(value, str) and value for value in (name, ref, relative)):
            raise ValueError("definition cache task row has invalid fields")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("definition cache task path escapes its root")
        path = (root / relative_path).resolve(strict=True)
        if root not in path.parents or path.is_symlink():
            raise ValueError("definition cache task path escapes its private root")
        config = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
        task_name = config.get("task", {}).get("name")
        if task_name != name or name in seen:
            raise ValueError(f"definition cache task identity mismatch: {name}")
        seen.add(name)
        environment = config.get("environment", {})
        image = environment.get("docker_image")
        has_dockerfile = (path / "environment" / "Dockerfile").is_file()
        if image:
            if not isinstance(image, str):
                raise ValueError(f"task docker_image is invalid: {name}")
        elif has_dockerfile:
            image = dockerfile_base_image(path / "environment")
        else:
            raise ValueError(f"task has neither docker_image nor Dockerfile: {name}")
        tasks.append(
            TaskDefinition(
                name=name,
                ref=ref,
                path=path,
                base_image=image,
                cpus=int(environment.get("cpus") or 1),
                memory_mib=int(environment.get("memory_mb") or 2048),
                dockerfile=has_dockerfile,
            )
        )
    return tasks


def _valid_asset(image: str, asset_dir: Path) -> dict[str, Any] | None:
    metadata_path = asset_dir / "asset.json"
    iso = asset_dir / "payload.iso"
    if metadata_path.is_symlink() or iso.is_symlink():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if metadata.get("schema") != _ASSET_SCHEMA or metadata.get("image") != image:
        return None
    digest = metadata.get("iso_sha256")
    if not isinstance(digest, str) or not iso.is_file() or sha256_file(iso) != digest:
        return None
    return metadata


def _prepare_asset(
    image: str,
    *,
    store: Path,
    crane: Path,
    archive_tool: Path,
    genisoimage: Path | None,
) -> dict[str, Any]:
    key = hashlib.sha256(image.encode()).hexdigest()
    images_root = store / "images"
    images_root.mkdir(mode=0o700, exist_ok=True)
    images_root.chmod(0o700)
    asset_dir = images_root / key
    asset_dir.mkdir(mode=0o700, exist_ok=True)
    existing = _valid_asset(image, asset_dir)
    if existing is not None:
        registry_digest = existing.get("registry_digest")
        if isinstance(registry_digest, str) and registry_digest.startswith("sha256:"):
            existing["loaded_image_reference"] = registry_digest
            atomic_write_json(asset_dir / "asset.json", existing)
        _say(f"ASSET HIT {image}")
        return existing
    manifest_digest = _network_run(
        [str(crane), "digest", "--platform", "linux/amd64", image],
        timeout=180,
    ).strip()
    if not manifest_digest.startswith("sha256:"):
        raise RuntimeError(f"registry returned invalid digest for {image}")
    pinned = f"{_repository_without_tag(image)}@{manifest_digest}"
    manifest = json.loads(
        _network_run(
            [str(crane), "manifest", "--platform", "linux/amd64", pinned],
            timeout=180,
        )
    )
    config_digest = manifest.get("config", {}).get("digest")
    if not isinstance(config_digest, str) or not config_digest.startswith("sha256:"):
        raise RuntimeError(f"registry manifest has no config digest: {image}")
    token = uuid.uuid4().hex
    oci_path = asset_dir / f".oci.{token}.partial"
    tar_path = asset_dir / f".payload.{token}.tar.partial"
    iso_path = asset_dir / f".payload.{token}.iso.partial"
    staging = asset_dir / f".staging.{token}"
    staging.mkdir(mode=0o700)
    staged_tar = staging / "payload.tar"
    try:
        for attempt in range(6):
            try:
                if oci_path.exists():
                    shutil.rmtree(oci_path)
                _run(
                    [
                        str(crane),
                        "pull",
                        "--platform",
                        "linux/amd64",
                        "--format",
                        "oci",
                        pinned,
                        str(oci_path),
                    ],
                    timeout=7200,
                )
                break
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                if attempt == 5:
                    raise
                time.sleep(min(30.0, 2.0**attempt) + random.random())
        index_path = oci_path / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or len(descriptors) != 1:
            raise RuntimeError(f"OCI index has an unexpected shape: {image}")
        descriptor = descriptors[0]
        if not isinstance(descriptor, dict) or descriptor.get("digest") != manifest_digest:
            raise RuntimeError(f"OCI index digest mismatch: {image}")
        descriptor["annotations"] = {"org.opencontainers.image.ref.name": image}
        atomic_write_json(index_path, index)
        _run(
            [str(archive_tool), "-cf", str(tar_path), "-C", str(oci_path), "."],
            timeout=7200,
        )
        if genisoimage is None:
            write_payload_iso(tar_path, iso_path)
        else:
            os.link(tar_path, staged_tar)
            _run(
                [
                    str(genisoimage),
                    "-quiet",
                    "-o",
                    str(iso_path),
                    "-iso-level",
                    "3",
                    "-J",
                    "-R",
                    str(staged_tar),
                ],
                timeout=7200,
            )
        final_iso = asset_dir / "payload.iso"
        os.replace(iso_path, final_iso)
        final_iso.chmod(0o600)
        metadata = {
            "schema": _ASSET_SCHEMA,
            "image": image,
            "registry_digest": manifest_digest,
            "image_config_digest": config_digest,
            "loaded_image_reference": manifest_digest,
            "iso_sha256": sha256_file(final_iso),
            "iso_bytes": final_iso.stat().st_size,
        }
        atomic_write_json(asset_dir / "asset.json", metadata)
        _say(f"ASSET OK {image} {final_iso.stat().st_size / 2**20:.1f} MiB")
        return metadata
    finally:
        for path in (staged_tar, tar_path, iso_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            staging.rmdir()
        except OSError:
            pass
        if oci_path.exists():
            shutil.rmtree(oci_path)


def prepare_image_store(
    definition: BenchmarkDefinition,
    tasks: list[TaskDefinition],
    store: Path,
    *,
    crane: str | Path,
    archive_tool: str | Path = "tar",
    genisoimage: str | Path | None = None,
    workers: int = 4,
    require_full: bool = True,
) -> Path:
    if not 1 <= workers <= 16:
        raise ValueError("asset workers must be between 1 and 16")
    store = _private_dir(store, "rootless image store")
    crane_path = resolve_crane(crane, store.parent / ".tools")
    archive_path = _executable(archive_tool, "archive tool")
    iso_path = _executable(genisoimage, "genisoimage") if genisoimage else None
    by_image: dict[str, list[TaskDefinition]] = {}
    for task in tasks:
        by_image.setdefault(task.base_image, []).append(task)
    if len({task.name for task in tasks}) != len(tasks):
        raise ValueError("selected task definitions contain duplicate names")
    if len(by_image) != len(tasks):
        raise ValueError(
            "this pinned dataset is expected to have one base image per task; "
            f"found {len(by_image)} images for {len(tasks)} selected tasks"
        )
    if require_full and len(tasks) != definition.task_count:
        raise ValueError(
            f"full image preparation requires {definition.task_count} tasks, "
            f"found {len(tasks)}"
        )
    completed: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _prepare_asset,
                image,
                store=store,
                crane=crane_path,
                archive_tool=archive_path,
                genisoimage=iso_path,
            ): image
            for image in by_image
        }
        for future in concurrent.futures.as_completed(futures):
            image = futures[future]
            completed[image] = future.result()
            if len(completed) % 10 == 0 or len(completed) == len(by_image):
                _say(f"ASSETS {len(completed)}/{len(by_image)}")
    images: dict[str, dict[str, Any]] = {}
    existing_index = store / "index.json"
    if existing_index.is_file() and not existing_index.is_symlink():
        try:
            previous = json.loads(existing_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("rootless image store index is not valid JSON") from exc
        expected = {
            "schema": _INDEX_SCHEMA,
            "benchmark": definition.key,
            "dataset": definition.dataset,
            "dataset_source_revision": definition.dataset_source_revision,
            "task_count": definition.task_count,
        }
        if any(previous.get(key) != value for key, value in expected.items()):
            raise ValueError("rootless image store dataset identity mismatch")
        prior_images = previous.get("images")
        if not isinstance(prior_images, dict):
            raise ValueError("rootless image store index images are invalid")
        images.update(prior_images)
    for image, metadata in completed.items():
        key = hashlib.sha256(image.encode()).hexdigest()
        images[image] = {
            "iso": f"images/{key}/payload.iso",
            "sha256": metadata["iso_sha256"],
            "loaded_image_reference": metadata["loaded_image_reference"],
            "registry_digest": metadata["registry_digest"],
            "tasks": sorted(task.name for task in by_image[image]),
        }
    atomic_write_json(
        store / "index.json",
        {
            "schema": _INDEX_SCHEMA,
            "benchmark": definition.key,
            "dataset": definition.dataset,
            "dataset_source_revision": definition.dataset_source_revision,
            "task_count": definition.task_count,
            "prepared_task_count": len(
                {
                    task
                    for metadata in images.values()
                    if isinstance(metadata, dict)
                    for task in metadata.get("tasks", [])
                    if isinstance(task, str)
                }
            ),
            "complete": len(images) == definition.task_count,
            "images": images,
        },
    )
    return store


def prepare_caches(
    tasks: list[TaskDefinition],
    *,
    image_store: Path,
    cache_root: Path,
    base_disk: Path,
    qemu: str | Path | None,
    qemu_img: str | Path | None,
    workers: int = 2,
) -> None:
    if not 1 <= workers <= 8:
        raise ValueError("cache workers must be between 1 and 8")
    cache_root = _private_dir(cache_root, "rootless prepared cache")
    runtime = QemuRuntime.discover(qemu, qemu_img)
    if runtime.qemu_img is None:
        raise ValueError("qemu-img is required to prepare rootless caches")
    base_disk = base_disk.expanduser().resolve(strict=True)
    base_sha = sha256_file(base_disk)
    completed = 0

    def one(task: TaskDefinition):
        iso, iso_sha, loaded_reference = resolve_image_store(
            image_store, task.base_image
        )
        return PreparedImageCache(
            PreparedImageSpec(
                runtime=runtime,
                cache_root=cache_root,
                base_disk=base_disk,
                payload_iso=iso,
                task_image=loaded_reference,
                build_context=(task.path / "environment" if task.dockerfile else None),
                expected_base_disk_sha256=base_sha,
                expected_payload_iso_sha256=iso_sha,
                memory_mib=max(512, task.memory_mib),
                cpus=max(2, task.cpus),
                boot_timeout_sec=360,
                prepare_timeout_sec=7200,
            )
        ).prepare()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, task): task for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            result = future.result()
            completed += 1
            _say(
                f"CACHE {'HIT' if result.cache_hit else 'OK'} {task.name} "
                f"{completed}/{len(tasks)} {result.elapsed_sec:.1f}s"
            )
