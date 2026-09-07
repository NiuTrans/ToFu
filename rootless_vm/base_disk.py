"""Reproducible, offline provisioning for the rootless VM base disk."""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .qemu import QemuRuntime, _isolated_subprocess_env
from .session import NetworkMode, SandboxSession, SandboxSpec


_ALPINE_HOST = "dl-cdn.alpinelinux.org"
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_HEX_512 = re.compile(r"^[0-9a-f]{128}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_MAX_IMAGE_BYTES = 2 * 1024**3
_MAX_PACKAGE_BYTES = 128 * 1024**2
_MAX_PACKAGES_BYTES = 512 * 1024**2


@dataclass(frozen=True)
class LockedDownload:
    name: str
    url: str
    digest: str
    algorithm: str
    size: int


@dataclass(frozen=True)
class BaseDiskLock:
    image: LockedDownload
    packages: tuple[LockedDownload, ...]
    digest: str


@dataclass(frozen=True)
class BaseDiskBuildResult:
    disk: Path
    metadata: Path
    sha256: str
    lock_sha256: str
    verification: str


def _private_dir(value: str | os.PathLike[str], label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = candidate.resolve(strict=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise PermissionError(f"{label} must be a private directory: {path}")
    return path


def _digest(path: Path, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _locked_download(value: Any, *, image: bool) -> LockedDownload:
    if not isinstance(value, dict):
        raise ValueError("base disk lock entries must be objects")
    name = value.get("name")
    url = value.get("url")
    size = value.get("size")
    algorithm = "sha512" if image else "sha256"
    digest = value.get(algorithm)
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"unsafe locked download name: {name!r}")
    if not image and not name.endswith(".apk"):
        raise ValueError(f"locked package is not an APK: {name}")
    parsed = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    if (
        parsed.scheme != "https"
        or parsed.hostname != _ALPINE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"locked download must use the official Alpine host: {url!r}")
    digest_pattern = _HEX_512 if image else _HEX_256
    if not isinstance(digest, str) or not digest_pattern.fullmatch(digest):
        raise ValueError(f"invalid locked {algorithm} for {name}")
    limit = _MAX_IMAGE_BYTES if image else _MAX_PACKAGE_BYTES
    if not isinstance(size, int) or not 0 < size <= limit:
        raise ValueError(f"invalid locked size for {name}: {size!r}")
    return LockedDownload(name, url, digest, algorithm, size)


def load_base_disk_lock(path: str | os.PathLike[str]) -> BaseDiskLock:
    lock_path = Path(path).expanduser().resolve(strict=True)
    raw = lock_path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid base disk lock JSON: {lock_path}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("unsupported base disk lock schema")
    if value.get("platform") != "linux/amd64":
        raise ValueError("base disk lock platform must be linux/amd64")
    image = _locked_download(value.get("image"), image=True)
    package_rows = value.get("packages")
    if not isinstance(package_rows, list) or not package_rows:
        raise ValueError("base disk lock must contain packages")
    packages = tuple(_locked_download(row, image=False) for row in package_rows)
    names = [package.name for package in packages]
    if len(set(names)) != len(names):
        raise ValueError("base disk lock contains duplicate package names")
    if sum(package.size for package in packages) > _MAX_PACKAGES_BYTES:
        raise ValueError("locked APK set exceeds the 512 MiB safety limit")
    return BaseDiskLock(
        image=image,
        packages=packages,
        digest=hashlib.sha256(raw).hexdigest(),
    )


def _fetch(source: LockedDownload, cache: Path) -> Path:
    target = cache / source.name
    if target.is_symlink():
        raise ValueError(f"download cache entry must not be a symlink: {target}")
    if target.is_file():
        if target.stat().st_size == source.size and _digest(
            target, source.algorithm
        ) == source.digest:
            target.chmod(0o600)
            return target
        raise ValueError(f"download cache entry failed its lock: {target}")
    error: Exception | None = None
    for attempt in range(5):
        partial = cache / f".{source.name}.{uuid.uuid4().hex}.partial"
        try:
            request = urllib.request.Request(
                source.url, headers={"User-Agent": "rootless-vm-base-builder/1"}
            )
            value = hashlib.new(source.algorithm)
            total = 0
            with urllib.request.urlopen(request, timeout=120) as response, partial.open(
                "xb"
            ) as stream:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > source.size:
                        raise RuntimeError(f"download exceeds locked size: {source.name}")
                    value.update(chunk)
                    stream.write(chunk)
            if total != source.size or value.hexdigest() != source.digest:
                raise RuntimeError(f"download failed its lock: {source.name}")
            partial.chmod(0o600)
            os.replace(partial, target)
            return target
        except Exception as exc:
            error = exc
            partial.unlink(missing_ok=True)
            if attempt + 1 < 5:
                time.sleep(min(20.0, 2.0**attempt) + random.random())
    assert error is not None
    raise RuntimeError(f"could not fetch {source.name}: {error}") from error


def write_offline_seed_iso(
    output: str | os.PathLike[str],
    packages: tuple[tuple[LockedDownload, Path], ...],
    *,
    instance_id: str,
) -> Path:
    """Write a NoCloud seed whose provisioning has no guest network dependency."""

    try:
        import pycdlib
    except ImportError as exc:
        raise RuntimeError(
            "pycdlib is required; install evaluations/swebench/requirements.txt"
        ) from exc
    output_path = Path(output).expanduser().resolve()
    if output_path.exists() or output_path.is_symlink():
        raise ValueError(f"refusing to overwrite seed ISO: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    user_data = (
        "#cloud-config\n"
        "hostname: rootless-vm-builder\n"
        "ssh_pwauth: false\n"
        "disable_root: true\n"
        "package_update: false\n"
        "runcmd:\n"
        "  - [ sh, -c, \"exec >/dev/ttyS0 2>&1; set -eux; "
        "trap 'sync; poweroff -f' EXIT; mkdir -p /mnt/rootless-seed "
        "/var/lib/rootless-vm; printf 'seed-mounted-pending\\n' > "
        "/var/lib/rootless-vm/provision-stage; "
        "mount -t iso9660 -o ro /dev/sr0 /mnt/rootless-seed; "
        "printf 'seed-mounted\\n' > /var/lib/rootless-vm/provision-stage; "
        "cd /mnt/rootless-seed/packages; sha256sum -c SHA256SUMS; "
        "printf 'packages-verified\\n' > /var/lib/rootless-vm/provision-stage; "
        "apk add --no-network ./*.apk; printf 'packages-installed\\n' > "
        "/var/lib/rootless-vm/provision-stage; "
        "for service in cloud-init-local cloud-init cloud-config cloud-final; do "
        "rc-update del $service boot 2>/dev/null || true; "
        "rc-update del $service default 2>/dev/null || true; done; "
        "rc-update add qemu-guest-agent default; rc-update add docker default; "
        "printf 'services-enabled\\n' > /var/lib/rootless-vm/provision-stage; "
        "printf 'rootless-vm-base-v1\\n' > /var/lib/rootless-vm/provisioned\" ]\n"
    ).encode()
    meta_data = (
        f"instance-id: {instance_id}\nlocal-hostname: rootless-vm-builder\n"
    ).encode()
    sums = "".join(
        f"{source.digest}  {source.name}\n" for source, _path in packages
    ).encode()
    image = pycdlib.PyCdlib()
    try:
        image.new(
            interchange_level=3,
            joliet=3,
            rock_ridge="1.09",
            vol_ident="cidata",
        )
        image.add_directory(
            iso_path="/PACKAGES",
            rr_name="packages",
            joliet_path="/packages",
        )
        for iso_path, rr_name, joliet_path, payload in (
            ("/USER_DAT.;1", "user-data", "/user-data", user_data),
            ("/META_DAT.;1", "meta-data", "/meta-data", meta_data),
            (
                "/PACKAGES/SHA256SUM.;1",
                "SHA256SUMS",
                "/packages/SHA256SUMS",
                sums,
            ),
        ):
            image.add_fp(
                io.BytesIO(payload),
                len(payload),
                iso_path=iso_path,
                rr_name=rr_name,
                joliet_path=joliet_path,
            )
        for index, (source, path) in enumerate(packages):
            image.add_file(
                str(path),
                iso_path=f"/PACKAGES/P{index:07d}.APK;1",
                rr_name=source.name,
                joliet_path=f"/packages/{source.name}",
            )
        image.write(str(output_path))
    finally:
        image.close()
    output_path.chmod(0o600)
    return output_path


def _qemu_img(runtime: QemuRuntime, arguments: list[str], *, timeout: float) -> None:
    assert runtime.qemu_img is not None
    result = subprocess.run(
        [str(runtime.qemu_img), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_isolated_subprocess_env(),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"qemu-img failed ({result.returncode}): {detail}")


def _verify_disk(
    runtime: QemuRuntime, disk: Path, state_root: Path, *, timeout: float
) -> str:
    with SandboxSession(
        SandboxSpec(
            runtime=runtime,
            state_root=state_root,
            base_disk=disk,
            memory_mib=2048,
            cpus=2,
            network=NetworkMode.NONE,
        )
    ) as session:
        session.start(timeout=30)
        session.wait_guest_agent(timeout=timeout)
        result = session.guest_agent.execute(
            "if test \"$(cat /var/lib/rootless-vm/provisioned 2>/dev/null)\" "
            "!= rootless-vm-base-v1; then "
            "echo stage=$(cat /var/lib/rootless-vm/provision-stage 2>/dev/null || "
            "echo missing); rc-service qemu-guest-agent status || true; "
            "rc-service docker status || true; docker info || true; "
            "tail -n 20 /var/log/messages 2>/dev/null || true; "
            "echo final-stage=$(cat /var/lib/rootless-vm/provision-stage "
            "2>/dev/null || echo missing); exit 1; fi; "
            "docker --version && runc --version | head -n 1 "
            "&& rc-service qemu-guest-agent status "
            "&& timeout 180 sh -c 'until docker info >/dev/null 2>&1; do sleep 2; done'",
            timeout=240,
        )
        if result.return_code:
            detail = (result.stdout + result.stderr).decode(errors="replace")[-2000:]
            serial = session.serial_log.read_text(errors="replace")[-6000:]
            raise RuntimeError(
                f"base disk verification failed: {detail}; serial tail: {serial}"
            )
        verification = result.stdout.decode(errors="replace").strip()
        session.shutdown_guest(timeout=120)
        return verification


def build_base_disk(
    *,
    lock_path: str | os.PathLike[str],
    output: str | os.PathLike[str],
    cache_root: str | os.PathLike[str],
    state_root: str | os.PathLike[str],
    runtime: QemuRuntime,
    disk_size_gib: int = 20,
    provision_timeout_sec: float = 1200,
    verify_timeout_sec: float = 360,
) -> BaseDiskBuildResult:
    """Build and verify a standalone qcow2 using only unprivileged QEMU TCG."""

    if not 4 <= disk_size_gib <= 1024:
        raise ValueError("base disk size must be between 4 and 1024 GiB")
    if runtime.qemu_img is None:
        raise ValueError("qemu-img is required to build a base disk")
    target = Path(output).expanduser()
    if target.exists() or target.is_symlink():
        raise ValueError(f"refusing to overwrite base disk: {target}")
    output_root = _private_dir(target.parent, "base disk output directory")
    target = output_root / target.name
    metadata = target.with_name(f"{target.name}.json")
    if metadata.exists() or metadata.is_symlink():
        raise ValueError(f"refusing to overwrite base disk metadata: {metadata}")
    cache = _private_dir(cache_root, "base disk download cache")
    state = _private_dir(state_root, "base disk state root")
    lock = load_base_disk_lock(lock_path)
    runtime.preflight()
    cloud_image = _fetch(lock.image, cache)
    packages = tuple((source, _fetch(source, cache)) for source in lock.packages)
    token = uuid.uuid4().hex
    seed = state / f"seed-{token}.iso"
    candidate = output_root / f".{target.name}.{token}.partial"
    try:
        write_offline_seed_iso(
            seed,
            packages,
            instance_id=f"rootless-vm-base-{lock.digest[:24]}",
        )
        with SandboxSession(
            SandboxSpec(
                runtime=runtime,
                state_root=state,
                base_disk=cloud_image,
                disk_virtual_size_gib=disk_size_gib,
                read_only_images=(seed,),
                memory_mib=2048,
                cpus=4,
                network=NetworkMode.NONE,
            )
        ) as session:
            session.start(timeout=30)
            assert session.process is not None and session.overlay is not None
            try:
                return_code = session.process.wait(timeout=provision_timeout_sec)
            except subprocess.TimeoutExpired as exc:
                serial = session.serial_log.read_text(errors="replace")[-4000:]
                raise TimeoutError(
                    f"offline base disk provisioning timed out; serial tail: {serial}"
                ) from exc
            if return_code:
                serial = session.serial_log.read_text(errors="replace")[-4000:]
                raise RuntimeError(
                    f"offline base disk provisioning exited {return_code}; "
                    f"serial tail: {serial}"
                )
            _qemu_img(
                runtime,
                ["convert", "-f", "qcow2", "-O", "qcow2", str(session.overlay), str(candidate)],
                timeout=600,
            )
        candidate.chmod(0o600)
        verification = _verify_disk(
            runtime, candidate, state, timeout=verify_timeout_sec
        )
        disk_sha256 = _digest(candidate, "sha256")
        os.replace(candidate, target)
        metadata_payload = {
            "schema": 1,
            "disk": target.name,
            "sha256": disk_sha256,
            "lock_sha256": lock.digest,
            "source_image": lock.image.url,
            "source_image_sha512": lock.image.digest,
            "packages": len(lock.packages),
            "disk_size_gib": disk_size_gib,
            "qemu_version": ".".join(map(str, runtime.version())),
            "network_during_guest_provisioning": "none",
            "verification": verification,
        }
        temporary_metadata = metadata.with_name(
            f".{metadata.name}.{uuid.uuid4().hex}.partial"
        )
        temporary_metadata.write_text(
            json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_metadata.chmod(0o600)
        os.replace(temporary_metadata, metadata)
        return BaseDiskBuildResult(
            disk=target,
            metadata=metadata,
            sha256=disk_sha256,
            lock_sha256=lock.digest,
            verification=verification,
        )
    finally:
        seed.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)
