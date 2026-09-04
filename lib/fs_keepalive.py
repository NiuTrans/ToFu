#!/usr/bin/env python3
"""DolphinFS (FUSE) keepalive daemon.

Problem
-------
When the user's VS Code SSH / port-forwarding session disconnects,
a FUSE-backed network mount (BeeGFS, NFS, etc.) can go idle.  After
enough idle time the kernel FUSE connection stales, causing ALL
subsequent I/O on the mount to block in uninterruptible sleep (D-state)
for minutes or even hours — until the network path recovers.

Because the application's database and files live on the same FUSE mount,
the entire application (task checkpoints, DB queries, tool I/O) freezes
until the mount wakes up.

Solution
--------
A lightweight daemon thread that periodically performs a tiny ``os.stat()``
on the project directory (which lives on DolphinFS).  This keeps the FUSE
mount's kernel ↔ userspace channel active and prevents the connection from
going idle long enough to stale.

The interval is **15 seconds** — short enough to prevent idle-disconnect
(most FUSE clients have >30 s idle thresholds) but cheap enough to have
zero measurable impact (``stat()`` is a single metadata lookup).

If ``stat()`` itself hangs (mount already stale), the daemon detects this
via a watchdog sub-thread and logs a warning — it can't fix a stale mount,
but at least makes the condition visible in ``logs/error.log``.

Usage
-----
Called from ``server.py`` at startup::

    from lib.fs_keepalive import start_fs_keepalive
    start_fs_keepalive()
"""

from __future__ import annotations

import os
import sys
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

# Platform detection (avoid circular import from lib.compat at module level)
_IS_LINUX = sys.platform.startswith('linux')
_IS_MACOS = sys.platform == 'darwin'
_IS_WINDOWS = os.name == 'nt'

# ═══════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How often to poke the filesystem (seconds).
# 15s is well under typical FUSE idle-disconnect thresholds (30-120s).
KEEPALIVE_INTERVAL_S = 15

# If a single stat() takes longer than this, log a warning.
STAT_WARN_THRESHOLD_S = 5.0

# If stat() doesn't return within this time, consider the mount stale.
STAT_TIMEOUT_S = 30.0

# Paths to stat — resolved lazily at start_fs_keepalive() time from the
# ACTUAL writable data/logs roots (lib.runtime_paths), NOT the code tree. A
# source checkout can now place its data root on a DIFFERENT mount than the
# repo (fresh-clone XDG default, or an explicit TOFU_DATA_DIR), so probing the
# code tree would keep the wrong mount warm and let the live-DB mount stale —
# the exact freeze this daemon exists to prevent. Populated by
# ``_resolve_probe_paths()``.
_PROBE_PATHS = []

_running = False
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_lifecycle_lock = threading.Lock()


def _probe_path(path: str) -> None:
    os.stat(path)


