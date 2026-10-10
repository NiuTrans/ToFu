"""Request Inspector — server-authoritative per-task request fold (P2).

Design: ``docs/FRONTEND_ARCHITECTURE.md`` (row schemas FROZEN in §3.3 — do not
rename keys). The frontend drawer renders ONLY what this module folds from
the persisted ``task_events`` log (durable-before-visible, 6h TTL). The
in-browser ``_debugRequests`` log is a live accelerator with gaps. A reconnect
window can drop rounds client-side; the server log never does. The server fold
is therefore the authority for both live and finished tasks.

Row shapes
==========
``fold_request_log(task_id)`` →
    ``{taskId, requests, coverage, eventsAvailable, requestCount}``
    Request row (metadata ONLY — never the payload):
    ``{roundNum, ts, model, params, messageCount, toolsCount, toolNames,
       label, legacy, attempts[, sourceTaskId]}``
    ``sourceTaskId`` is present ONLY on rows merged from a swarm child's
    event log (``parent#agent:<id>``) — it is the id the per-round
    payload/state endpoints must be addressed with.
    ``toolNames`` = the tools that round's response INVOKED, read from the
    new-message tail of the next snapshot (the post-tool mirror of loop
    round N carries roundNum=N+1, §3.1) — this is what lets the drawer's
    round list read like the chat timeline's turn blocks. Display hint
    only; never a count.
    Attempt row (joined from ``round_usage`` by roundNum):
    ``{tag, model, tokensIn, tokensOut, traceId, streamElapsedMs,
       cacheRead, cacheWrite, ts}``
``get_request_payload(task_id, round_num)`` → full payload for ONE round
    (messages + tools + params + model) — the on-demand detail fetch.
    State mirrors (post-tool / final / fallback) are served ONLY through
    this per-round endpoint (``kind='state'``); the fold's round list is
    requests only. With ``fold=True`` the payload also carries
    ``foldPrefix``/``diffBase`` — the shared leading-message count vs the
    previous round (same turn/kind), computed from the same cached event
    rows, so the drawer diffs without downloading two full payloads.
``list_conv_tasks(conv_id, user_id=…, limit=…, before=…)`` →
    ``{convId, tasks, hasMore[, readError]}`` — Task rows for the drawer
    (live registry + indexed durable attempts + legacy task_results, exact
    kind-counted tallies via one storage summary — no payload bulk). ``before``
    (exclusive createdAt-ms cursor) pages older persisted rows; live rows only
    appear on the first page. ``readError:true`` marks a storage read FAILURE —
    the UI must render load-failed+retry, never the "records cleaned up"
    empty state, which is reserved for a successful-but-empty read.
``get_conv_inspector_wire(conv_id, user_id=…)`` →
    ``{convId, messages, sections, turns[, readError, coverage,
      coverageReason, olderTurnsDropped]}`` — the FLAT WIRE FEED (the
    2026-09-22 drawer): every message exactly once, ``turn``/``agent``/
    ``rebuild`` section markers the browser renders as dividers and
    scroll targets, and the ``turns`` nav model. Retries collapse to the
    newest run with snapshots; consecutive turns emit only the tail they
    appended (the increment chain).

kind classification
===================
``kind=`` (the P1 emission contract) wins. Pre-contract persisted rows
carry no kind; ONLY for those legacy rows do we fall back to the
roundNum/label markers (migration shim — the contract itself never parses
labels; see design §3.1).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.orchestration.message_compat import is_flow_event_type
from lib.tasks_pkg.snapshot_delta import DELTA_MARKER, shared_prefix_len

logger = get_logger(__name__)

_SNAPSHOT = 'messages_snapshot'
_ROUND_USAGE = 'round_usage'
_WIRE_PROJECTION = 'tool_wire_projection'
_STATE_ROUND_LABELS = ('final', 'fallback')
# Legacy-only state markers (pre-contract snapshots carried no kind=).
_LEGACY_STATE_LABEL_MARKERS = ('工具结果后', '最终回复后', 'Fallback')


class RequestInspectorReadError(RuntimeError):
    """The event authority failed; absence of a round was not established."""


class _InspectorEventRows(list):
    """Event prefix and its read-bound witness share one cache identity."""
    def __init__(self, rows=(), *, read_bound=None):
        super().__init__(rows)
        self.read_bound = read_bound


def _event_read_bound(events, task_id):
    bound = getattr(events, 'read_bound', None)
    return dict(bound, taskId=task_id) if bound else None


def _read_events(task_id: str, rebuild: bool = True) -> tuple[list, bool]:
    """Return ([{event_id, type, payload, ts_ms}] ordered by event_id, ok).

    ``ok`` is False when the storage read itself FAILED — callers must not
    present that as "task expired / no records": an empty list with
    ``ok=True`` is the only honest no-data signal.

    Separate from ``event_log.read_events`` (which omits ``ts_ms`` — the
    request-row schema carries ``ts``). Read-only; never throws.

    ``rebuild`` selects the view: the FOLD only needs per-row metadata
    (counts ride the delta markers) plus the new-message tails, so it reads
    ``rebuild=False`` and never pays the full-payload reconstruction; the
    per-round endpoint asks the verified index for only its selected snapshots.
    shares original message tails and prefix digests; only selected payloads
    materialize. It derives from the same fresh raw read, with no second RPC.

    CACHED (short TTL): the drawer's natural usage is "fold the task, then
    open round after round", and every ``get_request_payload`` call used to
    re-read AND re-rebuild the task's whole event log — O(rounds^2) work for
    a linear UI action. Measured on a real 126-round task: 206s to walk every
    round, ~1.6s per click. The compact index preserves one storage read for the same walk.

    Two TTLs, chosen per entry at write time by :func:`_task_may_append`:

    * LIVE (may still append): deliberately short — a LIVE task appends
      rounds while the user watches, and a stale list would hide the newest
      request. 3s is long enough to collapse a burst of per-round fetches,
      short enough that the next poll sees new rounds.
    * SETTLED (registry-terminal or unknown task): much longer. The log of a
      finished task is immutable for this process (same-process writes
      invalidate the entry outright), so the drawer's real browsing pace —
      click a round, read for a minute, click the next — must not re-read
      and re-rebuild the whole log on EVERY click past the live TTL. That
      re-read was the "every click takes seconds" complaint.

    The settled TTL still bounds staleness for rows appended by a DIFFERENT
    process (the only writes invalidate_task_cache cannot reach).
    """
    if not task_id:
        return [], True
    if rebuild:
        replay, ok = _read_snapshot_replay(task_id)
        return (replay.event_rows(), True) if ok else ([], False)
    import time
    now = time.time()
    with _EVENTS_CACHE_LOCK:
        hit = _EVENTS_CACHE.get((task_id, False))
        if hit is not None and now - hit[0] < hit[3]:
            return hit[1], hit[2]
        derived = _EVENTS_CACHE.get((task_id, True))
        if (derived and now - derived[0] < derived[3]
                and isinstance(derived[1], _SnapshotEventReplay)):
            return derived[1].events, True
    ttl = (_EVENTS_CACHE_TTL_S if _task_may_append(task_id)
           else _EVENTS_CACHE_TTL_SETTLED_S)
    reading = (task_id, 'reading')
    witness = _cache_store(reading, object(), now, ttl)
    try:
        rows, ok = _read_events_uncached(task_id, rebuild=False)
        if not ok:
            return [], False
        if witness is not None:
            _cache_store((task_id, False), rows, now, ttl, source=(reading, witness))
        return rows, True
    finally:
        with _EVENTS_CACHE_LOCK:
            if _EVENTS_CACHE.get(reading) is witness:
                _EVENTS_CACHE.pop(reading, None)


# (task_id, rebuild) → (epoch, compact rows/replay index, read_ok, ttl, estimated bytes)
_EVENTS_CACHE: dict[tuple, tuple] = {}
_EVENTS_CACHE_LOCK = __import__('threading').RLock()
_EVENTS_CACHE_TTL_S = 3.0
_EVENTS_CACHE_TTL_SETTLED_S = 300.0
_EVENTS_CACHE_MAX = 16

class _SnapshotEventReplay:
    """Compact verified snapshots bound to one immutable event read."""
    def __init__(self, events):
        from lib.tasks_pkg.snapshot_delta import SnapshotReplayIndex
        self.events = events
        self.index = SnapshotReplayIndex(events)

    def event_rows(self, positions=None):
        return _InspectorEventRows((dict(row, payload=self.index.payload(position))
                if position in self.index.records else row
                for position, row in enumerate(self.events)
                if row.get('type') != _SNAPSHOT or positions is None or position in positions),
                read_bound=getattr(self.events, 'read_bound', None))

    def cache_components(self):
        return (self.events, self.index)


class _HistoryReadPending:
    """Only a weak busy witness stays cached while a worker grows its replay."""
    def __init__(self, scan):
        import weakref
        self.reference = weakref.ref(scan)

    def busy(self):
        scan = self.reference()
        return scan is not None and scan.lock.locked()

    def cache_components(self):
        return (self.reference,)


def _cache_budget_bytes():
    from runtime_guards import resolve_resource_budget
    return resolve_resource_budget('TOFU_TASK_INSPECTOR_CACHE_MAX_MIB', minimum=4,
                             maximum=65536) * 1024 * 1024


def _retained_bytes(value, maximum):
    """Conservative unique-object estimate, stopping at cache admission limit."""
    import sys
    from lib.tasks_pkg.snapshot_delta import SnapshotReplayState, _MessagePrefix
    from lib.tasks_pkg.request_inspector_history import SnapshotHistoryRead
    pending = [value]
    seen = set()
    total = 0
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(current)
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (tuple, list, set, frozenset)):
            pending.extend(current)
            if isinstance(current, _InspectorEventRows):
                pending.append(current.read_bound)
        elif isinstance(current, (_SnapshotEventReplay, SnapshotReplayState, SnapshotHistoryRead, _HistoryReadPending, _MessagePrefix)):
            pending.extend(current.cache_components())
            if isinstance(current, _MessagePrefix) and current.hasher is not None:
                total += 256  # conservative native digest carrier allowance
        if total > maximum:
            return maximum + 1
    return total


def _cache_store(key, value, now, ttl, *, source=None):
    budget = _cache_budget_bytes()
    size = _retained_bytes(value, budget)
    with _EVENTS_CACHE_LOCK:
        for old_key, entry in list(_EVENTS_CACHE.items()):
            if (now - entry[0] >= entry[3]
                    and not (isinstance(entry[1], _HistoryReadPending) and entry[1].busy())):
                _EVENTS_CACHE.pop(old_key, None)
        if size > budget:
            return  # original remains readable; reconstructible cache is optional
        if source is not None and _EVENTS_CACHE.get(source[0]) is not source[1]:
            return  # invalidation/another read won while the index was compiling
        _EVENTS_CACHE.pop(key, None)
        while _EVENTS_CACHE and (len(_EVENTS_CACHE) >= _EVENTS_CACHE_MAX
                or sum(entry[4] for entry in _EVENTS_CACHE.values()) + size > budget):
            eligible = [item for item, retained in _EVENTS_CACHE.items()
                        if not (isinstance(retained[1], _HistoryReadPending)
                                and retained[1].busy())]
            if not eligible:
                return
            oldest = min(eligible, key=lambda item: _EVENTS_CACHE[item][0])
            _EVENTS_CACHE.pop(oldest)
        entry = (now, value, True, ttl, size)
        _EVENTS_CACHE[key] = entry
        return entry


def _read_snapshot_replay(task_id, *, source_events=None):
    events, ok = ((source_events, True) if source_events is not None
                  else _read_events(task_id, rebuild=False))
    if not ok:
        return None, False
    import time
    with _EVENTS_CACHE_LOCK:
        hit = _EVENTS_CACHE.get((task_id, True))
        if (hit and time.time() - hit[0] < hit[3]
                and isinstance(hit[1], _SnapshotEventReplay) and hit[1].events is events):
            return hit[1], True
        base = _EVENTS_CACHE.get((task_id, False))
    try:
        replay = _SnapshotEventReplay(events)
    except Exception as exc:
        logger.warning('[RequestInspector] snapshot index failed task=%s: %s', task_id[:8], exc)
        return None, False
    if base is not None and base[1] is events:
        _cache_store((task_id, True), replay, base[0], base[3],
                     source=((task_id, False), base))
    return replay, True


def _read_snapshot_events(task_id, *, rounds=None, turn='', kind=None, last=False):
    events, ok = _read_events(task_id, rebuild=False)
    if not ok:
        return [], False
    if getattr(events, 'read_bound', None):
        # The prefix cannot establish latestness. Replay the full durable
        # source in bounded, source-fenced slices without indexing every row.
        selected, complete = _read_long_snapshot_events(
            task_id, rounds=rounds, turn=turn, kind=kind, last=last)
        return (selected, True) if complete else (events, False)
    replay, ok = _read_snapshot_replay(task_id, source_events=events)
    if not ok:
        return [], False
    positions = set()
    found = set()
    for position in range(len(replay.events) - 1, -1, -1):
        row = replay.events[position]
        if row.get('type') != _SNAPSHOT:
            continue
        payload = row['payload']
        if kind is not None and _snapshot_kind(payload) != kind:
            continue
        if turn and (payload.get('turn') or '') != turn:
            continue
        address = str(payload.get('roundNum'))
        if rounds is not None and (address not in rounds or address in found):
            continue
        positions.add(position)
        found.add(address)
        if last or (rounds is not None and found == rounds):
            break
    return replay.event_rows(positions), True


# Per HTTP read: one page can cross the decoded-byte boundary; native pages
# are independently capped at 8 MiB. State uses the existing probed byte budget.
_HISTORY_SLICE_EVENTS = 20_000
_HISTORY_SLICE_BYTES = 16 * 1024 * 1024
_HISTORY_SLICE_SECONDS = 2.0


def _read_long_snapshot_events(task_id, *, rounds, turn, kind, last):
    """Continue one optional cached scan; only fenced EOF proves a result."""
    import time
    from lib.storage import get_storage_client
    from lib.tasks_pkg.request_inspector_history import SnapshotHistoryRead, read_source_guard

    key = (task_id, 'history', tuple(sorted(rounds)) if rounds is not None else None,
           turn, kind, last)
    scan = None
    entry = None
    acquired = False
    try:
        client = get_storage_client()
        guard = read_source_guard(client, task_id)
        now = time.time()
        ttl = (_EVENTS_CACHE_TTL_S if _task_may_append(task_id)
               else _EVENTS_CACHE_TTL_SETTLED_S)
        with _EVENTS_CACHE_LOCK:
            entry = _EVENTS_CACHE.get(key)
            if entry is not None and isinstance(entry[1], _HistoryReadPending):
                current = entry[1].reference()
                if current is not None and current.guard == guard and entry[1].busy():
                    return [], False
            if (entry is not None and now - entry[0] < entry[3]
                    and isinstance(entry[1], SnapshotHistoryRead)
                    and entry[1].guard == guard):
                scan = entry[1]
            if scan is None:
                scan = SnapshotHistoryRead(task_id, guard, rounds=rounds,
                                           turn=turn, kind=kind, last=last)
            acquired = scan.lock.acquire(blocking=False)
            if not acquired:
                return [], False
            # The worker owns mutable replay memory; the cache keeps only a
            # weak busy witness until an immutable between-request checkpoint
            # can be measured and admitted under the aggregate byte budget.
            entry = _cache_store(key, _HistoryReadPending(scan), now, ttl)
            if entry is None:
                return [], False
        complete = scan.advance(
            client, kind_of=_snapshot_kind, measure=_retained_bytes,
            work_budget=_cache_budget_bytes(), event_budget=_HISTORY_SLICE_EVENTS,
            byte_budget=_HISTORY_SLICE_BYTES,
            deadline=time.monotonic() + _HISTORY_SLICE_SECONDS)
        if read_source_guard(client, task_id) != guard:
            raise ValueError('Event source changed during snapshot replay')
        if complete:
            scan.index.finish()
        # Refresh TTL and measured bytes only if no invalidation/other reader
        # replaced the admitted source while its pages were being read.
        if entry is not None:
            _cache_store(key, scan, time.time(), ttl, source=(key, entry))
        return (scan.event_rows(), True) if complete else ([], False)
    except Exception as exc:
        with _EVENTS_CACHE_LOCK:
            current = _EVENTS_CACHE.get(key)
            if (current is not None and isinstance(current[1], _HistoryReadPending)
                    and current[1].reference() is scan):
                _EVENTS_CACHE.pop(key, None)
        logger.warning('[RequestInspector] long snapshot read failed task=%s: %s',
                       task_id[:8], exc)
        return [], False
    finally:
        if acquired:
            scan.lock.release()
        with _EVENTS_CACHE_LOCK:
            current = _EVENTS_CACHE.get(key)
            if current is not None and current is entry and isinstance(current[1], _HistoryReadPending):
                _EVENTS_CACHE.pop(key, None)


# Statuses at which a registry record can no longer append event rows.
_TERMINAL_TASK_STATUSES = frozenset({
    'done', 'completed', 'complete', 'success', 'error', 'failed', 'failure',
    'aborted', 'interrupted', 'cancelled', 'canceled', 'stopped',
})


def _task_may_append(task_id: str) -> bool:
    """In-process liveness probe choosing the events-cache TTL.

    Only a task whose chat-runtime record is still non-terminal can append
    event rows in THIS process (and a same-process append invalidates the
    entry anyway — the TTL guards cross-process writers). A terminal or
    absent record ⇒ the settled TTL; an unknown/empty status ⇒ the safe
    live TTL. Probe failure answers True (short TTL), never a stale pin.
    """
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        rec = chat_task_runtime.get(task_id)
    except Exception as exc:
        logger.debug('[RequestInspector] task liveness probe failed task=%s '
                     '(choosing live TTL): %s', task_id, exc)
        return True
    if rec is None:
        return False
    status = str(rec.get('status') or '').lower()
    if not status:
        return True
    return status not in _TERMINAL_TASK_STATUSES


def invalidate_task_cache(task_id: str) -> None:
    """Drop the cached event rows for ONE task.

    Called from ``event_log.append_persistent_event`` right after a row is
    written. The TTL alone is not enough: it bounds staleness in wall-clock
    time, but a writer that appends a round and immediately reads it back
    (the live-task path, and every test that seeds rows under a fixed task
    id) must see its own write. Write-side invalidation makes the cache
    read-your-writes correct; the TTL then only covers writes made by a
    DIFFERENT process.
    """
    if task_id:
        with _EVENTS_CACHE_LOCK:
            for key in list(_EVENTS_CACHE):
                if key[0] == task_id:
                    _EVENTS_CACHE.pop(key, None)


def _read_events_uncached(task_id: str, *, rebuild: bool) -> tuple[list, bool]:
    """Uncached read (+ optional rebuild — see :func:`_read_events`).

    Reads ONLY the structural slice the inspector renders (snapshots, round
    usage, Flow markers) — NEVER the streaming noise (delta / phase /
    tool_progress / …). Every SSE delta is persisted as its own row, so an
    unfiltered read is dominated by noise: measured on a real 51,754-row
    task, the FIRST-10000-rows cap below cut every snapshot past round 6,
    and rounds 7+ all rendered "mirror expired". Structural rows span thousands
    of rounds. A capped prefix carries its read boundary through cache/replay;
    consumers cannot infer latest snapshots or absence from it.

    The filter is pushed into the storage query (``types`` /
    ``type_prefixes``) so noise rows are never decoded or transferred; the
    Python re-check below stays as the semantic guarantee.
    """
    try:
        from lib.orchestration.message_compat import FLOW_EVENT_PREFIXES
        from lib.storage import get_storage_client
        from lib.task_event_contract import STRUCTURAL_EVENT_TYPES

        from lib.tasks_pkg.request_inspector_history import is_event_page_size_error
        structural = _InspectorEventRows()
        after = -1
        scanned = 0
        page_limit = 1000
        client = get_storage_client()
        type_filter = {
            'types': sorted(STRUCTURAL_EVENT_TYPES),
            'type_prefixes': list(FLOW_EVENT_PREFIXES),
        }
        while True:
            try:
                rows = client.query(
                    'event.list', {'task_id': task_id,
                                   'after_sequence': after,
                                   'limit': page_limit, **type_filter}) or []
            except Exception as exc:
                # Native storage bounds the MATERIALIZED event page at 8 MiB.
                # Retry only this deterministic page-size rejection at the
                # SAME cursor; other availability/integrity errors are not
                # evidence that smaller pages would be safe or useful.
                if (is_event_page_size_error(exc) and page_limit > 1):
                    page_limit = max(1, page_limit // 2)
                    continue
                raise
            if not rows:
                break
            scanned += len(rows)
            for row in rows:
                sequence = int(row.get('sequence', 0))
                if sequence <= after:
                    raise ValueError('event cursor did not advance')
                after = sequence
                payload = row.get('event') or {}
                event_type = payload.get('type') or ''
                if (event_type not in STRUCTURAL_EVENT_TYPES
                        and not is_flow_event_type(event_type)):
                    continue
                structural.append({
                    'event_id': int(row.get('sequence', 0)),
                    'type': event_type,
                    'payload': payload,
                    'ts_ms': int(row.get('created_at_ms') or 0),
                })
            if len(rows) < page_limit:
                break
            if len(structural) >= 10_000 or scanned >= 200_000:
                # One bounded lookahead distinguishes an exact complete
                # boundary from a prefix. Its body is never retained/rebuilt.
                more = client.query('event.list', {'task_id': task_id,
                    'after_sequence': after, 'limit': 1, **type_filter}) or []
                if more:
                    if int(more[0].get('sequence', 0)) <= after:
                        raise ValueError('event lookahead cursor did not advance')
                    structural.read_bound = {
                        'reason': 'event-limit' if len(structural) >= 10_000 else 'scan-limit',
                        'afterSequence': after, 'structuralCount': len(structural),
                        'scannedCount': scanned,
                    }
                break
        if rebuild:
            structural = _rebuild_snapshot_rows(structural)
        return structural, True
    except Exception as e:
        logger.warning('[RequestInspector] Sidecar event read failed task=%s: %s',
                       task_id[:8], e)
        return [], False


def _rebuild_snapshot_rows(rows: list) -> list:
    """Restore full ``messages``/``tools`` on delta-stored snapshot rows.

    Storage is incremental (docs/FRONTEND_ARCHITECTURE.md §10) but every
    consumer of this module — the fold, the payload endpoint, the frontend —
    sees the FULL payload, exactly as before. Rebuild is server-side and
    total: a row that cannot be reconstructed is marked ``degraded`` by
    ``rebuild_snapshots`` rather than silently truncated.
    """
    snap_idx = [i for i, r in enumerate(rows)
                if r.get('type') == _SNAPSHOT]
    if not snap_idx:
        return rows
    try:
        from lib.tasks_pkg.snapshot_delta import rebuild_snapshots
        rebuilt = rebuild_snapshots([rows[i] for i in snap_idx])
    except Exception as e:
        logger.warning('[RequestInspector] snapshot rebuild failed (serving '
                       'rows as stored): %s', e)
        return rows
    for i, payload in zip(snap_idx, rebuilt):
        rows[i] = dict(rows[i], payload=payload)
    return rows


def _snapshot_kind(payload: dict) -> str:
    """'request' | 'state'. The P1 ``kind=`` contract wins; pre-contract
    rows (no kind) fall back to roundNum/label markers — the ONLY place
    label parsing is allowed (migration shim, NOT the contract)."""
    kind = payload.get('kind')
    if kind in ('request', 'state'):
        return kind
    rn = payload.get('roundNum')
    if isinstance(rn, str) and rn in _STATE_ROUND_LABELS:
        return 'state'
    label = payload.get('label') or ''
    if any(m in label for m in _LEGACY_STATE_LABEL_MARKERS):
        return 'state'
    return 'request'


def _snapshot_message_count(payload: dict) -> int:
    """Total message count WITHOUT rebuilding: delta rows record it as
    ``messageCount`` (§10); legacy full rows carry the array inline."""
    count = payload.get('messageCount')
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return len(payload.get('messages') or [])


def _snapshot_tools_count(payload: dict) -> int:
    """Tool-schema count WITHOUT rebuilding (same marker logic)."""
    count = payload.get('toolsCount')
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return len(payload.get('tools') or [])


def _snapshot_tail(payload: dict, full_prev: dict) -> list:
    """Messages this snapshot ADDED — the tool-name extraction window.

    Delta rows carry the tail explicitly as ``newMessages`` (the §10
    projector computes it against the per-(task, turn) chronological
    baseline). Full rows (legacy, and every pre-delta test fixture) are
    diffed positionally against the previous full snapshot of the same
    turn, which is the same chronological semantics. A mixed
    migrated history can over-report one row's tail (the full-row
    baseline cannot see delta-only predecessors) — the names are a
    display hint, never a count, so that is cosmetic.
    """
    if DELTA_MARKER in payload:
        return [m for m in (payload.get('newMessages') or [])
                if isinstance(m, dict)]
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return []
    turn = payload.get('turn') or ''
    prev = full_prev.get(turn)
    k = shared_prefix_len(prev, messages) if prev is not None else 0
    full_prev[turn] = messages
    return [m for m in messages[k:] if isinstance(m, dict)]


def _tool_call_names(messages: list) -> list:
    """Ordered unique tool names invoked by the given messages.

    Both wire shapes are read: OpenAI ``assistant.tool_calls`` and
    Anthropic ``tool_use`` content blocks. Names, not counts — the
    drawer's round row answers "which tools ran in this round" the way
    the chat timeline's turn blocks do.
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(name) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(str(name))

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for call in msg.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function')
            add((fn or {}).get('name') if isinstance(fn, dict)
                else call.get('name'))
        content = msg.get('content')
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict)
                        and block.get('type') == 'tool_use'):
                    add(block.get('name'))
    return names


