"""Per-conversation write-freshness tokens (shared-HEAD overwrite guard).

On a shared working tree, conversation A can read a file at T0, think for
many rounds, and at T2 blindly overwrite a change sibling B committed at T1.
The read-before-edit gate (``lib/tasks_pkg/handlers/_read_gate.py``) proves
"you read this file at SOME point in this conversation"; it cannot prove
"the file has not changed SINCE". This module closes that gap with an
optimistic-concurrency token:

  • Readers carry descriptor-pinned observations of their actual bytes under
    ``(actor_key, abs_path)``. A snapshot token is
    ``('s', size, SHA256, semantic_root, root_dev, root_ino)``. Ranges retain
    the full source digest; failed/unsupported items and legacy result text
    without an observation carrier never mint freshness evidence.
  • Successful writes renew tokens through the common writer from committed
    bytes, preserving an existing snapshot's semantic-root identity.
  • Write tools are CHECKED before execution (see
    ``lib/tasks_pkg/handlers/_write_freshness_gate.py``): a recorded
    fingerprint that no longer matches the on-disk one means someone else
    touched the file → the write is refused with a re-read instruction
    instead of silently clobbering the sibling's change.

Owned command mutations may advance an EXISTING matched content token with
``capture_owned_write`` / ``reconcile_owned_write``. The command admission
owner retains exact target exclusion through supervised process quiescence;
entry-identity CAS prevents overwriting an interleaved observation. An already
stale, missing or metadata-only preimage never gains ownership. Pure append
uses its live preimage inside the file transaction rather than stale replacement
knowledge; replacement and anchor edits retain their checks.

Deliberate failure directions:

  • A MISSING token never blocks (``is_stale`` → False). Blind full-rewrite
    flows that never read the file keep working — the read-before-edit gate
    owns the "must read first" axis for the edit tools; this module owns
    ONLY the "your knowledge went stale" axis.
  • A missing or substituted path/root cannot validate a snapshot token.
    Legacy non-snapshot tokens keep creation compatibility while the path is
    absent, without erasing evidence needed to detect a changed reborn file.

Fingerprint — why content, not mtime (MEASURED, do not "simplify" back):

  This deployment's filesystem (network FUSE) has **1-SECOND mtime
  granularity**: ``st_mtime_ns % 1_000_000_000 == 0`` for every file,
  ctime is equally coarse, and inode does not change on rewrite. A
  ``(mtime_ns, size)`` fingerprint is therefore BLIND to the edit class
  "same 1-second tick, same byte count, different content" — exactly what a
  fast sibling edit (or an atomic tmp+os.replace inside the same tick)
  produces. Verified live: 10 same-size writes within one second changed
  the fingerprint ZERO times.

  Actual read observations always hash their full source with SHA-256, with
  no size cutoff and no later disk read during token creation. Only legacy
  explicit ``record()`` calls use the following weaker compatibility format;
  they are never used to certify already-rendered bytes.

  For files ≤ ``_CONTENT_HASH_MAX_BYTES`` the legacy fingerprint is
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
  tests/tasks_pkg/test_write_freshness_gate.py::test_tracked_text_files_stay_under_hash_threshold
  flips red the day any tracked text file outgrows it.

Restart persistence (): the store is in-memory, so
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


# One process-local fence, not a token/cache owner registry. Owned formatter
# completion conservatively invalidates project-read receipts across tasks.
# Old objects survive only while bounded receipts/in-flight reads reference them.
_project_read_cache_generation = object()


def project_read_cache_generation() -> object:
    """Snapshot BEFORE a project read starts; never bind at late insertion."""
    return _project_read_cache_generation


def advance_project_read_cache_generation() -> None:
    """Fence receipts at proven owned formatter supervised completion."""
    global _project_read_cache_generation
    _project_read_cache_generation = object()

from collections import OrderedDict

from dataclasses import dataclass
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
# Tracked evidence JSON now reaches 13.1 MB. Keep all editable text covered;
# streaming the fingerprint bounds temporary memory to 256 KiB per caller.
_CONTENT_HASH_MAX_BYTES = 16 * 1024 * 1024


def _fingerprint(abs_path: str, *, force_content=False) -> tuple | None:
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
    if force_content or st.st_size <= _CONTENT_HASH_MAX_BYTES:
        try:
            digest = hashlib.blake2b(digest_size=16)
            with open(abs_path, 'rb') as f:
                for chunk in iter(lambda: f.read(256 * 1024), b''):
                    digest.update(chunk)
        except OSError as _e:
            logger.debug('fingerprint: unreadable (%s)', _e)
            return None
        return ('c', st.st_size, digest.hexdigest())
    return ('m', st.st_mtime_ns, st.st_size)


def _fingerprint_bytes(abs_path: str, data) -> tuple | None:
    """Fingerprint of *abs_path* given the bytes just written there.

    Hash the actual committed bytes at every size without re-reading the path.
    Non-bytes input retains the legacy explicit ``_fingerprint`` fallback.
    """
    if not isinstance(data, (bytes, bytearray)):
        return _fingerprint(abs_path)
    data = bytes(data)
    return ('c', len(data), hashlib.blake2b(data, digest_size=16).hexdigest())


@dataclass(frozen=True)
class ReadObservation:
    """Host-owned evidence from the bytes actually rendered, never a later read.

    Digest/size cover the entire source even for ranged reads. The physical
    semantic root is part of the token; an equal-byte replacement root is not
    the project the caller observed. This carrier retains no file bodies.
    """

    abs_path: str
    fingerprint: tuple


def observation_from_snapshot(snapshot) -> ReadObservation:
    return ReadObservation(snapshot.path, (
        's', snapshot.size, snapshot.sha256, snapshot.root, *snapshot.root_identity))


def record_observation(conv_key: str, observation: ReadObservation) -> bool:
    """Install only an issued snapshot digest, with NO filesystem re-read."""
    if not conv_key or not _gate_enabled() or not isinstance(observation, ReadObservation):
        return False
    _store_fingerprint(conv_key, observation.abs_path, observation.fingerprint)
    return True


def _current_fingerprint(abs_path: str, previous: tuple) -> tuple | None:
    if previous[0] != 's':
        return (_fingerprint(abs_path, force_content=True) if previous[0] == 'c'
                else _fingerprint(abs_path))
    from lib.artifacts.file_snapshot import read_project_snapshot
    try:
        snapshot = read_project_snapshot(abs_path, [previous[3]],
            expected_root_identity=previous[4:6], allow_empty=True)
        return observation_from_snapshot(snapshot).fingerprint
    except (OSError, ValueError):
        # A missing/substituted root, unsafe path or failed read cannot certify
        # freshness. Retain the observation so a later retry stays fenced.
        return None


def fingerprint_for_written_bytes(abs_path: str, data: bytes, previous=None) -> tuple:
    """Keep an existing snapshot's root binding through the common writer."""
    if previous and previous[0] == 's':
        return ('s', len(data), hashlib.sha256(data).hexdigest(), *previous[3:6])
    return _fingerprint_bytes(abs_path, data)


