"""Bounded optional CRC compiler/cache; invoked lazily by the wire codec.

One worker per process and one compiler per cache key. Cache entries are
reconstructible, owner-checked and atomically published; failures use Python.
No application imports, network access, import-time I/O, or persistent workers.
"""
from __future__ import annotations

import atexit
import hashlib
import importlib.util
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sysconfig
import tempfile
import threading
import time

COMPILE_TIMEOUT_SECONDS = 5
MAX_LIBRARY_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024
MAX_CACHE_KEYS = 8
MAX_ENTRY_FILES = 4
CACHE_PROBE_INTERVAL_SECONDS = 1
_state_lock = threading.Lock()
_started = False
_closing = False
_candidate = None
_compiler = None
_directory = None
_next_probe = 0.0


def _owned(path: Path, *, directory: bool, private_parent: bool = False) -> bool:
    try:
        info = path.lstat()
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        return (correct_type and info.st_uid == os.getuid()
                and (private_parent or not info.st_mode & 0o022)
                and (directory or (info.st_nlink == 1 and 0 <= info.st_size <= MAX_LIBRARY_BYTES)))
    except OSError:
        return False


def _entries(directory: Path, limit: int):
    with os.scandir(directory) as scan:
        entries = []
        for entry in scan:
            if len(entries) == limit:
                return None
            entries.append(Path(entry.path))
        return entries


