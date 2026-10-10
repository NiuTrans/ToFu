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


class _MessagePrefix:
    """Shared immutable prefix + original tail; no expanded historical arrays."""
    __slots__ = ('parent', 'prefix_length', 'tail', 'length', 'hasher')

    def __init__(self, parent, prefix_length, tail, *, legacy=False):
        if not prefix_length:
            parent = None
        while parent is not None and parent.parent is not None and prefix_length <= parent.prefix_length:
            parent = parent.parent
        if parent is not None and prefix_length < parent.length:
            clipped = object.__new__(_MessagePrefix)
            clipped.parent = parent
            clipped.prefix_length = prefix_length
            clipped.tail = []
            clipped.length = prefix_length
            try:
                clipped.hasher = parent.prefix_hasher(prefix_length)
            except (TypeError, ValueError, UnicodeError, OverflowError):
                clipped.hasher = None
            parent = clipped
        self.parent = parent
        self.prefix_length = prefix_length
        self.tail = tail
        self.length = prefix_length + len(tail)
        if legacy:
            self.hasher = None
            return
        try:
            self.hasher = (parent.prefix_hasher(prefix_length) if parent
                           else hashlib.sha256(b'['))
            count = prefix_length
            for message in tail:
                if count:
                    self.hasher.update(b',')
                self.hasher.update(_canon(message).encode('utf-8'))
                count += 1
        except (TypeError, ValueError, UnicodeError, OverflowError):
            # Frozen rows without prefixHash never required canonicalization.
            # Keep them readable; a later verified prefix still raises honestly.
            self.hasher = None

    def messages(self, length=None):
        """Iterative traversal also handles very deep append-only histories."""
        count = self.length if length is None else length
        stack = [(self, count, False)]
        while stack:
            node, count, tail_only = stack.pop()
            if tail_only:
                for position in range(count):
                    yield node.tail[position]
                continue
            inherited = min(count, node.prefix_length)
            appended = max(0, count - node.prefix_length)
            if appended:
                stack.append((node, appended, True))
            if inherited:
                stack.append((node.parent, inherited, False))

    def prefix_hasher(self, length):
        node = self
        remaining = length
        chunks = []
        while True:
            if remaining == node.length and node.hasher is not None:
                hasher = node.hasher.copy()
                count = remaining
                break
            inherited = min(remaining, node.prefix_length)
            appended = max(0, remaining - node.prefix_length)
            if appended:
                chunks.append((node.tail, appended))
            if not inherited:
                hasher = hashlib.sha256(b'[')
                count = 0
                break
            node = node.parent
            remaining = inherited
        for tail, count_in_tail in reversed(chunks):
            for position in range(count_in_tail):
                if count:
                    hasher.update(b',')
                hasher.update(_canon(tail[position]).encode('utf-8'))
                count += 1
        return hasher

    def cache_components(self):
        return (self.parent, self.tail, self.hasher)


