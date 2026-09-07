"""Bounded command-receipt compression and replay contracts."""

from __future__ import annotations

import hashlib
import sqlite3

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.receipt_codec import (
    COMMAND_RECEIPT_LOOKUP_SQL,
    COMPRESSED_RECEIPT_MAGIC,
    MAX_DECODED_RECEIPT_BYTES,
    MAX_STORED_RECEIPT_BYTES,
    command_receipt_identity_v2,
    decode_command_receipt_lookup,
    decode_receipt_response,
    encode_receipt_response,
)


pytestmark = pytest.mark.unit


def test_v2_identity_is_fixed_width_stable_and_validates_sha256_digest():
    command_key, request_digest = command_receipt_identity_v2(
        "命令-α", "record.put", "ab" * 32
    )

    assert command_key.hex() == (
        "dd91ad53e1926ae0d98915f235e5ed04632de02ad62a699b39046da74312f61f"
    )
    assert request_digest == bytes.fromhex("ab" * 32)
    assert len(command_key) == len(request_digest) == 32

    for invalid in ("", "not-hex", "ab" * 31, "ab" * 33):
        with pytest.raises(StorageError) as raised:
            command_receipt_identity_v2("command", "record.put", invalid)
        assert raised.value.code == "database_protocol_error"


def test_dual_format_lookup_replays_both_tables_and_rejects_ambiguity():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE storage_command_receipts(
            command_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
            request_digest TEXT NOT NULL, response_json BLOB NOT NULL,
            committed_at_ms INTEGER NOT NULL);
        CREATE TABLE storage_command_receipts_v2(
            command_key BLOB PRIMARY KEY, operation TEXT NOT NULL,
            request_digest BLOB NOT NULL, response_json BLOB NOT NULL,
            committed_at_ms INTEGER NOT NULL) WITHOUT ROWID;
        """
    )
    operation = "record.put"
    command_id = "dual-format-command"
    digest = "12" * 32
    command_key, digest_bytes = command_receipt_identity_v2(
        command_id, operation, digest
    )
    response = encode_receipt_response({"ok": True, "version": 7})
    params = (
        operation, digest, command_id,
        operation, digest_bytes, command_key,
    )

    connection.execute(
        "INSERT INTO storage_command_receipts VALUES (?,?,?,?,?)",
        (command_id, operation, digest, response, 1),
    )
    legacy_rows = [
        dict(row) for row in connection.execute(
            COMMAND_RECEIPT_LOOKUP_SQL, params
        ).fetchall()
    ]
    assert decode_command_receipt_lookup(legacy_rows) == (
        True, {"ok": True, "version": 7}
    )

    connection.execute("DELETE FROM storage_command_receipts")
    connection.execute(
        "INSERT INTO storage_command_receipts_v2 VALUES (?,?,?,?,?)",
        (command_key, operation, digest_bytes, response, 2),
    )
    v2_rows = [
        dict(row) for row in connection.execute(
            COMMAND_RECEIPT_LOOKUP_SQL, params
        ).fetchall()
    ]
    assert decode_command_receipt_lookup(v2_rows) == (
        True, {"ok": True, "version": 7}
    )

    mismatched = (
        operation, "34" * 32, command_id,
        operation, bytes.fromhex("34" * 32), command_key,
    )
    mismatch_rows = [
        dict(row) for row in connection.execute(
            COMMAND_RECEIPT_LOOKUP_SQL, mismatched
        ).fetchall()
    ]
    with pytest.raises(StorageError) as conflict:
        decode_command_receipt_lookup(mismatch_rows)
    assert conflict.value.code == "database_conflict"

    connection.execute(
        "INSERT INTO storage_command_receipts VALUES (?,?,?,?,?)",
        (command_id, operation, digest, response, 1),
    )
    duplicate_rows = [
        dict(row) for row in connection.execute(
            COMMAND_RECEIPT_LOOKUP_SQL, params
        ).fetchall()
    ]
    connection.close()
    with pytest.raises(StorageError) as integrity:
        decode_command_receipt_lookup(duplicate_rows)
    assert integrity.value.code == "database_integrity"


def test_v2_sqlite_file_budget_is_at_least_thirty_percent_smaller(tmp_path):
    row_count = 4_096
    response = encode_receipt_response({"ok": True, "version": 1})
    legacy_path = tmp_path / "legacy-receipts.db"
    compact_path = tmp_path / "compact-receipts.db"
    legacy = sqlite3.connect(legacy_path)
    compact = sqlite3.connect(compact_path)
    legacy.execute("PRAGMA page_size=4096")
    compact.execute("PRAGMA page_size=4096")
    legacy.execute(
        "CREATE TABLE receipts(command_id TEXT PRIMARY KEY, "
        "operation TEXT NOT NULL, request_digest TEXT NOT NULL, "
        "response_json BLOB NOT NULL, committed_at_ms INTEGER NOT NULL)"
    )
    compact.execute(
        "CREATE TABLE receipts(command_key BLOB PRIMARY KEY, "
        "operation TEXT NOT NULL, request_digest BLOB NOT NULL, "
        "response_json BLOB NOT NULL, committed_at_ms INTEGER NOT NULL) "
        "WITHOUT ROWID"
    )
    legacy_rows = []
    compact_rows = []
    for index in range(row_count):
        command_id = f"turn.event.record:{index:08d}:" + "x" * 96
        digest = hashlib.sha256(str(index).encode()).hexdigest()
        command_key, digest_bytes = command_receipt_identity_v2(
            command_id, "turn.event.record", digest
        )
        legacy_rows.append(
            (command_id, "turn.event.record", digest, response, index)
        )
        compact_rows.append(
            (command_key, "turn.event.record", digest_bytes, response, index)
        )
    legacy.executemany("INSERT INTO receipts VALUES (?,?,?,?,?)", legacy_rows)
    compact.executemany("INSERT INTO receipts VALUES (?,?,?,?,?)", compact_rows)
    legacy.commit()
    compact.commit()
    legacy.execute("VACUUM")
    compact.execute("VACUUM")
    legacy.close()
    compact.close()

    legacy_bytes = legacy_path.stat().st_size
    compact_bytes = compact_path.stat().st_size
    assert compact_bytes <= int(legacy_bytes * 0.70), {
        "rows": row_count,
        "legacy_bytes": legacy_bytes,
        "compact_bytes": compact_bytes,
    }


def test_small_receipts_keep_legacy_canonical_json_bytes():
    response = {"ok": True, "value": {"answer": 42}}
    expected = orjson.dumps(response, option=orjson.OPT_SORT_KEYS)

    encoded = encode_receipt_response(response)

    assert encoded == expected
    assert decode_receipt_response(encoded) == response
    assert decode_receipt_response(memoryview(encoded)) == response


def test_large_repeated_commit_response_is_compressed_and_exactly_replayed():
    projection = {"content": "projection-result-" * 20_000}
    response = {
        "value": {"turn": {"projection": projection}},
        "events": [{"event": {"payload": {"turns": [
            {"projection": projection},
        ]}}}],
    }

    encoded = encode_receipt_response(response)

    assert encoded.startswith(COMPRESSED_RECEIPT_MAGIC)
    assert len(encoded) <= MAX_STORED_RECEIPT_BYTES
    assert decode_receipt_response(encoded) == response


def test_incompressible_or_decoded_oversize_responses_keep_hard_limits():
    incompressible = "".join(
        hashlib.sha256(str(index).encode()).hexdigest()
        for index in range(5_000)
    )
    with pytest.raises(StorageError) as stored_limit:
        encode_receipt_response({"value": incompressible})
    assert stored_limit.value.code == "database_protocol_error"
    assert "too large for a receipt" in stored_limit.value.message

    with pytest.raises(StorageError) as decoded_limit:
        encode_receipt_response({"value": "x" * MAX_DECODED_RECEIPT_BYTES})
    assert decoded_limit.value.code == "database_protocol_error"
    assert "decoded receipt budget" in decoded_limit.value.message


def test_corrupt_or_unknown_compressed_receipts_fail_as_integrity_errors():
    encoded = bytearray(encode_receipt_response({
        "value": "compressible-value-" * 20_000,
    }))
    encoded[-1] ^= 0xFF

    for invalid in (
        bytes(encoded),
        b"tofu.receipt.future.v2\x00payload",
        COMPRESSED_RECEIPT_MAGIC + b"\x00\x00",
    ):
        with pytest.raises(StorageError) as raised:
            decode_receipt_response(invalid)
        assert raised.value.code == "database_integrity"