def _store_fingerprint(conv_key: str, abs_path: str, fp: tuple | None) -> None:
    """Shared tail of record/record_written (never raises)."""
    if fp is None:
        return
    key = (conv_key, abs_path)
    with _lock:
        _tokens.pop(key, None)  # refresh recency on re-record
        _tokens[key] = (fp, time.time())
        while len(_tokens) > _MAX_ENTRIES:
            _tokens.popitem(last=False)


def record(conv_key: str, abs_path: str) -> None:
    """Record the current fingerprint of *abs_path* for *conv_key*.

    Legacy explicit observation API. Not a post-read adapter: callers that
    already rendered bytes must use record_observation, or a race would bless
    content the model never saw. Never raises; a stat failure simply skips.
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return
    _store_fingerprint(conv_key, abs_path, _fingerprint(abs_path))


def record_written(conv_key: str, abs_path: str, data: bytes) -> None:
    """Record the post-write fingerprint of *abs_path* WITHOUT re-reading.

    The bytes just written are in hand, so all sizes are hashed directly from
    *data*. Existing snapshot observations retain their semantic-root binding.
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return
    with _lock:
        entry = _tokens.get((conv_key, abs_path))
    fp = fingerprint_for_written_bytes(abs_path, data, entry[0] if entry else None)
    _store_fingerprint(conv_key, abs_path, fp)