def _called_in_round(payload: dict, kind: str) -> int | None:
    """Which REQUEST round invoked the tools in this snapshot's tail.

    The post-tool mirror of loop round N carries roundNum=N+1 under the
    current contract (§3.1), and a v1-delta request row repeats that same
    growth, so new-contract rows attribute to roundNum-1. Pre-contract
    legacy rows numbered their post-tool state with the round that just
    ran ('Round N 工具结果后'), so a legacy STATE tail attributes to
    roundNum itself. Non-integer round labels ('final' / 'fallback') never
    attribute anywhere.
    """
    rn = payload.get('roundNum')
    if isinstance(rn, bool):
        return None
    if isinstance(rn, str) and rn.isdigit():
        rn = int(rn)
    if not isinstance(rn, int):
        return None
    if kind == 'state' and 'kind' not in payload:
        return rn
    return rn - 1


# Bound on merged swarm children: a delegating (Goal-mode / Flow) parent
# folds each child's request rows into its own round list — swarm fan-out is
# small, but the fold must never scale with an unbounded agent count.
_FOLD_CHILD_MERGE_MAX = 32


def _child_snapshot_sources(task_id: str) -> tuple[list, bool, bool]:
    """Swarm-child task ids (``parent#agent:<id>``) holding snapshots.

    A delegating parent (Goal mode runs its worker as a swarm agent)
    persists NO request snapshots of its own — every ``messages_snapshot``
    lands under the child's event id. Folding the parent alone therefore
    renders "0 rounds" for the exact run the user watched do dozens of
    model calls. Discovery rides the indexed inspector summary (counts
    only, never payload bulk). ``ok=False`` marks a storage read FAILURE —
    callers surface readError rather than silently folding parent-only.
    """
    if (not task_id) or ('#agent:' in task_id):
        return ([], True, False)
    try:
        from lib.storage import get_storage_client

        summary = get_storage_client().query(
            'event.inspector_summary', {'task_ids': [task_id]},
            deadline=30) or {}
    except Exception as e:
        logger.warning('[RequestInspector] child discovery failed task=%s: %s',
                       task_id[:8], e)
        return [], False, False
    prefix = f'{task_id}#agent:'
    children = []
    for record in summary.get('records') or []:
        child_id = str(record.get('task_id') or '')
        if not child_id.startswith(prefix):
            continue
        snapshots = (int(record.get('request_count') or 0)
                     + int(record.get('state_count') or 0)
                     + int(record.get('legacy_count') or 0))
        if snapshots > 0:
            children.append((int(record.get('first_event_at_ms') or 0), child_id))
    # Earliest observed execution first, with task id as deterministic tie
    # breaker. The merge remains bounded, but callers are told when it omits
    # valid child evidence instead of claiming complete coverage.
    children.sort(key=lambda item: (item[0], item[1]))
    truncated = len(children) > _FOLD_CHILD_MERGE_MAX
    return ([child_id for _ts, child_id in
             children[:_FOLD_CHILD_MERGE_MAX]], True, truncated)



