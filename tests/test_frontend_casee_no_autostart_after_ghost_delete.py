"""Startup remains metadata-only and never dispatches a billable Turn."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section, runtime_section_path


pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not shutil.which("node"), reason="node not installed"),
]


def test_startup_defers_all_turn_hydration_without_auto_dispatch():
    harness = r"""
const fs = require('fs');
global.window = global;
const calls = { dispatch: 0, hydrate: [], catalog: 0, folders: 0, warning: '' };
console.warn = (...args) => { calls.warning = args.map(String).join(' '); };
global.runtimeScope = global;
global.ConversationTurnStore = {
  hydrateConversation: async (conversation) => calls.hydrate.push(conversation.id),
};
global.requestAuthoritativeConversationRender = () => {};
global.conversations = Array.from({ length: 500 }, (_, index) => ({
  id: `conversation-${index}`,
  _serverTurnCount: index + 1,
  _turnSnapshotRequired: true,
}));
global.activeConvId = null;
global.loadConversationCatalog = async () => { calls.catalog += 1; };
global.loadFolders = async () => { calls.folders += 1; };
global._migratePinnedToFolder = () => {};
global._scheduleFolderLoadRetry = () => {};
global.startAssistantResponse = () => { calls.dispatch += 1; };
global.getActiveConv = () => null;
global.convIsBusy = () => false;
global.showStreamingUIForConv = () => {};
global.renderPendingQueueUI = () => {};
eval(fs.readFileSync(process.argv[1], 'utf8'));
initActiveTasks().then(() => process.stdout.write(JSON.stringify(calls)));
"""
    proc = subprocess.run(
        [
            "node", "-e", harness,
            runtime_section_path("main/main_init_tasks.js"),
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    result = json.loads(proc.stdout)
    assert result["warning"] == "", json.dumps(result, sort_keys=True)
    assert result["dispatch"] == 0
    # Catalog size must not turn into snapshot concurrency. Opening a selected
    # conversation owns the one on-demand hydration request.
    assert result["hydrate"] == []
    assert result["catalog"] == 1
    assert result["folders"] == 1


def test_startup_source_contains_no_legacy_lifecycle_classifier():
    source = runtime_section("main/main_init_tasks.js")
    assert "ConversationTurnStore" in source
    assert ".hydrateConversation(conversation)" not in source
    assert "startAssistantResponse" not in source
    assert "conversation.messages" not in source
    assert "conv.messages" not in source
    for retired_classifier in (
        "_classifyGhostTail",
        "_isBuriedEmptyGhost",
        "_sweepBuriedGhostAssistants",
        "assistantTailIsPriorTurn",
    ):
        assert retired_classifier not in source