class _ProbeRuntime:
    """One bounded daemon that serializes keepalive stats for one mount.

    A FUSE metadata call can enter uninterruptible sleep. Keeping at most one
    request in flight prevents a stale mount from accumulating a fresh daemon
    thread every timeout while the coordinator remains able to warn and stop.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._stop_requested = False
        self._thread: threading.Thread | None = None
        self._request_generation = 0
        self._completed_generation = 0
        self._paths: tuple[str, ...] = ()
        self._results: list[tuple[str, bool, float]] = []
        self._active_path = ''

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested = False
            thread = threading.Thread(
                target=self._loop, daemon=True, name='fs-ka-probe')
            self._thread = thread
            try:
                thread.start()
            except Exception:
                if self._thread is thread:
                    self._thread = None
                raise

    def is_alive(self) -> bool:
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    def request(self, paths: list[str] | tuple[str, ...]) -> int:
        """Submit one batch, or return the generation already in flight."""
        with self._condition:
            if self._stop_requested:
                return self._request_generation
            if self._request_generation <= self._completed_generation:
                self._request_generation += 1
                self._paths = tuple(paths)
                self._results = []
                self._condition.notify_all()
            return self._request_generation

    def wait(
        self, generation: int, timeout: float,
    ) -> tuple[list[tuple[str, bool, float]] | None, str]:
        try:
            wait_seconds = max(0.0, float(timeout))
        except (TypeError, ValueError, OverflowError):
            wait_seconds = STAT_TIMEOUT_S
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while (self._completed_generation < generation
                   and not self._stop_requested):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if self._completed_generation >= generation:
                return list(self._results), ''
            return None, self._active_path

    def request_stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()

    def join(self, timeout: float) -> bool:
        with self._condition:
            thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def _loop(self) -> None:
        current_thread = threading.current_thread()
        observed_generation = 0
        try:
            while True:
                with self._condition:
                    while (not self._stop_requested
                           and self._request_generation <= observed_generation):
                        self._condition.wait()
                    if self._stop_requested:
                        return
                    generation = self._request_generation
                    paths = self._paths

                results = []
                for path in paths:
                    with self._condition:
                        if self._stop_requested:
                            return
                        self._active_path = path
                        self._condition.notify_all()
                    started_at = time.monotonic()
                    ok = True
                    try:
                        _probe_path(path)
                    except OSError as exc:
                        # A missing leaf still proves the mount answered.
                        logger.debug(
                            '[fs_keepalive] stat(%s) returned %s: %s',
                            path, type(exc).__name__, exc)
                    except Exception as exc:
                        ok = False
                        logger.warning(
                            '[FS-Keepalive] unexpected stat(%s) failure: %s',
                            path, exc, exc_info=True)
                    results.append((path, ok, time.monotonic() - started_at))

                with self._condition:
                    self._results = results
                    self._completed_generation = generation
                    self._active_path = ''
                    observed_generation = generation
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._active_path = ''
                if self._thread is current_thread:
                    self._thread = None
                self._condition.notify_all()


_probe_runtime: _ProbeRuntime | None = None


# ═══════════════════════════════════════════════════════════════════════
#  Core keepalive logic
# ═══════════════════════════════════════════════════════════════════════


def _probe_paths_with_timeout(
    runtime: _ProbeRuntime,
    paths: list[str],
    timeout: float,
) -> list[tuple[str, bool, float]]:
    generation = runtime.request(paths)
    results, active_path = runtime.wait(generation, timeout)
    if results is not None:
        return results
    timed_out_path = active_path or (paths[0] if paths else '<unknown>')
    return [(timed_out_path, False, timeout)]


def _keepalive_loop(runtime: _ProbeRuntime):
    """Main loop — runs in a daemon thread."""
    logger.info('[FS-Keepalive] Started (interval=%ds, paths=%d)',
                KEEPALIVE_INTERVAL_S, len(_PROBE_PATHS))

    consecutive_failures = 0
    consecutive_slow = 0

    try:
        while not _stop_event.is_set():
            try:
                worst_elapsed = 0.0
                any_failure = False
                results = _probe_paths_with_timeout(
                    runtime, _PROBE_PATHS, STAT_TIMEOUT_S)
                if _stop_event.is_set():
                    break
                for path, ok, elapsed in results:
                    worst_elapsed = max(worst_elapsed, elapsed)
                    if not ok:
                        any_failure = True
                        logger.error(
                            '[FS-Keepalive] stat(%s) TIMED OUT after %.1fs — '
                            'FUSE mount appears stale/frozen!', path, elapsed)
                    elif elapsed > STAT_WARN_THRESHOLD_S:
                        logger.warning(
                            '[FS-Keepalive] stat(%s) slow: %.2fs '
                            '(threshold=%.1fs)',
                            path, elapsed, STAT_WARN_THRESHOLD_S)

                if any_failure:
                    consecutive_failures += 1
                    if consecutive_failures == 1:
                        logger.error(
                            '[FS-Keepalive] FUSE mount freeze detected! '
                            'All DolphinFS I/O will block until recovery. '
                            'consecutive_failures=%d', consecutive_failures)
                    elif consecutive_failures % 10 == 0:
                        logger.error(
                            '[FS-Keepalive] FUSE mount still frozen '
                            '(%.0f min, consecutive_failures=%d)',
                            consecutive_failures * KEEPALIVE_INTERVAL_S / 60,
                            consecutive_failures)
                else:
                    if consecutive_failures > 0:
                        logger.info(
                            '[FS-Keepalive] FUSE mount recovered after %d '
                            'failed probes (~%.0f min frozen)',
                            consecutive_failures,
                            consecutive_failures * KEEPALIVE_INTERVAL_S / 60)
                    consecutive_failures = 0

                    if worst_elapsed > STAT_WARN_THRESHOLD_S:
                        consecutive_slow += 1
                    else:
                        if consecutive_slow > 5:
                            logger.info(
                                '[FS-Keepalive] Latency normalized after %d '
                                'slow probes', consecutive_slow)
                        consecutive_slow = 0

            except Exception as exc:
                logger.error(
                    '[FS-Keepalive] Unexpected error in keepalive loop: %s',
                    exc, exc_info=True)

            if _stop_event.wait(KEEPALIVE_INTERVAL_S):
                break
    finally:
        runtime.request_stop()
        logger.info('[FS-Keepalive] Stopped')


# ═══════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════

def _resolve_data_root() -> str:
    """Return the ACTUAL writable data root (where the live DB lives).

    Delegates to ``lib.runtime_paths.data_root()`` — the single source of truth
    that honours ``$TOFU_DATA_DIR`` / ``TOFU_DATA_LAYOUT`` / frozen builds — so
    the keepalive follows the DB wherever it actually resides, not the code
    tree. The resolver is memoized, so calling it here is cheap. Falls back to
    the in-tree ``data/`` only if the import fails (should never happen).
    """
    try:
        from lib.runtime_paths import data_root
        return data_root()
    except Exception as e:  # pragma: no cover — defensive
        logger.warning('[FS-Keepalive] runtime_paths.data_root() unavailable, '
                       'falling back to in-tree data/: %s', e)
        return os.path.join(_BASE_DIR, 'data')


def _resolve_probe_paths() -> list:
    """The paths to stat — the resolved data + logs roots (where state lives)."""
    try:
        from lib.runtime_paths import data_root, logs_root
        return [data_root(), logs_root()]
    except Exception as e:  # pragma: no cover — defensive
        logger.warning('[FS-Keepalive] runtime_paths unavailable, probing '
                       'in-tree data/+logs/: %s', e)
        return [os.path.join(_BASE_DIR, 'data'), os.path.join(_BASE_DIR, 'logs')]


def _is_network_mount(path):
    """Detect if *path* is on a network/FUSE mount that may need keepalive.

    Detection strategy per platform:
      - Linux: check if path starts with /mnt/ (DolphinFS/BeeGFS/NFS convention)
      - macOS: check /Volumes/ (network mounts) — but skip on macOS for now
        as FUSE keepalive is a DolphinFS-specific concern.
      - Windows: UNC paths (\\\\server\\share) or non-C: drive letters could be
        network drives, but the keepalive daemon is Linux-specific.

    Returns:
        True if keepalive should be activated.
    """
    if _IS_LINUX:
        return path.startswith('/mnt/')
    # On macOS and Windows, FUSE keepalive is not needed — the problem
    # is specific to DolphinFS/BeeGFS on Linux SSH sessions.
    return False


def start_fs_keepalive():
    """Start the filesystem keepalive daemon thread.

    Safe to call multiple times — only one thread will run.
    Only activates on Linux when the project directory is on a FUSE/network
    mount. On macOS and Windows, this is a graceful no-op.
    """
    global _running, _thread, _probe_runtime

    with _lifecycle_lock:
        if _thread is not None and _thread.is_alive():
            logger.debug('[FS-Keepalive] Already running, skipping start')
            return
        if _probe_runtime is not None and _probe_runtime.is_alive():
            logger.warning(
                '[FS-Keepalive] previous filesystem probe is still stopping; '
                'refusing a duplicate runtime')
            return

    # Non-Linux platforms: graceful skip
    if not _IS_LINUX:
        logger.debug('[FS-Keepalive] Skipping on %s (only needed on Linux FUSE mounts)',
                     sys.platform)
        return

    # Resolve where state ACTUALLY lives (may differ from the code tree) and
    # gate on THAT mount — the daemon exists to keep the live-DB mount warm.
    data_root = _resolve_data_root()
    if not _is_network_mount(data_root):
        logger.info('[FS-Keepalive] Data root %s not on a network mount — '
                    'skipping (local disk does not need keepalive)', data_root)
        return

    global _PROBE_PATHS
    _PROBE_PATHS = _resolve_probe_paths()
    logger.info('[FS-Keepalive] Activating on network-mounted data root %s '
                '(probing: %s)', data_root, ', '.join(_PROBE_PATHS))

    with _lifecycle_lock:
        # Resolve/probe-path work happens outside the lifecycle lock. Recheck
        # after reacquiring it so concurrent startup callers cannot each own a
        # coordinator generation.
        if _thread is not None and _thread.is_alive():
            return
        if _probe_runtime is not None and _probe_runtime.is_alive():
            logger.warning(
                '[FS-Keepalive] previous filesystem probe is still stopping; '
                'refusing a duplicate runtime')
            return
        runtime = _ProbeRuntime()
        runtime.start()
        _stop_event.clear()
        thread = threading.Thread(
            target=_keepalive_loop,
            args=(runtime,),
            daemon=True,
            name='fs-keepalive'
        )
        _probe_runtime = runtime
        _thread = thread
        _running = True
        try:
            thread.start()
        except Exception:
            _running = False
            _thread = None
            _probe_runtime = None
            _stop_event.set()
            runtime.request_stop()
            runtime.join(2.0)
            raise


def stop_fs_keepalive(timeout: float = 2.0) -> bool:
    """Stop and bounded-join the keepalive daemon."""
    global _running, _thread, _probe_runtime
    with _lifecycle_lock:
        _running = False
        _stop_event.set()
        thread = _thread
        runtime = _probe_runtime
        if runtime is not None:
            runtime.request_stop()
        if thread is None and runtime is None:
            return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[FS-Keepalive] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    deadline = time.monotonic() + wait_seconds
    if thread is not None and thread is not threading.current_thread():
        coordinator_budget = (
            wait_seconds if runtime is None
            else max(0.0, deadline - time.monotonic()))
        thread.join(timeout=coordinator_budget)
    coordinator_stopped = thread is None or not thread.is_alive()
    probe_stopped = runtime is None or runtime.join(
        max(0.0, deadline - time.monotonic()))
    stopped = coordinator_stopped and probe_stopped
    if stopped:
        with _lifecycle_lock:
            if _thread is thread:
                _thread = None
            if _probe_runtime is runtime:
                _probe_runtime = None
    return stopped
