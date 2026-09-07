"""Contracts for bounded plugin-storage migration and atomic mutations."""

from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest

from lib.storage import StorageError, StorageSupervisor
from lib.storage.manifest import ManifestError, validate_manifest


pytestmark = pytest.mark.unit
_BACKENDS = ["sqlite"]
if os.environ.get("TOFU_STORAGE_TEST_POSTGRES") == "1":
    _BACKENDS.append("postgres")


@pytest.fixture(params=_BACKENDS)
def storage(request, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TOFU_STORAGE_SQLITE_READ_POOL", "2")
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend=request.param, startup_timeout=60
    )
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def _manifest(namespace: str) -> dict:
    return {
        "namespace": namespace,
        "version": 1,
        "tables": [
            {
                "name": "documents",
                "columns": [
                    {"name": "key", "type": "string", "required": True},
                    {"name": "value", "type": "json", "required": True},
                ],
                "primary_key": ["key"],
            }
        ],
        "operations": [
            {
                "name": "list_documents",
                "kind": "query",
                "action": "list",
                "table": "documents",
                "limit_max": 10,
            },
            {
                "name": "get_document",
                "kind": "query",
                "action": "get",
                "table": "documents",
            },
            {
                "name": "put_documents",
                "kind": "command",
                "action": "batch",
                "table": "documents",
                "limit_max": 10,
            },
            {
                "name": "scan_records",
                "kind": "query",
                "action": "legacy_scan",
                "table": "documents",
                "legacy_table": "storage_records",
                "legacy_columns": ["namespace", "record_key"],
                "legacy_order_by": ["namespace", "record_key"],
                "limit_max": 10,
            },
        ],
    }


def _register(storage, manifest: dict) -> None:
    storage.client.command(
        "plugin.register",
        {"manifest": manifest},
        f"register:{uuid.uuid4().hex}",
    )


def test_legacy_scan_manifest_is_exact_and_bounded():
    manifest = validate_manifest(_manifest("example.migration"))
    scan = next(
        operation
        for operation in manifest["operations"]
        if operation["action"] == "legacy_scan"
    )
    assert scan["legacy_table"] == "storage_records"
    assert scan["legacy_columns"] == ["namespace", "record_key"]

    invalid = _manifest("example.invalid_migration")
    invalid["operations"][-1]["legacy_order_by"] = ["undeclared"]
    with pytest.raises(ManifestError, match="invalid legacy order"):
        validate_manifest(invalid)


def test_plugin_batch_is_atomic_and_uses_cas_versions(storage):
    namespace = "example.atomic_batch"
    _register(storage, _manifest(namespace))
    operation = f"plugin.{namespace}.put_documents"
    first = storage.client.command(
        operation,
        {
            "mutations": [
                {"action": "put", "document": {"key": "a", "value": 1}},
                {"action": "put", "document": {"key": "b", "value": 2}},
            ]
        },
        f"batch:{uuid.uuid4().hex}",
    )
    assert [item["version"] for item in first["results"]] == [1, 1]

    with pytest.raises(StorageError) as raised:
        storage.client.command(
            operation,
            {
                "mutations": [
                    {
                        "action": "put",
                        "document": {"key": "a", "value": "changed"},
                        "expected_version": 1,
                    },
                    {
                        "action": "put",
                        "document": {"key": "b", "value": "conflict"},
                        "expected_version": 0,
                    },
                ]
            },
            f"batch:{uuid.uuid4().hex}",
        )
    assert raised.value.code == "database_conflict"
    row = storage.client.query(
        f"plugin.{namespace}.get_document", {"key": "a"}
    )
    assert row["document"]["value"] == 1
    assert row["version"] == 1


def test_legacy_scan_reads_only_manifest_declared_shape(storage):
    namespace = "example.legacy_scan"
    _register(storage, _manifest(namespace))
    for key in ("b", "a", "c"):
        storage.client.command(
            "record.put",
            {"namespace": "legacy-fixture", "key": key, "value": {"secret": key}},
            f"record:{key}:{uuid.uuid4().hex}",
        )

    page = storage.client.query(
        f"plugin.{namespace}.scan_records", {"limit": 2, "offset": 0}
    )
    assert page["exists"] is True
    assert len(page["rows"]) == 2
    assert set(page["rows"][0]) == {"namespace", "record_key"}
    assert page["next_offset"] == 2
    assert "value_json" not in page["rows"][0]


def test_plugin_batch_rejects_duplicate_keys(storage):
    namespace = "example.duplicate_batch"
    _register(storage, _manifest(namespace))
    with pytest.raises(StorageError) as raised:
        storage.client.command(
            f"plugin.{namespace}.put_documents",
            {
                "mutations": [
                    {"action": "put", "document": {"key": "same", "value": 1}},
                    {"action": "put", "document": {"key": "same", "value": 2}},
                ]
            },
            f"batch:{uuid.uuid4().hex}",
        )
    assert raised.value.code == "database_protocol_error"


def test_plugin_list_pages_through_one_key_prefix(storage):
    namespace = "example.prefix_pages"
    _register(storage, _manifest(namespace))
    storage.client.command(
        f"plugin.{namespace}.put_documents",
        {
            "mutations": [
                {
                    "action": "put",
                    "document": {"key": key, "value": key},
                }
                for key in ("row:a", "row:b", "row:c", "unrelated:a")
            ]
        },
        f"batch:{uuid.uuid4().hex}",
    )
    first = storage.client.query(
        f"plugin.{namespace}.list_documents",
        {"key_prefix": "row:", "limit": 2},
    )
    second = storage.client.query(
        f"plugin.{namespace}.list_documents",
        {
            "key_prefix": "row:",
            "after_key": first[-1]["document"]["key"],
            "limit": 2,
        },
    )
    assert [item["document"]["key"] for item in first + second] == [
        "row:a",
        "row:b",
        "row:c",
    ]
