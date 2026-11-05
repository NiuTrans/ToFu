"""Shared tofu-db engine binary resolution for xdist-parallel suites.

The differential suite builds the engine from source; the migration suites
only execute it. cargo relinks its output in place, and an xdist worker
execve()ing that same path fails with ETXTBSY — so all suites execute a
stable copy that is only ever replaced atomically (write-temp-then-rename
inside one flock), never written in place. Processes cache their resolution
for the whole lifetime: a replaced copy keeps the old inode alive for
already-running daemons.

Entry point: ensure_engine_binary. Returns None when no binary can be
resolved; the caller decides skip-vs-fail policy. A failed source build is
remembered for the process and raised to every later caller; an interrupted
build is retried. The build runs outside the calling test's per-test deadline
(docs/TESTING_STRATEGY.md, "Source-built test engines").
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
_FAILURE: str | None = None
_CARGO_BUILD_TIMEOUT_SECONDS = 1800


def _outside_test_deadline(build):
    """Run ``build`` with the caller's per-test deadline paused.

    pytest-timeout's signal method arms ITIMER_REAL for a test's setup, call
    and teardown. A cold engine build outlasts that hang guard, erroring the
    first dependent test and every test queued behind the build lock. The
    build keeps its own deadline (_CARGO_BUILD_TIMEOUT_SECONDS); the test's
    remaining time is re-armed afterwards, whatever the outcome.
    """
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return build()
    remaining, interval = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        return build()
    finally:
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, remaining, interval)


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


def _run_build(argv: list[str], *, cwd: Path, env: dict[str, str],
               timeout: float) -> subprocess.CompletedProcess:
    """Run one build in its own session; retire the whole group on every exit.

    cargo forks rustc, cc, collect2 and the linker. ``subprocess.run`` kills
    only its direct child on a timeout or on an exception raised into the wait
    (a pytest-timeout interruption), which leaves the toolchain running under
    PID 1. The group is retired before any outcome, including success, is
    reported (docs/DEFENSIVE_PATTERNS.md, "Joined producers").
    """
    from lib.project_mod.process_group import retire_process_group

    process = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    finally:
        try:
            retire_process_group(
                process.pid, wait_leader=lambda seconds: process.wait(timeout=seconds))
        finally:
            process.stdout.close()
            process.stderr.close()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, argv, stdout, stderr)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


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
            _run_build(
                [str(cargo), 'build', '--manifest-path', str(manifest)],
                # cargo discovers .cargo/config.toml from the working
                # directory, not the manifest path: build from the crate so
                # any package-local cargo configuration applies.
                cwd=manifest.parent, env=env, timeout=_CARGO_BUILD_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            import re
            diagnostic = str(getattr(exc, 'stderr', '') or exc)
            primary = re.search(r'^error(?:\[[^]\n]+\])?:', diagnostic, re.MULTILINE)
            # Keep the existing diagnostic budget around the actionable error;
            # warning-heavy tails must not hide the source location to repair.
            detail = (diagnostic[primary.start():primary.start() + 2000]
                      if primary else diagnostic[-2000:])
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
    global _RESOLVED, _RESOLUTION_ATTEMPTED, _FAILURE
    if _FAILURE is not None:
        raise RuntimeError(_FAILURE)
    if _RESOLUTION_ATTEMPTED:
        return _RESOLVED
    override = os.environ.get('TOFUDB_DIFFERENTIAL_BIN', '').strip()
    if override:
        _RESOLUTION_ATTEMPTED = True
        binary = Path(override)
        _RESOLVED = binary if binary.is_file() and os.access(binary, os.X_OK) else None
        return _RESOLVED
    cargo = next((path for path in _CARGO_CANDIDATES if path.is_file()), None)
    if cargo is not None:
        try:
            built = _outside_test_deadline(lambda: _cargo_build(project_root, cargo))
        except RuntimeError as exc:
            _FAILURE = str(exc)
            raise
        _RESOLVED, _RESOLUTION_ATTEMPTED = built, True
        return _RESOLVED
    _RESOLUTION_ATTEMPTED = True
    for candidate in (
        project_root / 'packages/tofu-db/target-differential/stable/tofu-db',
        project_root / 'packages/tofu-db/target/debug/tofu-db',
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            _RESOLVED = candidate
            return _RESOLVED
    _RESOLVED = None
    return None
