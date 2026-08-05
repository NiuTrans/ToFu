"""Request Inspector — server-authoritative per-task request fold (P2).

Design: ``docs/DEBUG_PANEL_REDESIGN.md`` (row schemas FROZEN in §3.3 — do not
rename keys). The frontend drawer renders ONLY what this module folds from
the persisted ``task_events`` log (durable-before-visible, 6h TTL). The
in-browser ``_debugRequests`` log (P1) is a live accelerator with gaps —
``sse_poll_fallback.js`` never processes ``messages_snapshot``, so a poll
window drops rounds client-side; the server log never does. The server fold
is therefore the authority for both live and finished tasks.

Row shapes
==========
``fold_request_log(task_id)`` →
    ``{taskId, requests, states, coverage, eventsAvailable, requestCount}``
    Request row (metadata ONLY — never the payload):
    ``{roundNum, ts, model, params, messageCount, toolsCount,
       approxTokens, label, legacy, attempts}``
    Attempt row (joined from ``round_usage`` by roundNum):
    ``{tag, model, tokensIn, tokensOut, traceId, streamElapsedMs,
       cacheRead, cacheWrite, ts}``
    State row: ``{roundNum, label, messageCount, ts, legacy}``
``get_request_payload(task_id, round_num)`` → full payload for ONE round
    (messages + tools + params + model) — the on-demand detail fetch.
``list_conv_tasks(conv_id)`` → ``{convId, tasks}`` — Task rows for the
    drawer (live registry + task_results, exact kind-counted tallies via
    one json_extract GROUP BY — no payload bulk).

kind classification
===================
``kind=`` (the P1 emission contract) wins. Pre-contract persisted rows
carry no kind; ONLY for those legacy rows do we fall back to the
roundNum/label markers (migration shim — the contract itself never parses
labels; see design §3.1).
"""

from __future__ import annotations

import json

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

_SNAPSHOT = 'messages_snapshot'
_ROUND_USAGE = 'round_usage'
_STATE_ROUND_LABELS = ('final', 'fallback')
# Legacy-only state markers (pre-contract snapshots carried no kind=).
_LEGACY_STATE_LABEL_MARKERS = ('工具结果后', '最终回复后', 'Fallback')


def _row_get(r, key, idx):
    """Row access tolerant of mapping vs positional rows (mirrors
    event_log.read_events)."""
    try:
        if key in r.keys():
            return r[key]
    except Exception as _e:
        logger.debug('row get: failed (%s)', _e)
        pass
    return r[idx]


