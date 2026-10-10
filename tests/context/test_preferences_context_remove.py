"""Behavior contract for My Context per-item deletion.

The trash icon used to only splice the item out of the panel's local array;
nothing reached the server until 保存更改 issued a full-list PUT. Users
deleted an item, closed the page, and it came back on the next load. The
contract now: deleting a persisted item calls ``Api.userContext.remove``
immediately and rebaselines the dirty tracker (no phantom unsaved state);
a failed delete keeps the item and surfaces an error; an unsaved row (no
id) is spliced locally without any server call.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _node_available() -> bool:
    return bool(shutil.which("node") and (ROOT / "node_modules/jsdom").is_dir())


_HARNESS = textwrap.dedent(
    r"""
    (async () => {
    const fs = require('fs');
    const path = require('path');
    const { JSDOM } = require(path.join(process.argv[1], 'node_modules', 'jsdom'));
    const dom = new JSDOM(`<!doctype html><body>
      <div id="settingsTab_preferences">
        <div id="ctxDirtyState"></div>
        <button id="ctxSaveBtn"></button>
        <div id="prefsCharCount"></div>
        <div id="ctxCapacityFill"></div>
        <div id="prefsStatus"></div>
        <div id="prefsList"></div>
      </div>
    </body>`, { url: 'http://localhost/' });

    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.Element = dom.window.Element;
    globalThis.HTMLElement = dom.window.HTMLElement;
    globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
    globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
    globalThis.AbortController = dom.window.AbortController;
    globalThis.AbortSignal = dom.window.AbortSignal;
    window.t = (key) => key;
    window.showConfirm = async () => true;

    const removeCalls = [];
    let failRemove = false;
    window.Api = { userContext: {
      get: async () => ({ cap: 2500, items: [
        { id: 'ctx_a', type: 'response_preference', source: 'assistant',
          text: '不要出现大模型表述风格',
          created_at: '2026-08-28T12:32:09Z', updated_at: '2026-08-28T12:32:09Z' },
        { id: 'ctx_b', type: 'identity', source: 'manual', text: '来自美团',
          created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' },
      ] }),
      remove: async (id) => {
        removeCalls.push(id);
        if (failRemove) throw new Error('network down');
        return { deleted: true };
      },
      replace: async () => { throw new Error('unexpected replace'); },
    } };

    (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));

    const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
    const articles = () => document.querySelectorAll('.ctx-item').length;
    const saveBtn = document.getElementById('ctxSaveBtn');
    const status = document.getElementById('prefsStatus');
    const result = {};

    await globalThis._populatePreferencesTab();
    result.loaded = { articles: articles(), saveDisabled: saveBtn.disabled };

    // Persisted item: trash click deletes on the server immediately and the
    // panel rebaselines — no phantom "unsaved changes" afterwards.
    document.querySelector('[data-context-action="remove"][data-index="0"]').click();
    await tick(); await tick(); await tick();
    result.removed = {
      removeCalls: [...removeCalls],
      articles: articles(),
      saveDisabled: saveBtn.disabled,
      dirtyText: document.getElementById('ctxDirtyState').textContent,
      statusClass: status.className,
    };

    // Failed delete: the item stays and the error is surfaced.
    failRemove = true;
    await globalThis._contextRemove(0);
    result.failed = {
      removeCalls: [...removeCalls],
      articles: articles(),
      statusClass: status.className,
      statusText: status.textContent,
    };

    // Unsaved row (no id): purely local removal, no server call.
    failRemove = false;
    globalThis._contextAdd('response_preference');
    const addedIndex = articles() - 1;
    await globalThis._contextRemove(addedIndex);
    result.unsaved = {
      removeCalls: [...removeCalls],
      articles: articles(),
    };

    console.log(JSON.stringify(result));
    })().catch((error) => { console.error(error); process.exitCode = 1; });
    """
)


def _run_remove_scenarios() -> dict:
    if not _node_available():
        pytest.skip("node + jsdom dev dependencies are required")
    bundle = native_module_path(
        ".native/preferences.js",
        "frontend/src/features/memory/preferences.ts",
    )
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(ROOT), bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, (
        f"preferences remove harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_trash_click_deletes_persisted_item_on_the_server():
    result = _run_remove_scenarios()
    assert result["loaded"] == {"articles": 2, "saveDisabled": True}
    assert result["removed"] == {
        "removeCalls": ["ctx_a"],
        "articles": 1,
        "saveDisabled": True,
        "dirtyText": "",
        "statusClass": "ctx-status",
    }


def test_failed_delete_keeps_the_item_and_surfaces_an_error():
    result = _run_remove_scenarios()
    assert result["failed"]["removeCalls"] == ["ctx_a", "ctx_b"]
    assert result["failed"]["articles"] == 1
    assert result["failed"]["statusClass"] == "ctx-status is-error"
    assert "删除失败" in result["failed"]["statusText"]


def test_unsaved_row_is_removed_locally_without_a_server_call():
    result = _run_remove_scenarios()
    assert result["unsaved"] == {
        "removeCalls": ["ctx_a", "ctx_b"],
        "articles": 1,
    }
