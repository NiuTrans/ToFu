"""lib/boot_identity.py — per-process boot identity for restart verification.

WHY
---
The restart button re-execs the server in place (``os.execv``), which KEEPS the
same PID and the same process start time — execv replaces the image but the
kernel does not reset either. So neither the PID nor ``ps``'s lstart can tell a
genuinely-new post-restart process from the OLD one still answering during the
drain window. The restart UI previously declared success the instant
``/api/health`` answered ``ok``, which cannot distinguish those two — the root
of "the restart looked fine but old code is still serving".

This module provides the reliable signal: a ``BOOT_ID`` generated ONCE at module
import. Because an ``os.execv`` re-exec re-imports every module from scratch, the
fresh process gets a NEW ``BOOT_ID`` while a lingering old process keeps its old
one. The restart client captures the pre-restart ``BOOT_ID`` and only declares
success when ``/api/health`` returns a DIFFERENT one — proof a new process
answered. ``cache_fix_gen()`` additionally surfaces the loaded (in-memory)
cache-fix version so a stale-code restart is visible, not silently green.

All values are cheap module constants; import has no side effects beyond reading
the pid and minting one uuid.
"""

from __future__ import annotations

import os
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

# Fresh per-process identity. Regenerated on every import — and an os.execv
# re-exec re-imports from scratch, so the restarted process gets a NEW value
# even though PID + start-time are unchanged. This is the load-bearing
# "different process?" signal the restart verifier keys on.
BOOT_ID = uuid.uuid4().hex

# Wall-clock at import (epoch seconds). Advisory display only — NOT used as the
# restart signal (a manual full stop+start would also advance it, but the
# execv path would not reset lstart; BOOT_ID covers both uniformly).
BOOT_TS = time.time()

# This process's pid. On an execv re-exec this stays the SAME, so it is NOT a
# sufficient restart signal on its own — reported for operator diagnostics only.
PID = os.getpid()


def cache_fix_gen():
    """Return the IN-MEMORY ``lib.llm.cache.CACHE_FIX_GEN`` (loaded bytecode
    version of the prefix-cache fix chain), or ``None`` if unavailable.

    Best-effort: a failure to import must never break the health endpoint.
    """
    try:
        from lib.llm.cache import CACHE_FIX_GEN
        return CACHE_FIX_GEN
    except Exception as e:
        logger.debug('[BootIdentity] cache_fix_gen unavailable: %s', e)
        return None


__all__ = ['BOOT_ID', 'BOOT_TS', 'PID', 'cache_fix_gen']