def inspector_child_belongs_to_parent(
    child_task_id: str, parent_task_id: str,
) -> bool:
    """Verify a synthetic child through persisted inspector authority.

    String shape alone is never authorization evidence.  The indexed summary
    must contain a structural snapshot record whose exact task id is the child
    and whose exact persisted parent prefix is ``parent_task_id``.  Storage
    failure, malformed ids, empty agent ids, and missing records all deny.
    Parent ownership is intentionally checked by the caller's normal task
    ownership boundary before this helper is consulted.
    """
    if not isinstance(child_task_id, str) or not child_task_id:
        return False
    if not isinstance(parent_task_id, str) or not parent_task_id:
        return False
    parent, marker, agent_id = child_task_id.partition('#agent:')
    if marker != '#agent:' or parent != parent_task_id or not agent_id:
        return False
    try:
        from lib.storage import get_storage_client

        summary = get_storage_client().query(
            'event.inspector_summary', {'task_ids': [parent_task_id]},
            deadline=30) or {}
    except Exception as e:
        logger.warning('[RequestInspector] child authorization failed task=%s: %s',
                       child_task_id[:8], e)
        return False
    for record in summary.get('records') or []:
        if str(record.get('task_id') or '') != child_task_id:
            continue
        snapshots = (int(record.get('request_count') or 0)
                     + int(record.get('state_count') or 0)
                     + int(record.get('legacy_count') or 0))
        return snapshots > 0
    return False

