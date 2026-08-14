"""Digest-addressed, offline-prepared VM images for fast repeated trials."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .qemu import QemuRuntime, _isolated_subprocess_env
from .session import NetworkMode, SandboxSession, SandboxSpec


_SCHEMA = 4
_MARKER = "prepared-image.json"
_DISK = "prepared.qcow2"


def sha256_file(path: Path) -> str:
    """Hash current bytes without trusting filesystem timestamp granularity.

    Some network filesystems can preserve nanosecond ``mtime`` and ``ctime``
    across a rapid same-size rewrite.  A stat-keyed digest cache would then
    accept different bytes as the old content-addressed object.  Benchmark
    inputs are hashed only at lifecycle boundaries, where the extra read is a
    small price for an actual integrity guarantee.
    """

    digest = hashlib.sha256()
    with path.resolve(strict=True).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_dir(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError(f"cache path must not be a symbolic link: {path}")
    path = path.resolve()
    if path.exists():
        if not path.is_dir() or path.stat().st_mode & 0o077:
            raise PermissionError(f"cache path must be a private directory: {path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    return path


def _checked_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file: {resolved}")
    return resolved


def _normalized_sha256(value: str | None, actual: str, label: str) -> str:
    if value is None:
        return actual
    expected = value.removeprefix("sha256:").lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise ValueError(f"{label} SHA-256 must contain 64 hexadecimal digits")
    if expected != actual:
        raise ValueError(f"{label} SHA-256 mismatch")
    return expected


@dataclass(frozen=True)
class PreparedImageRecipe:
    schema: int
    base_disk_sha256: str
    payload_iso_sha256: str
    task_image: str
    python_runtime_image: str | None
    runtime_backend: str = "runc"

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def prepared_image_reference(self) -> str:
        return f"rootless-prepared:{self.digest}"


@dataclass(frozen=True)
class PreparedImageResult:
    disk: Path
    image_reference: str
    recipe_digest: str
    cache_hit: bool
    elapsed_sec: float
    runtime_backend: str
    guest_rootfs: str
    image_env: tuple[str, ...]
    image_workdir: str


@dataclass(frozen=True)
class PreparedImageSpec:
    runtime: QemuRuntime
    cache_root: Path
    base_disk: Path
    payload_iso: Path
    task_image: str
    python_runtime_image: str | None = None
    expected_base_disk_sha256: str | None = None
    expected_payload_iso_sha256: str | None = None
    memory_mib: int = 2048
    cpus: int = 2
    boot_timeout_sec: float = 360.0
    prepare_timeout_sec: float = 3600.0


class PreparedImageCache:
    """Prepare Docker images once, then reuse the immutable guest disk.

    Cache entries are never mounted into a running guest. Each trial uses the
    prepared disk only as the backing file for a fresh qcow2 overlay.
    """

    def __init__(self, spec: PreparedImageSpec):
        if not 60 <= spec.prepare_timeout_sec <= 7200:
            raise ValueError("prepare_timeout_sec must be between 60 and 7200")
        self.spec = spec
        self.cache_root = _private_dir(spec.cache_root)
        self.base_disk = _checked_file(spec.base_disk, "base_disk")
        self.payload_iso = _checked_file(spec.payload_iso, "payload_iso")
        base_sha = _normalized_sha256(
            spec.expected_base_disk_sha256,
            sha256_file(self.base_disk),
            "base_disk",
        )
        iso_sha = _normalized_sha256(
            spec.expected_payload_iso_sha256,
            sha256_file(self.payload_iso),
            "payload_iso",
        )
        self.recipe = PreparedImageRecipe(
            schema=_SCHEMA,
            base_disk_sha256=base_sha,
            payload_iso_sha256=iso_sha,
            task_image=spec.task_image,
            python_runtime_image=spec.python_runtime_image,
        )
        self.entry = self.cache_root / self.recipe.digest
        self.disk = self.entry / _DISK
        self.marker = self.entry / _MARKER

    @property
    def guest_bundle(self) -> str:
        return f"/opt/rootless-prepared/{self.recipe.digest}"

    @property
    def guest_rootfs(self) -> str:
        return f"{self.guest_bundle}/rootfs"

    def _marker_payload(
        self,
        disk_sha256: str,
        image_config: dict[str, object],
    ) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "recipe": asdict(self.recipe),
            "recipe_digest": self.recipe.digest,
            "disk_sha256": disk_sha256,
            "image_reference": self.recipe.prepared_image_reference,
            "runtime_backend": self.recipe.runtime_backend,
            "guest_rootfs": self.guest_rootfs,
            "image_config": image_config,
        }

    def _load_marker(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _cache_hit(self) -> bool:
        if self.entry.is_symlink() or not self.entry.is_dir():
            return False
        if self.disk.is_symlink() or not self.disk.is_file() or not self.marker.is_file():
            return False
        payload = self._load_marker()
        if payload is None:
            return False
        expected_disk_sha = payload.get("disk_sha256")
        image_config = payload.get("image_config")
        if not isinstance(expected_disk_sha, str) or not isinstance(image_config, dict):
            return False
        expected = self._marker_payload(expected_disk_sha, image_config)
        if payload != expected:
            return False
        if self.disk.stat().st_mode & 0o222:
            return False
        return sha256_file(self.disk) == expected_disk_sha

    def _quarantine_invalid_entry(self) -> None:
        if not self.entry.exists() and not self.entry.is_symlink():
            return
        quarantine = self.cache_root / (
            f".invalid-{self.recipe.digest}-{uuid.uuid4().hex}"
        )
        os.replace(self.entry, quarantine)

    @staticmethod
    def _checked_exec(session: SandboxSession, command: str, timeout: float) -> str:
        result = session.guest_agent.execute(command, timeout=timeout)
        if result.return_code != 0:
            output = (result.stderr or result.stdout).decode(errors="replace").strip()
            raise RuntimeError(f"prepared-image command failed: {output}")
        return result.stdout.decode(errors="replace")

    def _prepare_guest(self, session: SandboxSession) -> dict[str, object]:
        prepare_timeout = self.spec.prepare_timeout_sec
        self._checked_exec(
            session,
            " && ".join(
                [
                    "mkdir -p /mnt/rootless-payload",
                    "mount -o ro /dev/sr0 /mnt/rootless-payload",
                    "archives=$(find /mnt/rootless-payload -maxdepth 1 -type f "
                    "-name '*.tar' -print | sort)",
                    'test -n "$archives"',
                    'for archive in $archives; do docker load -i "$archive"; done',
                ]
            ),
            prepare_timeout,
        )
        task_ref = shlex.quote(self.spec.task_image)
        self._checked_exec(
            session,
            f"docker image inspect {task_ref} >/dev/null",
            60.0,
        )
        prepared_ref = shlex.quote(self.recipe.prepared_image_reference)
        if self.spec.python_runtime_image:
            runtime_ref = shlex.quote(self.spec.python_runtime_image)
            self._checked_exec(
                session,
                " && ".join(
                    [
                        f"docker image inspect {runtime_ref} >/dev/null",
                        f"docker create --name prepare-task {task_ref} sleep infinity",
                        f"docker create --name prepare-runtime {runtime_ref}",
                        "mkdir -p /var/tmp/prepare-runtime",
                        "docker cp prepare-runtime:/usr/local/. /var/tmp/prepare-runtime",
                        "docker cp /var/tmp/prepare-runtime/. prepare-task:/usr/local",
                        f"docker commit prepare-task {prepared_ref} >/dev/null",
                        "docker rm prepare-task prepare-runtime >/dev/null",
                        "rm -rf /var/tmp/prepare-runtime",
                    ]
                ),
                prepare_timeout,
            )
        else:
            self._checked_exec(
                session,
                f"docker tag {task_ref} {prepared_ref}",
                60.0,
            )
        inspect_output = self._checked_exec(
            session,
            f"docker image inspect {prepared_ref}",
            60.0,
        )
        try:
            inspected = json.loads(inspect_output)
            config = inspected[0].get("Config") or {}
            image_env = config.get("Env") or []
            image_workdir = config.get("WorkingDir") or "/"
        except (IndexError, AttributeError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Docker returned invalid prepared image metadata") from exc
        if not isinstance(image_env, list) or not all(
            isinstance(item, str) for item in image_env
        ):
            raise RuntimeError("prepared image Env metadata is invalid")
        if not isinstance(image_workdir, str) or not image_workdir.startswith("/"):
            image_workdir = "/"
        bundle = shlex.quote(self.guest_bundle)
        self._checked_exec(
            session,
            " && ".join(
                [
                    f"mkdir -p {bundle}/rootfs",
                    f"container=$(docker create {prepared_ref} sleep infinity)",
                    f"docker export \"$container\" | tar -xpf - -C {bundle}/rootfs",
                    'docker rm "$container" >/dev/null',
                    "rc-update del docker boot >/dev/null 2>&1 || true",
                ]
            ),
            prepare_timeout,
        )
        self._checked_exec(
            session,
            f"docker image inspect {prepared_ref} >/dev/null && sync",
            120.0,
        )
        return {"env": image_env, "workdir": image_workdir}

    def _build(self) -> None:
        sessions_root = _private_dir(self.cache_root / ".sessions")
        temp_entry = self.cache_root / f".building-{self.recipe.digest}-{uuid.uuid4().hex}"
        temp_entry.mkdir(mode=0o700)
        session = SandboxSession(
            SandboxSpec(
                runtime=self.spec.runtime,
                state_root=sessions_root,
                base_disk=self.base_disk,
                memory_mib=self.spec.memory_mib,
                cpus=self.spec.cpus,
                read_only_images=(self.payload_iso,),
                network=NetworkMode.NONE,
            )
        )
        try:
            session.start(timeout=15.0)
            session.wait_guest_agent(timeout=self.spec.boot_timeout_sec)
            image_config = self._prepare_guest(session)
            session.shutdown_guest(timeout=60.0)
            assert session.overlay is not None
            assert self.spec.runtime.qemu_img is not None
            rebased = subprocess.run(
                [
                    str(self.spec.runtime.qemu_img),
                    "rebase",
                    "-u",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    str(self.base_disk),
                    str(session.overlay),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=_isolated_subprocess_env(),
            )
            if rebased.returncode:
                raise RuntimeError(
                    "cannot publish prepared image backing chain: "
                    + (rebased.stderr or rebased.stdout).strip()
                )
            target_disk = temp_entry / _DISK
            os.replace(session.overlay, target_disk)
            target_disk.chmod(0o444)
            disk_sha = sha256_file(target_disk)
            marker = temp_entry / _MARKER
            marker.write_text(
                json.dumps(
                    self._marker_payload(disk_sha, image_config),
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            marker.chmod(0o600)
            with target_disk.open("rb") as stream:
                os.fsync(stream.fileno())
            with marker.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temp_entry, self.entry)
            directory_fd = os.open(self.cache_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            session.delete()
            if temp_entry.exists():
                # The path is an exact UUID child created above. Avoid a
                # recursive delete here: incomplete builds remain inspectable
                # and can be quarantined by an operator.
                try:
                    temp_entry.rmdir()
                except OSError:
                    pass

    def prepare(self) -> PreparedImageResult:
        started = time.monotonic()
        locks = _private_dir(self.cache_root / ".locks")
        lock_path = locks / f"{self.recipe.digest}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                if self._cache_hit():
                    payload = self._load_marker()
                    assert payload is not None
                    image_config = payload["image_config"]
                    assert isinstance(image_config, dict)
                    return PreparedImageResult(
                        disk=self.disk,
                        image_reference=self.recipe.prepared_image_reference,
                        recipe_digest=self.recipe.digest,
                        cache_hit=True,
                        elapsed_sec=time.monotonic() - started,
                        runtime_backend="runc",
                        guest_rootfs=self.guest_rootfs,
                        image_env=tuple(image_config.get("env") or ()),
                        image_workdir=str(image_config.get("workdir") or "/"),
                    )
                self._quarantine_invalid_entry()
                self._build()
                if not self._cache_hit():
                    raise RuntimeError("prepared-image cache failed post-build validation")
                payload = self._load_marker()
                assert payload is not None
                image_config = payload["image_config"]
                assert isinstance(image_config, dict)
                return PreparedImageResult(
                    disk=self.disk,
                    image_reference=self.recipe.prepared_image_reference,
                    recipe_digest=self.recipe.digest,
                    cache_hit=False,
                    elapsed_sec=time.monotonic() - started,
                    runtime_backend="runc",
                    guest_rootfs=self.guest_rootfs,
                    image_env=tuple(image_config.get("env") or ()),
                    image_workdir=str(image_config.get("workdir") or "/"),
                )
        except Exception:
            # fdopen owns and closes descriptor after entry. This branch exists
            # only to preserve the original exception type and traceback.
            raise
