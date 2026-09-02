"""Conversation Sync invalidations wake one authoritative TurnStore owner."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit


def _run_node(source: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("node is required")
    script = f"""
const run = new Function({json.dumps(source)});
run().then((value) => console.log(JSON.stringify(value))).catch((error) => {{
  console.error(error?.stack || error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_resume_is_visible_lease_bounded_and_turn_store_authoritative():
    owner = runtime_section(
        "core/conversation_invalidation.js", scope_prelude=False,
    )
    result = _run_node(r"""
const runtimeScope = globalThis;
const TAB_ID = 'tab-a';
let conversations = [{id:'conv-a'}];
let activeConvId = 'conv-a';
const calls = {identity:0, list:0, hydrate:0, invalidate:[]};
const listeners = {};
const document = {
  visibilityState:'visible',
  addEventListener:(name, callback) => { listeners['document:' + name] = callback; },
};
const window = {
  addEventListener:(name, callback) => { listeners['window:' + name] = callback; },
};
globalThis.BroadcastChannel = class {
  constructor() { this.onmessage = null; }
  postMessage() {}
  unref() {}
};
let timerId = 0;
function setTimeout(callback, delay) { return ++timerId; }
function clearTimeout() {}
let releaseList;
let listGate = new Promise((resolve) => { releaseList = resolve; });
function initCurrentUserId() { calls.identity += 1; return Promise.resolve(7); }
async function loadConversationCatalog() {
  calls.list += 1;
  await listGate;
}
function decodeConversationInvalidation(frame) { return frame; }
function _frameIsOurs() { return true; }
function debugLog() {}
function renderConversationList() {}
function loadConversation() {}
function newChat() {}
runtimeScope.ConversationTurnStore = {
  hydrateConversation: async (conversation) => {
    calls.hydrate += 1;
    return conversation;
  },
  invalidateConversation: (conversationId, cursorHint) => {
    calls.invalidate.push([conversationId, cursorHint]);
  },
  disposeConversation() {},
};
""" + owner + r"""
return (async () => {
  const first = _revalidateOnResume('online');
  const duplicate = _revalidateOnResume('visibilitychange');
  releaseList();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  document.visibilityState = 'hidden';
  const hidden = _crossDeviceReconcile();
  document.visibilityState = 'visible';

  runtimeScope._bootLoadInFlight = Date.now() - 50000;
  listGate = Promise.resolve();
  const staleLeaseRecovered = _revalidateOnResume('periodic');
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  _onConversationInvalidation({
    conversationId:'conv-a', cursorHint:'cursor-9', userId:7,
  });
  return {
    first, duplicate, hidden, staleLeaseRecovered,
    calls,
    leaseReleased:runtimeScope._bootLoadInFlight === 0,
    listeners:Object.keys(listeners).sort(),
  };
})();
""")

    assert result == {
        "first": True,
        "duplicate": False,
        "hidden": False,
        "staleLeaseRecovered": True,
        "calls": {
            "identity": 2,
            "list": 2,
            "hydrate": 2,
            "invalidate": [["conv-a", "cursor-9"]],
        },
        "leaseReleased": True,
        "listeners": [
            "document:visibilitychange",
            "window:online",
        ],
    }


def test_invalidation_owner_has_no_editor_or_transcript_gate():
    source = runtime_section("core/conversation_invalidation.js")
    assert "_editingMsgIdx" not in source
    assert "ConversationTurnStore?.invalidateConversation?." in source
    assert "ConversationTurnStore?.hydrateConversation?." in source
    assert "loadConversationCatalog()" in source
    assert ".messages" not in source