def _open_lock(directory: Path):
    import fcntl
    descriptor = os.open(directory / 'compile.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size != 0):
            os.close(descriptor)
            return None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _clean_staging(directory: Path) -> bool:
    entries = _entries(directory, MAX_ENTRY_FILES)
    if entries is None:
        return False
    for path in entries:
        if path.name in {'compile.lock', '_tofudb_crc32c_native.so'}:
            if not _owned(path, directory=False):
                return False
        elif path.name.startswith('build-') and _owned(path, directory=True):
            files = _entries(path, 2)
            if files is None or any(
                file.name not in {'_tofudb_crc32c_native.c', '_tofudb_crc32c_native.so'}
                or not _owned(file, directory=False, private_parent=True) for file in files
            ):
                return False
            shutil.rmtree(path)
        else:
            return False
    return True


def _evict(directory: Path) -> bool:
    if not _owned(directory, directory=True):
        return False
    try:
        descriptor = _open_lock(directory)
    except OSError:
        return False
    if descriptor is None:
        return False
    try:
        if not _clean_staging(directory):
            return False
        shutil.rmtree(directory)
        return True
    finally:
        os.close(descriptor)


def _cache_directory(source: str) -> Path | None:
    if len(source.encode('utf-8')) > MAX_SOURCE_BYTES or not hasattr(os, 'getuid'):
        return None
    parent = Path(tempfile.gettempdir()).resolve(strict=True)
    info = parent.stat()
    # A writable non-sticky parent could substitute a checked child directory.
    if (info.st_uid not in {0, os.getuid()}
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
        return None
    root = parent / f'tofudb-crc32c-{os.getuid()}-v2'
    root.mkdir(mode=0o700, exist_ok=True)
    if not _owned(root, directory=True):
        return None
    identity = '\0'.join((source, str(sysconfig.get_config_var('SOABI')), platform.machine()))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    directory = root / digest
    if _owned(directory, directory=True):
        return directory
    descriptor = _open_lock(root)
    if descriptor is None:
        return None
    try:
        entries = _entries(root, MAX_CACHE_KEYS + 1)
        if entries is None:
            return None
        keys = [path for path in entries if path.name != 'compile.lock']
        if any(not re.fullmatch('[0-9a-f]{24}', path.name)
               or not _owned(path, directory=True) for path in keys):
            return None
        if len(keys) >= MAX_CACHE_KEYS:
            # Only reconstructible, bounded entries are evicted; live compilers
            # hold their entry lock and cannot be selected.
            if not any(_evict(path) for path in sorted(keys, key=lambda path: path.stat().st_mtime_ns)):
                return None
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory if _owned(directory, directory=True) else None
    finally:
        os.close(descriptor)


def _load_cached(directory: Path, reference):
    library = directory / '_tofudb_crc32c_native.so'
    if not _owned(directory, directory=True) or not _owned(library, directory=False):
        return None
    try:
        spec = importlib.util.spec_from_file_location('_tofudb_crc32c_native', library)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.crc32c
        probe = bytes(range(256)) * 5 + b'tofudb-crc32c-self-check'
        for seed in (0, 0x12345678):
            if candidate(probe, seed) != reference(probe, seed):
                return None
        if candidate(b'123456789') != 0xE3069283:
            return None
        return candidate
    except Exception:
        return None


def _kill_compiler(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=1)


def _shutdown():
    global _closing
    with _state_lock:
        _closing = True
        process = _compiler
    if process is not None:
        try:
            _kill_compiler(process)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _compile(source: str, output: Path) -> bool:
    global _compiler
    if len(source.encode('utf-8')) > MAX_SOURCE_BYTES:
        return False
    compiler = shutil.which('cc') or shutil.which('gcc')
    if compiler is None:
        return False
    source_path = output.with_suffix('.c')
    source_path.write_text(source)
    with _state_lock:
        if _closing:
            return False
        process = subprocess.Popen([
            compiler, '-O2', '-std=c99', '-shared', '-fPIC',
            '-I' + sysconfig.get_paths()['include'], '-o', str(output), str(source_path),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        _compiler = process
    try:
        try:
            return process.wait(timeout=COMPILE_TIMEOUT_SECONDS) == 0
        except subprocess.TimeoutExpired:
            _kill_compiler(process)
            return False
    finally:
        with _state_lock:
            if _compiler is process:
                _compiler = None


def _build(source: str, directory: Path, reference):
    global _candidate
    if not _owned(directory, directory=True):
        return
    try:
        descriptor = _open_lock(directory)
    except BlockingIOError:
        return
    if descriptor is None:
        return
    try:
        if not _clean_staging(directory):
            return
        candidate = _load_cached(directory, reference)
        if candidate is None:
            # Compiler output never aliases a library another process may map.
            with tempfile.TemporaryDirectory(prefix='build-', dir=directory) as temporary:
                output = Path(temporary) / '_tofudb_crc32c_native.so'
                if not _compile(source, output) or not _owned(output, directory=False, private_parent=True):
                    return
                output.chmod(0o500)
                os.replace(output, directory / output.name)
            candidate = _load_cached(directory, reference)
        if candidate is not None:
            _candidate = candidate
    finally:
        os.close(descriptor)


def _worker(source: str, directory: Path, reference):
    try:
        _build(source, directory, reference)
    except Exception:
        # Acceleration is optional. Errors must not break framing or leave an
        # executor waiting for another worker/compile retry.
        return


def accelerator(source: str, reference):
    """Return a verified callable, or Python fallback while one worker builds."""
    global _started, _candidate, _directory, _next_probe
    if _candidate is not None:
        return _candidate
    if time.monotonic() < _next_probe or (_started and _directory is None):
        return None
    if not _state_lock.acquire(blocking=False):
        return None
    try:
        if _closing:
            return None
        if _started:
            if _directory is not None and time.monotonic() >= _next_probe:
                _next_probe = time.monotonic() + CACHE_PROBE_INTERVAL_SECONDS
                _candidate = _load_cached(_directory, reference)
            return _candidate
        _started = True
        _next_probe = time.monotonic() + CACHE_PROBE_INTERVAL_SECONDS
        try:
            _directory = _cache_directory(source)
        except BlockingIOError:
            # Another process is allocating a cache key. Retry only after
            # the probe interval, without starting a duplicate build worker.
            _started = False
            return None
        if _directory is None:
            return None
        _candidate = _load_cached(_directory, reference)
        if _candidate is None:
            atexit.register(_shutdown)
            threading.Thread(target=_worker, args=(source, _directory, reference),
                             name='tofudb-crc32c-build', daemon=True).start()
        return _candidate
    except Exception:
        return None
    finally:
        _state_lock.release()
