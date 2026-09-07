"""Executable contract for the composer-to-turn authority boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path, runtime_section


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SEND_SOURCE = ROOT / "frontend/src/runtime/sections/main/main_send_pipeline.js"
SEND_STARTUP_SOURCE = ROOT / "frontend/src/core/send-startup.ts"
TITLE_LIFECYCLE = str(Path(JS_DIR) / "main" / "main_conv_lifecycle.js")

NODE_RUNNER = r"""
const fs = require('fs');
const vm = require('vm');

const sendSource = fs.readFileSync(process.argv[1], 'utf8');
const startupSource = fs.readFileSync(process.argv[2], 'utf8');
const exercise = process.argv[3];
const context = vm.createContext({
  console,
  AbortController,
  AbortSignal,
  DOMException,
  setTimeout,
  clearTimeout,
});
vm.runInContext(
  'globalThis.window = globalThis; globalThis.runtimeScope = globalThis;',
  context,
);

(async () => {
  vm.runInContext(startupSource, context, { filename: process.argv[2] });
  vm.runInContext(sendSource, context, { filename: process.argv[1] });
  await vm.runInContext(`(async () => {\n${exercise}\n})()`, context, {
    filename: 'authoritative-composer.exercise.js',
  });
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
"""

HARNESS_SETUP = r"""
function check(condition, message) {
  if (!condition) throw new Error(message);
}

const composerInput = { value: '', style: { height: '44px' } };
const pdfProgress = { style: { display: 'block' } };
const composerConv = {
  id: 'conv-a', title: 'Existing conversation', createdAt: 1, updatedAt: 1,
};
const debugEntries = [];
const toastEntries = [];
const abortMarkers = [];
const authorityQueue = [];
let pendingReplyQuotes = [];
let pendingConvRefs = [];
let activeAttempt = false;
let clientId = 0;
let followLatestCalls = 0;

document = {
  getElementById(id) {
    if (id === 'userInput') return composerInput;
    if (id === 'pdfProgress') return pdfProgress;
    return null;
  },
};
sessionStorage = { setItem() {} };
pendingImages = [];
pendingPdfTexts = [];
pendingVideos = [];
_pendingLogClean = null;
imageGenMode = false;
planMode = false;
projectState = { active: false, path: null, extraRoots: [] };
conversations = [composerConv];
activeConvId = composerConv.id;

getActiveConv = () => composerConv;
getActiveFolderId = () => null;
getPendingReplyQuotes = () => pendingReplyQuotes;
getPendingConvRefs = () => pendingConvRefs;
consumePendingReplyQuotes = (captured) => {
  pendingReplyQuotes = pendingReplyQuotes.filter(
    (item) => !captured.includes(item),
  );
};
consumePendingConvRefs = (captured) => {
  pendingConvRefs = pendingConvRefs.filter((item) => !captured.includes(item));
};
isBranchModeActive = () => false;
_newClientMsgId = () => `client-${++clientId}`;
_waitForPendingVideos = async () => {};
_waitForImageProcessing = async () => {};
_waitForVlmParsing = async () => {};
_buildConvSubmission = async () => ({
  config: { autoTranslate: false }, settings: {},
});
buildTurnSubmissionExtra = (options) => ({
  ...options,
  requestOptions: { signal: options.signal },
});
stripNoTranslateTags = (value) => value;
_videoPayloadForSend = (video) => video;
renderImagePreviews = () => {};
_vlmSaveState = () => {};
_vlmClearState = () => {};
_renderTranslatingBubble = () => {};
_removeTranslatingBubble = () => {};
updateSendButton = () => {};
renderConversationList = () => {};
buildTurnNav = () => {};
captureActiveConversationSettings = () => {};
debugLog = (...entry) => debugEntries.push(entry);
showToast = (...entry) => toastEntries.push(entry);
t = (key) => key;
Api = { chat: { abortConv: async (convId) => abortMarkers.push(convId) } };

runtimeScope.buildTurnCtxSnapshot = () => null;
runtimeScope.updateContextBar = () => {};
runtimeScope.requestAuthoritativeConversationRender = () => {};
runtimeScope.ConversationSurfacePresentation = {
  followLatest() { followLatestCalls += 1; },
};
const optimisticTurnUpserts = [];
const optimisticTurnRemovals = [];
createOptimisticTurnPair = (input) => {
  const inputTurn = {
    turnId: 'transient:outgoing:' + input.commandId,
    presentationId: input.commandId + ':input',
    conversationId: input.conversationId,
    laneId: 'main', parentTurnId: null,
    ordinal: Number.MAX_SAFE_INTEGER,
    actor: 'human', kind: 'input', runId: '', status: 'completed',
    currentAttemptId: null,
    projection: { content: input.text, timestamp: input.timestamp },
    projectionRevision: 1, settlement: {},
    createdAt: input.timestamp, updatedAt: input.timestamp,
    _input: input,
  };
  return {
    inputTurn,
    outputTurn: {
      turnId: 'transient:outgoing:' + input.commandId + ':output',
      presentationId: input.commandId + ':output',
      conversationId: input.conversationId,
      laneId: 'main', parentTurnId: inputTurn.turnId,
      ordinal: Number.MAX_SAFE_INTEGER,
      actor: 'assistant', kind: 'reply', runId: '', status: 'pending',
      currentAttemptId: 'transient:attempt:' + input.commandId,
      projection: { segments: [], timestamp: input.timestamp },
      projectionRevision: 1, settlement: {},
      createdAt: input.timestamp, updatedAt: input.timestamp,
    },
  };
};
restorePendingReplyQuotes = (quotes) => {
  const missing = (quotes || []).filter((q) => !pendingReplyQuotes.includes(q));
  if (missing.length) pendingReplyQuotes.unshift(...missing);
};
restorePendingConvRefs = (refs) => {
  const missing = (refs || []).filter((ref) => ref
    && !pendingConvRefs.some((c) => c.id === ref.id && c.title === ref.title));
  if (missing.length) pendingConvRefs.unshift(...missing);
};
runtimeScope.ConversationTransientTurns = {
  upsert(conv, turn) {
    optimisticTurnUpserts.push({ convId: conv.id, turn });
    return true;
  },
  remove(conv, turnId) {
    optimisticTurnRemovals.push({ convId: conv.id, turnId });
    return true;
  },
};
runtimeScope.ConversationTurnRead = {
  activeMainAttemptId: () => activeAttempt ? 'attempt-main' : null,
  hasActor: () => true,
  ordered: () => [],
};
runtimeScope.ConversationTurnStore = {
  async hydrateConversation() {},
  ensureRuntimeStore() {
    return { getState: () => ({ queueItems: authorityQueue }) };
  },
  hasAuthoritativeCommand(conversationId, commandId) {
    const state = this.ensureRuntimeStore(conversationId).getState();
    return (state.queueItems || []).some(
      (item) => item?.sourceMessageId === commandId,
    ) || Object.values(state.attemptsById || {}).some(
      (attempt) => attempt?.commandId === commandId,
    );
  },
  async abortConversation() {},
  async submitConversation() {
    throw new Error('scenario did not install submitConversation');
  },
};
"""


def _run_pipeline(exercise: str) -> None:
    startup_bundle = native_module_path(
        ".native/send-startup.js",
        SEND_STARTUP_SOURCE,
    )
    completed = subprocess.run(
        [
            "node",
            "-e",
            NODE_RUNNER,
            str(SEND_SOURCE),
            startup_bundle,
            HARNESS_SETUP + "\n" + exercise,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_send_pipeline_never_writes_unacknowledged_transcript_rows():
    source = SEND_SOURCE.read_text(encoding="utf-8")

    submit = source.index("submitConversation(")
    consume = source.index("_consumeAcceptedComposerDraft(draft)", submit)
    assert submit < consume
    submit_body = source.index("async function _submitComposerDraft")
    optimistic_clear = source.index(
        "_clearCapturedComposerDraft(draft)", submit_body)
    optimistic_echo = source.index("_showOptimisticUserTurn(", submit_body)
    submit_call = source.index("submitConversation(", submit_body)
    assert optimistic_clear < optimistic_echo < submit_call
    for retired_patch in (
        "conv.messages.push",
        "conv.messages.splice",
        "persistConversationSettings",
        "_sendInFlight",
        "_droppedTurnDraft",
        "_pendingQueued",
        "activeTaskId",
        "activeStreams",
    ):
        assert retired_patch not in source



def test_send_intent_during_slow_ack_is_drained_instead_of_silently_dropped():
    _run_pipeline(r"""
let releaseFirstAcknowledgement;
const submissions = [];

runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  submissions.push({ text: payload.text, commandId: extra.commandId });
  if (submissions.length === 1) {
    await new Promise((resolve) => { releaseFirstAcknowledgement = resolve; });
  }
  return { submittedTurn: { turnId: 'turn-' + submissions.length } };
};

composerInput.value = 'first message';
const firstSend = sendMessage();
while (!releaseFirstAcknowledgement) await Promise.resolve();
check(composerInput.value === '', 'first draft was not captured before ACK wait');

composerInput.value = 'second message';
await Promise.all([sendMessage(), sendMessage()]);
check(submissions.length === 1,
  'trailing commands started concurrently instead of waiting for the first ACK');

releaseFirstAcknowledgement();
await firstSend;

check(submissions.length === 2,
  'send intent pressed during the ACK wait was silently dropped');
check(submissions[1].text === 'second message',
  'drained send intent did not capture the current composer draft');
check(submissions[0].commandId !== submissions[1].commandId,
  'independent drafts reused one idempotency command id');
check(composerInput.value === '',
  'accepted drained draft remained in the composer');
""")



def test_steer_never_creates_a_provisional_transcript_turn():
    _run_pipeline(r"""
activeAttempt = true;
showChoice = async () => 'steer';
let submittedExtra;
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  submittedExtra = extra;
  return { steered: true, latestTurn: { turnId: 'turn-live' } };
};

