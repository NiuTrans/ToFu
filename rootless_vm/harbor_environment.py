"""Harbor environment backed by a rootless QEMU guest.

This module is an optional adapter: importing :mod:`rootless_vm` does not
require Harbor.  Harbor loads this class explicitly with
``--env rootless_vm.harbor_environment:RootlessQemuEnvironment``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.task.config import NetworkMode as HarborNetworkMode, TaskOS

from .image_cache import PreparedImageCache, PreparedImageResult, PreparedImageSpec
from .dockerfile import dockerfile_base_image as _dockerfile_base_image
from .image_store import resolve_image_store_entry
from .qemu import QemuRuntime
from .session import (
    LoopbackServiceForward,
    NetworkMode,
    SandboxSession,
    SandboxSpec,
)


_CONTAINER = "task-env"
_DEFAULT_TRANSFER_LIMIT = 512 * 1024 * 1024
_SECRET_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")


def _checked_file(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    return path


def _loopback_service_forwards(
    value: Any,
) -> tuple[LoopbackServiceForward, ...]:
    """Parse harness-owned control-plane routes from Harbor configuration."""

    if value in (None, ""):
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "loopback_service_forwards must be valid JSON"
            ) from exc
    if not isinstance(value, (list, tuple)):
        raise ValueError("loopback_service_forwards must be a list")
    result: list[LoopbackServiceForward] = []
    for index, raw in enumerate(value):
        if isinstance(raw, LoopbackServiceForward):
            service = raw
        elif isinstance(raw, dict):
            try:
                service = LoopbackServiceForward(**raw)
            except TypeError as exc:
                raise ValueError(
                    f"loopback service {index} has unsupported fields"
                ) from exc
        else:
            raise ValueError(f"loopback service {index} must be an object")
        service.validate()
        result.append(service)
    services = tuple(result)
    # Reuse SandboxSpec's complete uniqueness/count validation rather than
    # maintaining a second route policy in the Harbor adapter.
    names = [service.name for service in services]
    endpoints = [(service.guest_host, service.guest_port) for service in services]
    if len(services) > 8 or len(set(names)) != len(names) \
            or len(set(endpoints)) != len(endpoints):
        raise ValueError(
            "loopback services must have unique names/endpoints and at most 8 entries"
        )
    return services


def _verify_sha256(path: Path, expected: str | None, label: str) -> None:
    if expected is None:
        return
    normalized = expected.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{label} SHA-256 must contain exactly 64 hexadecimal digits")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != normalized:
        raise ValueError(f"{label} SHA-256 mismatch")


def _decode(data: bytes, truncated: bool) -> str:
    value = data.decode("utf-8", errors="replace")
    if truncated:
        value += "\n[rootless-vm: guest-agent output truncated]\n"
    return value


def _merged_environment(image_env: tuple[str, ...], overlay: dict[str, str]) -> list[str]:
    values: dict[str, str] = {}
    for item in image_env:
        key, separator, value = item.partition("=")
        if separator and key:
            values[key] = value
    values.update(overlay)
    if "PATH" not in values:
        values["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return [f"{key}={value}" for key, value in sorted(values.items())]


def _container_rootfs_path(rootfs: str, container_path: str) -> str:
    path = PurePosixPath(container_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"container path must be absolute without '..': {container_path}")
    relative = path.relative_to(PurePosixPath("/"))
    return str(PurePosixPath(rootfs) / relative)


def _container_timed_command(command: str, timeout: float) -> str:
    """Bound the whole command process group inside the task container.

    Killing only ``runc exec`` can leave its container child running, which in
    turn leaves package-manager locks and background builds behind.  The
    in-container timeout owns a fresh process group and therefore terminates
    the shell and its descendants together.  The fallback preserves support
    for unusually minimal task images; the guest-side outer watchdog remains
    active in that case.
    """

    limit = str(max(1, math.ceil(timeout)))
    rendered = shlex.quote(command)
    return (
        "if command -v timeout >/dev/null 2>&1; then "
        f"exec timeout -s TERM -k 5 {limit} /bin/bash -lc {rendered}; "
        f"else exec /bin/bash -lc {rendered}; fi"
    )


def _verifier_exec_timeout(
    default_timeout: float, task_timeout: float, multiplier: float
) -> float:
    """Return a guest watchdog that leaves time for phase diagnostics."""

    phase_timeout = task_timeout * multiplier
    diagnostic_margin = min(30.0, max(2.0, phase_timeout * 0.01))
    return min(default_timeout, max(1.0, phase_timeout - diagnostic_margin))


def _guest_network_setup_command(restricted_network: bool) -> str:
    """Configure a NIC only when QEMU owns at least one explicit guestfwd."""

    loopback = "ip link set lo up"
    if not restricted_network:
        return loopback
    return (
        loopback
        + " && iface=; for path in /sys/class/net/*; do "
        "candidate=${path##*/}; [ \"$candidate\" = lo ] || "
        "{ iface=$candidate; break; }; done; "
        "test -n \"$iface\" && ip link set \"$iface\" up && "
        "ip address replace 10.0.2.15/24 dev \"$iface\" && "
        "ip route replace default via 10.0.2.2 dev \"$iface\""
    )


def _oci_config(
    *,
    rootfs: str,
    env: list[str],
    cwd: str,
    cpus: int | None,
    memory_mb: int | None,
    private_network_namespace: bool,
) -> dict[str, object]:
    resources: dict[str, object] = {"pids": {"limit": 2048}}
    if cpus:
        resources["cpu"] = {"period": 100000, "quota": int(cpus) * 100000}
    if memory_mb:
        resources["memory"] = {"limit": int(memory_mb) * 1024 * 1024}
    capabilities = [
        "CAP_AUDIT_WRITE",
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_FOWNER",
        "CAP_FSETID",
        "CAP_KILL",
        "CAP_MKNOD",
        "CAP_NET_BIND_SERVICE",
        "CAP_SETFCAP",
        "CAP_SETGID",
        "CAP_SETPCAP",
        "CAP_SETUID",
        "CAP_SYS_CHROOT",
    ]
    return {
        "ociVersion": "1.2.0",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": ["/bin/sh", "-lc", "exec sleep infinity"],
            "env": env,
            "cwd": cwd,
            "capabilities": {
                key: capabilities
                for key in ("bounding", "effective", "inheritable", "permitted")
            },
            "noNewPrivileges": False,
            "oomScoreAdj": 0,
        },
        "root": {"path": rootfs, "readonly": False},
        "hostname": "rootless-task",
        "mounts": [
            {
                # Docker normally supplies this file. Prepared OCI rootfs
                # trees do not, and trusting an image-owned /etc/hosts path
                # would allow a symlink to target another guest file. Bind a
                # runtime-owned, fixed file instead.
                "destination": "/etc/hosts",
                "type": "bind",
                "source": "/run/rootless-task/hosts",
                "options": ["bind", "ro", "nosuid", "nodev", "noexec"],
            },
            {
                "destination": "/proc",
                "type": "proc",
                "source": "proc",
                "options": ["nosuid", "noexec", "nodev"],
            },
            {
                "destination": "/dev",
                "type": "tmpfs",
                "source": "tmpfs",
                "options": ["nosuid", "strictatime", "mode=755", "size=65536k"],
            },
            {
                "destination": "/dev/pts",
                "type": "devpts",
                "source": "devpts",
                "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620"],
            },
            {
                "destination": "/dev/shm",
                "type": "tmpfs",
                "source": "shm",
                "options": ["nosuid", "noexec", "nodev", "mode=1777", "size=65536k"],
            },
            {
                "destination": "/sys",
                "type": "sysfs",
                "source": "sysfs",
                "options": ["nosuid", "noexec", "nodev", "ro"],
            },
            {
                "destination": "/sys/fs/cgroup",
                "type": "cgroup",
                "source": "cgroup",
                "options": ["nosuid", "noexec", "nodev", "relatime", "ro"],
            },
        ],
        "linux": {
            "resources": resources,
            "namespaces": [
                {"type": value}
                for value in (
                    "pid",
                    *(("network",) if private_network_namespace else ()),
                    "ipc",
                    "uts",
                    "mount",
                    "cgroup",
                )
            ],
            "maskedPaths": [
                "/proc/acpi",
                "/proc/asound",
                "/proc/kcore",
                "/proc/keys",
                "/proc/latency_stats",
                "/proc/timer_list",
                "/proc/timer_stats",
                "/proc/sched_debug",
                "/sys/firmware",
                "/proc/scsi",
            ],
            "readonlyPaths": [
                "/proc/bus",
                "/proc/fs",
                "/proc/irq",
                "/proc/sys",
                "/proc/sysrq-trigger",
            ],
        },
    }


class RootlessQemuEnvironment(BaseEnvironment):
    """Run Harbor's task container inside a disposable QEMU/TCG VM.

    The host contributes exactly two read-only inputs: a trusted VM base disk
    and an ISO containing a pre-fetched OCI/Docker image archive.  No host
    directory, socket, device, or credential is mounted into the guest.
    """

    def __init__(
        self,
        *args: Any,
        base_disk: str | os.PathLike[str],
        image_iso: str | os.PathLike[str] | None = None,
        image_store: str | os.PathLike[str] | None = None,
        image_reference: str | None = None,
        python_runtime_image: str | None = None,
        qemu_path: str | os.PathLike[str] | None = None,
        qemu_img_path: str | os.PathLike[str] | None = None,
        state_root: str | os.PathLike[str] | None = None,
        prepared_cache_root: str | os.PathLike[str] | None = None,
        base_disk_sha256: str | None = None,
        image_iso_sha256: str | None = None,
        boot_timeout_sec: float = 360.0,
        image_prepare_timeout_sec: float = 3600.0,
        vm_cpus: int = 2,
        vm_memory_mib: int | None = None,
        transfer_limit_bytes: int = _DEFAULT_TRANSFER_LIMIT,
        egress_max_bytes: int = 4 * 1024**3,
        egress_global_concurrency: int = 16,
        loopback_service_forwards: Any = None,
        default_exec_timeout_sec: float = 900.0,
        verifier_timeout_multiplier: float = 1.0,
        virtual_time_shift: int | None = None,
        **kwargs: Any,
    ) -> None:
        self._base_disk = _checked_file(base_disk, "base_disk")
        _verify_sha256(self._base_disk, base_disk_sha256, "base_disk")
        if (image_iso is None) == (image_store is None):
            raise ValueError("provide exactly one of image_iso or image_store")
        self._image_store_value = (
            Path(image_store).expanduser() if image_store is not None else None
        )
        self._build_context: Path | None = None
        self._runtime = QemuRuntime.discover(qemu=qemu_path, qemu_img=qemu_img_path)
        if self._runtime.qemu_img is None:
            raise RuntimeError("qemu-img is required for disposable VM overlays")
        root = Path(state_root) if state_root else Path.cwd() / ".rootless-vm-state"
        self._state_root = root.expanduser()
        self._prepared_cache_root = (
            Path(prepared_cache_root).expanduser()
            if prepared_cache_root is not None
            else None
        )
        self._base_disk_sha256 = base_disk_sha256
        self._image_reference_override = image_reference
        self._python_runtime_image = python_runtime_image
        self._boot_timeout_sec = float(boot_timeout_sec)
        self._image_prepare_timeout_sec = float(image_prepare_timeout_sec)
        self._vm_cpus = int(vm_cpus)
        self._vm_memory_mib = int(vm_memory_mib) if vm_memory_mib is not None else None
        if not 1 <= self._vm_cpus <= 256:
            raise ValueError("vm_cpus must be between 1 and 256")
        if self._vm_memory_mib is not None and self._vm_memory_mib < 512:
            raise ValueError("vm_memory_mib must be at least 512")
        self._transfer_limit_bytes = int(transfer_limit_bytes)
        if self._transfer_limit_bytes < 1024 * 1024:
            raise ValueError("transfer_limit_bytes must be at least 1 MiB")
        self._egress_max_bytes = int(egress_max_bytes)
        if self._egress_max_bytes < 1024 * 1024:
            raise ValueError("egress_max_bytes must be at least 1 MiB")
        self._egress_global_concurrency = int(egress_global_concurrency)
        if not 1 <= self._egress_global_concurrency <= 128:
            raise ValueError(
                "egress_global_concurrency must be between 1 and 128"
            )
        self._loopback_services = _loopback_service_forwards(
            loopback_service_forwards
        )
        self._default_exec_timeout_sec = float(default_exec_timeout_sec)
        if not 1 <= self._default_exec_timeout_sec <= 86400:
            raise ValueError("default_exec_timeout_sec must be between 1 and 86400")
        self._verifier_timeout_multiplier = float(verifier_timeout_multiplier)
        if not 1 <= self._verifier_timeout_multiplier <= 64:
            raise ValueError("verifier_timeout_multiplier must be between 1 and 64")
        self._task_verifier_timeout_sec: float | None = None
        self._virtual_time_shift = (
            int(virtual_time_shift) if virtual_time_shift is not None else None
        )
        if self._virtual_time_shift is not None and not 0 <= self._virtual_time_shift <= 10:
            raise ValueError("virtual_time_shift must be between 0 and 10")
        self._session: SandboxSession | None = None
        self._prepared_result: PreparedImageResult | None = None
        self._container_backend = "docker"
        self._guest_rootfs: str | None = None
        self._runc_user_cache: dict[str, str] = {"root": "0:0"}
        super().__init__(*args, **kwargs)
        if image_store is not None:
            if image_iso_sha256 is not None or image_reference is not None:
                raise ValueError(
                    "image_store supplies per-task image references and SHA-256 values"
                )
            task_reference = self.task_env_config.docker_image
            if not task_reference:
                task_reference = _dockerfile_base_image(self.environment_dir)
                self._build_context = self.environment_dir
            (
                self._image_iso,
                self._image_iso_sha256,
                self._image_reference_override,
                image_metadata,
            ) = resolve_image_store_entry(image_store, task_reference)
            verifier_timeout = image_metadata.get("verifier_timeout_sec")
            if isinstance(verifier_timeout, (int, float)):
                self._task_verifier_timeout_sec = float(verifier_timeout)
        else:
            assert image_iso is not None
            self._image_iso = _checked_file(image_iso, "image_iso")
            _verify_sha256(self._image_iso, image_iso_sha256, "image_iso")
            self._image_iso_sha256 = image_iso_sha256

    @staticmethod
    def type() -> str:
        return "rootless-qemu"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(disable_internet=True, mounted=False)

    def loopback_service_url(self, name: str) -> str:
        """Return a guest-visible URL for one predeclared host control service."""

        for service in self._loopback_services:
            if service.name == name:
                return f"http://{service.guest_host}:{service.guest_port}"
        raise KeyError(f"loopback service is not configured: {name}")

    @classmethod
    def preflight(cls) -> None:
        # Custom CLI kwargs are not available to Harbor's class-level preflight.
        # Explicit paths can still be checked by exporting ROOTLESS_VM_QEMU and
        # ROOTLESS_VM_QEMU_IMG; instance construction always verifies them again.
        if os.environ.get("ROOTLESS_VM_QEMU"):
            QemuRuntime.discover().preflight()

    def _validate_definition(self) -> None:
        if self.task_env_config.os != TaskOS.LINUX:
            raise ValueError("rootless-qemu currently supports Linux tasks only")
        if not self.task_env_config.docker_image:
            if self._image_store_value is None:
                raise ValueError(
                    "rootless-qemu requires docker_image, or image_store plus "
                    "environment/Dockerfile"
                )
            _dockerfile_base_image(self.environment_dir)
            if self._prepared_cache_root is None:
                raise ValueError(
                    "Dockerfile tasks require prepared_cache_root so the build "
                    "runs once inside an isolated guest"
                )
        if self.extra_docker_compose_paths:
            raise ValueError("rootless-qemu does not support Docker Compose")
        if self.network_policy.network_mode not in {
            HarborNetworkMode.NO_NETWORK,
            HarborNetworkMode.PUBLIC,
        }:
            raise ValueError(
                "rootless-qemu supports network_mode='no-network' or its "
                "host-protected public HTTP(S) proxy; allowlist is not implemented"
            )
        if (
            self.network_policy.network_mode == HarborNetworkMode.PUBLIC
            and self._prepared_cache_root is None
        ):
            raise ValueError(
                "public rootless-qemu tasks require prepared_cache_root so the "
                "task runs with runc and cannot bypass the injected proxy"
            )
        secret_names = [
            name
            for name in self._startup_env()
            if any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
        ]
        if secret_names:
            raise ValueError(
                "rootless-qemu refuses secret-shaped task environment variables: "
                + ", ".join(sorted(secret_names))
            )

    @property
    def _guest(self):
        if self._session is None:
            raise RuntimeError("rootless-qemu environment is not started")
        return self._session.guest_agent

    async def _outer_exec(self, command: str, timeout: float = 120.0):
        return await asyncio.to_thread(self._guest.execute, command, timeout=timeout)

    def _network_environment(self) -> dict[str, str]:
        session = self._session
        if session is None or session.egress_proxy is None:
            return {}
        url = session.egress_proxy.proxy_url
        # Keep services inside the task container reachable to the task and its
        # verifier.  An empty NO_PROXY sends even curl http://localhost through
        # the public-egress proxy, which turns a healthy local service into a
        # misleading HTTP 403 benchmark failure.  Only loopback bypasses the
        # proxy; QEMU user networking still has restrict=on, so this does not
        # create a route to the host or private networks.
        loopback = "localhost,127.0.0.1,::1"
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "NO_PROXY": loopback,
            "http_proxy": url,
            "https_proxy": url,
            "no_proxy": loopback,
            # Git's libcurl backend does not reliably retry an authenticated
            # CONNECT after our deliberately fail-closed 407 response. Force
            # pre-emptive Basic proxy authentication, while keeping the
            # short-lived credential in the process environment rather than a
            # writable git config inside the benchmark image.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.proxyAuthMethod",
            "GIT_CONFIG_VALUE_0": "basic",
            # The public route may itself traverse a corporate parent proxy.
            # Package installers otherwise turn a brief tunnel reset into a
            # verifier failure (and therefore a false benchmark zero).
            "UV_HTTP_RETRIES": "30",
            "UV_HTTP_TIMEOUT": "120",
            "PIP_RETRIES": "30",
            "PIP_DEFAULT_TIMEOUT": "120",
        }

    def _runtime_environment(self) -> dict[str, str]:
        result = dict(self._startup_env())
        # The task cannot replace this route with a host/internal proxy. Direct
        # traffic is blocked by QEMU restrict=on regardless.
        result.update(self._network_environment())
        return result

    def _command_network_environment(self, *, is_verifier: bool) -> dict[str, str]:
        """Use bounded fail-fast retries for replaceable verifier attempts.

        Agent package installs retain the generous retry budget because losing
        them can discard substantial model work. A verifier infrastructure
        failure is classified and retried as a whole trial, so allowing one
        dead package tunnel to consume the entire phase only reduces accuracy
        and throughput.
        """

        result = self._network_environment()
        if is_verifier:
            result.update(
                {
                    # uv otherwise permits up to 50 simultaneous downloads.
                    # Across many disposable VMs that burst can overwhelm a
                    # required corporate parent proxy even though the outer
                    # egress gate bounds tunnels globally.
                    "UV_CONCURRENT_DOWNLOADS": "2",
                    "UV_HTTP_RETRIES": "20",
                    "UV_HTTP_TIMEOUT": "60",
                    "PIP_RETRIES": "20",
                    "PIP_DEFAULT_TIMEOUT": "60",
                }
            )
        return result

    async def start(self, force_build: bool) -> None:
        if force_build:
            raise ValueError("rootless-qemu consumes a pre-fetched image and cannot build")
        if self._session is not None:
            raise RuntimeError("rootless-qemu environment is already started")
        image_reference = self._image_reference_override or self.task_env_config.docker_image
        assert image_reference
        memory_mib = self._vm_memory_mib or max(
            512, int(self._effective_memory_mb or 2048)
        )
        # Keep a second vCPU for boot/services even when the benchmark limits
        # the task container to one CPU. The inner Docker limit below remains
        # authoritative for task execution.
        cpus = max(self._vm_cpus, int(self._effective_cpus or 1))
        storage_mb = int(self._effective_storage_mb or 10 * 1024)
        disk_gib = max(2, (storage_mb + 1023) // 1024)
        public_network = self.network_policy.network_mode == HarborNetworkMode.PUBLIC
        restricted_network = public_network or bool(self._loopback_services)
        base_disk = self._base_disk
        read_only_images = (self._image_iso,)
        needs_payload_setup = True
        if self._prepared_cache_root is not None:
            prepared_spec = PreparedImageSpec(
                runtime=self._runtime,
                cache_root=self._prepared_cache_root,
                base_disk=self._base_disk,
                payload_iso=self._image_iso,
                task_image=image_reference,
                python_runtime_image=self._python_runtime_image,
                build_context=self._build_context,
                expected_base_disk_sha256=self._base_disk_sha256,
                expected_payload_iso_sha256=self._image_iso_sha256,
                memory_mib=memory_mib,
                cpus=cpus,
                boot_timeout_sec=self._boot_timeout_sec,
                prepare_timeout_sec=self._image_prepare_timeout_sec,
            )
            prepared = await asyncio.to_thread(
                lambda: PreparedImageCache(prepared_spec).prepare()
            )
            self._prepared_result = prepared
            base_disk = prepared.disk
            image_reference = prepared.image_reference
            read_only_images = ()
            needs_payload_setup = False
            self._container_backend = prepared.runtime_backend
            self._guest_rootfs = prepared.guest_rootfs
            self.logger.info(
                "Prepared image cache %s recipe=%s elapsed=%.3fs",
                "hit" if prepared.cache_hit else "built",
                prepared.recipe_digest,
                prepared.elapsed_sec,
            )
        session = SandboxSession(
            SandboxSpec(
                runtime=self._runtime,
                state_root=self._state_root,
                base_disk=base_disk,
                memory_mib=memory_mib,
                cpus=cpus,
                disk_virtual_size_gib=disk_gib,
                read_only_images=read_only_images,
                network=NetworkMode.PUBLIC if public_network else NetworkMode.NONE,
                loopback_services=self._loopback_services,
                egress_max_bytes=self._egress_max_bytes,
                egress_global_concurrency=self._egress_global_concurrency,
                virtual_time_shift=self._virtual_time_shift,
            )
        )
        self._session = session
        try:
            await asyncio.to_thread(session.start, timeout=15.0)
            await asyncio.to_thread(session.wait_guest_agent, timeout=self._boot_timeout_sec)
            # The minimal VM image does not run a network manager and leaves
            # even loopback DOWN.  A no-network VM shares only that loopback;
            # a public VM additionally receives the one restricted QEMU NIC.
            configured = await self._outer_exec(
                _guest_network_setup_command(restricted_network), timeout=30.0
            )
            if configured.return_code != 0:
                raise RuntimeError(
                    "failed to configure isolated guest networking: "
                    + _decode(configured.stderr or configured.stdout, False).strip()
                )
            if public_network:
                if session.egress_proxy is None or not session.egress_proxy.is_alive():
                    raise RuntimeError("restricted host egress proxy exited during startup")
            if any(
                not relay.is_alive()
                for _service, relay in session.loopback_service_relays
            ):
                raise RuntimeError(
                    "restricted host loopback service relay exited during startup"
                )
            if needs_payload_setup:
                setup = " && ".join(
                    [
                        "mkdir -p /mnt/task-image",
                        "mount -o ro /dev/sr0 /mnt/task-image",
                        "archives=$(find /mnt/task-image -maxdepth 1 -type f "
                        "-name '*.tar' -print | sort)",
                        'test -n "$archives"',
                        'for archive in $archives; do docker load -i "$archive"; done',
                    ]
                )
                loaded = await self._outer_exec(setup, timeout=420.0)
                if loaded.return_code != 0:
                    raise RuntimeError(
                        "failed to load task image in guest: "
                        + _decode(loaded.stderr or loaded.stdout, False).strip()
                    )

            if self._container_backend == "runc":
                assert self._prepared_result is not None
                assert self._guest_rootfs is not None
                cwd = (
                    self.task_env_config.workdir
                    or self._prepared_result.image_workdir
                    or "/"
                )
                config = _oci_config(
                    rootfs=self._guest_rootfs,
                    env=_merged_environment(
                        self._prepared_result.image_env, self._runtime_environment()
                    ),
                    cwd=cwd,
                    cpus=self.task_env_config.cpus,
                    memory_mb=self.task_env_config.memory_mb,
                    # A network=none VM has no emulated NIC at all. Sharing its
                    # guest namespace preserves localhost without opening an
                    # egress path. Public or control-plane-enabled VMs share
                    # the QEMU NIC whose only routes are explicit guestfwds.
                    private_network_namespace=False,
                )
                guest_config = f"/var/tmp/rootless-config-{uuid.uuid4().hex}.json"
                with tempfile.TemporaryDirectory(prefix="rootless-oci-config-") as temp:
                    host_config = Path(temp) / "config.json"
                    host_config.write_text(
                        json.dumps(config, separators=(",", ":")), encoding="utf-8"
                    )
                    await asyncio.to_thread(
                        self._guest.upload, host_config, guest_config
                    )
                started = await self._outer_exec(
                    " && ".join(
                        [
                            "mkdir -p /run/rootless-task",
                            "umask 077 && printf '%s\\n' "
                            "'127.0.0.1 localhost localhost.localdomain' "
                            "'::1 localhost localhost.localdomain' "
                            "> /run/rootless-task/hosts && "
                            "chmod 0444 /run/rootless-task/hosts",
                            f"mv {shlex.quote(guest_config)} /run/rootless-task/config.json",
                            # A detached container still inherits runc's standard
                            # streams.  Without explicit redirection, its init
                            # process keeps QGA's capture pipes open forever and
                            # guest-exec-status never reports completion.
                            f"runc run -d --bundle /run/rootless-task {_CONTAINER} "
                            "</dev/null >/dev/null 2>/run/rootless-task/start.err",
                        ]
                    ),
                    timeout=120.0,
                )
            else:
                command = [
                    "docker",
                    "run",
                    "-d",
                    "--network",
                    "bridge" if restricted_network else "none",
                    "--name",
                    _CONTAINER,
                    "--pids-limit",
                    "2048",
                ]
                if self.task_env_config.cpus:
                    command += ["--cpus", str(self.task_env_config.cpus)]
                if self.task_env_config.memory_mb:
                    command += ["--memory", f"{self.task_env_config.memory_mb}m"]
                for key, value in sorted(self._runtime_environment().items()):
                    command += ["--env", f"{key}={value}"]
                if self.task_env_config.workdir:
                    command += ["--workdir", self.task_env_config.workdir]
                command += [image_reference, "sleep", "infinity"]
                started = await self._outer_exec(
                    " ".join(shlex.quote(part) for part in command), timeout=120.0
                )
            if started.return_code != 0:
                raise RuntimeError(
                    "failed to start task container: "
                    + _decode(started.stderr or started.stdout, False).strip()
                )
            if self._python_runtime_image and needs_payload_setup:
                runtime_image = shlex.quote(self._python_runtime_image)
                seed = await self._outer_exec(
                    " && ".join(
                        [
                            f"docker create --name verifier-python {runtime_image}",
                            "mkdir -p /var/tmp/verifier-python-runtime",
                            "docker cp verifier-python:/usr/local/. /var/tmp/verifier-python-runtime",
                            f"docker cp /var/tmp/verifier-python-runtime/. {_CONTAINER}:/usr/local",
                            "docker rm verifier-python",
                            "rm -rf /var/tmp/verifier-python-runtime",
                        ]
                    ),
                    timeout=300.0,
                )
                if seed.return_code != 0:
                    await self._outer_exec(
                        "docker rm -f verifier-python >/dev/null 2>&1 || true; "
                        "rm -rf /var/tmp/verifier-python-runtime",
                        timeout=30.0,
                    )
                    raise RuntimeError(
                        "failed to seed offline Python runtime: "
                        + _decode(seed.stderr or seed.stdout, False).strip()
                    )
            ensured = await self.ensure_dirs(
                self._mount_targets(writable_only=True), chmod=True
            )
            if ensured is not None and ensured.return_code != 0:
                raise RuntimeError(
                    "failed to create Harbor runtime directories: "
                    + (ensured.stderr or ensured.stdout or "unknown error").strip()
                )
            await self._upload_environment_dir_after_start()
        except Exception:
            await self.stop(delete=True)
            raise

    async def stop(self, delete: bool):
        session, self._session = self._session, None
        if session is None:
            return
        if delete:
            await asyncio.to_thread(session.delete)
        else:
            await asyncio.to_thread(session.stop)

    async def _runc_user_spec(self, user: str | int | None) -> str | None:
        """Translate Docker-style names to the numeric form runc requires."""

        if user is None:
            return None
        rendered = str(user)
        cached = self._runc_user_cache.get(rendered)
        if cached is not None:
            return cached
        user_name, separator, group_name = rendered.partition(":")
        if not user_name or (separator and not group_name):
            raise ValueError(f"invalid container user: {rendered!r}")
        if user_name.isdecimal():
            uid = user_name
            primary_gid = user_name
        else:
            assert self._guest_rootfs is not None
            passwd = f"{self._guest_rootfs}/etc/passwd"
            lookup = await self._outer_exec(
                "awk -F: -v wanted="
                + shlex.quote(user_name)
                + " '$1 == wanted {print $3 \":\" $4; found=1; exit} "
                + "END {if (!found) exit 1}' "
                + shlex.quote(passwd),
                timeout=30.0,
            )
            if lookup.return_code != 0:
                raise ValueError(f"container user does not exist: {user_name!r}")
            uid, delimiter, primary_gid = (
                lookup.stdout.decode(errors="replace").strip().partition(":")
            )
            if not delimiter or not uid.isdecimal() or not primary_gid.isdecimal():
                raise RuntimeError("container passwd entry has invalid numeric IDs")
        gid = primary_gid
        if separator:
            if group_name == "root":
                gid = "0"
            elif group_name.isdecimal():
                gid = group_name
            else:
                assert self._guest_rootfs is not None
                group_file = f"{self._guest_rootfs}/etc/group"
                lookup = await self._outer_exec(
                    "awk -F: -v wanted="
                    + shlex.quote(group_name)
                    + " '$1 == wanted {print $3; found=1; exit} "
                    + "END {if (!found) exit 1}' "
                    + shlex.quote(group_file),
                    timeout=30.0,
                )
                gid = lookup.stdout.decode(errors="replace").strip()
                if lookup.return_code != 0 or not gid.isdecimal():
                    raise ValueError(f"container group does not exist: {group_name!r}")
        result = f"{uid}:{gid}"
        self._runc_user_cache[rendered] = result
        return result

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        resolved_user = self._resolve_user(user)
        is_verifier = "/tests/" in command and "/logs/verifier/" in command
        timeout = float(
            timeout_sec
            if timeout_sec is not None
            else self._default_exec_timeout_sec
        )
        if (
            timeout_sec is None
            and is_verifier
            and self._task_verifier_timeout_sec is not None
        ):
            # Harbor owns the authoritative phase deadline but does not pass it
            # to verifier environment.exec(). End the guest process shortly
            # before that deadline so QGA returns, verifier logs can be copied,
            # and asyncio cancellation cannot strand a 14,400-second worker.
            timeout = _verifier_exec_timeout(
                timeout,
                self._task_verifier_timeout_sec,
                self._verifier_timeout_multiplier,
            )
        omit_guest_timeout = self._virtual_time_shift is not None and is_verifier
        container_command = (
            command if omit_guest_timeout else _container_timed_command(command, timeout)
        )
        if self._container_backend == "runc":
            argv = ["runc", "exec"]
            runc_user = await self._runc_user_spec(resolved_user)
            if runc_user is not None:
                argv += ["--user", runc_user]
            if cwd is not None:
                argv += ["--cwd", cwd]
            command_env = self._merge_env(env) or {}
            command_env.update(
                self._command_network_environment(is_verifier=is_verifier)
            )
            for key, value in sorted(command_env.items()):
                argv += ["--env", f"{key}={value}"]
            # Harbor's Docker main-service adapter deliberately uses Bash:
            # benchmark scripts and commands depend on source, arrays, and
            # Bash's ENOEXEC fallback for legacy scripts whose canary comment
            # precedes the shebang. Match that contract exactly.
            argv += [_CONTAINER, "/bin/bash", "-lc", container_command]
        else:
            argv = ["docker", "exec"]
            if resolved_user is not None:
                argv += ["--user", str(resolved_user)]
            if cwd is not None:
                argv += ["--workdir", cwd]
            command_env = self._merge_env(env) or {}
            command_env.update(
                self._command_network_environment(is_verifier=is_verifier)
            )
            for key, value in sorted(command_env.items()):
                argv += ["--env", f"{key}={value}"]
            argv += [_CONTAINER, "/bin/sh", "-lc", container_command]
        # QGA reports a transport TimeoutError without terminating the guest
        # process.  Bound the command inside the guest first so a slow agent
        # command becomes an ordinary non-zero tool result that the model can
        # inspect and recover from.
        if omit_guest_timeout:
            # coreutils timeout observes the icount guest clock. A dependency
            # install can therefore consume an hour of virtual instruction
            # time in a few host minutes and be killed long before Harbor's
            # host-clock verifier budget. The outer QGA request and Harbor's
            # asyncio phase timeout still bound this optional calibration path.
            outer_argv = argv
        else:
            outer_argv = [
                "timeout",
                "-s",
                "TERM",
                "-k",
                "5",
                str(max(1, math.ceil(timeout) + 10)),
                *argv,
            ]
        result = await self._outer_exec(
            " ".join(shlex.quote(part) for part in outer_argv),
            timeout=timeout + 20.0,
        )
        stdout = _decode(result.stdout, result.stdout_truncated)
        stderr = _decode(result.stderr, result.stderr_truncated)
        callback = self._output_callback()
        if callback is not None:
            if stdout:
                await callback(stdout, "stdout")
            if stderr:
                await callback(stderr, "stderr")
        return ExecResult(stdout=stdout, stderr=stderr, return_code=result.return_code)

    async def upload_file(self, source_path: Path | str, target_path: str):
        source = _checked_file(source_path, "upload source")
        transfer = f"/var/tmp/rootless-upload-{uuid.uuid4().hex}"
        await asyncio.to_thread(self._guest.upload, source, transfer)
        try:
            parent = str(Path(target_path).parent)
            if self._container_backend == "runc":
                assert self._guest_rootfs is not None
                guest_target = _container_rootfs_path(
                    self._guest_rootfs, target_path
                )
                guest_parent = str(PurePosixPath(guest_target).parent)
                mode = source.stat().st_mode & 0o7777
                result = await self._outer_exec(
                    f"mkdir -p {shlex.quote(guest_parent)} && "
                    f"cp {shlex.quote(transfer)} {shlex.quote(guest_target)} && "
                    f"chmod {mode:o} {shlex.quote(guest_target)}"
                )
            else:
                result = await self._outer_exec(
                    f"docker exec {_CONTAINER} mkdir -p {shlex.quote(parent)} && "
                    f"docker cp {shlex.quote(transfer)} "
                    f"{_CONTAINER}:{shlex.quote(target_path)}"
                )
            if result.return_code != 0:
                raise RuntimeError(_decode(result.stderr or result.stdout, False).strip())
        finally:
            await self._outer_exec(f"rm -f {shlex.quote(transfer)}", timeout=30.0)

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        source = Path(source_dir).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError(f"upload source must be a directory: {source}")
        transfer_id = uuid.uuid4().hex
        guest_tar = f"/var/tmp/rootless-upload-{transfer_id}.tar.gz"
        guest_dir = f"/var/tmp/rootless-upload-{transfer_id}"
        with tempfile.TemporaryDirectory(prefix="rootless-vm-upload-") as temp_dir:
            host_tar = Path(temp_dir) / "payload.tar.gz"
            with tarfile.open(host_tar, "w:gz") as archive:
                for child in source.iterdir():
                    archive.add(child, arcname=child.name, recursive=True)
            await asyncio.to_thread(self._guest.upload, host_tar, guest_tar)
        try:
            if self._container_backend == "runc":
                assert self._guest_rootfs is not None
                guest_target = _container_rootfs_path(
                    self._guest_rootfs, target_dir
                )
                command = (
                    f"mkdir -p {shlex.quote(guest_target)} && "
                    f"tar -xzf {shlex.quote(guest_tar)} "
                    f"-C {shlex.quote(guest_target)}"
                )
            else:
                command = (
                    f"mkdir -p {shlex.quote(guest_dir)} && "
                    f"tar -xzf {shlex.quote(guest_tar)} "
                    f"-C {shlex.quote(guest_dir)} && "
                    f"docker exec {_CONTAINER} mkdir -p {shlex.quote(target_dir)} && "
                    f"docker cp {shlex.quote(guest_dir)}/. "
                    f"{_CONTAINER}:{shlex.quote(target_dir)}"
                )
            result = await self._outer_exec(command, timeout=180.0)
            if result.return_code != 0:
                raise RuntimeError(_decode(result.stderr or result.stdout, False).strip())
        finally:
            await self._outer_exec(
                f"rm -rf {shlex.quote(guest_tar)} {shlex.quote(guest_dir)}", timeout=30.0
            )

    async def download_file(self, source_path: str, target_path: Path | str):
        target = Path(target_path).expanduser().resolve()
        transfer = f"/var/tmp/rootless-download-{uuid.uuid4().hex}"
        if self._container_backend == "runc":
            assert self._guest_rootfs is not None
            transfer = _container_rootfs_path(self._guest_rootfs, source_path)
        else:
            copied = await self._outer_exec(
                f"docker cp {_CONTAINER}:{shlex.quote(source_path)} "
                f"{shlex.quote(transfer)}",
                timeout=120.0,
            )
            if copied.return_code != 0:
                raise RuntimeError(
                    _decode(copied.stderr or copied.stdout, False).strip()
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="rootless-vm-download-") as temp_dir:
                staged = Path(temp_dir) / "payload"
                await asyncio.to_thread(
                    self._guest.download,
                    transfer,
                    staged,
                    max_bytes=self._transfer_limit_bytes,
                )
                os.replace(staged, target)
        finally:
            if self._container_backend != "runc":
                await self._outer_exec(
                    f"rm -f {shlex.quote(transfer)}", timeout=30.0
                )

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        target = Path(target_dir).expanduser().resolve()
        if target.is_symlink():
            raise ValueError(f"download target must not be a symbolic link: {target}")
        transfer_id = uuid.uuid4().hex
        guest_dir = f"/var/tmp/rootless-download-{transfer_id}"
        guest_tar = f"{guest_dir}.tar.gz"
        if self._container_backend == "runc":
            assert self._guest_rootfs is not None
            guest_source = _container_rootfs_path(self._guest_rootfs, source_dir)
            command = (
                f"test -d {shlex.quote(guest_source)} && "
                f"tar -czf {shlex.quote(guest_tar)} "
                f"-C {shlex.quote(guest_source)} ."
            )
        else:
            command = (
                f"mkdir -p {shlex.quote(guest_dir)} && "
                f"docker cp {_CONTAINER}:{shlex.quote(source_dir)}/. "
                f"{shlex.quote(guest_dir)} && "
                f"tar -czf {shlex.quote(guest_tar)} "
                f"-C {shlex.quote(guest_dir)} ."
            )
        copied = await self._outer_exec(command, timeout=180.0)
        if copied.return_code != 0:
            raise RuntimeError(_decode(copied.stderr or copied.stdout, False).strip())
        try:
            with tempfile.TemporaryDirectory(prefix="rootless-vm-download-") as temp_dir:
                host_tar = Path(temp_dir) / "payload.tar.gz"
                await asyncio.to_thread(
                    self._guest.download,
                    guest_tar,
                    host_tar,
                    max_bytes=self._transfer_limit_bytes,
                )
                target.mkdir(parents=True, exist_ok=True)
                with tarfile.open(host_tar, "r:gz") as archive:
                    archive.extractall(target, filter="data")
        finally:
            await self._outer_exec(
                f"rm -rf {shlex.quote(guest_tar)} {shlex.quote(guest_dir)}", timeout=30.0
            )
