"""Delta projection + rebuild for ``messages_snapshot`` rows.

Design: ``docs/FRONTEND_ARCHITECTURE.md`` §10. The unversioned v1 format is
frozen and remains readable; new rows use a backward-compatible v2 scope.
Measured on a real task (``efb479f6``): full-payload storage
was 123.2 MB across 167 rounds; the same rounds in delta form are 1.9 MB
(65.7x). Two redundancies dominate and BOTH must be removed:

  1. ``tools`` was byte-identical on every round (201,898 bytes x 167 =
     ~33 MB). Stored once per content hash as a ``tools_dict`` row; every
     snapshot then carries only ``toolsHash``. Removing only the messages
     redundancy would cap the win at ~4x.
  2. ``messages`` grows by ~2 entries per round while the whole array
     (180-294 KB) was re-stored each time. Stored as the shared-prefix
     length + the new tail.

Wire/contract invariants
========================
* The SSE event the FRONTEND receives is NEVER touched — projection happens
  at the persistence boundary only. Live rendering is byte-identical.
* Rebuild happens SERVER-SIDE (:func:`rebuild_snapshots`); the replay API
  keeps returning the fully-reconstructed payload, so no consumer ever
  learns that storage is incremental (§10.2 item 4).
* A prefix whose hash does not match its base is reported as
  ``degraded=True`` with a reason — never silently returned as if complete
  (§10.3).

The shared-prefix semantics mirror the frontend's ``_riSharedPrefix``
(canonical-JSON positional compare) so there is exactly ONE definition of
"shared prefix" in the system.

V1 kept separate request/state baselines. In real execution those frames
interleave, and the request following a post-tool state often has the exact
same messages, so v1 stored the new tool tail twice. V2 stamps a private
version and shares one chronological baseline per ``(task, turn)``. Rebuild
selects the key from each row's version, preserving mixed v1/v2 histories.
"""

from __future__ import annotations

import hashlib
import json

from lib.log import get_logger

logger = get_logger(__name__)

SNAPSHOT = 'messages_snapshot'
TOOLS_DICT = 'tools_dict'

# Marker key that identifies an already-projected (delta) row. Legacy rows
# (full payload) never carry it, which is what makes the migration and the
# rebuild path idempotent.
DELTA_MARKER = 'prefixLen'
SNAPSHOT_DELTA_VERSION_MARKER = 'snapshotDeltaVersion'
SNAPSHOT_DELTA_VERSION = 2