composerInput.value = 'add this constraint';
await _submitComposerDraft();

check(submittedExtra.injectMode === 'steer',
  'steer authority was not sent to the command boundary');
check(optimisticTurnUpserts.length === 0,
  'steer painted a provisional user or assistant turn');
check(optimisticTurnRemovals.length === 0,
  'steer attempted to remove transcript turns it must never create');
check(composerInput.value === '',
  'accepted steer remained in the composer');
""")


def test_locked_send_intent_stops_at_an_uncertain_failure_boundary():
    _run_pipeline(r"""
let releaseFirstAcknowledgement;
let submissionCount = 0;

runtimeScope.ConversationTurnStore.submitConversation = async () => {
  submissionCount += 1;
  if (submissionCount === 1) {
    await new Promise((resolve) => { releaseFirstAcknowledgement = resolve; });
    throw new Error('connection lost before acknowledgement');
  }
  return { submittedTurn: { turnId: 'turn-after-explicit-retry' } };
};

composerInput.value = 'first message';
const firstSend = sendMessage();
while (!releaseFirstAcknowledgement) await Promise.resolve();

composerInput.value = 'second message';
await sendMessage();
releaseFirstAcknowledgement();
await firstSend;

check(submissionCount === 1,
  'an uncertain failure automatically drained the trailing send intent');
