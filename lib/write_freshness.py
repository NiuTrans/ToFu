"""Per-conversation write-freshness tokens (shared-HEAD overwrite guard).

On a shared working tree, conversation A can read a file at T0, think for
many rounds, and at T2 blindly overwrite a change sibling B committed at T1.
The read-before-edit gate (``lib/tasks_pkg/handlers/_read_gate.py``) proves
"you read this file at SOME point in this conversation"; it cannot prove
"the file has not changed SINCE". This module closes that gap with an
optimistic-concurrency token:

  • Every successful read / write / edit records a fingerprint
    ``(mtime_ns, size)`` of the target under ``(conv_key, abs_path)``.
  • Write tools are CHECKED before execution (see
    ``lib/tasks_pkg/handlers/_write_freshness_gate.py``): a recorded
    fingerprint that no longer matches the on-disk one means someone else
    touched the file → the write is refused with a re-read instruction
    instead of silently clobbering the sibling's change.

Deliberate failure directions:

  • A MISSING token never blocks (``is_stale`` → False). Blind full-rewrite
    flows that never read the file keep working — the read-before-edit gate
    owns the "must read first" axis for the edit tools; this module owns
    ONLY the "your knowledge went stale" axis.
  • A VANISHED file drops the token and allows the write (creation
    semantics); the read gate surfaces the cleaner "File not found" path
    for the edit tools.

Fingerprint — why content, not mtime (MEASURED, do not "simplify" back):

  This deployment's filesystem (dolphinfs, FUSE) has **1-SECOND mtime
  granularity**: ``st_mtime_ns % 1_000_000_000 == 0`` for every file,
  ctime is equally coarse, and inode does not change on rewrite. A
  ``(mtime_ns, size)`` fingerprint is therefore BLIND to the edit class
  "same 1-second tick, same byte count, different content" — exactly what a
  fast sibling edit (or an atomic tmp+os.replace inside the same tick)
  produces. Verified live: 10 same-size writes within one second changed
  the fingerprint ZERO times.

  So for files ≤ ``_CONTENT_HASH_MAX_BYTES`` the fingerprint is
  CONTENT-addressed: ``('c', size, blake2b-128)``. Same-second same-size
  edits with different content are caught; a content-preserving touch is
  NOT stale (nothing to clobber). Files ABOVE the threshold keep the
  ``('m', mtime_ns, size)`` fast path — they are data/asset payloads, not
  the source-edit scenario, and the residual same-second blind spot there
  is documented rather than paid for with a full read on a FUSE mount.

  The threshold's job is to exclude multi-MB DATA/BINARY payloads
  (models, datasets, archives) — NOT source text. It is 4 MiB because the
  project's most sibling-contested tracked file, ``static/styles.css``,
  measured 1,026,466 bytes (owner-verified 2026-07-25) and sat ABOVE an
  earlier 256 KiB value — on the blind fast path, exactly where the CSS
  extraction/dead-style-sweep batches collide. blake2b at GB/s hashes
  4 MiB in single-digit milliseconds. Coverage is drift-guarded:
  tests/test_write_freshness_gate.py::test_tracked_text_files_stay_under_hash_threshold
  flips red the day any tracked text file outgrows it.

Restart persistence (pt_1bbd3cc82eb44ddc): the store is in-memory, so
any restart used to wipe every token and leave the gate fail-open until
each conversation's next read — the auto-restart watcher made that window
recur on every HEAD move. ``save_snapshot()`` (called before the re-exec
in ``routes/api_v1/update.py::_perform_server_reexec`` and via an atexit
hook for the signal path — atexit does NOT run on execv, hence both) and
``load_snapshot()`` (called once from server.py boot) carry the small LRU
across a restart via ``data/write_freshness_tokens.json``.

Replay semantics, deliberate: a replayed token preserves EXISTENCE ("this
conversation demonstrably read/wrote this file") — it does NOT vouch for
freshness across the downtime. The first ``is_stale`` after replay
re-fingerprints the file and compares against the replayed content hash,
so a file changed while the server was down is still judged stale
(refuse → re-read). That is the safe direction; the cost is one extra
read per file per conversation after a restart.

Env: ``TOFU_WRITE_FRESHNESS_GATE=0`` disables recording AND checks AND
persistence.
This module is a leaf: stdlib + lib.log only, so both ``lib/project_mod``
(write-side recording) and ``lib/tasks_pkg`` (the gate) can import it
without cycles (runtime_paths / json_store are imported lazily inside the
snapshot functions).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict

from lib.log import get_logger

logger = get_logger(__name__)

# Bounded LRU: (conv_key, abs_path) -> (mtime_ns, size, record_ts).
# 4096 entries ≈ plenty for concurrent conversations' working sets; eviction
# fails OPEN (an evicted token reads as "no token" → write allowed, which is
# the pre-guard behaviour).
_MAX_ENTRIES = 4096
_lock = threading.Lock()
_tokens: OrderedDict = OrderedDict()


def _gate_enabled() -> bool:
    val = os.environ.get('TOFU_WRITE_FRESHNESS_GATE', '1').strip().lower()
    return val not in ('0', 'false', 'no', 'off', '')


# Files at or below this size get a CONTENT fingerprint (see the module
# docstring for the measured 1-second-mtime-granularity reason, and for
# why 4 MiB: it must cover every tracked TEXT file — styles.css alone is
# ~1 MB — while excluding multi-MB data/binary payloads. Hashing 4 MiB
# with blake2b costs single-digit milliseconds.
# The retained Vite runtime is currently ~5.3 MiB. Hash it rather than leaving
# the generated migration owner on the same-second/same-size mtime fast path.
_CONTENT_HASH_MAX_BYTES = 8 * 1024 * 1024


def _fingerprint(abs_path: str) -> tuple | None:
    """Fingerprint of *abs_path*, or None when it can't be read.

    Two shapes (see module docstring):
      * ``('c', size, blake2b_hex)`` — content-addressed, files ≤ 4 MiB;
      * ``('m', mtime_ns, size)``  — fast path, larger files.
    """
    try:
        st = os.stat(abs_path)
    except OSError as _e:
        logger.debug('fingerprint: unreadable (%s)', _e)
        return None
    if st.st_size <= _CONTENT_HASH_MAX_BYTES:
        try:
            with open(abs_path, 'rb') as f:
                data = f.read()
        except OSError as _e:
            logger.debug('fingerprint: unreadable (%s)', _e)
            return None
        return ('c', st.st_size,
                hashlib.blake2b(data, digest_size=16).hexdigest())
    return ('m', st.st_mtime_ns, st.st_size)


def record(conv_key: str, abs_path: str) -> None:
    """Record the current fingerprint of *abs_path* for *conv_key*.

    Called after every successful read/write of the file by this
    conversation. Never raises; a stat failure simply skips recording.
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return
    fp = _fingerprint(abs_path)
    if fp is None:
        return
    key = (conv_key, abs_path)
    with _lock:
        _tokens.pop(key, None)  # refresh recency on re-record
        _tokens[key] = (fp, time.time())
        while len(_tokens) > _MAX_ENTRIES:
            _tokens.popitem(last=False)


