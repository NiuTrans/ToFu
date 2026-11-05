"""Reuse the compiled JavaScript reactor across processes.

Entry point: load_compiled_module. Compiling the pinned reactor takes over a
second on one core, while deserializing the same engine artifact takes
milliseconds. The file is derived data under ``data_root()``: a missing,
unreadable, foreign, corrupted or incompatible artifact is only a cache miss,
and compilation produces the identical module. Wasmtime itself rejects an
artifact built for another Wasmtime version, target or engine configuration;
the stored payload digest rejects other corruption before native code loads.
Dependencies: Wasmtime (passed by the caller) and lib.runtime.runtime_paths.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile

from lib.log import get_logger

logger = get_logger(__name__)

_PREFIX = 'quickjs-'
_SUFFIX = '.cwasm'


def compiled_module_path(wasm_sha256: str) -> str:
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'javascript-engine',
                        f'{_PREFIX}{wasm_sha256[:16]}{_SUFFIX}')


def _read_owned_payload(path: str) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as handle:
        status = os.fstat(handle.fileno())
        # Deserializing loads native code, so only this user's private file counts.
        if not stat.S_ISREG(status.st_mode) or (
                hasattr(os, 'getuid') and (status.st_uid != os.getuid()
                                           or status.st_mode & 0o022)):
            return None
        blob = handle.read()
    digest = hashlib.sha256()
    digest_size = digest.digest_size
    digest.update(memoryview(blob)[digest_size:])
    if len(blob) <= digest_size or digest.digest() != blob[:digest_size]:
        return None
    return blob[digest_size:]


def _write_payload(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + _PREFIX, suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(hashlib.sha256(payload).digest())
            handle.write(payload)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    current = os.path.basename(path)
    for name in os.listdir(directory):
        if name != current and name.startswith(_PREFIX) and name.endswith(_SUFFIX):
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(directory, name))


def load_compiled_module(wasmtime, engine, wasm: bytes, wasm_sha256: str):
    """Return the reactor module for *engine*, compiling only on a cache miss."""
    path = compiled_module_path(wasm_sha256)
    try:
        payload = _read_owned_payload(path)
        if payload is not None:
            return wasmtime.Module.deserialize(engine, payload)
    except Exception as exc:  # noqa: BLE001 -- any unusable artifact is a miss
        logger.info('[JavaScript] compiled engine cache ignored: %s', exc)
    module = wasmtime.Module(engine, wasm)
    try:
        _write_payload(path, bytes(module.serialize()))
    except Exception as exc:  # noqa: BLE001 -- the compiled module is already usable
        logger.info('[JavaScript] compiled engine cache not written: %s', exc)
    return module