def _wire_projection_payload(payload: dict) -> dict:
    """Bounded diagnostic only; never contains schemas or execution authority."""
    out = {
        'backend': payload.get('backend') or '',
        'toolNames': list(payload.get('toolNames') or []),
        'toolCount': int(payload.get('toolCount') or 0),
        'schemaTokens': int(payload.get('schemaTokens') or 0),
        'schemaFingerprint': str(payload.get('schemaFingerprint') or ''),
        'schemaBudgetTokens': int(payload.get('schemaBudgetTokens') or 0),
        'budgetDroppedNames': list(payload.get('budgetDroppedNames') or []),
        'compactedNames': list(payload.get('compactedNames') or []),
        'executableToolCount': int(payload.get('executableToolCount') or 0),
    }
    if payload.get('parentSpanId'):
        out['requestSpanId'] = str(payload['parentSpanId'])[:160]
    if isinstance(payload.get('dispatchSeq'), int):
        out['dispatchSeq'] = payload['dispatchSeq']
    return out


def _join_attempt_wire(attempts: list, projections: list) -> None:
    """Never attribute the final retry's tools to an earlier provider usage."""
    for attempt in attempts:
        span = attempt.get('requestSpanId')
        dispatch = attempt.get('dispatchSeq')
        if span:
            candidates = [p for p in projections
                          if p.get('requestSpanId') == span
                          and (dispatch is None or p.get('dispatchSeq') == dispatch)]
        else:
            candidates = projections if len(attempts) == 1 else []
        if len(candidates) == 1:
            attempt['wireProjection'] = candidates[0]
            attempt['wireProjectionStatus'] = (
                'request_identity' if span else 'unique_round')
        else:
            attempt['wireProjectionStatus'] = (
                'ambiguous' if len(candidates) > 1 or (not span and projections)
                else 'missing')