check(composerInput.value === 'second message\n\nfirst message',
  'failure did not preserve later composer input before the restored draft');

await sendMessage();
check(submissionCount === 2,
  'the serializer stayed latched after an uncertain failure');
check(composerInput.value === '',
  'the explicit successful retry did not consume the restored draft');
""")
TITLE_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let turns = [
  { turnId: 'human-1', laneId: 'main', actor: 'human', status: 'completed' },
  { turnId: 'planner-1', laneId: 'main', actor: 'planner', status: 'completed' },
];
let generateCalls = 0;
let saves = 0;
let renders = 0;
const conversation = { id: 'conv-title', title: 'Question text' };
const conversationRows = [conversation];
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body><div id="topbarTitle"></div></body>',
  targets: [process.argv[2]],
  globals: {
    ConversationTurnRead: { ordered: () => turns },
    conversations: conversationRows,
    activeConvId: conversation.id,
    config: { autoGenerateTitle: true },
    Api: { conversations: { generateTitle: async (conversationId) => {
      generateCalls += 1;
      return { title: conversationId === conversation.id
        ? 'Generated plan title' : 'Generated chat title' };
    } } },
    reconcileConversationCatalogMetadata: () => { saves += 1; },
    renderConversationList: () => { renders += 1; },
  },
});

(async () => {
  check('Failed output settlement is not eligible',
    window._maybeAutoGenerateTitleForSettledTurn(conversation, {
      laneId: 'main', actor: 'assistant', status: 'failed',
    }) === false);
  check('Branch output settlement is not eligible',
    window._maybeAutoGenerateTitleForSettledTurn(conversation, {
      laneId: 'branch-a', actor: 'assistant', status: 'completed',
    }) === false);
  check('Planner output settlement is eligible',
    window._maybeAutoGenerateTitleForSettledTurn(conversation, {
      laneId: 'main', actor: 'planner', status: 'completed',
    }) === true);
  await Promise.resolve();
  await Promise.resolve();
  check('Planner first turn generates and paints one authoritative title',
    generateCalls === 1 && conversation.title === 'Generated plan title'
    && document.getElementById('topbarTitle').textContent === 'Generated plan title'
    && saves === 1 && renders === 1);
  const assistantConversation = {
    id: 'conv-assistant-title', title: 'Second question',
  };
  conversationRows.push(assistantConversation);
  turns = [
    { turnId: 'human-2', laneId: 'main', actor: 'human', status: 'completed' },
    { turnId: 'assistant-2', laneId: 'main', actor: 'assistant', status: 'completed' },
  ];
  check('Assistant output settlement shares the same eligibility rule',
    window._maybeAutoGenerateTitleForSettledTurn(assistantConversation, {
      laneId: 'main', actor: 'assistant', status: 'completed',
    }) === true);
  await Promise.resolve();
  await Promise.resolve();
  check('Assistant first turn also persists one generated title',
    generateCalls === 2
    && assistantConversation.title === 'Generated chat title'
    && saves === 2 && renders === 2);
  window._maybeAutoGenerateTitleForSettledTurn(conversation, {
    laneId: 'main', actor: 'planner', status: 'completed',
  });
  await Promise.resolve();
  check('Attempt-once guard prevents duplicate title requests',
    generateCalls === 2);
  report();
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
"""


