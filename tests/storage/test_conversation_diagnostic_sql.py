"""Bounded diagnostic SQL reads: exact bytes, ownership, revisions and no writes."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.postgres import PostgresSession, _JsonBytesDumper, _load_json_document
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.json_document import JsonBytes
from lib.storage_sidecar.schema import initialize_schema
from lib.storage_sidecar.operation_registry import build_registry
from lib.storage_sidecar.operations_pkg._common import _dump
from lib.storage_sidecar.operations_pkg._conversation_diagnostic import _conversation_diagnostic
from lib.storage_sidecar.projection_codec import (
    STORAGE_PROJECTION_CODEC_KEY,
    decode_projection_from_storage,
)
from lib.storage_sidecar.turn_projection_head import PROJECTION_HEAD_MAX_PATCH_BYTES
from lib.turn_projection_patch import apply_projection_patch, build_projection_patch


pytestmark = pytest.mark.unit


class _Store:
    def __init__(self, session):
        self.session = session

    def write(self, sql, params=()):
        return self.session.execute(sql, params)

    def query(self, kind="header", **values):
        payload = {"conv_id": "conv", "user_id": 1, "kind": kind, **values}
        connection = self.session.connection
        if self.session.backend == "postgres":
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                return _conversation_diagnostic(self.session, payload)
        before = connection.total_changes
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            return _conversation_diagnostic(self.session, payload)
        finally:
            connection.rollback()
            connection.execute("PRAGMA query_only=OFF")
            assert connection.total_changes == before

    def chunk(self, metadata, **values):
        return self.query(
            "chunk", revision=metadata["revision"], index=metadata["index"],
            source_token=metadata["source"]["token"], **values)

    def turn(self, projection=None, *, turn_id="turn", ordinal=5, lane="main", owner=1):
        self.write(
            "INSERT INTO storage_conversation_turns "
            "(turn_id,conversation_id,user_id,presentation_id,lane_id,ordinal,actor,"
            "status,current_attempt_id,projection_json,projection_revision,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn_id, "conv", owner, "presentation-" + turn_id, lane, ordinal,
             "assistant", "running", "attempt-" + turn_id,
             _dump(projection if projection is not None else {"content": "hello 世界"}),
             7, 100, 110),
        )

    def projection_head(self, projection, *, checkpoint=False, patches=True):
        self.turn(projection)
        if checkpoint:
            self.write(
                "INSERT INTO storage_turn_projection_checkpoints "
                "(turn_id,conversation_id,user_id,attempt_id,projection_revision,"
                "projection_json,projection_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("turn", "conv", 1, "attempt-turn", 7, _dump(projection), len(_dump(projection)), 110),
            )
            self.write(
                "UPDATE storage_conversation_turns SET projection_json=?,projection_checkpoint_revision=7 "
                "WHERE turn_id='turn'", (_dump({}),))
        if not patches:
            return projection
        target = {**projection, "content": projection["content"] + " patched ✓", "model": "fixture"}
        patch = build_projection_patch(projection, target, base_revision=7, target_revision=8)
        self.write(
            "INSERT INTO storage_attempt_events "
            "(attempt_id,sequence,conversation_id,turn_id,projection_revision,type,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("attempt-turn", 1, "conv", "turn", 8, "task.progress",
             _dump({"payload": {"projectionPatch": patch}}), 120),
        )
        self.write(
            "UPDATE storage_conversation_turns SET projection_materialized_revision=7,"
            "projection_revision=8,projection_patch_count=1,projection_patch_bytes=? WHERE turn_id='turn'",
            (len(_dump(patch)),),
        )
        return target


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("TOFU_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("TOFU_TEST_POSTGRES_DSN is not configured")
        from tests.support.postgres_isolation import isolated_postgres_dsn

        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import set_json_loads

        source = tmp_path / "postgres-source-dsn"
        source.write_text(dsn)
        source.chmod(0o600)
        with isolated_postgres_dsn(source, tmp_path) as isolated:
            with psycopg.connect(isolated.read_text(), autocommit=True, row_factory=dict_row) as connection:
                connection.adapters.register_dumper(JsonBytes, _JsonBytesDumper)
                set_json_loads(_load_json_document, connection)
                session = PostgresSession(connection)
                initialize_schema(session)
                yield _seed(session)
    else:
        connection = sqlite3.connect(tmp_path / "diagnostic.db", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            session = SQLiteSession(connection)
            initialize_schema(session)
            yield _seed(session)
        finally:
            connection.close()


def _seed(session):
    result = _Store(session)
    result.write(
        "INSERT INTO storage_conversations "
        "(id,user_id,title,created_at_ms,updated_at_ms,msg_count,rev,settings_json,messages_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("conv", 1, "Diagnostic fixture", 100, 120, 999, 42, _dump({"model": "fixture"}), _dump([])),
    )
    return result


@pytest.fixture
def read_evidence(monkeypatch):
    evidence = {"sql": [], "maximum_cell_bytes": 0, "source_bytes": 0}

    def observe(value):
        if isinstance(value, dict):
            for item in value.values():
                observe(item)
        elif isinstance(value, list):
            for item in value:
                observe(item)
        elif isinstance(value, (str, bytes, memoryview)):
            size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
            evidence["maximum_cell_bytes"] = max(evidence["maximum_cell_bytes"], size)

    def wrap(original):
        def fetched(session, sql, params=()):
            assert sql.lstrip().upper().startswith("SELECT"), sql
            evidence["sql"].append(sql)
            result = original(session, sql, params)
            observe(result)
            if isinstance(result, dict) and "data" in result:
                evidence["source_bytes"] += len(result["data"])
            return result
        return fetched

    def forbidden_write(*_args, **_kwargs):
        pytest.fail("A diagnostic query attempted a SQL write")

    def forbidden_hydration(*_args, **_kwargs):
        pytest.fail("A diagnostic query called the full projection hydrator")

    monkeypatch.setattr(
        "lib.storage_sidecar.operations_pkg._turns_core.projection_from_turn_row", forbidden_hydration)
    for session in (SQLiteSession, PostgresSession):
        monkeypatch.setattr(session, "fetch_one", wrap(session.fetch_one))
        monkeypatch.setattr(session, "fetch_all", wrap(session.fetch_all))
        monkeypatch.setattr(session, "execute", forbidden_write)
    return evidence


def _assert_error(store, code, kind="turn", **values):
    with pytest.raises(StorageError) as failure:
        options = {} if kind == "header" else {"index": 0}
        if kind == "chunk":
            options["source_token"] = "0" * 64
        store.query(kind, revision=42, **{**options, **values})
    assert failure.value.code == code


def _reconstruct(store, metadata):
    offset = 0
    result = bytearray()
    while True:
        chunk = store.chunk(metadata, offset=offset, limit=65536)
        assert chunk["sourceToken"] == metadata["source"]["token"]
        assert chunk["revision"] == 42 and chunk["index"] == 0
        assert chunk["totalBytes"] == metadata["source"]["totalBytes"]
        raw = base64.b64decode(chunk["data"], validate=True)
        assert len(raw) <= 65536
        result.extend(raw)
        if chunk["nextOffset"] is None:
            break
        assert chunk["nextOffset"] == offset + len(raw)
        offset = chunk["nextOffset"]
    assert len(result) == metadata["source"]["totalBytes"]
    projection = decode_projection_from_storage(orjson.loads(bytes(result)))
    for patch in metadata["patches"]:
        assert patch["projectionRevision"] == patch["projectionPatch"]["targetRevision"]
        projection = apply_projection_patch(projection, patch["projectionPatch"])
    return projection


@pytest.mark.parametrize("source", ["inline", "inline-head", "checkpoint-head"])
def test_large_projection_reconstructs_without_any_unbounded_sql_cell(store, request, source):
    # The first 65536-byte chunk ends inside an emoji; decoding chunks as text
    # would corrupt the source. The base is >8 MiB before any patch is applied.
    prefix = "x" * (65533 - len(b'{"content":"'))
    projection = {"content": prefix + "🙂中文" + "x" * (8 * 1024 * 1024), "thinking": "思考"}
    expected = store.projection_head(
        projection, checkpoint=source == "checkpoint-head", patches=source != "inline")
    evidence = request.getfixturevalue("read_evidence")
    header = store.query()
    assert header["revision"] == 42 and header["totalCount"] == 1
    assert header["document"]["messages"] == [] and header["turnNative"] is True
    metadata = store.query("turn", revision=42, index=0)
    assert metadata["turn"]["ordinal"] == 5
    assert "projection" not in metadata["turn"]
    assert metadata["source"]["format"] == "projection"
    assert metadata["source"]["totalBytes"] > 8 * 1024 * 1024
    assert evidence["source_bytes"] == 0
    assert evidence["maximum_cell_bytes"] < 1024
    first = store.chunk(metadata)
    # JSONB's canonical whitespace can shift the emoji one byte; both sources
    # still split its 4-byte encoding across these adjacent 64KiB chunks.
    with pytest.raises(UnicodeDecodeError):
        base64.b64decode(first["data"]).decode("utf-8")
    assert _reconstruct(store, metadata) == expected
    assert evidence["maximum_cell_bytes"] <= 65536
    assert all("SELECT *" not in sql.upper() for sql in evidence["sql"])
    assert any("substr(" in sql for sql in evidence["sql"])
    assert any("length(" in sql for sql in evidence["sql"])


def test_checkpoint_without_head_codec_and_exact_raw_bytes(store):
    tool_round = {"toolCallId": "t", "toolName": "fixture", "toolContent": "界" * 5000}
    projection = {
        "content": "原始", "toolRounds": [tool_round],
        "segments": [{"type": "tool", "toolCallId": "t", "result": {"content": tool_round["toolContent"]}}],
    }
    assert STORAGE_PROJECTION_CODEC_KEY in orjson.loads(bytes(_dump(projection)))
    store.projection_head(projection, checkpoint=True, patches=False)
    metadata = store.query("turn", revision=42, index=0)
    assert metadata["patches"] == []
    assert _reconstruct(store, metadata) == projection


def test_rank_is_owner_scoped_not_ordinal_and_ignores_branches(store):
    store.turn(ordinal=4)
    store.turn(turn_id="deleted-gap", ordinal=22)
    store.turn(turn_id="branch", ordinal=0, lane="branch-1")
    store.turn(turn_id="foreign", ordinal=0, owner=2)
    assert store.query()["totalCount"] == 2
    result = store.query("turn", revision=42, index=1)
    assert result["turn"]["turnId"] == "deleted-gap"
    assert result["turn"]["ordinal"] == 22
    assert result["index"] == 1 and result["totalCount"] == 2
    with pytest.raises(StorageError) as failure:
        store.query("turn", revision=42, index=2)
    assert failure.value.code == "database_invalid_payload"


@pytest.mark.parametrize("kind", ["header", "turn", "chunk"])
def test_missing_and_other_owner_are_invisible(store, kind):
    store.turn()
    options = {} if kind == "header" else {"index": 0}
    if kind == "chunk":
        options["source_token"] = store.query("turn", revision=42, index=0)["source"]["token"]
    assert store.query(kind, revision=42, user_id=2, **options) is None
    assert store.query(kind, revision=42, conv_id="missing", **options) is None


@pytest.mark.parametrize("kind", ["turn", "chunk"])
@pytest.mark.parametrize("revision", [None, 0, 41, 43, 10**100])
def test_exact_revision_is_mandatory(store, kind, revision):
    store.turn()
    payload = {"index": 0}

    if kind == "chunk":
        payload["source_token"] = store.query("turn", revision=42, index=0)["source"]["token"]
    if revision is not None:
        payload["revision"] = revision
    with pytest.raises(StorageError) as failure:
        store.query(kind, **payload)
    assert failure.value.code == "database_conflict"


@pytest.mark.parametrize("values", [
    {"kind": "invalid"}, {"kind": True}, {"kind": []}, {"conv_id": ""},
    {"user_id": True}, {"user_id": 0}, {"user_id": "1"},
    {"revision": True}, {"revision": -1}, {"revision": 1.0}, {"revision": "42"},
    {"index": True}, {"index": -1}, {"index": 0.0}, {"index": None},
    {"offset": True}, {"offset": -1}, {"offset": "0"}, {"offset": None},
    {"limit": True}, {"limit": 0}, {"limit": 65537}, {"limit": 1.5},
    {"cursor": "opaque"},
])
def test_invalid_payloads_are_typed_before_storage_reads(store, values):
    with pytest.raises(StorageError) as failure:
        store.query(**values)
    assert failure.value.code == "database_invalid_payload"


def test_offset_is_byte_based_and_eof_is_explicit(store):
    store.turn({"content": "🙂世界"})
    metadata = store.query("turn", revision=42, index=0)
    size = metadata["source"]["totalBytes"]
    pieces = [base64.b64decode(store.chunk(metadata, offset=i, limit=1)["data"])
              for i in range(size)]
    assert orjson.loads(b"".join(pieces)) == {"content": "🙂世界"}
    assert store.chunk(metadata, offset=size)["nextOffset"] is None
    assert store.chunk(metadata, offset=size)["data"] == ""
    _assert_error(store, "database_invalid_payload", "chunk", offset=size + 1,
                  source_token=metadata["source"]["token"])
    _assert_error(store, "database_invalid_payload", "chunk", offset=10**100,
                  source_token=metadata["source"]["token"])
    with pytest.raises(StorageError) as failure:
        store.query("turn", revision=42)
    assert failure.value.code == "database_invalid_payload"


def test_legacy_header_is_honest_and_turn_source_is_explicitly_unsupported(store):
    store.write("UPDATE storage_conversations SET messages_json=? WHERE id='conv'",
                (_dump([{"role": "user", "content": "legacy"}, {"role": "assistant", "content": "response"}]),))
    header = store.query()
    assert header["turnNative"] is False and header["totalCount"] == 2
    assert header["document"]["messages"] == []
    for kind in ("turn", "chunk"):
        _assert_error(store, "database_unsupported", kind)


def test_empty_native_conversation_and_empty_main_lane(store):
    assert store.query()["turnNative"] is True
    assert store.query()["totalCount"] == 0
    _assert_error(store, "database_invalid_payload")
    store.turn(lane="branch")
    assert store.query()["turnNative"] is True and store.query()["totalCount"] == 0


@pytest.mark.parametrize("base", [None, [], False, "raw text", 1])
def test_non_object_projection_base_is_not_silently_coerced(store, base):
    store.turn()
    store.write("UPDATE storage_conversation_turns SET projection_json=? WHERE turn_id='turn'", (_dump(base),))
    _assert_error(store, "database_integrity")


def test_malformed_sqlite_json_source_is_rejected(store):
    if store.session.backend != "sqlite":
        pytest.skip("PostgreSQL JSONB refuses malformed JSON on write")
    store.turn()
    store.write("UPDATE storage_conversation_turns SET projection_json=? WHERE turn_id='turn'", (b'{"broken":',))
    _assert_error(store, "database_integrity")


@pytest.mark.parametrize("mutation", [
    "projection_revision=9", "projection_materialized_revision=6",
    "projection_patch_count=0", "projection_patch_count=65", "projection_patch_bytes=1048577",
    "projection_patch_bytes=1", "projection_checkpoint_revision=6", "current_attempt_id='foreign'",
])
def test_invalid_projection_head_metadata_is_rejected(store, mutation):
    store.projection_head({"content": "base"})
    store.write("UPDATE storage_conversation_turns SET " + mutation + " WHERE turn_id='turn'")
    _assert_error(store, "database_integrity")


@pytest.mark.parametrize("mutation", [
    "user_id=2", "conversation_id='other'", "attempt_id='foreign'", "projection_revision=6",
    "projection_bytes=0",
])
def test_checkpoint_identity_and_revision_fences(store, mutation):
    store.projection_head({"content": "base"}, checkpoint=True)
    store.write("UPDATE storage_turn_projection_checkpoints SET " + mutation + " WHERE turn_id='turn'")
    _assert_error(store, "database_integrity")


def test_checkpoint_missing_and_wrong_size_are_not_empty_bases(store):
    store.projection_head({"content": "base"}, checkpoint=True)
    if store.session.backend == "sqlite":
        store.write("UPDATE storage_turn_projection_checkpoints SET projection_bytes=1")
        _assert_error(store, "database_integrity")
    store.write("DELETE FROM storage_turn_projection_checkpoints")
    _assert_error(store, "database_integrity")


@pytest.mark.parametrize("mutation", ["DELETE", "foreign-conversation", "foreign-turn", "duplicate", "misbased", "invalid", "huge"])
def test_patch_head_corruption_is_explicit_and_payload_reads_stay_bounded(store, request, mutation):
    store.projection_head({"content": "base"})
    if mutation == "DELETE":
        store.write("DELETE FROM storage_attempt_events")
    elif mutation == "foreign-conversation":
        store.write("UPDATE storage_attempt_events SET conversation_id='other'")
    elif mutation == "foreign-turn":
        store.write("UPDATE storage_attempt_events SET turn_id='other'")
    elif mutation == "duplicate":
        store.write("INSERT INTO storage_attempt_events "
                    "(attempt_id,sequence,conversation_id,turn_id,projection_revision,type,payload_json,created_at) "
                    "SELECT attempt_id,sequence+1,conversation_id,turn_id,"
                    "projection_revision,type,payload_json,created_at FROM storage_attempt_events")
    else:
        patch = {"version": 1, "baseRevision": 7, "targetRevision": 8,
                 "operations": [{"op": "append_text", "path": ["content"], "value": "x"}]}
        if mutation == "misbased":
            patch["baseRevision"] = 1
        elif mutation == "invalid":
            patch["operations"][0]["path"] = [True]
        else:
            patch["operations"][0]["value"] = "x" * (8 * 1024 * 1024 + 1)
        store.write("UPDATE storage_attempt_events SET payload_json=?", (_dump({"payload": {"projectionPatch": patch}}),))
    evidence = request.getfixturevalue("read_evidence")
    _assert_error(store, "database_integrity")
    assert evidence["maximum_cell_bytes"] < 1024


def test_read_only_calls_do_not_materialize_checkpoint_or_retain_receipts(store, request):
    store.projection_head({"content": "base"}, checkpoint=True)
    evidence = request.getfixturevalue("read_evidence")
    store.query()
    metadata = store.query("turn", revision=42, index=0)
    assert _reconstruct(store, metadata)["content"] == "base patched ✓"
    assert len(evidence["sql"]) > 5
    assert all(sql.lstrip().startswith("SELECT") for sql in evidence["sql"])


def test_final_revision_witness_rejects_read_committed_mixture(store, monkeypatch):
    store.turn()
    session_cls = SQLiteSession if store.session.backend == "sqlite" else PostgresSession
    fetch_one = session_cls.fetch_one

    def changed(session, sql, params=()):
        if sql.startswith("SELECT rev FROM storage_conversations"):
            return {"rev": 43}
        return fetch_one(session, sql, params)

    monkeypatch.setattr(session_cls, "fetch_one", changed)
    _assert_error(store, "database_conflict")


def test_operation_and_ir_manifest_are_registered_as_receipt_free_query():
    spec = build_registry()["conversation.diagnostic"]
    assert spec.kind == "query" and spec.receipt_required is False
    assert spec.handler is _conversation_diagnostic
    root = Path(__file__).resolve().parents[2]
    ir = json.loads((root / "contracts/tofudb_ir_v1.json").read_text())
    operation = next(op for op in ir["executableOperations"] if op["name"] == "conversation.diagnostic")
    assert operation["kind"] == "query" and operation["receiptRequired"] is False
    assert "conversationDiagnostic" in ir["transactionIr"]["stepKinds"]
    from scripts.gen_storage_operation_catalog import _document
    assert _document() == json.loads((root / "contracts/storage_operations_v1.json").read_text())


@pytest.mark.parametrize("checkpoint", [False, True])
def test_full_64_patch_head_reconstructs_in_revision_order(store, checkpoint):
    projection = {"content": "base"}
    store.projection_head(projection, checkpoint=checkpoint, patches=False)
    total_bytes = 0
    for position in range(64):
        target = {"content": projection["content"] + f"{position}✓"}
        patch = build_projection_patch(
            projection, target, base_revision=7 + position, target_revision=8 + position)
        total_bytes += len(_dump(patch))
        store.write(
            "INSERT INTO storage_attempt_events "
            "(attempt_id,sequence,conversation_id,turn_id,projection_revision,type,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("attempt-turn", position + 1, "conv", "turn", 8 + position, "task.progress",
             _dump({"payload": {"projectionPatch": patch}}), 120 + position),
        )
        projection = target
    store.write(
        "UPDATE storage_conversation_turns SET projection_materialized_revision=7,"
        "projection_revision=71,projection_patch_count=64,projection_patch_bytes=? WHERE turn_id='turn'",
        (total_bytes,),
    )
    metadata = store.query("turn", revision=42, index=0)
    assert len(metadata["patches"]) == 64
    assert _reconstruct(store, metadata) == projection


def _many_small_object_patch_head(store, *, patch_count=1, exact_bytes=None, checkpoint=False):
    projection = {"segments": []}
    store.projection_head(projection, checkpoint=checkpoint, patches=False)
    patches = []
    canonical_bytes = 0
    for position in range(patch_count):
        segments = [{"type": "text", "content": "x"} for _ in range(32000 // patch_count)]
        target = {"segments": projection["segments"] + segments}
        patch = build_projection_patch(
            projection, target, base_revision=7 + position, target_revision=8 + position)
        if exact_bytes is not None and position == patch_count - 1:
            padding = exact_bytes - canonical_bytes - len(_dump(patch))
            assert padding >= 0
            segments[-1]["content"] += "x" * padding
            patch = build_projection_patch(
                projection, target, base_revision=7 + position, target_revision=8 + position)
        canonical_bytes += len(_dump(patch))
        patches.append(patch)
        store.write(
            "INSERT INTO storage_attempt_events "
            "(attempt_id,sequence,conversation_id,turn_id,projection_revision,type,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("attempt-turn", position + 1, "conv", "turn", 8 + position, "task.progress",
             _dump({"payload": {"projectionPatch": patch}}), 120 + position),
        )
        projection = target
    if exact_bytes is not None:
        assert canonical_bytes == exact_bytes
    store.write(
        "UPDATE storage_conversation_turns SET projection_materialized_revision=7,"
        "projection_revision=?,projection_patch_count=?,projection_patch_bytes=? WHERE turn_id='turn'",
        (7 + patch_count, patch_count, min(canonical_bytes, PROJECTION_HEAD_MAX_PATCH_BYTES)),
    )
    return projection, patches, canonical_bytes


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("exact_bytes", [None, PROJECTION_HEAD_MAX_PATCH_BYTES])
def test_many_small_objects_near_canonical_limit_reconstruct(store, request, checkpoint, exact_bytes):
    expected, patches, canonical_bytes = _many_small_object_patch_head(
        store, checkpoint=checkpoint, exact_bytes=exact_bytes)
    # Unlike a single long string, many tiny objects add enough JSONB separator
    # spaces to cross 1 MiB even though the canonical patch is admitted.
    physical_bytes = len(json.dumps(patches[0], ensure_ascii=False).encode("utf-8"))
    assert canonical_bytes <= PROJECTION_HEAD_MAX_PATCH_BYTES < physical_bytes
    if exact_bytes is None:
        assert canonical_bytes == 960110 and physical_bytes == 1088121
    evidence = request.getfixturevalue("read_evidence")
    metadata = store.query("turn", revision=42, index=0)
    assert [item["projectionPatch"] for item in metadata["patches"]] == patches
    expected_read_bytes = physical_bytes if store.session.backend == "postgres" else canonical_bytes
    assert evidence["maximum_cell_bytes"] == expected_read_bytes
    assert evidence["maximum_cell_bytes"] <= 2 * PROJECTION_HEAD_MAX_PATCH_BYTES
    assert _reconstruct(store, metadata) == expected


def test_jsonb_exponent_expansion_has_a_bounded_physical_allowance(store, request):
    projection = {"values": []}
    expected = {"values": [5e-324] * 8192}
    store.projection_head(projection, patches=False)
    patch = build_projection_patch(projection, expected, base_revision=7, target_revision=8)
    canonical_bytes = len(_dump(patch))
    assert canonical_bytes < PROJECTION_HEAD_MAX_PATCH_BYTES
    store.write(
        "INSERT INTO storage_attempt_events "
        "(attempt_id,sequence,conversation_id,turn_id,projection_revision,type,payload_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("attempt-turn", 1, "conv", "turn", 8, "task.progress",
         _dump({"payload": {"projectionPatch": patch}}), 120),
    )
    store.write(
        "UPDATE storage_conversation_turns SET projection_materialized_revision=7,"
        "projection_revision=8,projection_patch_count=1,projection_patch_bytes=? WHERE turn_id='turn'",
        (canonical_bytes,),
    )
    evidence = request.getfixturevalue("read_evidence")
    metadata = store.query("turn", revision=42, index=0)
    assert len(_dump(metadata["patches"][0]["projectionPatch"])) == canonical_bytes
    if store.session.backend == "postgres":
        # Separator-only 2x allowances miss JSONB's exponent-to-decimal rewrite.
        assert evidence["maximum_cell_bytes"] > 2 * PROJECTION_HEAD_MAX_PATCH_BYTES
        assert evidence["maximum_cell_bytes"] <= 64 * canonical_bytes
    else:
        assert evidence["maximum_cell_bytes"] == canonical_bytes
    assert _reconstruct(store, metadata) == expected


def test_patch_text_exceeding_declared_head_allowance_is_not_fetched(store, request):
    _many_small_object_patch_head(store)
    store.write("UPDATE storage_conversation_turns SET projection_patch_bytes=1 WHERE turn_id='turn'")
    evidence = request.getfixturevalue("read_evidence")
    with pytest.raises(StorageError) as failure:
        store.query("turn", revision=42, index=0)
    assert failure.value.code == "database_integrity"
    assert "physical byte budget" in str(failure.value)
    assert evidence["maximum_cell_bytes"] < 1024


@pytest.mark.parametrize("patch_count", [1, 2])
def test_physical_text_allowance_does_not_admit_oversize_canonical_head(store, request, patch_count):
    _, patches, canonical_bytes = _many_small_object_patch_head(
        store, patch_count=patch_count, exact_bytes=PROJECTION_HEAD_MAX_PATCH_BYTES + 1)
    physical_bytes = sum(len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) for patch in patches)
    assert PROJECTION_HEAD_MAX_PATCH_BYTES < canonical_bytes < physical_bytes
    assert physical_bytes < 2 * PROJECTION_HEAD_MAX_PATCH_BYTES
    if patch_count > 1:
        assert all(len(_dump(patch)) < PROJECTION_HEAD_MAX_PATCH_BYTES for patch in patches)
    evidence = request.getfixturevalue("read_evidence")
    with pytest.raises(StorageError) as failure:
        store.query("turn", revision=42, index=0)
    assert failure.value.code == "database_integrity"
    if store.session.backend == "postgres":
        # Forged in-budget metadata must not hide overflow in the decoded sum.
        assert "canonical byte budget" in str(failure.value)
        assert evidence["maximum_cell_bytes"] <= 2 * PROJECTION_HEAD_MAX_PATCH_BYTES
    else:
        # SQLite has no JSONB whitespace allowance and refuses before decoding.
        assert evidence["maximum_cell_bytes"] < 1024


@pytest.mark.parametrize("field,kind", [("settings_json", "header"), ("settlement_json", "turn")])
def test_oversize_metadata_is_rejected_before_payload_fetch(store, request, field, kind):
    store.turn()
    table = "storage_conversations" if field == "settings_json" else "storage_conversation_turns"
    store.write(f"UPDATE {table} SET {field}=?", (_dump({"oversize": "x" * (2 * 1024 * 1024)}),))
    evidence = request.getfixturevalue("read_evidence")
    _assert_error(store, "database_integrity", kind)
    assert evidence["maximum_cell_bytes"] < 1024


def test_huge_legacy_header_counts_without_selecting_message_payload(store, request):
    store.write("UPDATE storage_conversations SET messages_json=?", (_dump([
        {"role": "assistant", "content": "x" * (8 * 1024 * 1024 + 1)},
        {"role": "user", "content": "尾"},
    ]),))
    evidence = request.getfixturevalue("read_evidence")
    header = store.query()
    assert header["totalCount"] == 2 and header["turnNative"] is False
    assert evidence["maximum_cell_bytes"] < 1024
    _assert_error(store, "database_unsupported", "chunk")


def test_sqlite_raw_text_representation_is_not_reserialized(store):
    if store.session.backend != "sqlite":
        pytest.skip("PostgreSQL JSONB intentionally canonicalizes the physical representation")
    store.turn()
    source = '{  "content" : "世界", "other" : [ 1, 2 ] }\n'
    store.write("UPDATE storage_conversation_turns SET projection_json=?", (source,))
    metadata = store.query("turn", revision=42, index=0)
    chunk = store.chunk(metadata)
    assert metadata["source"]["totalBytes"] == len(source.encode("utf-8"))
    assert base64.b64decode(chunk["data"]) == source.encode("utf-8")


@pytest.mark.parametrize("kind", ["header", "turn", "chunk"])
def test_owner_disappearing_at_final_witness_is_invisible(store, monkeypatch, kind):
    store.turn()
    session_cls = type(store.session)
    fetch_one = session_cls.fetch_one

    options = {} if kind == "header" else {"index": 0}
    if kind == "chunk":
        options["source_token"] = store.query("turn", revision=42, index=0)["source"]["token"]

    def removed(session, sql, params=()):
        if sql.startswith("SELECT rev FROM storage_conversations"):
            return None
        return fetch_one(session, sql, params)

    monkeypatch.setattr(session_cls, "fetch_one", removed)
    assert store.query(kind, revision=42, **options) is None


def test_chunk_alone_rejects_a_malformed_base(store):
    store.turn()
    store.write("UPDATE storage_conversation_turns SET projection_json=?", (_dump(["not a projection"]),))
    _assert_error(store, "database_integrity", "chunk")


def test_wrongly_typed_projection_envelope_is_not_returned_as_empty_patch_head(store):
    store.projection_head({"content": "base"})
    store.write("UPDATE storage_attempt_events SET payload_json=?", (_dump({"payload": {"projectionPatch": []}}),))
    _assert_error(store, "database_integrity")


@pytest.mark.parametrize("token", [None, True, 7, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "０" * 64])
def test_chunk_requires_canonical_source_token_before_any_read(store, monkeypatch, token):
    def forbidden_read(*_args, **_kwargs):
        pytest.fail("Malformed token reached SQL")

    monkeypatch.setattr(type(store.session), "fetch_one", forbidden_read)
    _assert_error(store, "database_invalid_payload", "chunk", source_token=token)


def test_missing_source_token_is_invalid_before_owner_lookup(store, monkeypatch):
    def forbidden_read(*_args, **_kwargs):
        pytest.fail("Missing token reached SQL")

    monkeypatch.setattr(type(store.session), "fetch_one", forbidden_read)
    with pytest.raises(StorageError) as failure:
        store.query("chunk", revision=42, index=0)
    assert failure.value.code == "database_invalid_payload"


@pytest.mark.parametrize("kind,extra", [
    ("header", {"index": 0}), ("header", {"offset": 0}), ("header", {"limit": 65536}),
    ("header", {"source_token": "a" * 64}), ("turn", {"offset": 0}),
    ("turn", {"limit": 65536}), ("turn", {"source_token": "a" * 64}),
    ("chunk", {"sourceToken": "a" * 64}), ("chunk", {"unknown": 0}),
])
def test_fields_from_other_diagnostic_kinds_are_not_silently_ignored(store, monkeypatch, kind, extra):
    def forbidden_read(*_args, **_kwargs):
        pytest.fail("Invalid kind fields reached SQL")

    monkeypatch.setattr(type(store.session), "fetch_one", forbidden_read)
    _assert_error(store, "database_invalid_payload", kind, **extra)


@pytest.mark.parametrize("checkpoint", [False, True])
def test_stale_token_is_conflict_even_at_eof_and_before_fetching_chunk(store, request, checkpoint):
    store.projection_head({"content": "base"}, checkpoint=checkpoint)
    metadata = store.query("turn", revision=42, index=0)
    token = metadata["source"]["token"]
    assert len(token) == 64 and all(character in "0123456789abcdef" for character in token)
    assert store.query("turn", revision=42, index=0)["source"]["token"] == token
    evidence = request.getfixturevalue("read_evidence")
    for offset in (0, metadata["source"]["totalBytes"]):
        _assert_error(store, "database_conflict", "chunk", source_token="0" * 64, offset=offset)
    assert evidence["source_bytes"] == 0


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("rewrite", ["update", "delete-insert"])
def test_equal_length_equal_revision_replacement_cannot_mix_chunks(store, checkpoint, rewrite):
    original = {"content": "a" * 131072}
    replacement = {"content": "b" * 131072}
    store.projection_head(original, checkpoint=checkpoint, patches=False)
    before = store.query("turn", revision=42, index=0)
    first = store.chunk(before, limit=65536)
    table = "storage_turn_projection_checkpoints" if checkpoint else "storage_conversation_turns"
    # Preserve identity, revisions, timestamps, checkpoint evidence and length.
    # Reinsert exercises SQLite rowid reuse, not just UPDATE detection.
    if rewrite == "delete-insert":
        store.write(f"CREATE TEMP TABLE saved_diagnostic_source AS SELECT * FROM {table}")
        store.write("UPDATE saved_diagnostic_source SET projection_json=?", (_dump(replacement),))
        store.write(f"DELETE FROM {table}")
        store.write(f"INSERT INTO {table} SELECT * FROM saved_diagnostic_source")
    else:
        store.write(f"UPDATE {table} SET projection_json=?", (_dump(replacement),))
    after = store.query("turn", revision=42, index=0)
    assert before["turn"] == after["turn"]
    assert before["source"]["totalBytes"] == after["source"]["totalBytes"]
    assert before["source"]["token"] != after["source"]["token"]
    with pytest.raises(StorageError) as failure:
        store.chunk(before, offset=first["nextOffset"])
    assert failure.value.code == "database_conflict"
    assert _reconstruct(store, after) == replacement


@pytest.mark.parametrize("mutation", ["status", "settlement", "patch", "checkpoint-source"])
def test_source_token_binds_descriptor_and_patch_plan_not_just_base_bytes(store, mutation):
    projection = {"content": "base"}
    store.projection_head(projection)
    before = store.query("turn", revision=42, index=0)
    if mutation == "status":
        store.write("UPDATE storage_conversation_turns SET status='interrupted'")
    elif mutation == "settlement":
        store.write("UPDATE storage_conversation_turns SET settlement_json=?", (_dump({"outcome": "interrupted"}),))
    elif mutation == "patch":
        patch = before["patches"][0]["projectionPatch"]
        altered = orjson.loads(_dump(patch).replace(b"patched", b"CHANGED"))
        assert len(_dump(altered)) == len(_dump(patch)) and altered != patch
        store.write("UPDATE storage_attempt_events SET payload_json=?", (_dump({"payload": {"projectionPatch": altered}}),))
    else:
        store.write(
            "INSERT INTO storage_turn_projection_checkpoints "
            "(turn_id,conversation_id,user_id,attempt_id,projection_revision,projection_json,projection_bytes,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("turn", "conv", 1, "attempt-turn", 7, _dump(projection), len(_dump(projection)), 110))
        store.write("UPDATE storage_conversation_turns SET projection_checkpoint_revision=7,projection_json=?", (_dump({}),))
    after = store.query("turn", revision=42, index=0)
    assert before["source"]["totalBytes"] == after["source"]["totalBytes"]
    assert before["source"]["token"] != after["source"]["token"]
    with pytest.raises(StorageError) as failure:
        store.chunk(before)
    assert failure.value.code == "database_conflict"


def test_token_cannot_be_reused_for_identical_projection_at_another_turn(store):
    store.turn()
    store.turn(turn_id="second", ordinal=6)
    first = store.query("turn", revision=42, index=0)
    second = store.query("turn", revision=42, index=1)
    assert first["source"]["totalBytes"] == second["source"]["totalBytes"]
    assert first["source"]["token"] != second["source"]["token"]
    _assert_error(store, "database_conflict", "chunk", index=1, source_token=first["source"]["token"])


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("kind", ["turn", "chunk"])
def test_postgres_same_revision_replacement_during_request_is_conflict(store, monkeypatch, checkpoint, kind):
    if store.session.backend != "postgres":
        pytest.skip("SQLite reads use one snapshot; this exercises PostgreSQL READ COMMITTED")
    import psycopg

    store.projection_head({"content": "before"}, checkpoint=checkpoint, patches=False)
    before = store.query("turn", revision=42, index=0)
    fetch_one = PostgresSession.fetch_one
    replaced = False
    table = "storage_turn_projection_checkpoints" if checkpoint else "storage_conversation_turns"
    with psycopg.connect(store.session.connection.info.dsn, autocommit=True) as writer:
        def replace_after_digest(session, sql, params=()):
            nonlocal replaced
            result = fetch_one(session, sql, params)
            if " AS source_digest" in sql and not replaced:
                replaced = True
                writer.execute(f"UPDATE {table} SET projection_json=%s::jsonb", ('{"content":"AFTER!"}',))
            return result

        monkeypatch.setattr(PostgresSession, "fetch_one", replace_after_digest)
        with pytest.raises(StorageError) as failure:
            if kind == "turn":
                store.query("turn", revision=42, index=0)
            else:
                store.chunk(before)
    assert replaced
    assert failure.value.code == "database_conflict"


def test_sqlite_hash_streams_bounded_blob_reads_without_decoding_or_full_fetch(store, monkeypatch, request):
    if store.session.backend != "sqlite":
        pytest.skip("PostgreSQL hashes inside SQL; driver bounds are covered by the shared large-source test")
    import hashlib

    projection = {"content": "世界" * 200000}
    store.projection_head(projection, checkpoint=True, patches=False)
    connection = store.session.connection
    reads = []
    digests = []
    sha256 = hashlib.sha256

    class ObservedBlob:
        def __init__(self, blob):
            self.blob = blob

        def __len__(self):
            return len(self.blob)

        def __enter__(self):
            self.blob.__enter__()
            return self

        def __exit__(self, *args):
            return self.blob.__exit__(*args)

        def read(self, length=-1):
            assert 0 < length <= 65536
            reads.append(length)
            return self.blob.read(length)

    class ObservedConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def blobopen(self, table, column, rowid, *, readonly):
            assert readonly and column == "projection_json"
            return ObservedBlob(connection.blobopen(table, column, rowid, readonly=readonly))

    class ObservedHash:
        def __init__(self, *args):
            self.hash = sha256(*args)

        def update(self, data):
            assert len(data) <= 65536
            self.hash.update(data)

        def hexdigest(self):
            result = self.hash.hexdigest()
            digests.append(result)
            return result

    monkeypatch.setattr(store.session, "connection", ObservedConnection())
    monkeypatch.setattr("lib.storage_sidecar.operations_pkg._conversation_diagnostic.hashlib.sha256", ObservedHash)
    evidence = request.getfixturevalue("read_evidence")
    metadata = store.query("turn", revision=42, index=0)
    assert sum(reads) == metadata["source"]["totalBytes"]
    assert digests[0] == sha256(_dump(projection)).hexdigest()
    assert evidence["maximum_cell_bytes"] < 1024
    assert evidence["source_bytes"] == 0
    reads.clear()
    assert store.chunk(metadata, limit=1)["sourceToken"] == metadata["source"]["token"]
    assert sum(reads) == metadata["source"]["totalBytes"]
    assert evidence["source_bytes"] == 1