def _fold_event_rows(events: list) -> tuple[list, bool]:
    """Fold ONE task's event log into request rows (+ the flow-seen bit).

    Kept per-log so every join axis (attempts, wire projections, tool
    names, tail diffs) stays scoped to its own (turn, roundNum) space —
    two swarm agents share the 'swarm-agent' turn tag, and merging at the
    row level (with ``sourceTaskId``) is what keeps their same-numbered
    rounds distinct.
    """
    requests = []
    attempts: dict[tuple, list] = {}
    wire_projections: dict[tuple[str, str], list] = {}
    tool_names: dict[tuple[str, int], list] = {}
    full_prev: dict[str, list] = {}
    flow_seen = False
    for e in events:
        p = e['payload']
        et = e['type']
        if et == _SNAPSHOT:
            kind = _snapshot_kind(p)
            tail_names = _tool_call_names(_snapshot_tail(p, full_prev))
            called = _called_in_round(p, kind)
            if tail_names and called is not None and called >= 1:
                key = (p.get('turn') or '', called)
                bucket = tool_names.setdefault(key, [])
                seen = set(bucket)
                for name in tail_names:
                    if name not in seen:
                        seen.add(name)
                        bucket.append(name)
            if kind == 'state':
                # State mirrors stay available per round via
                # get_request_payload(kind='state'); the round LIST is
                # requests only — mirroring every round twice was the
                # drawer's worst source of noise.
                continue
            row = {
                'roundNum': p.get('roundNum'),
                'ts': e['ts_ms'],
                'model': p.get('model') or '',
                # Flow node turns tag their phase (P4) so same-numbered
                # planner/worker/critic rounds stay distinct rows.
                'turn': p.get('turn') or '',
                'params': p.get('params') or {},
                'messageCount': _snapshot_message_count(p),
                'toolsCount': _snapshot_tools_count(p),
                'label': p.get('label') or '',
                'legacy': 'kind' not in p,
            }
            if p.get('agentId'):
                row['agentId'] = p['agentId']
                row['agentRole'] = p.get('agentRole') or ''
            if p.get('degraded'):
                row['degraded'] = True
                row['degradedReason'] = p.get('degradedReason') or ''
            requests.append(row)
        elif et == _ROUND_USAGE:
            u = p.get('usage') or {}
            try:
                from lib.cost import normalize_usage
                nu = normalize_usage(u)
            except Exception as _e:
                logger.debug('[RequestInspector] normalize_usage failed: %s', _e)
                nu = {}
            attempts.setdefault(
                (p.get('turn') or '', str(p.get('roundNum'))), []).append({
                'tag': p.get('tag') or '',
                'model': p.get('model') or '',
                'tokensIn': int(p.get('tokensIn') or 0),
                'tokensOut': int(p.get('tokensOut') or 0),
                'traceId': u.get('trace_id') or '',
                'requestSpanId': str(u.get('request_span_id') or '')[:160],
                'dispatchSeq': u.get('request_dispatch_seq'),
                'streamElapsedMs': int(u.get('stream_elapsed_ms') or 0),
                'cacheRead': int(nu.get('cache_read') or 0),
                'cacheWrite': int(nu.get('cache_write') or 0),
                'ts': e['ts_ms'],
            })
        elif et == _WIRE_PROJECTION:
            wire_projections.setdefault((
                p.get('turn') or '', str(p.get('roundNum')),
            ), []).append(_wire_projection_payload(p))
        elif is_flow_event_type(et):
            # Flow-driven task. Planner/Worker/Critic turns all run
            # run_task (snapshots fire) — but each re-numbers rounds from
            # 1, so a task whose snapshots carry NO turn tag (pre-P4 log)
            # is genuinely ambiguous, not uncovered.
            flow_seen = True
    for row in requests:
        row['attempts'] = attempts.get(
            (row['turn'], str(row['roundNum'])), [])
        rn = row['roundNum']
        if isinstance(rn, str) and rn.isdigit():
            rn = int(rn)
        row['toolNames'] = (
            tool_names.get((row['turn'], rn)) or []
            if isinstance(rn, int) and not isinstance(rn, bool) else [])
        projections = wire_projections.get(
            (row['turn'], str(row['roundNum'])), [])
        _join_attempt_wire(row['attempts'], projections)
        row['wireProjectionStatus'] = (
            'unique_round' if len(projections) == 1
            else 'ambiguous' if projections else 'missing')
        wire = projections[0] if len(projections) == 1 else None
        if wire:
            row['wireToolsCount'] = int(wire.get('toolCount') or 0)
            row['wireSchemaTokens'] = int(wire.get('schemaTokens') or 0)
            row['wireSchemaFingerprint'] = str(
                wire.get('schemaFingerprint') or '')
            row['wireBackend'] = wire.get('backend') or ''
            row['schemaBudgetTokens'] = int(
                wire.get('schemaBudgetTokens') or 0)
            row['budgetDroppedCount'] = len(
                wire.get('budgetDroppedNames') or [])
    return requests, flow_seen