def test_planner_and_assistant_outputs_share_the_auto_title_lifecycle():
    run_harness(
        TITLE_LIFECYCLE,
        TITLE_HARNESS,
        expect_pass=7,
        label="conversation title lifecycle",
    )


def test_turn_store_wires_the_terminal_title_callback():
    source = runtime_section('main/conversation_turn_store.js')
    assert 'onTurnSettled(conv, turn)' in source
    assert (
        'runtimeScope._maybeAutoGenerateTitleForSettledTurn?.(conv, turn)'
        in source
    )


def test_accepted_submit_consumes_only_the_exact_captured_draft():
    _run_pipeline(r"""
const sentImage = { name: 'sent.png' };
const addedImage = { name: 'added.png' };
const sentPdf = { name: 'sent.pdf' };
const addedPdf = { name: 'added.pdf' };
const sentVideo = { name: 'sent.mp4' };
const addedVideo = { name: 'added.mp4' };
const sentQuote = { text: 'sent quote' };
const addedQuote = { text: 'added quote' };
const sentRef = { id: 'sent-ref' };
const addedRef = { id: 'added-ref' };
const submissions = [];

composerInput.value = 'captured draft';
pendingImages = [sentImage];
pendingPdfTexts = [sentPdf];
pendingVideos = [sentVideo];
pendingReplyQuotes = [sentQuote];
pendingConvRefs = [sentRef];
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  submissions.push({ conv, payload, config, extra });
  composerInput.value = 'edited while sending';
  composerInput.style.height = '72px';
  pendingImages.push(addedImage);
  pendingPdfTexts.push(addedPdf);
  pendingVideos.push(addedVideo);
  pendingReplyQuotes.push(addedQuote);
  pendingConvRefs.push(addedRef);
  return { submittedTurn: { id: 'turn-1' } };
};

await _submitComposerDraft();

check(submissions.length === 1, 'submitConversation was not called once');
const submitted = submissions[0];
check(submitted.conv === composerConv, 'the active conversation was not submitted');
check(submitted.payload.text === 'captured draft', 'submitted text changed');
check(submitted.payload._msgId === submitted.extra.commandId,
  'payload and command envelope used different command ids');
check(submitted.payload.images.length === 1
  && submitted.payload.images[0] === sentImage, 'submitted images changed');
check(submitted.payload.pdfTexts.length === 1
  && submitted.payload.pdfTexts[0] === sentPdf, 'submitted PDFs changed');
check(submitted.payload.videos.length === 1
  && submitted.payload.videos[0] === sentVideo, 'submitted videos changed');
check(composerInput.value === 'edited while sending', 'ack erased newer text');
check(composerInput.style.height === '72px', 'ack reset the edited input height');
check(pendingImages.length === 1 && pendingImages[0] === addedImage,
  'ack did not preserve the newly attached image');
check(pendingPdfTexts.length === 1 && pendingPdfTexts[0] === addedPdf,
  'ack did not preserve the newly attached PDF');
check(pendingVideos.length === 1 && pendingVideos[0] === addedVideo,
  'ack did not preserve the newly attached video');
check(pendingReplyQuotes.length === 1 && pendingReplyQuotes[0] === addedQuote,
  'ack did not preserve the newer reply quote');
check(pendingConvRefs.length === 1 && pendingConvRefs[0] === addedRef,
  'ack did not preserve the newer conversation reference');
check(composerConv._genStartCtrl === null && composerConv._genStartStop === null,
  'successful submit left startup markers behind');
check(followLatestCalls === 1,
  'accepted submit did not follow the authoritative tail exactly once');
""")


