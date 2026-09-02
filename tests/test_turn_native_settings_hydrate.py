"""Conversation hydration has one snapshot and one settings owner."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_sync_snapshot_contract_requires_settings() -> None:
    contract = yaml.safe_load(
        (ROOT / "contracts/conversation_sync_v3.yaml").read_text(encoding="utf-8")
    )
    snapshot = contract["components"]["schemas"]["ConversationSyncSnapshot"]
    assert "settings" in snapshot["required"]
    assert snapshot["properties"]["settings"] == {
        "$ref": "#/components/schemas/JsonObject"
    }


def test_runtime_applies_settings_from_the_authoritative_snapshot() -> None:
    runtime = (ROOT / "frontend/src/core/turn-runtime.ts").read_text(
        encoding="utf-8"
    )
    adapter = (
        ROOT / "frontend/src/runtime/sections/main/conversation_turn_store.js"
    ).read_text(encoding="utf-8")

    assert "applySettings?(conversation: RuntimeConversation" in runtime
    assert "options.applySettings?.(conversation, record(snapshot.settings))" in runtime
    assert "applySettings(conv, settings)" in adapter
    assert "_applySettingsToConv(conv, settings)" in adapter


def test_open_path_has_no_second_settings_fetch_or_archive_fallback() -> None:
    lifecycle = (
        ROOT / "frontend/src/runtime/sections/main/main_conv_lifecycle.js"
    ).read_text(encoding="utf-8")
    assert "One snapshot hydrates turns, attempts, revision, and settings." in lifecycle
    assert "Api.conversations.get(c.id, { query: { window: '1' } })" not in lifecycle
    assert "await hydrateConversationRuntime(id);" not in lifecycle
    assert "isTurnAuthorityActive" not in lifecycle