class SnapshotReplayState:
    """One verified chronological replay with explicitly releasable results.

    Baselines and tool definitions survive page boundaries; callers may discard
    unselected result records while retaining their dependency state. This is
    the same decoder used by the complete SnapshotReplayIndex.
    """
    def __init__(self):
        self.records = {}
        self.legacy_positions = set()
        self.tools_by_hash = {}
        self.baselines = {}

    def append(self, source_index, row):
        payload = row.get('payload') if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            return
        if (payload.get('type') or row.get('type') or '') != SNAPSHOT:
            return
        if isinstance(payload.get('tools'), list):
            self.tools_by_hash[payload.get('toolsHash') or content_hash(payload['tools'])] = payload['tools']
        key = SnapshotProjector._key('', payload)
        if DELTA_MARKER not in payload:
            full = dict(payload)
            messages = list(full.get('messages') or [])
            history = _MessagePrefix(None, 0, messages, legacy=True)
            self.baselines[key] = history
            self.baselines[('', payload.get('turn') or '')] = history
            self.records[source_index] = (full, history)
            self.legacy_positions.add(source_index)
            return
        base = self.baselines.get(key)
        base_count = base.length if base else 0
        k = int(payload.get('prefixLen') or 0)
        full = {name: value for name, value in payload.items()
                if name not in ('prefixLen', 'prefixHash', 'newMessages',
                                'toolsHash', 'messageCount', 'tools',
                                SNAPSHOT_DELTA_VERSION_MARKER)}
        reason = ''
        version = payload.get(SNAPSHOT_DELTA_VERSION_MARKER)
        if version is not None and version != SNAPSHOT_DELTA_VERSION:
            reason = f'unsupported snapshot delta version {version!r}'
        elif k > base_count:
            reason = (f'baseline has {base_count} message(s) but this round claims a '
                      f'{k}-message shared prefix (baseline row missing or pruned)')
            k = base_count
        # Preserve Python-slice semantics of frozen legacy records.
        prefix_length = max(0, base_count + k) if k < 0 else min(k, base_count)
        if not reason and payload.get('prefixHash'):
            prefix = (base.prefix_hasher(prefix_length) if base
                      else hashlib.sha256(b'['))
            prefix.update(b']')
            if prefix.hexdigest()[:16] != payload['prefixHash']:
                reason = ('prefix hash mismatch — the baseline this round was recorded '
                          'against is not the one reconstructed here')
        history = _MessagePrefix(base, prefix_length, list(payload.get('newMessages') or []))
        expected = payload.get('messageCount')
        if not reason and isinstance(expected, int) and history.length != expected:
            reason = f'rebuilt {history.length} message(s) but the row recorded {expected}'
        th = payload.get('toolsHash')
        if th:
            if isinstance(payload.get('tools'), list):
                full['tools'] = payload['tools']
            elif th in self.tools_by_hash:
                full['tools'] = self.tools_by_hash[th]
            else:
                full['tools'] = []
                reason = reason or f'the row carrying tools hash {th} is missing'
        if reason:
            full.update(degraded=True, degradedReason=reason)
            logger.warning('[SnapshotDelta] round=%s degraded: %s', payload.get('roundNum'), reason)
        self.baselines[key] = history
        if version is None:
            self.baselines[('', payload.get('turn') or '')] = history
        self.records[source_index] = (full, history)

    def discard(self, source_index):
        self.records.pop(source_index, None)
        self.legacy_positions.discard(source_index)

    def finish(self):
        """Release continuation-only dependencies after the final source fence."""
        self.baselines.clear()
        self.tools_by_hash.clear()

    def payload(self, source_index):
        metadata, history = self.records[source_index]
        if source_index in self.legacy_positions:
            # Legacy payloads pass through exactly, including absent/non-list fields.
            return dict(metadata)
        return {**metadata, 'messages': list(history.messages())}

    def cache_components(self):
        return (self.records, self.legacy_positions, self.baselines, self.tools_by_hash)


class SnapshotReplayIndex(SnapshotReplayState):
    """Verified compact snapshot index; materialize only requested payloads.

    V1/V2, legacy migration, tools dictionaries, prefix hashes and degradation
    have one implementation shared with ``rebuild_snapshots``. Original JSON
    remains in the input rows; this index has no write or model authority.
    """
    def __init__(self, rows):
        super().__init__()
        for source_index, row in enumerate(rows or []):
            self.append(source_index, row)


def rebuild_snapshots(rows: list) -> list:
    """Rebuild all payloads through the same verified compact replay index."""
    index = SnapshotReplayIndex(rows)
    return [index.payload(position) for position in index.records]


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
    'SnapshotProjector', 'SnapshotReplayIndex', 'rebuild_snapshots', 'get_projector',
    'forget_projector_task',
    'shared_prefix_len', 'prefix_hash', 'content_hash',
]
