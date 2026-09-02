"""The machine lifecycle contract matches storage, HTTP, and browser owners."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
CONTRACT = yaml.safe_load(
    (ROOT / "contracts/conversation_lifecycle_v1.yaml").read_text(
        encoding="utf-8"
    )
)


def test_lifecycle_operations_are_registered_with_the_declared_durability():
    from lib.storage_sidecar.operation_domains.conversations import OPERATIONS

    declared = {
        row["storage"]
        for row in CONTRACT["operations"].values()
        if row.get("storage")
    }
    assert declared <= set(OPERATIONS)
    assert OPERATIONS["conversation.delete"].receipt_required
    assert OPERATIONS["conversation.restore"].receipt_required
    assert OPERATIONS["conversation.clone"].receipt_required
    assert not OPERATIONS["conversation.trash.prune"].receipt_required


def test_schema_and_maintenance_match_the_contract():
    import sqlite3

    from lib.storage_sidecar import schema
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.tasks_pkg import event_log

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        schema.initialize_schema(SQLiteSession(connection))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert schema.SCHEMA_VERSION == 40
    assert {
        "storage_conversation_trash",
        "storage_conversation_trash_turns",
    } <= tables
    assert event_log._CONVERSATION_TRASH_TTL_MS == (
        CONTRACT["authorities"]["trash"]["retentionMs"]
    )


def test_http_and_browser_surfaces_match_the_lifecycle_contract():
    from quart import Quart

    # Importing the route owner populates the declarative blueprint. Register
    # it on a fresh app and inspect Quart's public routing map, not source text.
    import routes.conversations  # noqa: F401
    from routes.api_v1 import api_v1_conversations_bp

    app = Quart("conversation-lifecycle-contract")
    app.register_blueprint(api_v1_conversations_bp)
    registered = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in rule.methods
    }
    for operation in ("delete", "restore", "clone"):
        method, declared_path = CONTRACT["operations"][operation]["http"].split(
            " ", 1
        )
        route_path = declared_path.replace("{conversationId}", "<conv_id>")
        assert (route_path, method) in registered

    api = runtime_section("api.js")
    lifecycle = runtime_section("main/main_conv_lifecycle.js")
    assert "Api.conversations.remove(id)" in lifecycle
    assert "Api.conversations.restore(deletedConv.id)" in lifecycle
    assert "Api.conversations.clone(id, newId, title)" in lifecycle
    assert "JSON.stringify(srcConv.messages" not in lifecycle
    assert "persistConversationSettings(restored)" not in lifecycle
    assert "restore:" in api and "clone:" in api
