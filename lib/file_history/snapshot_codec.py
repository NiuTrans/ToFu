"""Pure snapshot-log v2 delta codec.

Responsibility: encode a materialized FileHistory snapshot into either a full
anchor or a base-id-guarded delta, and reconstruct deltas for existing readers.
This module performs no filesystem I/O.  ``lib.file_history.store`` owns log
durability, cache validation, locking, and compaction.
"""
from __future__ import annotations

import json
from typing import Any


SNAPSHOT_STORAGE_VERSION = 2
SNAPSHOT_DELTA_ANCHOR_EVERY = 64
MIN_DELTA_SAVINGS_BYTES = 128


def compact_json_bytes(payload: dict) -> bytes:
    """Return the canonical compact UTF-8 representation used by JSONL."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')


def _files_map(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if not all(isinstance(path, str) for path in payload):
        return None
    return payload


def _forward_delta(
    previous_files: dict[str, Any],
    current_files: dict[str, Any],
) -> dict:
    changed = {
        path: version
        for path, version in current_files.items()
        if path not in previous_files or previous_files[path] != version
    }
    removed = [path for path in previous_files if path not in current_files]
    return {'set': changed, 'remove': removed}


def encode_snapshot_record(
    record: dict,
    previous_record: dict | None,
    delta_depth: int,
) -> tuple[dict, int]:
    """Return ``(stored_record, new_delta_depth)``.

    A full record is always selected for the first row, after a broken/unknown
    base, every ``SNAPSHOT_DELTA_ANCHOR_EVERY`` rows, or when delta JSON would
    not save at least ``MIN_DELTA_SAVINGS_BYTES``.  Callers therefore get
    bounded recovery chains without paying delta overhead for tiny snapshots.
    """
    full_record = dict(record)
    current_files = _files_map(full_record.get('files'))
    previous_files = _files_map(
        previous_record.get('files') if isinstance(previous_record, dict)
        else None,
    )
    previous_id = (
        previous_record.get('id')
        if isinstance(previous_record, dict)
        else None
    )
    if (
        current_files is None
        or previous_files is None
        or not isinstance(previous_id, str)
        or not previous_id
        or delta_depth >= SNAPSHOT_DELTA_ANCHOR_EVERY - 1
    ):
        return full_record, 0

    delta = _forward_delta(previous_files, current_files)
    stored = {
        key: value
        for key, value in full_record.items()
        if key != 'files'
    }
    stored['storageVersion'] = SNAPSHOT_STORAGE_VERSION
    stored['filesDelta'] = {
        'baseId': previous_id,
        **delta,
    }
    if (
        len(compact_json_bytes(stored)) + MIN_DELTA_SAVINGS_BYTES
        >= len(compact_json_bytes(full_record))
    ):
        return full_record, 0
    return stored, delta_depth + 1


def materialize_snapshot_record(
    stored_record: Any,
    base_id: str | None,
    base_files: dict[str, Any] | None,
    delta_depth: int,
) -> tuple[dict, str, dict[str, Any], int] | None:
    """Materialize one stored row, or return ``None`` for a broken row.

    Full ``files`` rows are independent anchors, including every legacy row.
    A v2 delta is accepted only when its explicit ``baseId`` matches the last
    successfully materialized record.  This makes corruption fail closed and a
    later full anchor restore the stream without guessing state.
    """
    if not isinstance(stored_record, dict):
        return None
    snapshot_id = stored_record.get('id')
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None

    full_files = _files_map(stored_record.get('files'))
    if full_files is not None:
        materialized = dict(stored_record)
        materialized['files'] = dict(full_files)
        return materialized, snapshot_id, materialized['files'], 0

    if stored_record.get('storageVersion') != SNAPSHOT_STORAGE_VERSION:
        return None
    delta = stored_record.get('filesDelta')
    if not isinstance(delta, dict) or base_files is None:
        return None
    if delta.get('baseId') != base_id:
        return None
    changed = _files_map(delta.get('set'))
    removed = delta.get('remove')
    if changed is None or not isinstance(removed, list):
        return None
    if not all(isinstance(path, str) for path in removed):
        return None
    if set(changed).intersection(removed):
        return None

    files = dict(base_files)
    files.update(changed)
    for path in removed:
        files.pop(path, None)
    materialized = {
        key: value
        for key, value in stored_record.items()
        if key not in ('storageVersion', 'filesDelta')
    }
    materialized['files'] = files
    return materialized, snapshot_id, files, delta_depth + 1


def build_reverse_files_delta(
    previous_files: dict[str, Any] | None,
    current_files: dict[str, Any],
) -> dict | None:
    """Encode the small patch that reconstructs the preceding file map."""
    if previous_files is None:
        return None
    return _forward_delta(current_files, previous_files)


def apply_files_delta(files: dict[str, Any], delta: Any) -> dict | None:
    """Apply a cache delta to a copy of ``files``; reject malformed input."""
    if not isinstance(delta, dict):
        return None
    changed = _files_map(delta.get('set'))
    removed = delta.get('remove')
    if changed is None or not isinstance(removed, list):
        return None
    if not all(isinstance(path, str) for path in removed):
        return None
    if set(changed).intersection(removed):
        return None
    restored = dict(files)
    restored.update(changed)
    for path in removed:
        restored.pop(path, None)
    return restored
