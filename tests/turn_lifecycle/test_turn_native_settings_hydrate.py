"""Conversation hydration has one snapshot and one settings owner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


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


@pytest.mark.parametrize("metadata", [{}, {"hasArtifacts": False}, {"hasArtifacts": True}])
def test_runtime_applies_settings_from_the_authoritative_snapshot(metadata) -> None:
    runtime = native_module_path(
        '.native/snapshot-settings-runtime.js',
        ROOT / 'frontend/src/core/turn-runtime.ts',
    )
    adopter = ROOT / 'frontend/src/runtime/sections/core/conv_apply_settings.js'
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
global.window = globalThis;
global.document = { visibilityState: 'visible' };
global.protectHydratingProjectSettings = (_conversation, settings) => settings;
require(process.argv[1]);
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const metadata = JSON.parse(process.argv[3]);
const conversation = { id: 'settings', paperDocument: { hash: 'stale' },
  fetchEnabled: true, model: 'old' };
// Partial catalog projections cannot erase an absent document.
_applySettingsToConv(conversation, { model: 'catalog' }, 'partial');
assert.deepEqual(conversation.paperDocument, { hash: 'stale' });
const settings = { model: 'authoritative', fetchEnabled: false };
const snapshot = { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
  conversationId: conversation.id, scope: { kind: 'conversation', id: conversation.id },
  conversationRevision: 7, syncSeq: 9, cursor: 'cursor-9', serverBootId: 'boot',
  heartbeatIntervalMs: 15000, settings, turns: [], attempts: [], queueItems: [],
  ...metadata };
const calls = [];
const runtime = createConversationTurnRuntime({
  api: { snapshot: async id => { calls.push(['snapshot', id]); return snapshot; } },
  eventSourceFactory: () => { throw new Error('inactive conversation opened a stream'); },
  isActive: () => false,
  applySettings(conv, value, projection) {
    assert.equal(conv, conversation);
    assert.deepEqual(value, settings);
    calls.push(['settings', projection]);
    _applySettingsToConv(conv, value, projection);
  },
  applySnapshotMetadata(conv, value) {
    assert.equal(conv, conversation);
    assert.equal(value.hasArtifacts, metadata.hasArtifacts);
    assert.equal(Object.hasOwn(value, 'hasArtifacts'), Object.hasOwn(metadata, 'hasArtifacts'));
    calls.push(['metadata', value.conversationRevision]);
  },
});
(async () => {
  try {
    const store = await runtime.hydrateConversation(conversation);
    assert.equal(conversation.model, 'authoritative');
    assert.equal(conversation.fetchEnabled, false);
    assert.equal(conversation.paperDocument, null);
    assert.equal(store.getState().conversationRevision, 7);
    assert.deepEqual(calls, [['snapshot', 'settings'], ['settings', 'full'], ['metadata', 7]]);
  } finally { runtime.disposeConversation(conversation.id); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        ['node', '-e', harness, runtime, str(adopter), json.dumps(metadata)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_open_path_wakes_warm_conversations_without_a_second_snapshot() -> None:
    lifecycle = ROOT / "frontend/src/runtime/sections/main/main_conv_lifecycle.js"
    harness = r"""
const fs = require('fs');

var conversations = [];
var activeConvId = null;
var calls;
var Api = { conversations: { get() {
  calls.legacyFetch += 1;
  throw new Error('opening must use the authoritative sync snapshot');
} } };
var runtimeScope = {
  PlanDecisionPresentation: { activateConversation() {} },
  requestAuthoritativeConversationRender() {},
  ConversationTurnStore: {
    async wakeConversation() { calls.wake += 1; },
  },
};
var config = {};
var composerTranslationStatus = { refresh() {} };
var _conversationProjectController = { begin: () => () => true };
var _conversationModelController = { begin: () => () => true };
// Prelude import of the typed link-navigation owner; every conversation this
// harness opens is already in the loaded catalog, so nothing is resolved.
function createProjectConversationNavigation() {
  return {
    invalidate() {},
    resolveMissing(id, ports) {
      if (!ports.contains(id)) throw new Error(`harness opened an uncatalogued conversation: ${id}`);
      return null;
    },
  };
}
function _clearProjectStateLocal() {}
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
function renderComposerModelNotice() {}
function holdComposerModelNotice() {}
function _restoreConvProject() { calls.projectRestore += 1; }
function _resumePendingTranslations() {}
function restoreConversationSettingsToComposer() {}
async function hydrateConversationRuntime() { calls.hydrate += 1; }

eval(fs.readFileSync(process.argv[1], 'utf8'));

async function open(snapshotRequired) {
  calls = { hydrate: 0, wake: 0, projectRestore: 0, legacyFetch: 0 };
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
        "warm": {"hydrate": 0, "wake": 1, "projectRestore": 2, "legacyFetch": 0},
        "cold": {"hydrate": 1, "wake": 0, "projectRestore": 2, "legacyFetch": 0},
    }
