"""Durable live Turn projection patch-head contracts."""

from __future__ import annotations

import os
import sqlite3

import pytest


pytestmark = pytest.mark.unit


class _Session:
    backend = "sqlite"

    def __init__(self, events, checkpoint=None):
        self.events = list(events)
        self.checkpoint = checkpoint
        self.fetches = 0
        self.checkpoint_fetches = 0
        self.turn_projection_cache = None

    def fetch_all(self, _sql, _params=()):
        self.fetches += 1
        return list(self.events)

    def fetch_one(self, sql, _params=()):
        assert 'storage_turn_projection_checkpoints' in sql
        self.checkpoint_fetches += 1
        return self.checkpoint


def _event(revision, patch):
    from lib.storage_sidecar.operations_pkg._common import _dump

    return {
        "projection_revision": revision,
        "payload_json": _dump({"payload": {"projectionPatch": patch}}),
    }


def _row(
    base,
    *,
    materialized=7,
    revision=9,
    count=2,
    patch_bytes=100,
    checkpoint=None,
):
    from lib.storage_sidecar.operations_pkg._common import _dump

    return {
        "turn_id": "turn-head",
        "conversation_id": "conv-head",
        "user_id": 1,
        "current_attempt_id": "attempt-head",
        "projection_json": _dump(base),
        "projection_revision": revision,
        "projection_checkpoint_revision": checkpoint,
        "projection_materialized_revision": materialized,
        "projection_patch_count": count,
        "projection_patch_bytes": patch_bytes,
    }


def test_projection_head_folds_exact_revision_chain_and_reuses_cache():
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache
    from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
    from lib.turn_projection_patch import build_projection_patch

    base = {"content": "a", "toolRounds": []}
    middle = {"content": "ab", "toolRounds": []}
    current = {
        "content": "ab",
        "toolRounds": [{"toolCallId": "call-1", "status": "done"}],
    }
    first = build_projection_patch(
        base, middle, base_revision=7, target_revision=8)
    second = build_projection_patch(
        middle, current, base_revision=8, target_revision=9)
    session = _Session([_event(8, first), _event(9, second)])
    session.turn_projection_cache = TurnProjectionCache(
        1024 * 1024, max_entries=4)
    row = _row(base)

    assert projection_from_turn_row(session, row) == current
    assert projection_from_turn_row(session, row) == current
    assert session.fetches == 1
    stats = session.turn_projection_cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 1


def test_projection_head_folds_from_owner_fenced_checkpoint():
    from lib.storage_sidecar.operations_pkg._common import _dump
    from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
    from lib.turn_projection_patch import build_projection_patch

    base = {"content": "checkpoint"}
    current = {"content": "checkpoint plus patch"}
    patch = build_projection_patch(
        base, current, base_revision=7, target_revision=8)
    encoded = _dump(base)
    session = _Session(
        [_event(8, patch)],
        checkpoint={
            "projection_json": encoded,
            "projection_bytes": len(encoded),
        },
    )
    row = _row(
        {}, materialized=7, revision=8, count=1,
        patch_bytes=len(_dump(patch)), checkpoint=7,
    )

    assert projection_from_turn_row(session, row) == current
    assert session.checkpoint_fetches == 1
    assert session.fetches == 1


def test_materialized_settled_projection_does_not_consume_live_cache():
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache
    from lib.storage_sidecar.turn_projection_head import (
        discard_projection_cache_for_row,
        projection_from_turn_row,
    )

    projection = {"content": "settled"}
    row = _row(
        projection,
        materialized=None,
        revision=9,
        count=0,
        patch_bytes=0,
    )
    row["status"] = "completed"
    session = _Session([])
    session.turn_projection_cache = TurnProjectionCache(
        1024 * 1024, max_entries=4)

    assert projection_from_turn_row(session, row) == projection
    assert session.turn_projection_cache.stats()["entries"] == 0

    row["status"] = "running"
    assert projection_from_turn_row(session, row) == projection
    assert session.turn_projection_cache.stats()["entries"] == 1
    assert discard_projection_cache_for_row(session, row) is True
    assert session.turn_projection_cache.stats()["entries"] == 0