def is_stale(conv_key: str, abs_path: str) -> bool:
    """True iff a token exists AND the file exists AND its fingerprint moved.

    False (fail-open) when: gate disabled, no token recorded, or the file
    has since vanished (creation semantics — the stale token is dropped).
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return False
    key = (conv_key, abs_path)
    with _lock:
        entry = _tokens.get(key)
    if entry is None:
        return False
    cur = _fingerprint(abs_path)
    if cur is None:
        with _lock:
            _tokens.pop(key, None)
        return False
    return cur != entry[0]


def has_token(conv_key: str, abs_path: str) -> bool:
    """True iff a (conv_key, abs_path) token exists, regardless of freshness.

    Existence alone proves this conversation successfully read/wrote the
    file at some point (tokens are only written after successful ops) —
    the read-before-edit gate uses ``has_token(...) and not is_stale(...)``
    as its compaction/restart-proof evidence path. Never raises.
    """
    if not conv_key or not abs_path:
        return False
    with _lock:
        return (conv_key, abs_path) in _tokens


def drop(conv_key: str, abs_path: str) -> None:
    """Forget one token (e.g. after an intentional delete). Never raises."""
    with _lock:
        _tokens.pop((conv_key, abs_path), None)


# ── Restart persistence ──────────────────────────────────────────────
_SNAPSHOT_VERSION = 1


def _snapshot_path() -> str:
    """Snapshot file under the resolved writable data root.

    ``data_root()`` honours ``$TOFU_DATA_DIR``, so co-booted instances with
    separate data dirs (e.g. the e2e harness's instance A/B) get isolated
    snapshots and cannot leak tokens into each other.
    """
    from lib.runtime_paths import data_root
    return os.path.join(data_root(), 'write_freshness_tokens.json')


def save_snapshot() -> bool:
    """Persist the token LRU to disk. Returns True on success.

    Called before the server re-execs (atexit does NOT run on execv) and
    from an atexit hook for the signal path. Never raises — a failed save
    must never block or break a restart.
    """
    if not _gate_enabled():
        return False
    try:
        with _lock:
            items = [
                {'conv': ck, 'path': ap, 'fp': list(fp), 'ts': ts}
                for (ck, ap), (fp, ts) in _tokens.items()
            ]
        payload = {'version': _SNAPSHOT_VERSION, 'saved_at': time.time(),
                   'tokens': items}
        from lib.json_store import write_json_atomic
        write_json_atomic(_snapshot_path(), payload)
        logger.info('[WriteFreshness] snapshot saved (%d token(s))', len(items))
        return True
    except Exception as e:
        logger.warning('[WriteFreshness] snapshot save failed (non-fatal): %s', e)
        return False


def load_snapshot() -> int:
    """Replay a saved snapshot into the store. Returns tokens loaded.

    Called once from server boot. Missing / corrupt / wrong-version file
    → 0, store untouched (fail-open). Malformed entries are skipped
    individually; an oversized snapshot keeps the NEWEST ``_MAX_ENTRIES``.
    Replayed tokens keep their ORIGINAL fingerprint — the first is_stale
    after replay re-fingerprints the file, so downtime edits are caught
    (see module docstring).
    """
    if not _gate_enabled():
        return 0
    try:
        from lib.json_store import read_json
        data = read_json(_snapshot_path(), default=None)
        if not isinstance(data, dict) or data.get('version') != _SNAPSHOT_VERSION:
            return 0
        items = data.get('tokens')
        if not isinstance(items, list):
            return 0
        loaded = 0
        with _lock:
            for it in items[-_MAX_ENTRIES:]:
                try:
                    ck = str(it['conv'])
                    ap = str(it['path'])
                    fp = tuple(it['fp'])
                    ts = float(it.get('ts') or 0)
                except (KeyError, TypeError, ValueError) as _e:
                    logger.debug('load snapshot: missing key/unexpected type/unparseable (%s)', _e)
                    continue
                if not ck or not ap:
                    continue
                _tokens[(ck, ap)] = (fp, ts)
                loaded += 1
            while len(_tokens) > _MAX_ENTRIES:
                _tokens.popitem(last=False)
        if loaded:
            logger.info('[WriteFreshness] snapshot replayed (%d token(s))', loaded)
        return loaded
    except Exception as e:
        logger.warning('[WriteFreshness] snapshot load failed (fail-open): %s', e)
        return 0


def _reset_for_tests() -> None:
    """Clear ALL tokens — test isolation helper (the store is process-global)."""
    with _lock:
        _tokens.clear()


__all__ = ['record', 'is_stale', 'has_token', 'drop', '_reset_for_tests', '_fingerprint',
           '_CONTENT_HASH_MAX_BYTES', 'save_snapshot', 'load_snapshot',
           '_snapshot_path']
