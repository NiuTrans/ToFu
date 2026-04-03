#!/usr/bin/env python3
"""DolphinFS (FUSE) keepalive daemon.

Problem
-------
When the user's VS Code SSH / port-forwarding session disconnects,
the DolphinFS (BeeGFS over FUSE) mount can go idle.  After enough idle
time the kernel FUSE connection stales, causing ALL subsequent I/O on
``/mnt/your-fs`` to block in uninterruptible sleep (D-state) for
minutes or even hours — until the network path recovers.

Because PostgreSQL's ``data/pgdata/`` lives on the same FUSE mount,
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

# Paths to stat — we touch the project root (data/) and pgdata.
# If pgdata doesn't exist yet (fresh install), we just stat the project root.
_PROBE_PATHS = [
    os.path.join(_BASE_DIR, 'data'),
    os.path.join(_BASE_DIR, 'data', 'pgdata'),
    os.path.join(_BASE_DIR, 'logs'),
]

_running = False
_thread = None


# ═══════════════════════════════════════════════════════════════════════
#  Core keepalive logic
# ═══════════════════════════════════════════════════════════════════════

def _stat_with_timeout(path, timeout):
    """Perform os.stat(path) with a timeout.

    Returns:
        (ok: bool, elapsed: float)
        ok=True if stat completed within timeout, False if it hung.
    """
    result = [False, 0.0]
    event = threading.Event()

    def _do_stat():
        t0 = time.monotonic()
        try:
            os.stat(path)
            result[0] = True
        except OSError:
            # Path doesn't exist — that's fine, the mount is still alive
            result[0] = True
        finally:
            result[1] = time.monotonic() - t0
            event.set()

    t = threading.Thread(target=_do_stat, daemon=True, name='fs-ka-probe')
    t.start()
    completed = event.wait(timeout=timeout)

    if not completed:
        result[1] = timeout
        return False, timeout

    return result[0], result[1]


def _keepalive_loop():
    """Main loop — runs in a daemon thread."""
    global _running

    logger.info('[FS-Keepalive] Started (interval=%ds, paths=%d)',
                KEEPALIVE_INTERVAL_S, len(_PROBE_PATHS))

    consecutive_failures = 0
    consecutive_slow = 0

    while _running:
        try:
            worst_elapsed = 0.0
            any_failure = False

            for path in _PROBE_PATHS:
                ok, elapsed = _stat_with_timeout(path, STAT_TIMEOUT_S)
                worst_elapsed = max(worst_elapsed, elapsed)

                if not ok:
                    any_failure = True
                    logger.error(
                        '[FS-Keepalive] stat(%s) TIMED OUT after %.1fs — '
                        'FUSE mount appears stale/frozen!', path, elapsed
                    )
                elif elapsed > STAT_WARN_THRESHOLD_S:
                    logger.warning(
                        '[FS-Keepalive] stat(%s) slow: %.2fs (threshold=%.1fs)',
                        path, elapsed, STAT_WARN_THRESHOLD_S
                    )

            if any_failure:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    logger.error(
                        '[FS-Keepalive] FUSE mount freeze detected! '
                        'All DolphinFS I/O will block until recovery. '
                        'consecutive_failures=%d', consecutive_failures
                    )
                elif consecutive_failures % 10 == 0:
                    logger.error(
                        '[FS-Keepalive] FUSE mount still frozen '
                        '(%.0f min, consecutive_failures=%d)',
                        consecutive_failures * KEEPALIVE_INTERVAL_S / 60,
                        consecutive_failures
                    )
            else:
                if consecutive_failures > 0:
                    logger.info(
                        '[FS-Keepalive] FUSE mount recovered after %d failed probes '
                        '(~%.0f min frozen)',
                        consecutive_failures,
                        consecutive_failures * KEEPALIVE_INTERVAL_S / 60
                    )
                consecutive_failures = 0

                if worst_elapsed > STAT_WARN_THRESHOLD_S:
                    consecutive_slow += 1
                else:
                    if consecutive_slow > 5:
                        logger.info(
                            '[FS-Keepalive] Latency normalized after %d slow probes',
                            consecutive_slow
                        )
                    consecutive_slow = 0

        except Exception as e:
            logger.error('[FS-Keepalive] Unexpected error in keepalive loop: %s',
                         e, exc_info=True)

        # Sleep in small increments so we can stop quickly
        for _ in range(KEEPALIVE_INTERVAL_S * 2):
            if not _running:
                break
            time.sleep(0.5)

    logger.info('[FS-Keepalive] Stopped')


# ═══════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════

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
    global _running, _thread

    if _thread is not None and _thread.is_alive():
        logger.debug('[FS-Keepalive] Already running, skipping start')
        return

    # Non-Linux platforms: graceful skip
    if not _IS_LINUX:
        logger.debug('[FS-Keepalive] Skipping on %s (only needed on Linux FUSE mounts)',
                     sys.platform)
        return

    # Only activate on network/FUSE mounts — no point on local disk
    if not _is_network_mount(_BASE_DIR):
        logger.info('[FS-Keepalive] Project not on a network mount — skipping '
                     '(local disk does not need keepalive)')
        return

    _running = True
    _thread = threading.Thread(
        target=_keepalive_loop,
        daemon=True,
        name='fs-keepalive'
    )
    _thread.start()


def stop_fs_keepalive():
    """Stop the keepalive daemon (for clean shutdown)."""
    global _running
    _running = False
    if _thread is not None:
        _thread.join(timeout=5)