def fold_request_log(task_id: str) -> dict:
    """Fold a task's persisted events into the Request Inspector rows.

    Request rows are METADATA-ONLY (no ``messages``/``tools`` bulk) —
    payloads are served on demand via :func:`get_request_payload`.

    The fold runs on the UNREBUILT read: counts ride the §10 delta
    markers and the tool names come from each snapshot's stored
    new-message tail, so listing the rounds of a 100+ round task never
    pays the full-payload reconstruction (that stays on the per-round
    payload endpoint, where the user actually asked for one round).

    Delegation merge: swarm-child (``#agent:``) request rows fold into
    the PARENT list carrying ``sourceTaskId`` — the round list of a
    delegating run then answers "what did this run actually do" in one
    place, and the per-round payload fetch addresses the child's own
    event log. Child rows keep their own ``turn``/``agentId`` badges.
    """
    events, read_ok = _read_events(task_id, rebuild=False)
    read_bounds = [_event_read_bound(events, task_id)]
    requests, flow_seen = _fold_event_rows(events)
    events_available = bool(events)
    child_ids, discovery_ok, child_limit_reached = _child_snapshot_sources(task_id)
    read_ok = read_ok and discovery_ok
    for child_id in child_ids:
        child_events, child_ok = _read_events(child_id, rebuild=False)
        read_bounds.append(_event_read_bound(child_events, child_id))
        read_ok = read_ok and child_ok
        if child_events:
            events_available = True
        child_requests, child_flow = _fold_event_rows(child_events)
        flow_seen = flow_seen or child_flow
        for row in child_requests:
            row['sourceTaskId'] = child_id
        requests.extend(child_requests)
    if child_ids:
        # Per-log folds are chronological; the merged list re-interleaves
        # parent and agents by emission time (same total order the chat
        # timeline showed).
        requests.sort(key=lambda row: (row.get('ts') or 0,
                                       row.get('sourceTaskId') or ''))
    has_turn_tags = any(r['turn'] for r in requests)
    out = {
        'taskId': task_id,
        'requests': requests,
        'eventsAvailable': events_available,
        'requestCount': len(requests),
    }
    if not read_ok:
        # Read FAILURE, not an expired/empty log — the UI must offer retry
        # instead of the "records cleaned up" empty state.
        out['readError'] = True
    read_bounds = [bound for bound in read_bounds if bound]
    if read_bounds:
        out['coverage'] = 'partial'
        out['coverageReason'] = read_bounds[0]['reason']
        out['eventReadBounds'] = read_bounds
    elif child_limit_reached:
        out['coverage'] = 'partial'
        out['coverageReason'] = 'child-limit'
    elif flow_seen and requests and not has_turn_tags:
        # Untagged Flow log: planner/worker/critic rounds share numbers
        # with no phase tag — rows exist but cannot be told apart. An
        # EMPTY round list has nothing ambiguous to warn about (a pure
        # delegator's flow marker is not uncovered evidence).
        out['coverage'] = 'partial'
        out['coverageReason'] = 'flow-untagged'
    else:
        out['coverage'] = 'full'
    return out


def _latest_snapshot(events: list, round_num, turn: str, kind: str):
    """Newest (re-emitted supersedes) ``(event_row, payload)`` snapshot match
    for one (roundNum, turn, kind) address, or None."""
    best = None
    for e in events:
        p = e['payload']
        if e['type'] != _SNAPSHOT or _snapshot_kind(p) != kind:
            continue
        if str(p.get('roundNum')) != str(round_num):
            continue
        if turn and (p.get('turn') or '') != turn:
            continue
        best = (e, p)  # last wins (a re-emitted round supersedes)
    return best


def get_request_payload(task_id: str, round_num, turn: str = '',
                        kind: str = 'request',
                        user_id: int | None = None,
                        fold: bool = False) -> dict | None:
    """Full payload for ONE snapshot round (the on-demand detail fetch).

    ``turn`` (optional): Flow node phase tag ('planning'|'working'|
    'reviewing') or 'swarm-agent' — disambiguates same-numbered rounds.
    When given, only snapshots with a matching turn qualify; when empty,
    the last matching snapshot wins (legacy / untagged behavior).

    ``kind``: 'request' (default) reads pre-request snapshots; 'state'
    reads the post-tool / final / fallback mirrors. Both share the SAME
    roundNum axis (docs/FRONTEND_ARCHITECTURE.md §3.1: the post-tool mirror
    of loop round N carries roundNum=N+1, exactly the request that produced
    those tool calls), so ONE addressing scheme serves both — this is what
    the in-chat state inspector fetches.

    ``fold``: when True, also diff this round's messages against the
    PREVIOUS round's (same turn/kind, roundNum-1) and set ``foldPrefix`` /
    ``diffBase`` on the result. The drawer used to download BOTH full
    payloads per click just to compute that shared prefix client-side —
    two full-context transfers for a number the server derives from the
    SAME cached event rows for free.

    Returns None when no matching snapshot exists for that round (expired
    log, wrong kind, or unknown task).
    """
    if kind not in ('request', 'state'):
        return None
    best = None
    wire_projections = []
    rounds = {str(round_num)}
    if fold:
        try:
            previous = int(round_num) - 1
            if previous >= 1:
                rounds.add(str(previous))
        except (TypeError, ValueError):
            pass
    events, read_ok = _read_snapshot_events(task_id, rounds=rounds, turn=turn, kind=kind)
    if not read_ok:
        raise RequestInspectorReadError('round event read failed')
    for e in events:
        p = e['payload']
        if e['type'] != _WIRE_PROJECTION:
            continue
        if str(p.get('roundNum')) != str(round_num):
            continue
        if turn and (p.get('turn') or '') != turn:
            continue
        wire_projections.append(_wire_projection_payload(p))
    best = _latest_snapshot(events, round_num, turn, kind)
    if best is None:
        return None
    e, p = best
    out = {
        'taskId': task_id,
        'roundNum': p.get('roundNum'),
        'kind': kind,
        'ts': e['ts_ms'],
        'model': p.get('model') or '',
        'turn': p.get('turn') or '',
        'params': p.get('params') or {},
        'label': p.get('label') or '',
        'messages': p.get('messages') or [],
        'tools': p.get('tools') or [],
        'contextManifest': p.get('contextManifest') or [],
    }
    if fold:
        try:
            prev_num = int(round_num) - 1
        except (TypeError, ValueError):
            prev_num = 0
        if prev_num >= 1 and out['messages']:
            prev = _latest_snapshot(events, prev_num, turn, kind)
            prev_msgs = prev[1].get('messages') if prev else None
            if isinstance(prev_msgs, list) and prev_msgs:
                k = shared_prefix_len(prev_msgs, out['messages'])
                if k > 0:
                    out['foldPrefix'] = k
                    out['diffBase'] = f'R{prev_num}'
    if len(wire_projections) == 1:
        out['wireProjection'] = wire_projections[0]
    elif wire_projections:
        out['wireProjections'] = wire_projections
    out['wireProjectionStatus'] = (
        'unique_round' if len(wire_projections) == 1
        else 'ambiguous' if wire_projections else 'missing')
    # §10.3: a round that could not be exactly reconstructed says so — the
    # UI must never present a partial rebuild as the real request.
    if p.get('degraded'):
        out['degraded'] = True
        out['degradedReason'] = p.get('degradedReason') or ''
    if user_id is not None and kind == 'request':
        try:
            from lib.storage import get_storage_client

            archives = get_storage_client().query(
                'raw_archive.list', {
                    'user_id': int(user_id),
                    'task_id': task_id,
                    'round_num': int(round_num),
                    'limit': 32,
                }, deadline=30) or {}
            out['rawArchives'] = list(archives.get('archives') or [])
        except (TypeError, ValueError, OverflowError):
            out['rawArchives'] = []
        except Exception as exc:
            logger.warning(
                '[RequestInspector] raw archive list failed task=%s round=%s: %s',
                task_id[:8], round_num, exc)
            out['rawArchives'] = []
    return out


def get_raw_archive_chunk(task_id: str, archive_id: str, part: str, *,
                          user_id: int, offset: int = 0,
                          limit: int = 256 * 1024) -> dict | None:
    """Read one bounded owner/task-scoped request or response archive chunk."""
    from lib.storage import get_storage_client

    result = get_storage_client().query(
        'raw_archive.read', {
            'user_id': int(user_id),
            'task_id': task_id,
            'archive_id': archive_id,
            'part': part,
            'offset': max(0, int(offset)),
            'limit': max(1, min(1024 * 1024, int(limit))),
        }, deadline=30)
    return dict(result) if isinstance(result, dict) else None


# ── Flat conversation wire feed (2026-09-22 owner directive) ─────────
# The drawer's drill-down (task list → round list → single-round detail)
# was retired: "直接平铺展示所有 messages，turn 之间画个分界线，跳转就
# 直接跳转". get_conv_inspector_wire emits ONE flat message list for the
# whole conversation — every message exactly once — plus section markers
# the browser renders as dividers and scroll-targets.

_WIRE_FEED_TASK_LIMIT = 100
_WIRE_FEED_MESSAGE_CAP = 2000


def _wire_message_text(msg: dict) -> str:
    content = msg.get('content')
    if isinstance(content, list):
        content = ' '.join(str(b.get('text') or '') for b in content
                           if isinstance(b, dict) and b.get('type') == 'text')
    return str(content or '').strip()


