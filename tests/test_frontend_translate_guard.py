"""Translation is deduplicated and presented by stable Turn identity."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
CLAIM_OWNER = ROOT / 'frontend/src/core/translation-claim-registry.ts'
CLAIM_OWNER_BUNDLE = native_module_path(
    '.native/translation-claim-integration.js', CLAIM_OWNER,
)


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
    guard = Path(CLAIM_OWNER_BUNDLE).read_text()
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
  const firstClaim = TRANSLATION_CLAIMS.claim('conv-a', 'turn-a');
  const duplicateClaim = TRANSLATION_CLAIMS.claim('conv-a', 'turn-a');
  const otherTurnClaim = TRANSLATION_CLAIMS.claim('conv-a', 'turn-b');
  const missingIdentityAllowed = TRANSLATION_CLAIMS.claim('conv-a', '');
  TRANSLATION_CLAIMS.release('conv-a', 'turn-a');

  TRANSLATION_CLAIMS.claim('conv-a', 'turn-a');
  await _runTranslationPipeline(conversation, 'turn-a', {
    targetLang:'Chinese', sourceLang:'English', field:'translatedContent',
  });
  const startsWhileClaimed = calls.start.length;
  TRANSLATION_CLAIMS.release('conv-a', 'turn-a');
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
  const staleClaimRecovered = TRANSLATION_CLAIMS.claim('conv-a', 'turn-b');
  return {
    firstClaim, duplicateClaim, otherTurnClaim, missingIdentityAllowed,
    startsWhileClaimed, sourceUnchanged, staleClaimRecovered,
    pushCaptured:typeof pushHandler === 'function',
    startPayload:calls.start[0],
    poll:calls.poll,
    hydrate:calls.hydrate,
    activity:calls.activity,
    toastCount:calls.toasts.length,
    released:!TRANSLATION_CLAIMS.isClaimed('conv-a', 'turn-a'),
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
    guard = CLAIM_OWNER.read_text()
    pipeline = runtime_section("translation.js")
    assert "createTranslationClaimRegistry" in guard
    assert "MAX_TRANSLATION_CLAIMS = 256" in guard
    assert "TRANSLATION_CLAIMS.claim(conv.id, turnId)" in pipeline
    assert "_translationTaskKey(convId, turnId)" in pipeline
    for retired in ("msgIdx", "messageIndex", "conv.messages", ".messages["):
        assert retired not in guard
        assert retired not in pipeline


def test_same_translation_task_has_one_poll_loop():
    pipeline = runtime_section("translation.js", scope_prelude=False)
    result = _run_node(r"""
const runtimeScope = globalThis;
const window = globalThis;
const conversation = {id:'conv-a'};
let conversations = [conversation];
function setTimeout(callback) { callback(); return 1; }
const calls = {poll:0, hydrate:0};
const Api = {
  translate:{
    poll:async () => {
      calls.poll += 1;
      return {taskId:'task-a', status:'done'};
    },
    pollBatch:async () => [],
  },
};
runtimeScope.ConversationTurnStore = {
  hydrateConversation:async () => { calls.hydrate += 1; },
};
runtimeScope.ConversationSurfacePresentation = {
  setTranslationActivity:() => {},
};
function errorEnvelopeMessage() { return ''; }
""" + pipeline + r"""
return (async () => {
  _translationTasksByTurn.set('conv-a:turn-a', {taskId:'task-a'});
  await Promise.all([
    _pollTranslationUntilSettled({
      conv:conversation, turnId:'turn-a', taskId:'task-a',
    }),
    _pollTranslationUntilSettled({
      conv:conversation, turnId:'turn-a', taskId:'task-a',
    }),
  ]);
  return {
    poll:calls.poll,
    hydrate:calls.hydrate,
    pollers:_translationPollersByTask.size,
  };
})();
""")

    assert result == {"poll": 1, "hydrate": 1, "pollers": 0}


def test_terminal_push_cancels_sleeping_translation_poll():
    pipeline = runtime_section("translation.js", scope_prelude=False)
    result = _run_node(r"""
const runtimeScope = globalThis;
const window = globalThis;
const conversation = {id:'conv-a'};
let conversations = [conversation];
let releaseTimer = null;
let pushHandler = null;
function setTimeout(callback) { releaseTimer = callback; return 1; }
function pushSubscribe(channel, key, callback) {
  if (channel === 'translate') pushHandler = callback;
}
const calls = {poll:0, hydrate:0};
const Api = {
  translate:{
    poll:async () => {
      calls.poll += 1;
      return {taskId:'task-a', status:'done'};
    },
    pollBatch:async () => [],
  },
};
runtimeScope.ConversationTurnStore = {
  hydrateConversation:async () => { calls.hydrate += 1; },
};
runtimeScope.ConversationSurfacePresentation = {
  setTranslationActivity:() => {},
};
function errorEnvelopeMessage() { return ''; }
""" + pipeline + r"""
return (async () => {
  _translationTasksByTurn.set('conv-a:turn-a', {taskId:'task-a'});
  const poller = _pollTranslationUntilSettled({
    conv:conversation, turnId:'turn-a', taskId:'task-a',
  });
  await Promise.resolve();
  await pushHandler({
    convId:'conv-a', turnId:'turn-a', taskId:'task-a', status:'done',
  });
  releaseTimer();
  await poller;
  return {
    poll:calls.poll,
    hydrate:calls.hydrate,
    pollers:_translationPollersByTask.size,
  };
})();
""")

    assert result == {"poll": 0, "hydrate": 1, "pollers": 0}
