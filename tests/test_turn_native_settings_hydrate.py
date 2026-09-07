"""Conversation hydration has one snapshot and one settings owner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

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
    assert "hasArtifacts" not in snapshot["required"]
    assert snapshot["properties"]["hasArtifacts"]["type"] == "boolean"


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
    assert "applySnapshotMetadata?(" in runtime
    assert "options.applySnapshotMetadata?.(conversation, snapshot)" in runtime
    assert "applySnapshotMetadata(conv, snapshot)" in adapter
    assert "snapshot.hasArtifacts" in adapter


def test_open_path_has_no_second_settings_fetch_or_archive_fallback() -> None:
    lifecycle = (
        ROOT / "frontend/src/runtime/sections/main/main_conv_lifecycle.js"
    ).read_text(encoding="utf-8")
    assert "One snapshot hydrates turns, attempts, revision, and settings." in lifecycle
    assert "Api.conversations.get(c.id, { query: { window: '1' } })" not in lifecycle
    assert "await hydrateConversationRuntime(id);" not in lifecycle
    assert "isTurnAuthorityActive" not in lifecycle
    assert lifecycle.count(
        "ConversationTurnStore?.reconcileConversationActivity?.("
    ) == 2
    assert "prevConv?.id, id," in lifecycle


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_open_path_wakes_warm_conversations_without_a_second_snapshot() -> None:
    lifecycle = ROOT / "frontend/src/runtime/sections/main/main_conv_lifecycle.js"
    harness = r"""
const fs = require('fs');

var conversations = [];
var activeConvId = null;
var calls = { hydrate: 0, wake: 0, projectRestore: 0 };
var runtimeScope = {
  PlanDecisionPresentation: { activateConversation() {} },
  requestAuthoritativeConversationRender() {},
  ConversationTurnStore: {
    async wakeConversation() { calls.wake += 1; },
  },
};
var config = {};
var sessionStorage = { setItem() {} };
var document = {
  getElementById() { return { textContent: '' }; },
};

function getActiveConv() {
  return conversations.find((item) => item.id === activeConvId) || null;
}
function _purgeEmptyConvs() {}
function closeBranchPanel() {}
function isBranchModeActive() { return false; }
function getActiveFolderId() { return null; }
function _conversationDisplayTitle(value) { return value; }
function t(value) { return value; }
function _swapActiveConvItem() { return true; }
function renderConversationList() {}
function renderPendingQueueUI() {}
function _refreshServerQueue() {}
function updateSendButton() {}
function _restoreConvProject() { calls.projectRestore += 1; }
function _resumePendingTranslations() {}
function restoreConversationSettingsToComposer() {}
async function hydrateConversationRuntime() { calls.hydrate += 1; }

eval(fs.readFileSync(process.argv[1], 'utf8'));

async function open(snapshotRequired) {
  calls = { hydrate: 0, wake: 0, projectRestore: 0 };
  const conversation = {
    id: snapshotRequired ? 'cold' : 'warm',
    title: 'Conversation',
    _turnSnapshotRequired: snapshotRequired,
  };
  conversations = [conversation];
  activeConvId = conversation.id;
  loadConversation(conversation.id);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { ...calls };
}

(async () => {
  const warm = await open(false);
  const cold = await open(true);
  process.stdout.write(JSON.stringify({ warm, cold }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "-e", harness, str(lifecycle)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == {
        "warm": {"hydrate": 0, "wake": 1, "projectRestore": 1},
        "cold": {"hydrate": 1, "wake": 0, "projectRestore": 1},
    }