def test_unified_media_refs_replace_inline_pdf_and_video_payloads():
    _run_pipeline(r"""
const documentRef = {
  attachmentId: 'doc-1', kind: 'document', name: 'report.pdf', status: 'ready',
};
const videoRef = {
  attachmentId: 'video-1', kind: 'video', name: 'demo.mp4',
  status: 'processing', _status: 'processing',
};
composerInput.value = 'use both attachments';
pendingPdfTexts = [documentRef];
pendingVideos = [videoRef];
let submitted = null;
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  submitted = payload;
  return { submittedTurn: { id: 'turn-media' } };
};

await _submitComposerDraft();

check(submitted.attachments.length === 2, 'media refs were not unified');
check(submitted.attachments[0].attachmentId === 'doc-1',
  'document ref changed');
check(submitted.attachments[1].attachmentId === 'video-1',
  'processing video ref was dropped');
check(!('pdfTexts' in submitted), 'new document replayed legacy pdfTexts');
check(!('videos' in submitted), 'new video replayed legacy videos payload');
check(optimisticTurnUpserts[0].turn._input.attachments.length === 2,
  'optimistic turn did not render unified refs');
""")


def test_ambiguous_ack_keeps_draft_and_reuses_command_id_on_safe_retry():
    _run_pipeline(r"""
const sentImage = { name: 'retry.png' };
const commandIds = [];
let submitCount = 0;

composerInput.value = 'retry this exact draft';
pendingImages = [sentImage];
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  submitCount += 1;
  commandIds.push(extra.commandId);
  check(payload._msgId === extra.commandId, 'retry envelope lost idempotency key');
  if (submitCount === 1) throw new TypeError('network closed before ACK');
  return { submittedTurn: { id: 'turn-after-retry' } };
};

await _submitComposerDraft();
check(followLatestCalls === 0,
  'ambiguous uncommitted response moved the viewport before authority resolved');
check(composerInput.value === 'retry this exact draft',
  'ambiguous ACK cleared unsafely retained text');
check(pendingImages.length === 1 && pendingImages[0] === sentImage,
  'ambiguous ACK cleared an unsafely retained attachment');
check(abortMarkers.length === 0, 'network ambiguity wrote a stop marker');
check(composerConv._genStartCtrl === null && composerConv._genStartStop === null,
  'ambiguous ACK left startup markers behind');

await _submitComposerDraft();
check(commandIds.length === 2, 'safe retry did not resubmit');
check(commandIds[0] === commandIds[1],
  'unchanged retry minted a different command id');
check(composerInput.value === '', 'accepted retry did not consume its text');
check(pendingImages.length === 0,
  'accepted retry did not consume its attachment');
check(followLatestCalls === 1,
  'accepted retry did not follow the authoritative tail exactly once');
""")


def test_ambiguous_ack_adopts_authoritative_command_and_follows_latest():
    _run_pipeline(r"""
composerInput.value = 'committed before connection closed';
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  authorityQueue.push({sourceMessageId:extra.commandId});
  throw new TypeError('connection closed after durable commit');
};

await _submitComposerDraft();

check(composerInput.value === '',
  'authoritatively visible command left a duplicate draft');
check(followLatestCalls === 1,
  'ambiguous committed recovery did not follow the authoritative tail');
check(toastEntries.length === 0,
  'authoritatively recovered commit was presented as a send failure');
check(composerConv._genStartCtrl === null && composerConv._genStartStop === null,
  'authoritatively recovered commit left startup markers behind');
""")