@pytest.mark.parametrize(
    ("row_updates", "message"),
    [
        ({"projection_patch_count": 0}, "exceeds its budget"),
        ({"projection_patch_count": 65}, "exceeds its budget"),
        ({"projection_patch_bytes": 1024 * 1024 + 1}, "exceeds its budget"),
        ({"projection_materialized_revision": None}, "residual"),
        ({"projection_materialized_revision": 9}, "inconsistent"),
        ({"projection_checkpoint_revision": 6}, "checkpoint revision"),
    ],
)
def test_projection_head_rejects_inconsistent_or_unbounded_metadata(
        row_updates, message):
    from lib.storage.errors import StorageError
    from lib.storage_sidecar.turn_projection_head import projection_from_turn_row

    row = _row({"content": "base"})
    row.update(row_updates)
    with pytest.raises(StorageError, match=message):
        projection_from_turn_row(_Session([]), row)


def test_projection_head_rejects_gap_duplicate_and_misbased_patches():
    from lib.storage.errors import StorageError
    from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
    from lib.turn_projection_patch import build_projection_patch

    base = {"content": "a"}
    middle = {"content": "b"}
    first = build_projection_patch(
        base, middle, base_revision=7, target_revision=8)
    second = build_projection_patch(
        middle, {"content": "c"}, base_revision=8, target_revision=9)
    cases = [
        ([_event(8, first)], "gap"),
        ([_event(8, first), _event(8, first), _event(9, second)], "duplicate"),
        ([_event(8, first), _event(9, first)], "misbased"),
    ]
    for events, message in cases:
        with pytest.raises(StorageError, match=message):
            projection_from_turn_row(_Session(events), _row(base))


def test_projection_head_append_plan_bounds_chain_and_skips_empty_new_head():
    from lib.storage_sidecar.turn_projection_head import (
        PROJECTION_HEAD_MAX_PATCH_BYTES,
        PROJECTION_HEAD_MAX_PATCHES,
        TurnProjectionHeadState,
        plan_projection_head_append,
    )

    materialized = TurnProjectionHeadState(None, 0, 0)
    assert plan_projection_head_append(
        materialized,
        current_revision=7,
        patch_bytes=10,
        exact_patch=True,
        projection_changed=False,
    ) is None
    first = plan_projection_head_append(
        materialized,
        current_revision=7,
        patch_bytes=10,
        exact_patch=True,
        projection_changed=True,
    )
    assert first == TurnProjectionHeadState(7, 1, 10)
    checkpoint_first = plan_projection_head_append(
        TurnProjectionHeadState(None, 0, 0, checkpoint_revision=7),
        current_revision=7,
        patch_bytes=10,
        exact_patch=True,
        projection_changed=True,
    )
    assert checkpoint_first == TurnProjectionHeadState(7, 1, 10, 7)
    checkpoint_unchanged = plan_projection_head_append(
        TurnProjectionHeadState(None, 0, 0, checkpoint_revision=7),
        current_revision=7,
        patch_bytes=10,
        exact_patch=True,
        projection_changed=False,
    )
    assert checkpoint_unchanged == TurnProjectionHeadState(7, 1, 10, 7)
    assert plan_projection_head_append(
        TurnProjectionHeadState(7, PROJECTION_HEAD_MAX_PATCHES, 100),
        current_revision=7 + PROJECTION_HEAD_MAX_PATCHES,
        patch_bytes=1,
        exact_patch=True,
        projection_changed=False,
    ) is None
    assert plan_projection_head_append(
        TurnProjectionHeadState(
            7, 1, PROJECTION_HEAD_MAX_PATCH_BYTES),
        current_revision=8,
        patch_bytes=1,
        exact_patch=True,
        projection_changed=True,
    ) is None
    assert plan_projection_head_append(
        first,
        current_revision=8,
        patch_bytes=10,
        exact_patch=False,
        projection_changed=True,
    ) is None


