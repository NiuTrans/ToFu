#!/usr/bin/env python3
"""Cross-DC latency probing — pure I/O measurement, no shared state.

This submodule contains only ``_probe_latency`` and the probe timeout
constant.  It touches NONE of the process-wide detection singletons
(``_clusters``, ``_initialized``, …) that live in ``_state.py`` — so it is
safe to keep separate from the state module.
"""

import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


# Benchmark probe timeout per cluster
_PROBE_TIMEOUT_S = 10.0


def _probe_latency(path, timeout=_PROBE_TIMEOUT_S):
    """Measure real I/O latency to a storage cluster.

    Uses stat() on a non-existent path to avoid FUSE metadata cache hits.
    Cached stat() on existing paths returns ~0ms even for cross-DC clusters,
    so we must probe paths that force a round-trip to the metadata server.

    Returns latency in seconds, or None if the probe timed out.
    """
    import random
    result = [None]
    event = threading.Event()

    def _do_probe():
        t0 = time.monotonic()
        try:
            # Probe a non-existent path to bypass FUSE metadata cache
            probe_name = f'_latency_probe_{random.randint(100000, 999999)}'
            probe_path = os.path.join(path, probe_name)
            try:
                os.stat(probe_path)
            except FileNotFoundError as _e_audit:
                logger.debug('[cross_dc] _do_probe caught %s: %s', type(_e_audit).__name__, _e_audit)
                pass  # Expected — we're measuring the round-trip time
            result[0] = time.monotonic() - t0
        except OSError as _e_audit:
            # Mount point itself is inaccessible
            logger.debug('[cross_dc] _do_probe caught %s: %s', type(_e_audit).__name__, _e_audit)
            result[0] = time.monotonic() - t0
        finally:
            event.set()

    t = threading.Thread(target=_do_probe, daemon=True, name='cross-dc-probe')
    t.start()
    completed = event.wait(timeout=timeout)

    if not completed:
        logger.warning('[CrossDC] Probe timed out for %s (>%.0fs)', path, timeout)
        return None

    return result[0]