def test_explicit_aborted_ack_keeps_draft_but_retires_command_id():
    _run_pipeline(r"""
const sentImage = { name: 'aborted.png' };
const commandIds = [];
let submitCount = 0;

composerInput.value = 'server rejected before creation';
pendingImages = [sentImage];
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  submitCount += 1;
  commandIds.push(extra.commandId);
  return submitCount === 1
    ? { aborted: true }
    : { submittedTurn: { id: 'turn-after-abort' } };
};

await _submitComposerDraft();
check(composerInput.value === 'server rejected before creation',
  'aborted acknowledgement cleared text');
check(pendingImages.length === 1 && pendingImages[0] === sentImage,
  'aborted acknowledgement cleared an attachment');
check(composerConv._genStartCtrl === null && composerConv._genStartStop === null,
  'aborted acknowledgement left startup markers behind');

await _submitComposerDraft();
check(commandIds.length === 2, 'post-abort retry was not submitted');
check(commandIds[0] !== commandIds[1],
  'definitively aborted command id was reused');
check(composerInput.value === '' && pendingImages.length === 0,
  'accepted post-abort retry did not consume its draft');
""")


def test_user_stop_before_commit_rolls_back_markers_and_preserves_draft():
    _run_pipeline(r"""
const sentPdf = { name: 'stop.pdf' };

composerInput.value = '停止前保留草稿';
pendingPdfTexts = [sentPdf];
_buildConvSubmission = async () => ({
  config: { autoTranslate: true }, settings: {},
});
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  const controller = composerConv._genStartCtrl;
  check(controller instanceof AbortController, 'startup controller was not owned');
  composerConv._genStartStop = controller;
  composerConv._genStartCtrl = null;
  composerConv._translateAborted = true;
  controller.abort();
  throw new DOMException('stopped by user', 'AbortError');
};

await _submitComposerDraft();

check(composerInput.value === '停止前保留草稿',
  'pre-commit stop cleared text');
check(pendingPdfTexts.length === 1 && pendingPdfTexts[0] === sentPdf,
  'pre-commit stop cleared an attachment');
check(abortMarkers.length === 1 && abortMarkers[0] === composerConv.id,
  'pre-commit stop did not persist the conversation abort marker');
check(composerConv._genStartCtrl === null && composerConv._genStartStop === null,
  'pre-commit stop left generation-start markers behind');
check(composerConv._translateAbortCtrl === null
  && composerConv._translateAborted === false
  && composerConv._translating === false,
  'pre-commit stop left translation markers behind');
check(toastEntries.length === 0, 'user stop was presented as a send error');
""")


def test_user_stop_racing_an_accepted_ack_aborts_the_authoritative_attempt():
    _run_pipeline(r"""
let authorityAbortCount = 0;

composerInput.value = 'accepted while stop raced';
runtimeScope.ConversationTurnStore.abortConversation = async (conv) => {
  check(conv === composerConv, 'abort targeted the wrong conversation');
  authorityAbortCount += 1;
};
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  const controller = composerConv._genStartCtrl;
  composerConv._genStartStop = controller;
  composerConv._genStartCtrl = null;
  controller.abort();
  activeAttempt = true;
  return { submittedTurn: { id: 'turn-stop-race' } };
};

await _submitComposerDraft();

check(authorityAbortCount === 1,
  'accepted turn was not aborted after a racing user stop');
check(abortMarkers.length === 0,
  'accepted turn incorrectly used the pre-commit abort marker');
check(composerInput.value === '', 'accepted turn did not consume its draft');
check(composerConv._genStartCtrl === null && composerConv._genStartStop === null,
  'accepted stop race left generation-start markers behind');
""")


