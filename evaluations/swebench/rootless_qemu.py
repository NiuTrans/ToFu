from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rootless_vm.image_cache import sha256_file
from rootless_vm.qemu import QemuRuntime
from rootless_vm.session import LoopbackServiceForward

from .constants import BenchmarkDefinition


def rootless_sandbox_identity(config: dict[str, Any]) -> dict[str, object]:
    """Project stable VM/network controls, excluding run-specific paths/ports."""

    environment = config.get("environment") or {}
    kwargs = environment.get("kwargs") or {}
    if environment.get("import_path") != (
        "rootless_vm.harbor_environment:RootlessQemuEnvironment"
    ) or not isinstance(kwargs, dict):
        raise ValueError("rootless sandbox identity requires QEMU environment kwargs")

    def digest(value: object, label: str) -> str:
        path = Path(str(value or "")).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"{label} is not a regular file: {path}")
        return sha256_file(path)

    return {
        "kind": "rootless-qemu",
        "networkPolicy": "rootless-restricted-task-egress-v1",
        "baseDiskSha256": str(kwargs.get("base_disk_sha256") or ""),
        "qemuSha256": digest(kwargs.get("qemu_path"), "QEMU executable"),
        "qemuImgSha256": digest(
            kwargs.get("qemu_img_path"), "qemu-img executable"
        ),
        "vmCpus": int(kwargs.get("vm_cpus") or 0),
        "egressMaxBytes": int(kwargs.get("egress_max_bytes") or 0),
        "egressGlobalConcurrency": int(
            kwargs.get("egress_global_concurrency") or 0
        ),
    }


def _private_dir(path: Path, label: str, *, create: bool) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    if create:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory: {resolved}")
    if resolved.stat().st_mode & 0o077:
        raise PermissionError(f"{label} must not be group/world accessible: {resolved}")
    return resolved