def _read_events(task_id: str) -> list:
    """Return [{event_id, type, payload, ts_ms}] ordered by event_id.

    Separate from ``event_log.read_events`` (which omits ``ts_ms`` — the
    request-row schema carries ``ts``). Read-only; never throws.

    CACHED (short TTL): the drawer's natural usage is "fold the task, then
    open round after round", and every ``get_request_payload`` call used to
    re-read AND re-rebuild the task's whole event log — O(rounds^2) work for
    a linear UI action. Measured on a real 126-round task: 206s to walk every
    round, ~1.6s per click. With the cache the same walk is one read.

    The TTL is deliberately short: a LIVE task appends rounds while the user
    watches, and a stale list would hide the newest request. 3s is long
    enough to collapse a burst of per-round fetches, short enough that the
    next poll sees new rounds.
    """
    if not task_id:
        return []
    import time as _time
    now = _time.time()
    hit = _EVENTS_CACHE.get(task_id)
    if hit is not None and (now - hit[0]) < _EVENTS_CACHE_TTL_S:
        return hit[1]
    rows = _read_events_uncached(task_id)
    # Bound the cache: drop the oldest entry when full (a browsing session
    # touches a handful of tasks; this is not a hot-path structure).
    if len(_EVENTS_CACHE) >= _EVENTS_CACHE_MAX:
        try:
            oldest = min(_EVENTS_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _EVENTS_CACHE.pop(oldest, None)
        except ValueError:
            _EVENTS_CACHE.clear()
    _EVENTS_CACHE[task_id] = (now, rows)
    return rows


# task_id → (cached_at_epoch, rebuilt_event_rows)
_EVENTS_CACHE: dict[str, tuple] = {}
_EVENTS_CACHE_TTL_S = 3.0
_EVENTS_CACHE_MAX = 8


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
        _EVENTS_CACHE.pop(task_id, None)


def _read_events_uncached(task_id: str) -> list:
    """Uncached read + rebuild (see :func:`_read_events`).

    Reads ONLY the structural slice the inspector renders (snapshots, round
    usage, endpoint markers) — NEVER the streaming noise (delta / phase /
    tool_progress / …). Every SSE delta is persisted as its own row, so an
    unfiltered read is dominated by noise: measured on a real 51,754-row
    task, the FIRST-10000-rows cap below cut every snapshot past round 6,
    and rounds 7+ all rendered "mirror expired". Structural rows are a few
    per round, so the same cap now spans thousands of rounds.
    """
    try:
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug('[RequestInspector] thread db unavailable: %s', e)
        return []
    from lib.tasks_pkg.event_log import STRUCTURAL_EVENT_TYPES
    _struct_ph = ','.join(['?'] * len(STRUCTURAL_EVENT_TYPES))
    try:
        rows = db.execute(
            'SELECT event_id, type, payload, ts_ms FROM task_events '
            f'WHERE task_id=? AND (type IN ({_struct_ph}) '
            "OR type LIKE 'endpoint\\_%' ESCAPE '\\') "
            'ORDER BY event_id ASC LIMIT 10000',
            (task_id, *STRUCTURAL_EVENT_TYPES)).fetchall()
    except Exception as e:
        logger.warning('[RequestInspector] read failed for task=%s: %s',
                       task_id[:8], e)
        return []
    out = []
    for r in rows:
        payload = _row_get(r, 'payload', 2)
        if not isinstance(payload, dict):
            try:
                payload = json.loads(payload or '{}')
            except (TypeError, ValueError, json.JSONDecodeError) as _e:
                logger.debug('read events uncached: unexpected type/unparseable/malformed JSON (%s)', _e)
                payload = {}
        try:
            out.append({
                'event_id': int(_row_get(r, 'event_id', 0)),
                'type': _row_get(r, 'type', 1),
                'payload': payload,
                'ts_ms': int(_row_get(r, 'ts_ms', 3) or 0),
            })
        except (TypeError, ValueError) as e:
            logger.debug('[RequestInspector] row skipped for task=%s: %s',
                         task_id[:8], e)
    return _rebuild_snapshot_rows(out)


def _rebuild_snapshot_rows(rows: list) -> list:
    """Restore full ``messages``/``tools`` on delta-stored snapshot rows.

    Storage is incremental (docs/DEBUG_PANEL_REDESIGN.md §10) but every
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


def _msg_chars(msg: dict) -> int:
    """Char count of one message (mirrors the frontend _debugMsgChars)."""
    c = msg.get('content')
    n = 0
    if isinstance(c, str):
        n = len(c)
    elif isinstance(c, list):
        for b in c:
            if isinstance(b, dict):
                if b.get('type') == 'text':
                    n += len(b.get('text') or '')
                elif b.get('type') == 'image_url':
                    n += len((b.get('image_url') or {}).get('url') or '')
    for tc in (msg.get('tool_calls') or []):
        args = ((tc or {}).get('function') or {}).get('arguments')
        if isinstance(args, str):
            n += len(args)
        elif args:
            n += len(json.dumps(args))
    return n


def _est_tokens(messages: list) -> int:
    """Rough token estimate (chars/3.5, diagnostic not billing — mirrors
    the frontend _debugMsgTokens)."""
    chars = sum(_msg_chars(m) for m in messages if isinstance(m, dict))
    return max(1, round(chars / 3.5)) if chars else 0


def fold_request_log(task_id: str) -> dict:
    """Fold a task's persisted events into the Request Inspector rows.

    Request rows are METADATA-ONLY (no ``messages``/``tools`` bulk) —
    payloads are served on demand via :func:`get_request_payload`.
    """
    events = _read_events(task_id)
    requests = []
    states = []
    attempts: dict[str, list] = {}
    endpoint_seen = False
    for e in events:
        p = e['payload']
        et = e['type']
        if et == _SNAPSHOT:
            msgs = p.get('messages') or []
            if _snapshot_kind(p) == 'state':
                states.append({
                    'roundNum': p.get('roundNum'),
                    'label': p.get('label') or '',
                    'messageCount': len(msgs),
                    'ts': e['ts_ms'],
                    'legacy': 'kind' not in p,
                })
            else:
                row = {
                    'roundNum': p.get('roundNum'),
                    'ts': e['ts_ms'],
                    'model': p.get('model') or '',
                    # Endpoint turns tag their phase (P4) so same-numbered
                    # planner/worker/critic rounds stay distinct rows.
                    'turn': p.get('turn') or '',
                    'params': p.get('params') or {},
                    'messageCount': len(msgs),
                    'toolsCount': len(p.get('tools') or []),
                    'approxTokens': _est_tokens(msgs),
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
                'streamElapsedMs': int(u.get('stream_elapsed_ms') or 0),
                'cacheRead': int(nu.get('cache_read') or 0),
                'cacheWrite': int(nu.get('cache_write') or 0),
                'ts': e['ts_ms'],
            })
        elif et.startswith('endpoint_'):
            # Endpoint-driven task. Planner/Worker/Critic turns all run
            # run_task (snapshots fire) — but each re-numbers rounds from
            # 1, so a task whose snapshots carry NO turn tag (pre-P4 log)
            # is genuinely ambiguous, not uncovered.
            endpoint_seen = True
    for row in requests:
        row['attempts'] = attempts.get(
            (row['turn'], str(row['roundNum'])), [])
    has_turn_tags = any(r['turn'] for r in requests)
    out = {
        'taskId': task_id,
        'requests': requests,
        'states': states,
        'eventsAvailable': bool(events),
        'requestCount': len(requests),
    }
    if endpoint_seen and not has_turn_tags:
        # Legacy endpoint log: planner/worker/critic rounds share numbers
        # with no phase tag — rows exist but cannot be told apart.
        out['coverage'] = 'partial'
        out['coverageReason'] = 'endpoint-untagged'
    else:
        out['coverage'] = 'full'
    return out


def get_request_payload(task_id: str, round_num, turn: str = '',
                        kind: str = 'request') -> dict | None:
    """Full payload for ONE snapshot round (the on-demand detail fetch).

    ``turn`` (optional): endpoint phase tag ('planning'|'working'|
    'reviewing') or 'swarm-agent' — disambiguates same-numbered rounds.
    When given, only snapshots with a matching turn qualify; when empty,
    the last matching snapshot wins (legacy / untagged behavior).

    ``kind``: 'request' (default) reads pre-request snapshots; 'state'
    reads the post-tool / final / fallback mirrors. Both share the SAME
    roundNum axis (docs/DEBUG_PANEL_REDESIGN.md §3.1: the post-tool mirror
    of loop round N carries roundNum=N+1, exactly the request that produced
    those tool calls), so ONE addressing scheme serves both — this is what
    the in-chat state inspector fetches.

    Returns None when no matching snapshot exists for that round (expired
    log, wrong kind, or unknown task).
    """
    if kind not in ('request', 'state'):
        return None
    best = None
    for e in _read_events(task_id):
        p = e['payload']
        if e['type'] != _SNAPSHOT or _snapshot_kind(p) != kind:
            continue
        if str(p.get('roundNum')) != str(round_num):
            continue
        if turn and (p.get('turn') or '') != turn:
            continue
        best = (e, p)  # last wins (a re-emitted round supersedes)
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
    }
    # §10.3: a round that could not be exactly reconstructed says so — the
    # UI must never present a partial rebuild as the real request.
    if p.get('degraded'):
        out['degraded'] = True
        out['degradedReason'] = p.get('degradedReason') or ''
    return out


def list_conv_tasks(conv_id: str, limit: int = 15) -> dict:
    """Task rows for the drawer: live chat registry + persisted
    task_results, newest first, each annotated with EXACT kind-counted
    snapshot tallies (one json_extract GROUP BY — translates to PG jsonb
    accessors via the dialect bridge, no payload bulk).

    VU sub-tasks run with convId='' and are therefore NOT returned here;
    they remain reachable per-task via the bubble anchor (P3).
    """
    rows: dict[str, dict] = {}
    try:
        from lib.tasks_pkg.manager import _chat_runtime
        with _chat_runtime._lock:  # type: ignore[attr-defined]
            live = [t for t in _chat_runtime._tasks.values()  # type: ignore[attr-defined]
                    if t.get('convId') == conv_id]
        for t in live:
            rows[t['id']] = {
                'taskId': t['id'],
                'status': t.get('status') or 'running',
                'createdAt': int(t.get('created_at', 0) * 1000),
                'completedAt': None,
                'live': True,
            }
    except Exception as e:
        logger.debug('[RequestInspector] live registry read failed: %s', e)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        trs = db.execute(
            'SELECT task_id, status, created_at, completed_at '
            'FROM task_results WHERE conv_id=? '
            'ORDER BY created_at DESC LIMIT ?',
            (conv_id, limit)).fetchall()
        for r in trs:
            tid = _row_get(r, 'task_id', 0)
            if tid in rows:
                continue
            rows[tid] = {
                'taskId': tid,
                'status': _row_get(r, 'status', 1),
                'createdAt': int(_row_get(r, 'created_at', 2) or 0),
                'completedAt': _row_get(r, 'completed_at', 3),
                'live': False,
            }
    except Exception as e:
        logger.warning('[RequestInspector] task_results read failed for '
                       'conv=%s: %s', (conv_id or '')[:8], e)
    tasks = sorted(rows.values(), key=lambda x: x['createdAt'] or 0,
                   reverse=True)[:limit]
    # Swarm sub-agent rows (P4): sub-agents persist their LLM-request
    # snapshots under '{parent_task_id}#agent:{agent_id}' (see
    # lib/swarm/agent.py::_emit_request_snapshot). Surface them as child
    # rows of their parent so the drawer can drill into agent calls.
    parent_ids = {t['taskId'] for t in tasks}
    if parent_ids:
        try:
            db = get_thread_db(DOMAIN_CHAT)
            like_rows = db.execute(
                "SELECT DISTINCT task_id FROM task_events "
                "WHERE task_id LIKE '%#agent:%'").fetchall()
            by_parent = {t['taskId']: t for t in tasks}
            for r in like_rows:
                cid = _row_get(r, 'task_id', 0)
                parent, _, agent_id = cid.partition('#agent:')
                if parent not in parent_ids or not agent_id:
                    continue
                tasks.append({
                    'taskId': cid,
                    'parentTaskId': parent,
                    'agentId': agent_id,
                    'isSwarmAgent': True,
                    'status': 'swarm-agent',
                    'createdAt': by_parent[parent]['createdAt'],
                    'completedAt': None,
                    'live': False,
                })
        except Exception as e:
            logger.debug('[RequestInspector] swarm-agent discovery failed: %s', e)
    ids = [t['taskId'] for t in tasks]
    if ids:
        try:
            db = get_thread_db(DOMAIN_CHAT)
            placeholders = ','.join(['?'] * len(ids))
            counts = db.execute(
                "SELECT task_id, json_extract(payload, '$.kind') AS k, "
                "COUNT(*) AS n FROM task_events "
                f"WHERE task_id IN ({placeholders}) "
                "AND type='messages_snapshot' GROUP BY task_id, k",
                tuple(ids)).fetchall()
            by_id = {t['taskId']: t for t in tasks}
            for c in counts:
                row = by_id.get(_row_get(c, 'task_id', 0))
                if row is None:
                    continue
                k = _row_get(c, 'k', 1)
                n = int(_row_get(c, 'n', 2) or 0)
                if k == 'state':
                    row['stateCount'] = row.get('stateCount', 0) + n
                elif k == 'request':
                    row['requestCount'] = row.get('requestCount', 0) + n
                else:
                    # Pre-contract rows (no kind) — exact request/state split
                    # needs the label shim; surface as a legacy tally so the
                    # UI can mark the count approximate.
                    row['legacyCount'] = row.get('legacyCount', 0) + n
        except Exception as e:
            logger.debug('[RequestInspector] kind tally failed: %s', e)
    for t in tasks:
        t.setdefault('requestCount', 0)
        t.setdefault('stateCount', 0)
        t.setdefault('legacyCount', 0)
        t['hasEvents'] = bool(
            t['requestCount'] or t['stateCount'] or t['legacyCount'])
    return {'convId': conv_id, 'tasks': tasks}


__all__ = ['fold_request_log', 'get_request_payload', 'list_conv_tasks',
           'invalidate_task_cache']