def test_send_clears_composer_and_echoes_user_turn_before_ack():
    _run_pipeline(r"""
const observed = {};

composerInput.value = 'echo me immediately';
pendingImages = [{ name: 'now.png' }];
pendingReplyQuotes = ['quoted line'];
pendingConvRefs = [{ id: 'ref-now', title: 'Referenced' }];
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  const echo = optimisticTurnUpserts[0]?.turn;
  Object.assign(observed, {
    inputCleared: composerInput.value === '',
    heightReset: composerInput.style.height === 'auto',
    imagesCleared: pendingImages.length === 0,
    quotesCleared: pendingReplyQuotes.length === 0,
    refsCleared: pendingConvRefs.length === 0,
    echoCount: optimisticTurnUpserts.length,
    echoActor: echo?.actor,
    echoKind: echo?.kind,
    echoStatus: echo?.status,
    echoText: echo?.projection?.content,
    echoTurnId: echo?.turnId,
    echoUsesCommandId: echo?._input?.commandId === extra.commandId,
    echoImages: echo?._input?.images?.length,
    echoQuotes: echo?._input?.replyQuotes?.length,
    echoRefs: echo?._input?.convRefs?.length,
    removalsBeforeAck: optimisticTurnRemovals.length,
  });
  return { submittedTurn: { id: 'turn-1' } };
};

await _submitComposerDraft();

check(observed.inputCleared === true,
  'composer text was not cleared before the command round trip');
check(observed.heightReset === true,
  'composer height was not reset before the command round trip');
check(observed.imagesCleared === true && observed.quotesCleared === true
  && observed.refsCleared === true,
  'captured attachments were not cleared before the command round trip');
check(observed.echoCount === 2,
  'optimistic user/assistant pair was not painted before the command round trip');
check(observed.echoActor === 'human' && observed.echoKind === 'input'
  && observed.echoStatus === 'completed',
  'optimistic echo did not mirror the authoritative human turn identity');
check(observed.echoText === 'echo me immediately',
  'optimistic echo did not carry the submitted text');
check(observed.echoUsesCommandId === true,
  'optimistic echo did not reuse the idempotency command id');
check(observed.echoImages === 1 && observed.echoQuotes === 1
  && observed.echoRefs === 1,
  'optimistic echo dropped submitted attachments');
check(observed.removalsBeforeAck === 0,
  'optimistic echo was removed before the acknowledgement');
check(optimisticTurnRemovals.length === 2
  && optimisticTurnRemovals[0].turnId === observed.echoTurnId,
  'accepted acknowledgement did not remove exactly the optimistic pair');
check(composerInput.value === '' && pendingImages.length === 0,
  'accepted acknowledgement left a duplicate draft behind');
""")


def test_uncommitted_failure_restores_draft_and_removes_echo():
    _run_pipeline(r"""
const sentImage = { name: 'restore.png' };
const sentPdf = { name: 'restore.pdf' };
const sentVideo = { name: 'restore.mp4' };

composerInput.value = 'restore me after failure';
pendingImages = [sentImage];
pendingPdfTexts = [sentPdf];
pendingVideos = [sentVideo];
pendingReplyQuotes = ['quote-restore'];
pendingConvRefs = [{ id: 'ref-restore', title: 'Restore Ref' }];
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  throw new TypeError('network closed before ACK');
};

await _submitComposerDraft();

check(optimisticTurnUpserts.length === 2,
  'failed send never painted the optimistic pair');
check(optimisticTurnRemovals.length === 2
  && optimisticTurnRemovals[0].turnId === optimisticTurnUpserts[0].turn.turnId,
  'failed send did not remove exactly the optimistic pair');
check(composerInput.value === 'restore me after failure',
  'failed send did not restore the captured text');
check(pendingImages.length === 1 && pendingImages[0] === sentImage,
  'failed send did not restore the captured image');
check(pendingPdfTexts.length === 1 && pendingPdfTexts[0] === sentPdf,
  'failed send did not restore the captured PDF');
check(pendingVideos.length === 1 && pendingVideos[0] === sentVideo,
  'failed send did not restore the captured video');
check(pendingReplyQuotes.length === 1
  && pendingReplyQuotes[0] === 'quote-restore',
  'failed send did not restore the captured reply quote');
check(pendingConvRefs.length === 1 && pendingConvRefs[0].id === 'ref-restore',
  'failed send did not restore the captured conversation reference');
""")


