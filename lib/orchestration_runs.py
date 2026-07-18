"""lib/orchestration_runs.py — Durable, DB-backed orchestration run instances.

Why this exists
---------------
An orchestration *template* (the authored graph) persists in
``data/config/orchestrations.json``. *Running* one, however, used to spin a
purely in-memory ``TaskRuntime`` task (``routes/api_v1/orchestrations.py``)
whose events and result are TTL-purged shortly after the run ends — so a run
could not be reopened tomorrow, and nothing survived a server restart.

This module adds the durable **run instance**: a header row that pins a
SNAPSHOT of the definition (editing the template later never mutates a finished
or in-flight run) plus an append-only event log for cursor replay. See
``docs/proposals/TASK_MODE.md`` §3 for the schema rationale.

Two tables (DDL in ``lib/database/_schema_{sqlite,pg}.py``, schema v24):
  * ``orchestration_runs``       — one row per run (header + final result).
  * ``orchestration_run_events`` — append-only; ``seq`` is monotonic per run
    and mirrors the ``TaskRuntime`` event ``seq``.

Design rules (mirrors ``lib/swarm/persistence.py``)
---------------------------------------------------
* Every function is best-effort and **never raises into the caller** — a DB
  hiccup must not kill a running flow. Failures log at WARNING and return a
  falsy/empty value. Durability is a safety net, not a critical path.
* All SQL uses ``?`` placeholders (translated for PG by the wrapper layer) and
  goes through ``get_thread_db(DOMAIN_SYSTEM)`` / ``db_execute_with_retry``
  like the rest of the system-domain code.
"""

from __future__ import annotations

import json
import secrets
import time

from lib.log import get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

#: Statuses considered terminal (no further events expected).
_TERMINAL = frozenset({'done', 'error', 'aborted'})


_now_ms = now_ms


def new_run_id() -> str:
    """Mint a run-instance id. Distinct prefix from template ids (``orch_``)."""
    return 'run_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)