@dataclass(frozen=True)
class OwnedWriteEvidence:
    """An existing observation matched under the caller's writer exclusion.

    This is not authorization or a lease. Only the command admission owner may
    capture it for a proven literal output, and must retain exclusion through
    supervised process quiescence and reconciliation. No file bytes are retained.
    """

    conv_key: str
    abs_path: str
    token_entry: tuple


def capture_owned_write(conv_key: str, abs_path: str) -> OwnedWriteEvidence | None:
    """Pin a content-proven existing observation before an owned mutation.

    Never bless an already-stale preimage, create a missing token, or rely on
    the large-file metadata fallback as proof of byte ownership. Call after
    writer admission, before execution. Interleaved observations invalidate CAS.
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return None
    key = (conv_key, abs_path)
    with _lock:
        entry = _tokens.get(key)
    if entry is None or entry[0][0] not in ('c', 's'):
        return None
    if _current_fingerprint(abs_path, entry[0]) != entry[0]:
        return None
    with _lock:
        if _tokens.get(key) is not entry:
            return None
    return OwnedWriteEvidence(conv_key, abs_path, entry)


def reconcile_owned_write(evidence: OwnedWriteEvidence | None) -> bool:
    """Advance only the captured observation to the owned, settled postimage.

    Call while the same writer exclusion remains held. Failure/partial writes
    can settle here too, but launch/admission failure and unretired descendants
    provide no authority to reconcile. A newer read/write, eviction or reset
    wins: compare entry identity, not just its fingerprint (ABA-safe). Never
    create a token for an unobserved file or advance another conversation.
    """
    if evidence is None or not _gate_enabled():
        return False
    current = _current_fingerprint(evidence.abs_path, evidence.token_entry[0])
    if current is None or current[0] not in ('c', 's') or current == evidence.token_entry[0]:
        return False
    key = (evidence.conv_key, evidence.abs_path)
    with _lock:
        if _tokens.get(key) is not evidence.token_entry:
            return False
        _tokens.pop(key)
        _tokens[key] = (current, time.time())
    return True


def is_stale(conv_key: str, abs_path: str) -> bool:
    """True iff a token exists AND the file exists AND its fingerprint moved.

    False when disabled or no token exists. Missing/substituted snapshot
    paths/roots stay stale; legacy tokens retain creation compatibility while
    absent, without forgetting the observation needed to detect a reborn file.

    Pre-write cost note: a content token (``'c'``) can NOT be verified with
    a stat-only ``(mtime_ns, size)`` short-circuit — the very reason content
    hashing exists is the measured 1-second-mtime / same-size blind spot on
    this FUSE mount, where ``(mtime_ns, size)`` is provably unchanged by a
    same-tick same-size external edit. So the full re-read stays here for
    content tokens; ``('m', …)`` tokens are already stat-only.
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return False
    key = (conv_key, abs_path)
    with _lock:
        entry = _tokens.get(key)
    if entry is None:
        return False
    cur = _current_fingerprint(abs_path, entry[0])
    if cur is None:
        if entry[0][0] == 's':
            return True
        # Legacy creation semantics remain fail-open while the file is gone,
        # but do not erase history: a changed reborn file must still be stale.
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
    from lib.runtime.runtime_paths import data_root
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


__all__ = ['record', 'record_written', 'is_stale', 'has_token', 'drop',
           'ReadObservation', 'observation_from_snapshot', 'record_observation',
           'fingerprint_for_written_bytes',
           'OwnedWriteEvidence', 'capture_owned_write', 'reconcile_owned_write',
           '_reset_for_tests', '_fingerprint', '_fingerprint_bytes',
           '_CONTENT_HASH_MAX_BYTES', 'save_snapshot', 'load_snapshot',
           '_snapshot_path']
