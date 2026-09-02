"""Translation is deduplicated and presented by stable Turn identity."""

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


def test_translation_claim_and_pipeline_use_conversation_and_turn_id_only():
    guard = runtime_section("core/translate_guard.js", scope_prelude=False)
    pipeline = runtime_section("translation.js", scope_prelude=False)
    result = _run_node(r"""
const runtimeScope = globalThis;
const window = globalThis;
let now = 1000;
Date.now = () => now;
let pushHandler = null;
function pushSubscribe(channel, key, callback) {
  if (channel === 'translate') pushHandler = callback;
}
function setTimeout(callback) { callback(); return 1; }
const calls = {start:[], poll:0, hydrate:0, activity:[], toasts:[]};
const turn = {
  turnId:'turn-a', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', status:'completed', currentAttemptId:null,
  projectionRevision:1, projection:{content:'english answer'},
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
};
const conversation = {id:'conv-a'};
let conversations = [conversation];
const Api = {
  text:{detectLanguage:async () => ({detected:{code:'en'}})},
  translate:{
    start:async (payload) => {
      calls.start.push(payload);
      return {taskId:'task-a'};
    },
    poll:async () => {
      calls.poll += 1;
      return {taskId:'task-a', status:'done'};
    },
    pollBatch:async () => [],
  },
};
runtimeScope.ConversationTurnStore = {
  ensureRuntimeStore:() => ({getState:() => ({turnsById:{'turn-a':turn}})}),
  hydrateConversation:async () => { calls.hydrate += 1; },
  updateConversationTurn:async () => { throw new Error('unexpected skip'); },
};
runtimeScope.ConversationSurfacePresentation = {
  setTranslationActivity:(conversationId, turnId, activity) => {
    calls.activity.push([conversationId, turnId, activity?.status || null]);
  },
};
function showToast(message) { calls.toasts.push(message); }
function errorEnvelopeMessage() { return ''; }
""" + guard + "\n" + pipeline + r"""
return (async () => {
  const firstClaim = translateClaim('conv-a', 'turn-a');
  const duplicateClaim = translateClaim('conv-a', 'turn-a');
  const otherTurnClaim = translateClaim('conv-a', 'turn-b');
  const missingIdentityAllowed = translateClaim('conv-a', '');
  translateRelease('conv-a', 'turn-a');

  translateClaim('conv-a', 'turn-a');
  await _runTranslationPipeline(conversation, 'turn-a', {
    targetLang:'Chinese', sourceLang:'English', field:'translatedContent',
  });
  const startsWhileClaimed = calls.start.length;
  translateRelease('conv-a', 'turn-a');
  const before = JSON.stringify(turn);
  await _runTranslationPipeline(conversation, 'turn-a', {
    targetLang:'Chinese', sourceLang:'English', field:'translatedContent',
  });
  const sourceUnchanged = before === JSON.stringify(turn);
  await _runTranslationPipeline(conversation, 'missing-turn', {
    text:'missing authoritative turn', targetLang:'Chinese',
  });
  await pushHandler({convId:'conv-a', turnId:'turn-a', status:'done'});

  now += 180001;
  const staleClaimRecovered = translateClaim('conv-a', 'turn-b');
  return {
    firstClaim, duplicateClaim, otherTurnClaim, missingIdentityAllowed,
    startsWhileClaimed, sourceUnchanged, staleClaimRecovered,
    pushCaptured:typeof pushHandler === 'function',
    startPayload:calls.start[0],
    poll:calls.poll,
    hydrate:calls.hydrate,
    activity:calls.activity,
    toastCount:calls.toasts.length,
    released:!translateInflight('conv-a', 'turn-a'),
  };
})();
""")

    assert result["firstClaim"] is True
    assert result["duplicateClaim"] is False
    assert result["otherTurnClaim"] is True
    assert result["missingIdentityAllowed"] is True
    assert result["startsWhileClaimed"] == 0
    assert result["sourceUnchanged"] is True
    assert result["staleClaimRecovered"] is True
    assert result["pushCaptured"] is True
    assert result["startPayload"] == {
        "text": "english answer",
        "targetLang": "Chinese",
        "sourceLang": "English",
        "convId": "conv-a",
        "turnId": "turn-a",
        "field": "translatedContent",
    }
    assert result["poll"] == 1
    assert result["hydrate"] == 2
    assert result["toastCount"] == 1
    assert result["released"] is True
    assert result["activity"] == [
        ["conv-a", "turn-a", "pending"],
        ["conv-a", "turn-a", None],
        # A missing/deleted authoritative Turn gets an idempotent clear only;
        # it must never leave presentation activity behind.
        ["conv-a", "missing-turn", None],
        # The durable done push is independently safe to replay and clears the
        # exact Turn again after hydration.
        ["conv-a", "turn-a", None],
    ]


def test_translation_sources_have_no_positional_fallback_or_document_write():
    guard = runtime_section("core/translate_guard.js")
    pipeline = runtime_section("translation.js")
    assert "_translateGuardKey(convId, turnId)" in guard
    assert "_translationTaskKey(convId, turnId)" in pipeline
    for retired in ("msgIdx", "messageIndex", "conv.messages", ".messages["):
        assert retired not in guard
        assert retired not in pipeline
