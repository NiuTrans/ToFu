"""Contracts for bounded, passive-only browser site observations."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest
from jsonschema import Draft202012Validator

from lib.storage import StorageError, StorageSupervisor


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def storage(tmp_path: Path):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend="sqlite", startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor.client
    finally:
        supervisor.stop()


def _analysis() -> dict:
    return {
        "strategy": "captured_api",
        "anti_bot": {"detected": True, "vendor": "cloudflare"},
        "network": {
            "candidates": [
                {
                    "verdict": "likely_data",
                    "method": "POST",
                    "url": (
                        "https://api.internal.example/people/123456/reports"
                        "?access_token=secret&department=finance"),
                    "real_data_score": 0.91,
                    "shape": {
                        "$": "object",
                        "$.data": "object",
                        "$.data.items": "array(2)",
                        "$.data.items[0].accessToken": "string",
                    },
                },
                {
                    "verdict": "blocked",
                    "method": "GET",
                    "url": "https://api.internal.example/private?ticket=secret",
                    "real_data_score": 0.8,
                    "shape": {},
                },
            ],
        },
    }


def _stored_observation() -> dict:
    from lib.browser.site_observations import distill_site_observation

    return distill_site_observation(_analysis(), elapsed_ms=321)


def _record(client, *, owner=1, operation="research", at=1_000, observation=None):
    return client.command(
        "browser.site_observation.record",
        {
            "owner_user_id": owner,
            "origin": "https://internal.example",
            "route_family": "/teams/{segment}/reports",
            "operation": operation,
            "outcome": "success",
            "observed_at_ms": at,
            "observation": observation or _stored_observation(),
        },
        command_id=None,
    )


def test_distillation_removes_queries_and_dynamic_identifiers():
    from lib.browser.site_observations import (
        SITE_OBSERVATION_MAX_BYTES,
        distill_site_observation,
        site_observation_identity,
    )

    identity = site_observation_identity(
        "https://Internal.Example:443/teams/123e4567-e89b-12d3-a456-426614174000/Reports"
        "?employee=alice&token=secret#section")
    assert identity == {
        "origin": "https://internal.example",
        "route_family": "/teams/{segment}/reports",
        "operation": "research",
    }
    assert site_observation_identity(
        "https://internal.example/reset/password/alice")['route_family'] == (
            "/reset/{segment}/{segment}")
    assert site_observation_identity(
        "https://internal.example/users/alice/reports")['route_family'] == (
            "/users/{segment}/reports")

    observation = distill_site_observation(_analysis(), elapsed_ms=321)
    assert observation["api_hints"] == [{
        "method": "POST",
        "origin": "https://api.internal.example",
        "path_template": "/people/{segment}/reports",
        "shape_summary": {
            "$": "object",
            "$.data": "object",
            "$.data.items": "array(2)",
            "$.[sensitive]": "string",
        },
        "score": 0.91,
        "passive_only": True,
    }]
    encoded = json.dumps(observation, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= SITE_OBSERVATION_MAX_BYTES
    assert b"secret" not in encoded and b"finance" not in encoded


def test_machine_contract_accepts_the_sidecar_projection(storage):
    row = _record(storage)
    schema = json.loads((
        ROOT / "contracts/browser_site_observation_v1.schema.json"
    ).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(row)
    assert row["confidence_milli"] == 500
    assert row["visit_count"] == row["successful_visits"] == 1
    assert row["hinted_visits"] == row["hint_match_visits"] == 0
    assert row["api_hints"][0]["passive_only"] is True


def test_storage_is_owner_scoped_expires_and_quarantines_structural_drift(storage):
    row = _record(storage, owner=1, at=10_000)
    identity = {
        "origin": row["origin"],
        "route_family": row["route_family"],
        "operation": row["operation"],
    }
    assert storage.query(
        "browser.site_observation.get",
        {"owner_user_id": 2, **identity, "now_ms": 10_001},
    ) is None

    # Auth/rate/transient failures describe the current session, not structure.
    auth = storage.command(
        "browser.site_observation.record",
        {"owner_user_id": 1, **identity, "outcome": "auth_challenge",
         "observed_at_ms": 10_001},
        command_id=None,
    )
    assert auth["confidence_milli"] == 500
    assert auth["consecutive_failures"] == 0

    for offset in range(2, 5):
        drifted = storage.command(
            "browser.site_observation.record",
            {"owner_user_id": 1, **identity, "outcome": "structure_mismatch",
             "observed_at_ms": 10_000 + offset},
            command_id=None,
        )
    assert drifted["status"] == "quarantined"
    assert drifted["confidence_milli"] == 0
    assert storage.query(
        "browser.site_observation.get",
        {"owner_user_id": 1, **identity,
         "now_ms": drifted["expires_at_ms"]},
    ) is None


def test_sidecar_rejects_unredacted_or_active_hints(storage):
    observation = _stored_observation()
    observation["api_hints"][0]["path_template"] += "?employee=alice"
    with pytest.raises(StorageError, match="query data"):
        _record(storage, observation=observation)

    observation = _stored_observation()
    observation["api_hints"][0]["passive_only"] = False
    with pytest.raises(StorageError, match="passive-only"):
        _record(storage, observation=observation)

    observation = _stored_observation()
    observation["api_hints"][0]["raw_url"] = "https://secret.example/value"
    with pytest.raises(StorageError, match="fields are invalid"):
        _record(storage, observation=observation)

    with pytest.raises(StorageError, match="route family"):
        storage.command(
            "browser.site_observation.record",
            {"owner_user_id": 1, "origin": "https://internal.example",
             "route_family": "/users/alice", "operation": "research",
             "outcome": "success", "observed_at_ms": 1_000,
             "observation": _stored_observation()},
            command_id=None,
        )


def test_owner_lru_is_hard_bounded(storage):
    observation = _stored_observation()
    for index in range(201):
        _record(
            storage, operation=f"research{index}", at=20_000 + index,
            observation=observation,
        )
    oldest = storage.query(
        "browser.site_observation.get",
        {"owner_user_id": 1, "origin": "https://internal.example",
         "route_family": "/teams/{segment}/reports", "operation": "research0",
         "now_ms": 21_000},
    )
    newest = storage.query(
        "browser.site_observation.get",
        {"owner_user_id": 1, "origin": "https://internal.example",
         "route_family": "/teams/{segment}/reports", "operation": "research200",
         "now_ms": 21_000},
    )
    assert oldest is None
    assert newest is not None


def test_adapter_promotion_requires_stability_and_measured_hint_reuse():
    from lib.browser.site_observations import render_adapter_promotion

    candidate = {
        "status": "active", "visit_count": 5, "successful_visits": 5,
        "confidence_milli": 900, "hinted_visits": 2,
        "hint_match_visits": 2,
    }
    assert render_adapter_promotion(candidate).startswith("Adapter promotion candidate")
    assert render_adapter_promotion({**candidate, "hint_match_visits": 1}) == ""
    assert render_adapter_promotion({**candidate, "status": "quarantined"}) == ""


def test_schema_58_migration_is_restart_safe(tmp_path: Path):
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema

    connection = sqlite3.connect(tmp_path / "schema-v57.db")
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.execute("DROP TABLE storage_browser_site_observations")
    connection.execute("DROP TABLE storage_browser_site_observation_owners")
    connection.execute(
        "UPDATE storage_meta SET meta_value='57' WHERE meta_key='schema_version'")

    initialize_schema(session)
    initialize_schema(session)

    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    assert int(version) == SCHEMA_VERSION == 58
    assert {
        "storage_browser_site_observations",
        "storage_browser_site_observation_owners",
    } <= tables