def _db():
    """Return a thread-local system-domain DB handle, or None if unavailable."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        return get_thread_db(DOMAIN_SYSTEM)
    except Exception as e:
        logger.warning('[OrchRuns] DB handle unavailable: %s', e)
        return None


def _dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning('[OrchRuns] JSON encode failed (%s) — storing empty', e)
        return '{}'


def _loads(raw, default):
    if raw is None or raw == '':
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[OrchRuns] JSON decode failed: %s', e)
        return default


# ═══════════════════════════════════════════════════════════
#  Run header
# ═══════════════════════════════════════════════════════════

def create_run(run_id: str, *, definition: dict, input_text: str = '',
               orch_id: str = '', name: str = '', created_by: str = '') -> None:
    """Insert a new run header row in status='pending'.

    ``definition`` is stored verbatim as the pinned snapshot.
    """
    if not run_id:
        return
    db = _db()
    if db is None:
        return
    now = _now_ms()
    try:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(
            db,
            'INSERT INTO orchestration_runs (id, orch_id, name, definition, '
            'input, status, created_by, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (run_id, orch_id or '', name or '', _dumps(definition or {}),
             input_text or '', 'pending', created_by or '', now, now))
        logger.debug('[OrchRuns] created run id=%s orch=%s', run_id, orch_id)
    except Exception as e:
        logger.warning('[OrchRuns] create_run(%s) failed: %s', run_id, e)


def update_status(run_id: str, status: str, *, final: str | None = None,
                  error: dict | str | None = None) -> None:
    """Update a run's status (and optionally final/error). Stamps updated_at,
    and finished_at when the status is terminal."""
    if not run_id or not status:
        return
    db = _db()
    if db is None:
        return
    now = _now_ms()
    finished = now if status in _TERMINAL else 0
    err_text = '' if error is None else (
        error if isinstance(error, str) else _dumps(error))
    try:
        from lib.database import db_execute_with_retry
        if final is not None and error is not None:
            db_execute_with_retry(
                db, 'UPDATE orchestration_runs SET status=?, final=?, error=?, '
                'updated_at=?, finished_at=? WHERE id=?',
                (status, final, err_text, now, finished, run_id))
        elif final is not None:
            db_execute_with_retry(
                db, 'UPDATE orchestration_runs SET status=?, final=?, '
                'updated_at=?, finished_at=? WHERE id=?',
                (status, final, now, finished, run_id))
        elif error is not None:
            db_execute_with_retry(
                db, 'UPDATE orchestration_runs SET status=?, error=?, '
                'updated_at=?, finished_at=? WHERE id=?',
                (status, err_text, now, finished, run_id))
        else:
            db_execute_with_retry(
                db, 'UPDATE orchestration_runs SET status=?, updated_at=?, '
                'finished_at=? WHERE id=?',
                (status, now, finished, run_id))
        logger.debug('[OrchRuns] run %s → %s', run_id, status)
    except Exception as e:
        logger.warning('[OrchRuns] update_status(%s) failed: %s', run_id, e)


def get_run(run_id: str) -> dict | None:
    """Return one run header as a dict (definition/error JSON-parsed), or None."""
    if not run_id:
        return None
    db = _db()
    if db is None:
        return None
    try:
        row = db.execute(
            'SELECT id, orch_id, name, definition, input, status, final, error, '
            'created_by, created_at, updated_at, finished_at '
            'FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
    except Exception as e:
        logger.warning('[OrchRuns] get_run(%s) failed: %s', run_id, e)
        return None
    if not row:
        return None
    return _row_to_header(row, include_definition=True)


def list_runs(*, status: str = '', orch_id: str = '', limit: int = 50) -> list[dict]:
    """Return run headers (newest first), without the definition blob.

    Optional filters: ``status`` and ``orch_id``. ``limit`` is clamped to
    [1, 200].
    """
    db = _db()
    if db is None:
        return []
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if status:
        where.append('status=?')
        params.append(status)
    if orch_id:
        where.append('orch_id=?')
        params.append(orch_id)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    try:
        rows = db.execute(
            'SELECT id, orch_id, name, status, final, error, created_by, '
            'created_at, updated_at, finished_at FROM orchestration_runs'
            + clause + ' ORDER BY created_at DESC LIMIT ' + str(limit),
            tuple(params)).fetchall()
    except Exception as e:
        logger.warning('[OrchRuns] list_runs failed: %s', e)
        return []
    return [_row_to_header(r, include_definition=False) for r in rows]


def delete_run(run_id: str) -> bool:
    """Remove a run and its events. Returns True on success."""
    if not run_id:
        return False
    db = _db()
    if db is None:
        return False
    try:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(
            db, 'DELETE FROM orchestration_run_events WHERE run_id=?', (run_id,))
        db_execute_with_retry(
            db, 'DELETE FROM orchestration_runs WHERE id=?', (run_id,))
        logger.debug('[OrchRuns] deleted run %s', run_id)
        return True
    except Exception as e:
        logger.warning('[OrchRuns] delete_run(%s) failed: %s', run_id, e)
        return False


def _parse_error(raw):
    """Decode a stored error column. update_status() stores dict errors as
    JSON and plain-string errors verbatim, so a non-JSON value is returned
    as-is rather than collapsed to None."""
    if not (raw or ''):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[OrchRuns] error column not JSON, returning verbatim: %s', e)
        return raw


def _row_to_header(row, *, include_definition: bool) -> dict:
    out = {
        'id':          row['id'],
        'orch_id':     row['orch_id'] or '',
        'name':        row['name'] or '',
        'status':      row['status'] or 'pending',
        'final':       row['final'] or '',
        'error':       _parse_error(row['error']),
        'created_by':  row['created_by'] or '',
        'created_at':  row['created_at'] or 0,
        'updated_at':  row['updated_at'] or 0,
        'finished_at': row['finished_at'] or 0,
    }
    if include_definition:
        out['definition'] = _loads(row['definition'], {})
        out['input'] = row['input'] or ''
    return out


# ═══════════════════════════════════════════════════════════
#  Event log
# ═══════════════════════════════════════════════════════════

def append_event(run_id: str, seq: int, event: dict) -> None:
    """Mirror one engine event into the durable log. ``seq`` is the
    TaskRuntime-assigned monotonic sequence (the PK with run_id)."""
    if not run_id or seq is None:
        return
    db = _db()
    if db is None:
        return
    try:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(
            db,
            'INSERT INTO orchestration_run_events (run_id, seq, type, node_id, '
            'payload, ts) VALUES (?, ?, ?, ?, ?, ?)',
            (run_id, int(seq), str(event.get('type') or ''),
             str(event.get('node_id') or ''), _dumps(event), _now_ms()))
    except Exception as e:
        # A duplicate (run_id, seq) hits the PK — benign on replay races.
        logger.debug('[OrchRuns] append_event(%s/%s) non-fatal: %s',
                     run_id, seq, e)


def get_events(run_id: str, cursor: int = 0) -> list[dict]:
    """Return persisted event payloads with seq > cursor, in order."""
    if not run_id:
        return []
    db = _db()
    if db is None:
        return []
    try:
        rows = db.execute(
            'SELECT seq, payload FROM orchestration_run_events '
            'WHERE run_id=? AND seq>=? ORDER BY seq ASC', (run_id, int(cursor))).fetchall()
    except Exception as e:
        logger.warning('[OrchRuns] get_events(%s) failed: %s', run_id, e)
        return []
    out = []
    for r in rows:
        ev = _loads(r['payload'], None)
        if isinstance(ev, dict):
            ev.setdefault('seq', r['seq'])
            out.append(ev)
    return out
