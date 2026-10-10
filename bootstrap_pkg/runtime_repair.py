"""Installer-only, transactional repair of a pinned Wasmtime native library.

Entry point: repair_javascript_runtime. Dependencies: stdlib, bounded installer
runner, canonical host probe. No repair happens in a tool call or server import.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib import metadata
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen

from .process import run_command, redact

WASMTIME_VERSION = '49.0.0'
SOURCE_SHA256 = '5d5af18b1f21e354186a51958ac2e2a02f186c06df7ff606511542b5d5449459'
SOURCE_URL = f'https://codeload.github.com/bytecodealliance/wasmtime/tar.gz/refs/tags/v{WASMTIME_VERSION}'
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
BUILD_TIMEOUT = 1800


def _download_source(destination: Path) -> Path:
    archive = destination / 'source.tar.gz'
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 120
    with urlopen(SOURCE_URL, timeout=30) as response, archive.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES or time.monotonic() > deadline:
                raise RuntimeError('Wasmtime source download exceeded its size/time bound')
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != SOURCE_SHA256:
        raise RuntimeError('Wasmtime source checksum mismatch; refusing build')
    with tarfile.open(archive, 'r:gz') as source:
        if sum(member.size for member in source.getmembers()) > MAX_SOURCE_BYTES:
            raise RuntimeError('Wasmtime source exceeds extraction size bound')
        source.extractall(destination, filter='data')
    archive.unlink()
    return destination / f'wasmtime-{WASMTIME_VERSION}'


def _build_library(destination: Path, emit, *, root: Path | None = None) -> Path:
    environment = dict(os.environ)
    if root is not None:
        from tofu_dotenv import read_dotenv_values
        configured = read_dotenv_values(root / '.env')
        for name in ('CARGO_HOME', 'RUSTUP_HOME', 'PATH', 'CC', 'CXX',
                     'HOST_CC', 'HOST_CXX', 'CARGO_BUILD_JOBS'):
            if name in configured:
                environment.setdefault(name, configured[name])
    cargo = shutil.which('cargo', path=environment.get('PATH', os.defpath)) or shutil.which('cargo', path=str(
        Path(environment.get('CARGO_HOME', str(Path.home() / '.cargo'))) / 'bin'))
    if not cargo:
        raise RuntimeError('Host-compatible Wasmtime build requires Rust >=1.96 and a C compiler; Cargo was not found')
    environment['PATH'] = str(Path(cargo).parent) + os.pathsep + environment.get('PATH', os.defpath)
    # Honor compiler/toolchain overrides. Defaults share the launch-time probe.
    from runtime_guards import probe_system_resources
    capacity = probe_system_resources(environment)
    jobs = max(1, min(capacity.effective_cpu_count,
                      (capacity.effective_memory_available_mb or 1024) // 1024))
    environment.setdefault('CARGO_BUILD_JOBS', str(jobs))
    environment['CARGO_TARGET_DIR'] = str(destination / 'target')
    if not shutil.which('cmake', path=environment['PATH']):
        # A private build dependency, never installed over the application env.
        build_tools = destination / 'build-tools'
        code, tail = run_command([sys.executable, '-m', 'pip', 'install', '--no-input',
                                  '--no-user', '--target', str(build_tools), 'cmake==3.31.6'],
                                 timeout=300, env=environment, emit=emit)
        if code:
            raise RuntimeError(f'Cannot prepare CMake for Wasmtime build: {tail[-4096:]}')
        environment['PATH'] = str(build_tools / 'cmake/data/bin') + os.pathsep + environment['PATH']
    emit(f'Downloading checksum-pinned Wasmtime {WASMTIME_VERSION} source')
    source = _download_source(destination)
    emit(f'Building Wasmtime {WASMTIME_VERSION} for this host ({environment["CARGO_BUILD_JOBS"]} jobs)')
    code, tail = run_command([cargo, 'build', '--release', '--locked', '-p', 'wasmtime-c-api'],
                             cwd=str(source), env=environment,
                             timeout=BUILD_TIMEOUT, emit=emit)
    if code:
        raise RuntimeError(f'Wasmtime source build failed (exit {code}): {tail[-4096:]}')
    return destination / 'target/release/libwasmtime.so'


def _publish_library(candidate: Path, target: Path, *, root: Path, check) -> tuple[bool, str]:
    """Atomic replacement followed by verification; restore on every failure."""
    if target.is_symlink() or not target.is_file():
        raise RuntimeError('Refusing an absent or symlinked Wasmtime native library')
    with tempfile.TemporaryDirectory(prefix='.wasmtime-repair-', dir=target.parent) as directory:
        staging = Path(directory)
        previous = staging / 'previous.so'
        incoming = staging / 'incoming.so'
        shutil.copy2(target, previous)
        shutil.copy2(candidate, incoming)
        os.replace(incoming, target)
        healthy = False
        try:
            healthy, detail = check(root=root)
            if not healthy:
                return False, detail
            return True, f'Host-compatible Wasmtime {WASMTIME_VERSION} installed. {detail}'
        finally:
            # Includes cancellation and exceptions during the verification.
            if sys.exc_info()[0] is not None or not healthy:
                os.replace(previous, target)


@contextmanager
def _repair_lock(directory: Path):
    """One writer per installed native library; never unlink an advisory lock."""
    import fcntl
    descriptor = os.open(directory / '.tofu-wasmtime-repair.lock',
                         os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another Wasmtime repair owns this environment; retry after it finishes') from exc
        yield
    finally:
        os.close(descriptor)


def repair_javascript_runtime(detail: str, *, root: Path, emit=None) -> tuple[bool, str]:
    """Rebuild only recognized Linux glibc loader failures, never downgrade."""
    emit = emit or (lambda line: print(redact(line), file=sys.stderr))
    if sys.platform != 'linux' or not re.search(r'GLIBC_\d+\.\d+[^\n]*not found', detail):
        return False, detail
    from .runtime_checks import check_javascript_runtime
    try:
        distribution = metadata.distribution('wasmtime')
        if distribution.version != WASMTIME_VERSION:
            raise RuntimeError(f'Repair supports only pinned Wasmtime {WASMTIME_VERSION}, found {distribution.version}')
        machine = {'arm64': 'aarch64', 'AMD64': 'x86_64'}.get(platform.machine(), platform.machine())
        if machine not in ('x86_64', 'aarch64'):
            raise RuntimeError(f'Unsupported Wasmtime build architecture: {machine}')
        package = Path(distribution.locate_file('wasmtime')).resolve()
        target = package / f'linux-{machine}/_libwasmtime.so'
        if target.is_symlink() or not target.is_file():
            raise RuntimeError('Refusing an absent or symlinked Wasmtime native library')
        # Compile on the host temporary volume, avoiding slow installation
        # mounts. Only atomic publication stages on the target filesystem.
        # No persistent cache survives settled success/failure/cancellation.
        with _repair_lock(target.parent), tempfile.TemporaryDirectory(prefix='tofu-wasmtime-build-') as directory:
            staging = Path(directory)
            library = _build_library(staging, emit, root=root)
            copied_package = staging / 'probe/wasmtime'
            shutil.copytree(package, copied_package, ignore=shutil.ignore_patterns('__pycache__', '.wasmtime-*'))
            shutil.copy2(library, copied_package / f'linux-{machine}/_libwasmtime.so')
            healthy, probe_detail = check_javascript_runtime(root=root, package_root=copied_package.parent)
            if not healthy:
                raise RuntimeError(f'Host-built Wasmtime execution check failed: {probe_detail}')
            return _publish_library(library, target, root=root, check=check_javascript_runtime)
    except Exception as exc:
        return False, f'{detail}\nAutomatic host-compatible repair failed: {redact(str(exc))}'
