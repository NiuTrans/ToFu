"""Shared tofu-db engine binary resolution for xdist-parallel suites.

The differential suite builds the engine from source; the migration suites
only execute it. cargo relinks its output in place, and an xdist worker
execve()ing that same path fails with ETXTBSY — so all suites execute a
stable copy that is only ever replaced atomically (write-temp-then-rename
inside one flock), never written in place. Processes cache their resolution
for the whole lifetime: a replaced copy keeps the old inode alive for
already-running daemons.

Entry point: ensure_engine_binary. Returns None when no binary can be
resolved; the caller decides skip-vs-fail policy.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

_CARGO_CANDIDATES = (
    Path(
        '/path/to/your/data'
        '.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/cargo'
    ),
    Path(shutil.which('cargo') or '/nonexistent'),
)

_RESOLVED: Path | None = None
_RESOLUTION_ATTEMPTED = False


def _digest(path: Path) -> str:
    reader = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            reader.update(chunk)
    return reader.hexdigest()


def _sync_stable_copy(built: Path, stable: Path) -> None:
    """Replace ``stable`` with ``built`` only when content differs, atomically.

    Callers hold the build lock. A plain rename keeps already-exec'd daemons
    on the old inode and never presents a partially-written file to a new
    execve — this is the ETXTBSY-free publication channel.
    """
    if stable.is_file() and _digest(stable) == _digest(built):
        return
    temporary = stable.with_name(f'{stable.name}.pid-{os.getpid()}.new')
    shutil.copyfile(built, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, stable)


def _cargo_build(project_root: Path, cargo: Path) -> Path:
    """Build inside the cross-process lock and return the stable copy."""
    import fcntl

    manifest = project_root / 'packages/tofu-db/Cargo.toml'
    target = project_root / 'packages/tofu-db/target-differential'
    target.mkdir(parents=True, exist_ok=True)
    stable = target / 'stable' / 'tofu-db'
    stable.parent.mkdir(parents=True, exist_ok=True)
    with open(target / '.build.lock', 'w', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        env = dict(os.environ)
        env.setdefault(
            'CARGO_HOME',
            '/path/to/your/data',
        )
        env.setdefault(
            'RUSTUP_HOME',
            '/path/to/your/data',
        )
        # cargo needs rustc on PATH; the shared toolchain bin directory
        # provides both (see the rustup-shared-toolchain project memory).
        env['PATH'] = f'{cargo.parent}:{env.get("PATH", "")}'
        env['CARGO_TARGET_DIR'] = str(target)
        try:
            subprocess.run(
                [str(cargo), 'build', '--manifest-path', str(manifest)],
                # cargo discovers .cargo/config.toml from the working
                # directory, not the manifest path: the package config pins
                # a devtoolset C toolchain that libmimalloc-sys requires.
                cwd=str(manifest.parent),
                check=True, capture_output=True, text=True, timeout=1800,
                env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = str(getattr(exc, 'stderr', '') or exc)[-2000:]
            raise RuntimeError(
                f'tofu-db source build failed; refusing a stale binary '
                f'fallback: {detail}') from exc
        built = target / 'debug/tofu-db'
        if not (built.is_file() and os.access(built, os.X_OK)):
            raise RuntimeError('tofu-db source build returned no executable candidate')
        _sync_stable_copy(built, stable)
    return stable


def ensure_engine_binary(project_root: Path) -> Path | None:
    """Resolve an executable tofu-db engine, building one when possible.

    Honors TOFUDB_DIFFERENTIAL_BIN first (validated executable). Without
    cargo, falls back to the stable copy (if a previous run produced one) or
    the prebuilt debug engine. Returns None when nothing is executable.
    """
    global _RESOLVED, _RESOLUTION_ATTEMPTED
    if _RESOLUTION_ATTEMPTED:
        return _RESOLVED
    _RESOLUTION_ATTEMPTED = True
    override = os.environ.get('TOFUDB_DIFFERENTIAL_BIN', '').strip()
    if override:
        binary = Path(override)
        _RESOLVED = binary if binary.is_file() and os.access(binary, os.X_OK) else None
        return _RESOLVED
    cargo = next((path for path in _CARGO_CANDIDATES if path.is_file()), None)
    if cargo is not None:
        _RESOLVED = _cargo_build(project_root, cargo)
        return _RESOLVED
    for candidate in (
        project_root / 'packages/tofu-db/target-differential/stable/tofu-db',
        project_root / 'packages/tofu-db/target/debug/tofu-db',
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            _RESOLVED = candidate
            return _RESOLVED
    _RESOLVED = None
    return None
