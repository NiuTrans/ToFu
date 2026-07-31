"""Delta projection + rebuild for ``messages_snapshot`` rows.

Design: ``docs/DEBUG_PANEL_REDESIGN.md`` §10 (format FROZEN by the owner
2026-07-25). Measured on a real task (``efb479f6``): full-payload storage
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
    """Per-task projection state: previous messages + last tools hash.

    One instance per task id. ``project`` turns a FULL snapshot payload into
    its delta form and (when the tool set is new) the ``tools_dict`` row that
    must be persisted alongside it.

    Memory: holds the previous round's message list for the task, which is
    the same order of magnitude as one snapshot and is released by
    :meth:`forget` at terminal state.
    """

    def __init__(self):
        self._prev_messages: dict[tuple, list] = {}
        self._known_tools: dict[str, set] = {}

    @staticmethod
    def _key(task_id: str, payload: dict) -> tuple:
        # Endpoint phases re-number rounds from 1, so the baseline chain is
        # per (task, turn) — mixing phases would produce a bogus prefix.
        return (task_id, payload.get('turn') or '', payload.get('kind') or 'request')

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
        key = self._key(task_id, payload)

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
        prev = self._prev_messages.get(key)
        k = shared_prefix_len(prev, messages) if prev is not None else 0
        out['prefixLen'] = k
        out['prefixHash'] = prefix_hash(messages, k)
        out['messageCount'] = len(messages)
        new_tail = messages[k:]
        # §10.2 item 3: a repeat emission of the same round (nothing new)
        # lands as an EMPTY record — never the whole payload again.
        if new_tail:
            out['newMessages'] = new_tail

        self._prev_messages[key] = list(messages)
        return out

    def forget(self, task_id: str) -> None:
        """Drop per-task projection state (call at terminal state)."""
        for key in [k for k in self._prev_messages if k[0] == task_id]:
            self._prev_messages.pop(key, None)
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
            baselines[key] = list(full.get('messages') or [])
            out.append(full)
            continue

        key = SnapshotProjector._key('', payload)
        base = baselines.get(key) or []
        k = int(payload.get('prefixLen') or 0)
        full = {kk: vv for kk, vv in payload.items()
                if kk not in ('prefixLen', 'prefixHash', 'newMessages',
                              'toolsHash', 'messageCount', 'tools')}

        degraded_reason = ''
        if k > len(base):
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
        out.append(full)
    return out



# ── Process-wide projector ───────────────────────────────────────────────
# One instance backs the persistence hook in event_log.append_persistent_event.
# It holds the previous round's messages per (task, turn) — bounded by
# _MAX_TASKS so a long-lived process can never accumulate state for every task
# it has ever seen; the oldest task's state is evicted first. Losing state just
# means the next round stores a full baseline (correct, merely larger).

_MAX_TASKS = 64
_projector_lock = __import__('threading').Lock()
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
        super().forget(task_id)
        if task_id in self._task_order:
            self._task_order.remove(task_id)


def get_projector() -> SnapshotProjector:
    """Return the process-wide projector (lazily created)."""
    global _projector
    if _projector is None:
        _projector = _BoundedProjector()
    return _projector

__all__ = [
    'SNAPSHOT', 'TOOLS_DICT', 'DELTA_MARKER',
    'SnapshotProjector', 'rebuild_snapshots', 'get_projector',
    'shared_prefix_len', 'prefix_hash', 'content_hash',
]