def _wire_turn_preview(messages: list) -> str:
    """The turn's question: first user-message text snippet."""
    for m in messages:
        if isinstance(m, dict) and m.get('role') == 'user':
            text = _wire_message_text(m)
            if text:
                return text[:80]
    return ''


def _last_snapshot_payload(events: list) -> dict | None:
    """The most complete wire state of ONE run: the LAST snapshot across
    kinds (a finished run's final mirror; a live run's latest request)."""
    best = None
    for e in events:
        if e['type'] == _SNAPSHOT:
            best = e['payload']
    if best is None or not isinstance(best.get('messages'), list):
        return None
    return best


def get_conv_inspector_wire(conv_id: str, *, user_id: int) -> dict:
    """Flat wire feed for a whole conversation.

    Per reply turn, the authoritative run (the latest one with snapshots —
    retries collapse to their newest attempt, the owner removed the task
    concept from the panel) contributes its FINAL wire. Consecutive turns
    share a long prefix (turn N+1's context opens with turn N's history),
    so each turn emits only the tail its run appended — the increment
    chain. Divergence (compaction / regenerate / delete between turns)
    breaks the chain with an honest ``rebuild`` marker instead of faking
    continuity. Swarm children of the authoritative run render as ``agent``
    sub-sections chained against the parent's final wire (their context is
    the parent's at spawn plus their own additions) and never advance the
    main chain.

    Returns ``{convId, messages, sections, turns[, readError, coverage,
    coverageReason, olderTurnsDropped]}``. ``sections`` are ordered
    ``{type, msgIndex, ...}`` markers (turn | agent | rebuild); ``turns``
    is the nav model the drawer's stepper/jump scrolls to.
    """
    out: dict = {'convId': conv_id, 'messages': [], 'sections': [],
                 'turns': [], 'coverage': 'full'}
    page = list_conv_tasks(conv_id, user_id=user_id,
                           limit=_WIRE_FEED_TASK_LIMIT)
    if page.get('readError'):
        out['readError'] = True
    if page.get('hasMore'):
        out['coverage'] = 'partial'
        out['coverageReason'] = 'task-limit'
    rows = [r for r in (page.get('tasks') or []) if not r.get('isSwarmAgent')]
    by_turn: dict[str, list] = {}
    for row in rows:
        # Pre-TurnStore rows carry no turnId: each is its OWN exchange,
        # not one merged bucket sharing a single wire.
        key = (str(row.get('turnId') or '')
               or f"__task__{row.get('taskId') or ''}")
        by_turn.setdefault(key, []).append(row)
    ordered = sorted(
        by_turn.items(),
        key=lambda kv: min((r.get('createdAt') or 0) for r in kv[1]))

    feed: list = []
    sections: list = []
    nav_turns: list = []
    prev: list = []
    for turn_id, runs in ordered:
        # The synthetic per-task bucket key is not a turn identity.
        turn_id = '' if turn_id.startswith('__task__') else turn_id
        runs = sorted(runs, key=lambda r: (r.get('createdAt') or 0,
                                           r.get('taskId') or ''))
        auth = None
        for r in reversed(runs):
            if r.get('hasEvents') or r.get('live'):
                auth = r
                break
        if auth is None:
            auth = runs[-1]
        divider = {'type': 'turn', 'msgIndex': len(feed), 'turnId': turn_id,
                   'taskId': auth['taskId'],
                   'createdAt': auth.get('createdAt') or 0,
                   'live': bool(auth.get('live')),
                   'status': str(auth.get('status') or '')}
        nav = {'turnId': turn_id, 'taskId': auth['taskId'],
               'msgIndex': divider['msgIndex'],
               'createdAt': divider['createdAt'], 'live': divider['live'],
               'status': divider['status'], 'preview': ''}
        msgs = None
        read_ok = not auth.get('readError', False)
        if not read_ok:
            out['readError'] = True
            divider['readError'] = True
            nav['readError'] = True
        if auth.get('hasEvents') or auth.get('live'):
            events, read_ok = _read_snapshot_events(auth['taskId'], last=True)
            bound = _event_read_bound(events, auth['taskId'])
            if bound:
                out['coverage'] = 'partial'
                out['coverageReason'] = bound['reason']
                out.setdefault('eventReadBounds', []).append(bound)
            if not read_ok:
                out['readError'] = True
                divider['readError'] = True
                nav['readError'] = True
            snap = _last_snapshot_payload(events) if read_ok else None
            if snap is not None:
                msgs = snap['messages']
        if not msgs:
            # Honest gap: no retained wire — the chain RESTARTS after it.
            # A failed read is retryable, not proof of expired evidence.
            if read_ok:
                divider['expired'] = True
                nav['expired'] = True
            sections.append(divider)
            nav_turns.append(nav)
            prev = []
            continue
        k = shared_prefix_len(prev, msgs) if prev else 0
        if prev and k < len(prev):
            sections.append({'type': 'rebuild', 'msgIndex': len(feed),
                             'kept': k, 'dropped': len(prev) - k})
        nav['preview'] = _wire_turn_preview(msgs[k:]) or _wire_turn_preview(msgs)
        divider['preview'] = nav['preview']
        sections.append(divider)
        feed.extend(msgs[k:])
        prev = msgs
        child_ids, discovery_ok, child_limit = _child_snapshot_sources(
            auth['taskId'])
        if not discovery_ok:
            out['readError'] = True
        if child_limit:
            out['coverage'] = 'partial'
            out['coverageReason'] = 'child-limit'
        for child_id in child_ids:
            child_events, child_ok = _read_snapshot_events(child_id, last=True)
            bound = _event_read_bound(child_events, child_id)
            if bound:
                out['coverage'] = 'partial'
                out['coverageReason'] = bound['reason']
                out.setdefault('eventReadBounds', []).append(bound)
            if not child_ok:
                out['readError'] = True
                continue
            child_snap = _last_snapshot_payload(child_events)
            if child_snap is None:
                continue
            cmsgs = child_snap['messages']
            ck = shared_prefix_len(msgs, cmsgs)
            sections.append({'type': 'agent', 'msgIndex': len(feed),
                             'agentId': child_id.split('#agent:')[-1],
                             'taskId': child_id, 'keptPrefix': ck})
            feed.extend(cmsgs[ck:])
        nav_turns.append(nav)

    # Bound the feed: drop OLDEST turns over the cap. Increments are
    # already computed, so trimming the front never rewrites later turns.
    if len(feed) > _WIRE_FEED_MESSAGE_CAP:
        keep_from = len(feed) - _WIRE_FEED_MESSAGE_CAP
        cut = 0
        dropped = 0
        turn_dividers = [s for s in sections if s['type'] == 'turn']
        for i, s in enumerate(turn_dividers):
            if s['msgIndex'] <= keep_from and i < len(turn_dividers) - 1:
                cut = s['msgIndex']
                dropped = i
            else:
                break
        if cut:
            feed = feed[cut:]
            sections = [dict(s, msgIndex=s['msgIndex'] - cut)
                        for s in sections if s['msgIndex'] >= cut]
            nav_turns = [dict(t, msgIndex=t['msgIndex'] - cut)
                         for t in nav_turns[dropped:]]
            out['olderTurnsDropped'] = dropped
            out['coverage'] = 'partial'
            out.setdefault('coverageReason', 'feed-cap')
    out['messages'] = feed
    out['sections'] = sections
    out['turns'] = nav_turns
    return out


