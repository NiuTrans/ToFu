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

import hashlib
import os
import threading
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


# Computed ONCE at first request and frozen for the process lifetime. Because a
# restart is an os.execv re-exec (fresh import), the next process recomputes it
# from the on-disk source it actually loaded. Freezing means the value reflects
# code-as-loaded-at-boot, not a later mid-run edit — so the restart verifier
# compares "source the OLD process loaded" vs "source the NEW process loaded".
_FINGERPRINT_LOCK = threading.Lock()
_FINGERPRINT_CACHE: 'dict | None' = None


def _compute_code_fingerprint() -> dict:
    """Fingerprint the tracked source tree as loaded by this process.

    Combines the committed HEAD sha with a hash of ``git diff HEAD`` so that
    UNCOMMITTED edits to tracked files (this project's dominant change mode)
    are reflected too — a HEAD-only signal would report "unchanged" across a
    restart that only picked up working-tree edits. Returns a dict with:

      ``head``   — HEAD commit sha (short), or None outside a git checkout.
      ``dirty``  — True when there are uncommitted tracked-source edits.
      ``digest`` — 12-hex fold of (head + ``git diff HEAD`` bytes); the single
                   value the restart verifier compares. None when git is
                   unavailable (non-checkout deploy) — the caller then falls
                   back to the bootId-only rule.

    Best-effort: any failure yields ``{'head': None, 'dirty': None,
    'digest': None}`` so ``/api/health`` never breaks over this.
    """
    try:
        from lib.self_update._git import _head_sha, _run_git
    except Exception as e:
        logger.debug('[BootIdentity] git helpers unavailable: %s', e)
        return {'head': None, 'dirty': None, 'digest': None}

    # NOTE: we deliberately do NOT gate on _git.git_available() — it probes
    # os.path.isdir(_ROOT/'.git') where _ROOT resolves to <project>/lib, so it
    # false-negatives on a real checkout. _head_sha() + `git diff HEAD` run with
    # cwd inside the tree and git walks UP to the repo, so they succeed
    # regardless; a genuine non-git deploy yields head=None + non-zero diff rc,
    # which the all-None guard below handles correctly.
    head = _head_sha()
    diff_bytes = b''
    dirty = False
    try:
        # diff HEAD covers staged + unstaged edits to TRACKED files. Untracked
        # files (build artifacts, .tofu runtime state) are intentionally
        # excluded — they are not the loaded source and would make the digest
        # churn on every request.
        cp = _run_git(['diff', 'HEAD'])
        if cp.returncode == 0 and cp.stdout:
            diff_bytes = cp.stdout.encode('utf-8', 'replace')
            dirty = bool(diff_bytes.strip())
    except Exception as e:
        logger.debug('[BootIdentity] git diff HEAD failed: %s', e)

    if not head and not diff_bytes:
        return {'head': None, 'dirty': None, 'digest': None}

    h = hashlib.sha256()
    h.update((head or '').encode('ascii', 'replace'))
    h.update(b'\x00')
    h.update(diff_bytes)
    return {
        'head': (head or '')[:12] or None,
        'dirty': dirty,
        'digest': h.hexdigest()[:12],
    }


def code_fingerprint() -> dict:
    """Return the frozen source-tree fingerprint for this process.

    Computed lazily on first call and cached for the process lifetime (a
    restart re-execs → fresh process → recomputes from freshly-loaded source).
    See :func:`_compute_code_fingerprint` for the dict shape.
    """
    global _FINGERPRINT_CACHE
    if _FINGERPRINT_CACHE is not None:
        return _FINGERPRINT_CACHE
    with _FINGERPRINT_LOCK:
        if _FINGERPRINT_CACHE is None:
            _FINGERPRINT_CACHE = _compute_code_fingerprint()
    return _FINGERPRINT_CACHE


__all__ = ['BOOT_ID', 'BOOT_TS', 'PID', 'cache_fix_gen', 'code_fingerprint']