def _checked_file(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file: {resolved}")
    return resolved


def load_image_store_index(
    image_store: Path,
    definition: BenchmarkDefinition,
    *,
    required_tasks: tuple[str, ...] = (),
) -> tuple[Path, dict[str, Any]]:
    store = _private_dir(image_store, "rootless image store", create=False)
    index_path = store / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise ValueError(f"rootless image store index is missing: {index_path}")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("rootless image store index is not valid JSON") from exc
    images = index.get("images") if isinstance(index, dict) else None
    if not isinstance(index, dict) or index.get("schema") != 1 or not isinstance(images, dict):
        raise ValueError("rootless image store index has an unsupported schema")
    expected_metadata = {
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": definition.task_count,
    }
    mismatches = {
        key: (index.get(key), expected)
        for key, expected in expected_metadata.items()
        if index.get(key) != expected
    }
    if mismatches:
        raise ValueError(f"rootless image store dataset identity mismatch: {mismatches}")
    task_owners: dict[str, str] = {}
    for image, metadata in images.items():
        if not isinstance(image, str) or not image or not isinstance(metadata, dict):
            raise ValueError("rootless image store contains an invalid image entry")
        tasks = metadata.get("tasks")
        if not isinstance(tasks, list) or not tasks or not all(
            isinstance(task, str) and task for task in tasks
        ):
            raise ValueError(f"rootless image store has invalid task metadata: {image}")
        for task in tasks:
            if task in task_owners:
                raise ValueError(f"rootless image store maps a task more than once: {task}")
            task_owners[task] = image
    if required_tasks:
        missing = sorted(set(required_tasks) - task_owners.keys())
        if missing:
            rendered = ", ".join(missing[:10])
            raise ValueError(
                f"rootless image store is missing {len(missing)} requested tasks: {rendered}"
            )
    elif len(images) != definition.task_count or len(task_owners) != definition.task_count:
        raise ValueError(
            "rootless image store is incomplete: "
            f"found {len(images)} images/{len(task_owners)} tasks, "
            f"expected {definition.task_count}"
        )
    return store, index


@dataclass(frozen=True)
class RootlessQemuSettings:
    base_disk: Path
    image_store: Path
    qemu_path: Path | None = None
    qemu_img_path: Path | None = None
    state_root: Path | None = None
    prepared_cache_root: Path | None = None
    egress_max_gib: int = 4
    egress_global_concurrency: int = 16
    vm_cpus: int = 2
    loopback_services: tuple[LoopbackServiceForward, ...] = ()

    @classmethod
    def from_environment_kwargs(cls, value: dict[str, Any]) -> "RootlessQemuSettings":
        required = ("base_disk", "image_store")
        missing = [name for name in required if not value.get(name)]
        if missing:
            raise ValueError(f"rootless environment config is missing {missing}")
        egress_bytes = int(value.get("egress_max_bytes") or 4 * 1024**3)
        if egress_bytes % 1024**3:
            raise ValueError("rootless egress_max_bytes must be a whole number of GiB")
        raw_services = value.get("loopback_service_forwards") or []
        if not isinstance(raw_services, list):
            raise ValueError("rootless loopback_service_forwards must be a list")
        try:
            services = tuple(
                LoopbackServiceForward(**row)
                for row in raw_services
                if isinstance(row, dict)
            )
        except TypeError as exc:
            raise ValueError("rootless loopback service fields are invalid") from exc
        if len(services) != len(raw_services):
            raise ValueError("rootless loopback services must be objects")
        return cls(
            base_disk=Path(str(value["base_disk"])),
            image_store=Path(str(value["image_store"])),
            qemu_path=(Path(str(value["qemu_path"])) if value.get("qemu_path") else None),
            qemu_img_path=(
                Path(str(value["qemu_img_path"]))
                if value.get("qemu_img_path")
                else None
            ),
            state_root=(Path(str(value["state_root"])) if value.get("state_root") else None),
            prepared_cache_root=(
                Path(str(value["prepared_cache_root"]))
                if value.get("prepared_cache_root")
                else None
            ),
            egress_max_gib=egress_bytes // 1024**3,
            egress_global_concurrency=int(
                value.get("egress_global_concurrency") or 16
            ),
            vm_cpus=int(value.get("vm_cpus") or 2),
            loopback_services=services,
        )

    def validate(
        self,
        definition: BenchmarkDefinition,
        *,
        runtime_probe: bool,
        required_tasks: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        base_disk = _checked_file(self.base_disk, "rootless base disk")
        store, index = load_image_store_index(
            self.image_store,
            definition,
            required_tasks=required_tasks,
        )
        runtime = QemuRuntime.discover(self.qemu_path, self.qemu_img_path)
        if runtime.qemu_img is None:
            raise ValueError("rootless-qemu requires qemu-img")
        if not 1 <= self.egress_max_gib <= 1024:
            raise ValueError("rootless egress limit must be between 1 and 1024 GiB")
        if not 1 <= self.egress_global_concurrency <= 128:
            raise ValueError("rootless egress concurrency must be between 1 and 128")
        if not 1 <= self.vm_cpus <= 256:
            raise ValueError("rootless VM CPUs must be between 1 and 256")
        if not isinstance(self.loopback_services, tuple):
            raise ValueError("rootless loopback services must be an immutable tuple")
        names: set[str] = set()
        endpoints: set[tuple[str, int]] = set()
        if len(self.loopback_services) > 8:
            raise ValueError("rootless loopback services are limited to 8 entries")
        for service in self.loopback_services:
            if not isinstance(service, LoopbackServiceForward):
                raise ValueError("rootless loopback service type is invalid")
            service.validate()
            endpoint = (service.guest_host, service.guest_port)
            if service.name in names or endpoint in endpoints:
                raise ValueError("rootless loopback services must be unique")
            names.add(service.name)
            endpoints.add(endpoint)
        report = runtime.preflight().to_dict() if runtime_probe else None
        result: dict[str, object] = {
            "base_disk": str(base_disk),
            "base_disk_sha256": sha256_file(base_disk),
            "image_store": str(store),
            "image_count": len(index["images"]),
            "qemu": str(runtime.qemu),
            "qemu_img": str(runtime.qemu_img),
            "runtime_preflight": report,
        }
        return result

    def environment_kwargs(
        self,
        definition: BenchmarkDefinition,
        *,
        run_dir: Path,
        required_tasks: tuple[str, ...] = (),
    ) -> dict[str, object]:
        resolved = self.validate(
            definition,
            runtime_probe=False,
            required_tasks=required_tasks,
        )
        state_root = _private_dir(
            self.state_root or run_dir / ".runtime-state",
            "rootless state root",
            create=True,
        )
        cache_root = _private_dir(
            self.prepared_cache_root
            or run_dir.parent / ".image-cache" / "rootless-qemu",
            "rootless prepared cache root",
            create=True,
        )
        result: dict[str, object] = {
            "base_disk": resolved["base_disk"],
            "base_disk_sha256": resolved["base_disk_sha256"],
            "image_store": resolved["image_store"],
            "state_root": str(state_root),
            "prepared_cache_root": str(cache_root),
            "qemu_path": resolved["qemu"],
            "qemu_img_path": resolved["qemu_img"],
            "egress_max_bytes": self.egress_max_gib * 1024**3,
            "egress_global_concurrency": self.egress_global_concurrency,
            "vm_cpus": self.vm_cpus,
            "image_prepare_timeout_sec": 7200,
        }
        if self.loopback_services:
            result["loopback_service_forwards"] = [
                asdict(service) for service in self.loopback_services
            ]
        return result