def test_failure_restore_preserves_content_added_while_in_flight():
    _run_pipeline(r"""
const sentImage = { name: 'sent.png' };
const addedImage = { name: 'added.png' };

composerInput.value = 'captured before failure';
pendingImages = [sentImage];
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  composerInput.value = 'typed while sending';
  pendingImages.push(addedImage);
  throw new TypeError('network closed before ACK');
};

await _submitComposerDraft();

check(composerInput.value === 'typed while sending\n\ncaptured before failure',
  'failure restore clobbered text typed while the send was in flight');
check(pendingImages.length === 2 && pendingImages[0] === addedImage
  && pendingImages[1] === sentImage,
  'failure restore clobbered attachments added while in flight');
""")


def test_preparation_bubble_teardown_survives_a_mid_send_conversation_switch():
    _run_pipeline(r"""
const removalTargets = [];

composerInput.value = '这条消息需要翻译';
_buildConvSubmission = async () => ({
  config: { autoTranslate: true }, settings: {},
});
_removeTranslatingBubble = (conversationId) => {
  removalTargets.push(conversationId);
};
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  activeConvId = 'conv-elsewhere';
  return { submittedTurn: { id: 'turn-backgrounded' } };
};

await _submitComposerDraft();

check(removalTargets.length === 1 && removalTargets[0] === composerConv.id,
  'send-preparation bubble leaked when the conversation switched mid-send');
""")


def test_send_preparation_merges_into_the_optimistic_assistant_bubble():
    _run_pipeline(r"""
const statusBubbleCalls = [];
const phaseUpdates = [];

withOptimisticAssistantPreparation = (turn, phase, label) => ({
  ...turn,
  transientPresentation: { kind: 'preparation', phase, label, detail: '' },
});
runtimeScope.ConversationTransientTurns.get = (conversationId, turnId) => {
  const found = [...optimisticTurnUpserts].reverse().find(
    (entry) => entry.convId === conversationId
      && entry.turn.turnId === turnId);
  return found ? found.turn : null;
};
const baseUpsert = runtimeScope.ConversationTransientTurns.upsert;
runtimeScope.ConversationTransientTurns.upsert = (conv, turn) => {
  if (turn.transientPresentation) {
    phaseUpdates.push({
      turnId: turn.turnId,
      phase: turn.transientPresentation.phase,
      label: turn.transientPresentation.label,
    });
  }
  return baseUpsert(conv, turn);
};
_renderTranslatingBubble = (label) => {
  statusBubbleCalls.push(label ?? null);
};

composerInput.value = '这条消息需要翻译';
_buildConvSubmission = async () => ({
  config: { autoTranslate: true }, settings: {},
});
runtimeScope.ConversationTurnStore.submitConversation = async () => (
  { submittedTurn: { id: 'turn-merged' } }
);

await _submitComposerDraft();

const outputTurnId = optimisticTurnUpserts[1].turn.turnId;
check(statusBubbleCalls.length === 0,
  'send preparation stacked a second status bubble beside the assistant turn');
check(phaseUpdates.length === 2
  && phaseUpdates.every((entry) => entry.turnId === outputTurnId),
  'preparation phases did not re-label the optimistic assistant turn in place');
check(phaseUpdates[0].phase === 'connecting'
  && phaseUpdates[0].label === 'sidebar.connecting',
  'connecting phase did not reach the optimistic assistant turn');
check(phaseUpdates[1].phase === 'translating'
  && phaseUpdates[1].label === 'sidebar.translating',
  'translating phase did not reach the optimistic assistant turn');
check(optimisticTurnRemovals.length === 2,
  'accepted acknowledgement did not remove exactly the optimistic pair');
""")


def test_steer_keeps_the_standalone_preparation_bubble():
    _run_pipeline(r"""
const statusBubbleCalls = [];
_renderTranslatingBubble = (label) => {
  statusBubbleCalls.push(label ?? null);
};
activeAttempt = true;
showChoice = async () => 'steer';
runtimeScope.ConversationTurnStore.submitConversation = async () => (
  { steered: true, latestTurn: { turnId: 'turn-live' } }
);

composerInput.value = 'add this constraint';
await _submitComposerDraft();

check(optimisticTurnUpserts.length === 0,
  'steer painted a provisional user or assistant turn');
check(statusBubbleCalls.length === 1
  && statusBubbleCalls[0] === 'sidebar.connecting',
  'steer lost its standalone send-preparation bubble');
""")
