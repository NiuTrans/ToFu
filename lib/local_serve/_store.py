"""lib/local_serve/_store.py — Durable ledger of managed instances.

Authority: ``data/config/local_serve.json`` (inherits the ``data/`` export
exclusion). One record per managed deployment the user asked for:

    {
      "id":            "ls_vllm_qwen3-8b",
      "owner_user_id": 1,
      "engine":        "vllm",
      "model_path":    "/models/Qwen3-8B",
      "served_name":   "Qwen3-8B",
      "port":          18100,
      "base_url":      "http://127.0.0.1:18100/v1",
      "argv": [...], "env": {...},       # last attempted launch (no secrets
                                         # — only planner-generated values)
      "tier":          "tight",
      "status":        "running",        # planned|installing|starting|
                                         # running|stopped|failed
      "pid":           12345,
      "provider_id":   "managed_vllm_18100",
      "degrade_index": 0,                # OOM ladder position
      "last_error":    null,
      "created_at":    ..., "updated_at": ...
    }

The ledger is DURABLE user state (what was deployed for them); the venvs,
downloaded binaries and logs under ``data/local_serve/`` are
reconstructible and may be reclaimed without touching this file. The file
is capped at ``_MAX_INSTANCES`` records — stale stopped/failed rows are
evicted first, running rows are never evicted.
"""

from __future__ import annotations

import time

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['LEDGER_PATH', 'get_instance', 'list_instances', 'remove_instance',
           'upsert_instance']

LEDGER_PATH = config_path('local_serve.json')

_MAX_INSTANCES = 32


def _empty() -> dict:
    return {'instances': []}


def list_instances() -> list:
    st = read_json(LEDGER_PATH, default=None)
    if not isinstance(st, dict):
        return []
    rows = st.get('instances')
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def get_instance(instance_id: str) -> dict | None:
    for r in list_instances():
        if r.get('id') == instance_id:
            return r
    return None


def _evict(rows: list) -> list:
    if len(rows) <= _MAX_INSTANCES:
        return rows
    terminal = [r for r in rows if r.get('status') in ('stopped', 'failed')]
    keep = [r for r in rows if r.get('status') not in ('stopped', 'failed')]
    terminal.sort(key=lambda r: r.get('updated_at') or 0)
    room = _MAX_INSTANCES - len(keep)
    if room < 0:
        # All running — shed the oldest terminal-free tail anyway; the cap is
        # a hard bound, not a suggestion.
        keep.sort(key=lambda r: r.get('updated_at') or 0)
        return keep[-_MAX_INSTANCES:]
    return keep + terminal[-room:]


def upsert_instance(record: dict) -> dict:
    """Insert or replace one record (matched by ``id``); returns the stored row."""
    now = time.time()
    record = dict(record)
    record.setdefault('created_at', now)
    record['updated_at'] = now

    def _mutate(st):
        if not isinstance(st, dict):
            st = _empty()
        rows = st.get('instances')
        if not isinstance(rows, list):
            rows = []
            st['instances'] = rows
        for i, r in enumerate(rows):
            if isinstance(r, dict) and r.get('id') == record['id']:
                record['created_at'] = r.get('created_at', record['created_at'])
                rows[i] = record
                st['instances'] = _evict(rows)
                return st
        rows.append(record)
        st['instances'] = _evict(rows)
        return st

    update_json_atomic(LEDGER_PATH, _mutate, default=_empty())
    return record


def update_fields(instance_id: str, **fields) -> dict | None:
    """Patch selected fields of one record; None when the id is unknown."""
    row = get_instance(instance_id)
    if row is None:
        return None
    row.update(fields)
    return upsert_instance(row)


def remove_instance(instance_id: str) -> bool:
    removed = {'ok': False}

    def _mutate(st):
        if not isinstance(st, dict):
            st = _empty()
        rows = st.get('instances')
        if isinstance(rows, list):
            before = len(rows)
            st['instances'] = [r for r in rows
                               if not (isinstance(r, dict)
                                       and r.get('id') == instance_id)]
            removed['ok'] = len(st['instances']) < before
        return st

    update_json_atomic(LEDGER_PATH, _mutate, default=_empty())
    return removed['ok']