def list_conv_tasks(conv_id: str, *, user_id: int, limit: int = 30,
                    before: int | None = None) -> dict:
    """Task rows for the drawer: live registry + durable attempts + legacy
    task_results, newest first, each annotated with exact structural tallies.

    Attempt identity rows are the primary postmortem discovery authority. They
    are owner-scoped and keyset-paged through a compact partial index, so a
    global task-results work cap cannot make a retained timing trace invisible.
    ``task_results`` remains a compatibility source for pre-TurnStore and VU
    tasks that have no generation attempt.

    ``before`` is an exclusive createdAt-ms cursor paging OLDER persisted
    rows; live-registry rows only belong to the first page and are skipped
    once a cursor is supplied (they are always the newest, so they can
    never fall behind the cursor). ``hasMore`` tells the UI a further page
    may exist. A storage read failure sets ``readError`` — an empty
    ``tasks`` list is only honest when the reads succeeded.

    VU sub-tasks run with convId='' and are therefore NOT returned from attempt
    discovery here; they remain reachable per-task via the bubble anchor (P3).
    """
    limit = min(max(1, int(limit or 30)), 100)
    rows: dict[str, dict] = {}
    read_error = False
    if before is None:
        try:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            live = [
                task for task in chat_task_runtime.snapshot_owned(
                    user_id=user_id)
                if task.get('convId') == conv_id
            ]
            for t in live:
                row = {
                    'taskId': t['id'],
                    'status': t.get('status') or 'running',
                    'createdAt': int(t.get('created_at', 0) * 1000),
                    'completedAt': None,
                    'live': True,
                }
                # Display context the persisted rows get for free from the
                # summary query: which reply bubble this run belongs to and
                # what the user asked. Without these a RUNNING row renders
                # as a bare id with no anchor ("task order makes no sense").
                turn_id = t.get('turn_id')
                if isinstance(turn_id, str) and turn_id:
                    row['turnId'] = turn_id
                msgs = t.get('messages')
                if isinstance(msgs, list):
                    for m in reversed(msgs):
                        if not (isinstance(m, dict)
                                and m.get('role') == 'user'):
                            continue
                        content = m.get('content')
                        if isinstance(content, list):
                            content = ' '.join(
                                str(b.get('text') or '') for b in content
                                if isinstance(b, dict)
                                and b.get('type') == 'text')
                        preview = str(content or '')[:80].strip()
                        if preview:
                            row['userPreview'] = preview
                        break
                rows[t['id']] = row
        except Exception as e:
            logger.debug('[RequestInspector] live registry read failed: %s', e)
            read_error = True
    persisted_ids: set[str] = set()
    durable_has_more = False
    try:
        from lib.storage import get_storage_client

        trace_payload = {
            'conversation_id': conv_id,
            'user_id': user_id,
            'limit': limit,
        }
        if before is not None:
            trace_payload['before_created_at'] = int(before)
        trace_page = get_storage_client().query(
            'turn.timing_trace.list', trace_payload, deadline=30) or {}
        durable_has_more = bool(trace_page.get('has_more'))
        for record in trace_page.get('records') or []:
            tid = str(record.get('task_id') or '')
            if not tid or tid in rows:
                continue
            created_at = int(record.get('created_at') or 0)
            rows[tid] = {
                'taskId': tid,
                'status': str(record.get('status') or ''),
                'createdAt': created_at,
                'completedAt': record.get('settled_at'),
                'turnId': str(record.get('turn_id') or ''),
                'live': False,
            }
            persisted_ids.add(tid)
    except Exception as e:
        logger.warning('[RequestInspector] durable attempt discovery failed '
                       'for conv=%s: %s', (conv_id or '')[:8], e)
        read_error = True

    # Fetch beyond one page so a ``before`` cursor can still fill ``limit``
    # rows after filtering, and so ``hasMore`` is a real signal instead of
    # a guess. Bounded: the summary row is metadata-only.
    fetch_limit = min(1000, max(limit * 4, limit + 1, 60))
    legacy_has_more = False
    try:
        from lib.storage import get_storage_client

        result = get_storage_client().query(
            'task_results.summary_list', {
                'conv_id': conv_id,
                'limit': fetch_limit,
                'user_id': user_id,
                'scan_limit': 10_000,
                'order_by': 'created_at_desc',
            }, deadline=30) or {}
        if result.get('capped'):
            logger.warning(
                '[RequestInspector] task summary scan hit its 10000-row '
                'work cap for conv=%s', (conv_id or '')[:8])
        legacy_records = result.get('records') or []
        legacy_has_more = len(legacy_records) >= fetch_limit
        for row in legacy_records:
            tid = row.get('key')
            if tid in rows:
                continue
            created_at = int(row.get('created_at') or 0)
            if before is not None and created_at >= int(before):
                continue
            rows[tid] = {
                'taskId': tid,
                'status': row.get('status') or '',
                'createdAt': created_at,
                'completedAt': row.get('completed_at'),
                'live': False,
            }
            persisted_ids.add(tid)
    except Exception as e:
        logger.warning('[RequestInspector] task_results read failed for '
                       'conv=%s: %s', (conv_id or '')[:8], e)
        read_error = True
    tasks = sorted(rows.values(), key=lambda x: x['createdAt'] or 0,
                   reverse=True)[:limit]
    parent_ids = {t['taskId'] for t in tasks}
    if parent_ids:
        try:
            from lib.storage import get_storage_client

            summary = get_storage_client().query(
                'event.inspector_summary', {
                    'task_ids': sorted(parent_ids),
                }, deadline=30) or {}
            by_parent = {t['taskId']: t for t in tasks}
            by_id = dict(by_parent)
            for record in summary.get('records') or []:
                task_id = str(record.get('task_id') or '')
                parent, marker, agent_id = task_id.partition('#agent:')
                if not marker or parent not in parent_ids or not agent_id:
                    continue
                child = {
                    'taskId': task_id,
                    'parentTaskId': parent,
                    'agentId': agent_id,
                    'isSwarmAgent': True,
                    'status': 'swarm-agent',
                    'createdAt': (int(record.get('first_event_at_ms') or 0)
                                  or by_parent[parent]['createdAt']),
                    'completedAt': None,
                    'live': False,
                }
                tasks.append(child)
                by_id[task_id] = child
            # Children were appended AFTER the parent list was sorted, so
            # without this pass every swarm agent clusters at the bottom
            # regardless of when it actually ran. Re-sort the whole list
            # by start time; the sort is stable, so a child that inherits
            # its parent's timestamp still lands right after that parent.
            tasks.sort(key=lambda x: (x['createdAt'] or 0, x['taskId']),
                       reverse=True)
            for record in summary.get('records') or []:
                task = by_id.get(str(record.get('task_id') or ''))
                if task is None:
                    continue
                task['requestCount'] = int(record.get('request_count') or 0)
                task['stateCount'] = int(record.get('state_count') or 0)
                task['legacyCount'] = int(record.get('legacy_count') or 0)
                task['hasEvents'] = bool(record.get('event_count'))
        except Exception as e:
            logger.warning(
                '[RequestInspector] event summary read failed conv=%s: %s',
                (conv_id or '')[:8], e)
            read_error = True
            for task in tasks:
                task['readError'] = True
    for t in tasks:
        t.setdefault('requestCount', 0)
        t.setdefault('stateCount', 0)
        t.setdefault('legacyCount', 0)
        t['hasEvents'] = bool(
            t.get('hasEvents')
            or t['requestCount'] or t['stateCount'] or t['legacyCount'])
    selected_persisted_ids = {
        str(t.get('taskId') or '') for t in tasks if not t.get('live')
    }
    out = {
        'convId': conv_id,
        'tasks': tasks,
        'hasMore': bool(
            durable_has_more
            or legacy_has_more
            or len(persisted_ids) > len(selected_persisted_ids)
        ),
    }
    if read_error:
        out['readError'] = True
    return out


__all__ = ['fold_request_log', 'get_conv_inspector_wire',
           'get_request_payload', 'list_conv_tasks',
           'inspector_child_belongs_to_parent', 'invalidate_task_cache']
