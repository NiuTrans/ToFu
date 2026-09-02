"""Folder catalog loading survives conversation-catalog startup failure."""

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


def test_folder_continuation_runs_when_conversation_catalog_fails():
    harness = r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
const calls = { folders: 0, migrated: 0, warnings: 0 };
console.warn = () => { calls.warnings += 1; };
global.conversations = [];
global.activeConvId = null;
global.loadConversationCatalog = async () => { throw new Error('catalog offline'); };
global.loadFolders = async () => { calls.folders += 1; };
global._migratePinnedToFolder = () => { calls.migrated += 1; };
global._scheduleFolderLoadRetry = () => {};
global.ConversationTurnStore = { hydrateConversation: async () => {} };
global.getActiveConv = () => null;
global.convIsBusy = () => false;
global.showStreamingUIForConv = () => {};
global.requestAuthoritativeConversationRender = () => {};
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
    assert result["folders"] == 1
    assert result["migrated"] == 1
    assert result["warnings"] == 1


def test_folder_promise_is_owned_outside_catalog_try_block():
    source = runtime_section("main/main_init_tasks.js")
    folder_owner = source.index("const folderLoad =")
    catalog_try = source.index("try {", folder_owner)
    finally_clause = source.index("finally {", catalog_try)
    assert folder_owner < catalog_try < finally_clause
    assert "await folderLoad;" in source[finally_clause:]
    assert "Promise.all([" not in source[folder_owner:finally_clause]