def _canon(obj) -> str:
    """Canonical JSON for hashing/compare (stable key order, no ASCII escape)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def content_hash(obj) -> str:
    """Short stable content hash for a JSON-able object."""
    return hashlib.sha256(_canon(obj).encode('utf-8')).hexdigest()[:16]


def shared_prefix_len(prev: list, cur: list) -> int:
    """Longest positional shared prefix, compared by canonical JSON.

    Mirrors the frontend ``_riSharedPrefix`` exactly — round N's payload is
    round N-1's payload plus the messages that round appended, so a
    positional compare is exact in the normal case and degrades safely to 0
    on any divergence (which just means "no fold", never a wrong result).
    """
    n = min(len(prev), len(cur))
    k = 0
    while k < n and _canon(prev[k]) == _canon(cur[k]):
        k += 1
    return k


def prefix_hash(messages: list, k: int) -> str:
    """Hash of the first ``k`` messages — the rebuild-time integrity check."""
    return content_hash(messages[:k])


class SnapshotProjector:
    """Per-task projection state: message fingerprints + known tools hashes.

    One instance per task id. ``project`` turns a FULL snapshot payload into
    its delta form and (when the tool set is new) the ``tools_dict`` row that
    must be persisted alongside it.

    Memory: retains one content-free full SHA-256 digest per prior message,
    never another prompt copy. It is released by :meth:`forget` at terminal
    state. The stored row's canonical prefix hash remains the replay authority;
    a hypothetical fingerprint collision therefore degrades at rebuild rather
    than silently authorizing a different prefix.
    """

    def __init__(self):
        self._prev_message_fingerprints: dict[tuple, list[bytes]] = {}
        self._known_tools: dict[str, set] = {}

    @staticmethod
    def _scan_message_prefix(
        messages: list,
        previous_fingerprints: list[bytes] | None,
    ) -> tuple[int, str, list[bytes]]:
        """Fingerprint each current message once and hash its shared prefix.

        ``shared_prefix_len`` plus ``prefix_hash`` previously canonicalized
        the unchanged history about three times per snapshot. This fused scan
        preserves the exact canonical-list hash while retaining only digests
        for the next comparison.
        """
        current_fingerprints: list[bytes] = []
        shared_prefix = 0
        prefix_is_open = previous_fingerprints is not None
        prefix_hasher = hashlib.sha256()
        prefix_hasher.update(b'[')
        for index, message in enumerate(messages):
            canonical_message = _canon(message).encode('utf-8')
            fingerprint = hashlib.sha256(canonical_message).digest()
            current_fingerprints.append(fingerprint)
            if (prefix_is_open
                    and index < len(previous_fingerprints)
                    and fingerprint == previous_fingerprints[index]):
                if shared_prefix:
                    prefix_hasher.update(b',')
                prefix_hasher.update(canonical_message)
                shared_prefix += 1
            else:
                prefix_is_open = False
        prefix_hasher.update(b']')
        return (
            shared_prefix,
            prefix_hasher.hexdigest()[:16],
            current_fingerprints,
        )

    @staticmethod
    def _key(task_id: str, payload: dict) -> tuple:
        # Flow node turns re-number rounds from 1, so the baseline chain is
        # per (task, turn) — mixing nodes would produce a bogus prefix. Frozen
        # v1 rows additionally split request/state; v2 follows their actual
        # chronological order and avoids storing the same post-tool tail twice.
        base = (task_id, payload.get('turn') or '')
        if payload.get(SNAPSHOT_DELTA_VERSION_MARKER) == SNAPSHOT_DELTA_VERSION:
            return base
        return base + (payload.get('kind') or 'request',)

    def project(self, task_id: str, payload: dict) -> dict:
        """Return the delta-form payload. The input is NOT mutated.

        When the payload is already in delta form, or is not a snapshot, it
        is returned unchanged (idempotent — the migration can be re-run).
        """
        if not isinstance(payload, dict) or payload.get('type') != SNAPSHOT:
            return payload
        if DELTA_MARKER in payload:
            return payload  # already projected — idempotent

        messages = payload.get('messages')
        if not isinstance(messages, list):
            return payload

        out = {k: v for k, v in payload.items() if k not in ('messages', 'tools')}
        out[SNAPSHOT_DELTA_VERSION_MARKER] = SNAPSHOT_DELTA_VERSION
        key = self._key(task_id, out)

        # ── tools: content-hash dedup (§10.2 item 1) ──
        # The FIRST row carrying a given hash keeps the array inline; every
        # later row references it by hash alone. Deliberately NOT a separate
        # ``tools_dict`` event row: event ids are the SSE replay cursor and
        # the (task_id, event_id) primary key, so injecting synthetic rows
        # would perturb both. One row = one event stays true.
        tools = payload.get('tools')
        if isinstance(tools, list) and tools:
            th = content_hash(tools)
            out['toolsHash'] = th
            out['toolsCount'] = len(tools)
            seen = self._known_tools.setdefault(task_id, set())
            if th not in seen:
                seen.add(th)
                out['tools'] = tools      # first carrier keeps the payload
        else:
            out['toolsCount'] = 0

        # ── messages: shared-prefix delta (§10.2 item 2) ──
        k, canonical_prefix_hash, current_fingerprints = (
            self._scan_message_prefix(
                messages,
                self._prev_message_fingerprints.get(key),
            )
        )
        out['prefixLen'] = k
        out['prefixHash'] = canonical_prefix_hash
        out['messageCount'] = len(messages)
        new_tail = messages[k:]
        # §10.2 item 3: a repeat emission of the same round (nothing new)
        # lands as an EMPTY record — never the whole payload again.
        if new_tail:
            out['newMessages'] = new_tail

        self._prev_message_fingerprints[key] = current_fingerprints
        return out

    def forget(self, task_id: str) -> None:
        """Drop per-task projection state (call at terminal state)."""
        for key in [
            key for key in self._prev_message_fingerprints
            if key[0] == task_id
        ]:
            self._prev_message_fingerprints.pop(key, None)
        self._known_tools.pop(task_id, None)


def rebuild_snapshots(rows: list) -> list:
    """Rebuild FULL snapshot payloads from an ordered list of stored rows.

    ``rows`` is the task's event rows in ``event_id`` order, each a dict with
    at least ``type`` and ``payload`` (the shape ``event_log.read_events``
    returns). Returns the reconstructed snapshot payloads in order, each with
    its ``messages`` / ``tools`` restored.

    Legacy full rows pass through untouched, so a partially-migrated table
    rebuilds correctly. A row whose ``prefixHash`` does not match the running
    baseline is returned with ``degraded=True`` + ``degradedReason`` rather
    than a silently-wrong payload (§10.3).
    """
    tools_by_hash: dict[str, list] = {}
    baselines: dict[tuple, list] = {}
    out = []
    for row in rows or []:
        payload = row.get('payload') if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            continue
        etype = payload.get('type') or (row.get('type') if isinstance(row, dict) else '')
        if etype != SNAPSHOT:
            continue

        # A row that carries the array inline is the dictionary entry for its
        # hash; later rows reference it. Legacy full rows have no ``toolsHash``
        # field, so derive it — that lets a PARTIALLY migrated table (legacy
        # row followed by delta rows) still resolve the reference.
        if isinstance(payload.get('tools'), list):
            _th = payload.get('toolsHash') or content_hash(payload['tools'])
            tools_by_hash[_th] = payload['tools']

        if DELTA_MARKER not in payload:
            # Legacy full row — it also (re)establishes the baseline.
            full = dict(payload)
            key = SnapshotProjector._key('', payload)
            messages = list(full.get('messages') or [])
            baselines[key] = messages
            # A partially migrated history may place a v2 delta after this
            # self-contained legacy row. The exact full messages are also a
            # valid chronological v2 baseline; v1 keeps its separate key.
            baselines[('', payload.get('turn') or '')] = messages
            out.append(full)
            continue

        key = SnapshotProjector._key('', payload)
        base = baselines.get(key) or []
        k = int(payload.get('prefixLen') or 0)
        full = {kk: vv for kk, vv in payload.items()
                if kk not in ('prefixLen', 'prefixHash', 'newMessages',
                              'toolsHash', 'messageCount', 'tools',
                              SNAPSHOT_DELTA_VERSION_MARKER)}

        degraded_reason = ''
        version = payload.get(SNAPSHOT_DELTA_VERSION_MARKER)
        if version is not None and version != SNAPSHOT_DELTA_VERSION:
            degraded_reason = (
                f'unsupported snapshot delta version {version!r}')
        elif k > len(base):
            degraded_reason = (
                f'baseline has {len(base)} message(s) but this round claims a '
                f'{k}-message shared prefix (baseline row missing or pruned)')
            k = len(base)
        elif payload.get('prefixHash') and prefix_hash(base, k) != payload['prefixHash']:
            degraded_reason = (
                'prefix hash mismatch — the baseline this round was recorded '
                'against is not the one reconstructed here')

        messages = list(base[:k]) + list(payload.get('newMessages') or [])
        expected = payload.get('messageCount')
        if not degraded_reason and isinstance(expected, int) and len(messages) != expected:
            degraded_reason = (
                f'rebuilt {len(messages)} message(s) but the row recorded '
                f'{expected}')

        full['messages'] = messages
        th = payload.get('toolsHash')
        if th:
            if isinstance(payload.get('tools'), list):
                full['tools'] = payload['tools']
            elif th in tools_by_hash:
                full['tools'] = tools_by_hash[th]
            else:
                full['tools'] = []
                degraded_reason = degraded_reason or (
                    f'the row carrying tools hash {th} is missing')
        if degraded_reason:
            full['degraded'] = True
            full['degradedReason'] = degraded_reason
            logger.warning('[SnapshotDelta] round=%s degraded: %s',
                           payload.get('roundNum'), degraded_reason)
        baselines[key] = messages
        if version is None:
            # Advance the shared migration baseline only after a v1 row was
            # itself rebuilt. V2 rows never overwrite v1's kind-scoped state.
            baselines[('', payload.get('turn') or '')] = messages
        out.append(full)
    return out



# ── Process-wide projector ───────────────────────────────────────────────
# One instance backs the persistence hook in event_log.append_persistent_event.
# It holds the previous round's messages per (task, turn) — bounded by
# _MAX_TASKS so a long-lived process can never accumulate state for every task
# it has ever seen; the oldest task's state is evicted first. Losing state just
# means the next round stores a full baseline (correct, merely larger).

_MAX_TASKS = 64
_projector_lock = __import__('threading').RLock()
_projector: SnapshotProjector | None = None


class _BoundedProjector(SnapshotProjector):
    """SnapshotProjector with FIFO eviction of per-task state."""

    def __init__(self, max_tasks: int = _MAX_TASKS):
        super().__init__()
        self._max_tasks = max_tasks
        self._task_order: list[str] = []

    def project(self, task_id: str, payload: dict) -> dict:
        with _projector_lock:
            if task_id not in self._task_order:
                self._task_order.append(task_id)
                while len(self._task_order) > self._max_tasks:
                    self.forget(self._task_order.pop(0))
            return super().project(task_id, payload)

    def forget(self, task_id: str) -> None:
        with _projector_lock:
            super().forget(task_id)
            if task_id in self._task_order:
                self._task_order.remove(task_id)


def get_projector() -> SnapshotProjector:
    """Return the process-wide projector (lazily created)."""
    global _projector
    with _projector_lock:
        if _projector is None:
            _projector = _BoundedProjector()
        return _projector


def forget_projector_task(task_id: str) -> None:
    """Release one task baseline without instantiating an unused projector."""
    with _projector_lock:
        projector = _projector
        if projector is not None and task_id:
            projector.forget(str(task_id))

__all__ = [
    'SNAPSHOT', 'TOOLS_DICT', 'DELTA_MARKER',
    'SNAPSHOT_DELTA_VERSION', 'SNAPSHOT_DELTA_VERSION_MARKER',
    'SnapshotProjector', 'rebuild_snapshots', 'get_projector',
    'forget_projector_task',
    'shared_prefix_len', 'prefix_hash', 'content_hash',
]