def test_sqlite_checkpoint_isolates_large_projection_from_hot_row_wal(tmp_path):
    """A metadata revision must not journal the untouched checkpoint BLOB."""
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._common import _dump
    from lib.storage_sidecar.schema import initialize_schema

    projection_blob = _dump({"content": "x" * (1024 * 1024)})

    def metadata_wal_bytes(path, *, isolated):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL').fetchall()
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        initialize_schema(SQLiteSession(connection))
        connection.execute(
            "INSERT INTO storage_conversation_turns("
            "turn_id,conversation_id,user_id,ordinal,actor,status,"
            "current_attempt_id,projection_json,projection_revision,"
            "projection_checkpoint_revision,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                'turn', 'conversation', 1, 0, 'assistant', 'running',
                'attempt', _dump({}) if isolated else projection_blob, 1,
                1 if isolated else None, 1, 1,
            ),
        )
        if isolated:
            connection.execute(
                "INSERT INTO storage_turn_projection_checkpoints VALUES "
                "(?,?,?,?,?,?,?,?)",
                (
                    'turn', 'conversation', 1, 'attempt', 1,
                    projection_blob, len(projection_blob), 1,
                ),
            )
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            "UPDATE storage_conversation_turns SET projection_revision=2,"
            "projection_materialized_revision=1,projection_patch_count=1,"
            "projection_patch_bytes=128,updated_at=2 WHERE turn_id='turn'"
        )
        connection.execute(
            "INSERT INTO storage_attempt_events("
            "attempt_id,sequence,conversation_id,turn_id,projection_revision,"
            "type,payload_json,payload_bytes,created_at) "
            "VALUES ('attempt',1,'conversation','turn',2,'projection_updated',"
            "?,2,2)",
            (_dump({}),),
        )
        connection.commit()
        wal_bytes = os.path.getsize(f'{path}-wal')
        connection.close()
        return wal_bytes

    inline_wal_bytes = metadata_wal_bytes(
        tmp_path / 'inline.db', isolated=False)
    isolated_wal_bytes = metadata_wal_bytes(
        tmp_path / 'isolated.db', isolated=True)

    assert inline_wal_bytes > len(projection_blob)
    assert isolated_wal_bytes < 64 * 1024
    assert isolated_wal_bytes * 16 < inline_wal_bytes


def test_checkpoint_upsert_reports_stable_integrity_error():
    from lib.storage.errors import StorageError
    from lib.storage_sidecar.turn_projection_write import (
        _upsert_projection_checkpoint,
    )

    class _NoWriteSession:
        def execute(self, _sql, _params=()):
            return 0

    with pytest.raises(StorageError) as failure:
        _upsert_projection_checkpoint(
            _NoWriteSession(),
            turn_id="turn",
            conversation_id="conversation",
            user_id=1,
            attempt_id="attempt",
            revision=2,
            projection_json=b"{}",
            now=3,
        )
    assert failure.value.code == "database_integrity"


def test_recovery_budget_charges_checkpoint_without_loading_its_blob():
    from lib.storage.errors import StorageError
    from lib.storage_sidecar.operations_pkg._turns_lifecycle import (
        _turn_recovery_projection_budget,
    )
    from lib.storage_sidecar.projection_codec import (
        STORAGE_PROJECTION_MAX_HYDRATION_RATIO,
    )
    from lib.storage_sidecar.turn_projection_cache import (
        PROJECTION_CACHE_CHARGE_MULTIPLIER,
    )

    row = _row(
        {},
        materialized=7,
        revision=8,
        count=1,
        patch_bytes=123,
        checkpoint=7,
    )
    row["projection_checkpoint_bytes"] = 600_000
    assert _turn_recovery_projection_budget(row) == (
        600_000 * STORAGE_PROJECTION_MAX_HYDRATION_RATIO
        + 123 * PROJECTION_CACHE_CHARGE_MULTIPLIER
    )

    row["projection_checkpoint_bytes"] = None
    with pytest.raises(StorageError) as failure:
        _turn_recovery_projection_budget(row)
    assert failure.value.code == "database_integrity"
