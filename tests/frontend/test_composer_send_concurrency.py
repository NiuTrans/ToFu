"""Independent conversation sends retain owner-specific drafts and receipts."""
import pytest

from tests.frontend.test_frontend_authoritative_composer import _run_pipeline

pytestmark = pytest.mark.unit


def test_preparation_exception_is_visible_and_releases_the_composer():
    _run_pipeline(r"""
composerInput.value = 'keep this text';
pendingVideos = [{ id: 'video-a' }];
_waitForPendingVideos = async () => { throw new Error('Video processing failed'); };
await sendMessage();
check(composerInput.value === 'keep this text', 'preparation lost draft');
check(toastEntries.some(x => x[0].includes('Video processing failed') && x[1] === 'error'),
  'preparation failure was silent');
pendingVideos = [];
runtimeScope.ConversationTurnStore.submitConversation = async () => ({submittedTurn: {turnId: 'ok'}});
await sendMessage();
check(composerInput.value === '', 'failure latched the send lane');
""")


def test_slow_conversation_does_not_block_another_or_steal_its_draft():
    _run_pipeline(r"""
const other = {id: 'conv-b', title: 'B', createdAt: 1};
conversations.push(other);
getActiveConv = () => conversations.find(c => c.id === activeConvId);
let failA;
const commands = [];
runtimeScope.ConversationTurnStore.submitConversation = async (conv, payload, config, extra) => {
  commands.push([conv.id, extra.commandId]);
  if (commands.length === 1) await new Promise((resolve, reject) => { failA = reject; });
  return {submittedTurn: {turnId: conv.id}};
};
composerInput.value = 'A draft';
const sendingA = sendMessage();
while (!failA) await Promise.resolve();
activeConvId = other.id;
composerInput.value = 'B draft';
await sendMessage();
check(commands.length === 2, 'slow A blocked B');
composerInput.value = 'B next draft';
failA(new Error('ACK lost'));
await sendingA;
check(composerInput.value === 'B next draft', 'failed A changed B composer');
check(composerConv._unsentComposerDraft.inputValue === 'A draft', 'A draft lost after navigation');
activeConvId = composerConv.id;
composerInput.value = '';
_restoreCapturedComposerDraft(composerConv._unsentComposerDraft);
delete composerConv._unsentComposerDraft;
await sendMessage();
check(commands[0][1] === commands[2][1], 'sending B destroyed A retry identity');
""")


def test_navigation_during_preprocessing_never_submits_to_the_new_selection():
    _run_pipeline(r"""
let releaseMedia;
composerInput.value = 'belongs to A';
pendingVideos = [{ id: 'video-a' }];
_waitForPendingVideos = () => new Promise(resolve => { releaseMedia = resolve; });
let submissions = 0;
runtimeScope.ConversationTurnStore.submitConversation = async () => { submissions++; return {}; };
const sending = sendMessage();
while (!releaseMedia) await Promise.resolve();
activeConvId = 'conv-b';
composerInput.value = 'belongs to B';
releaseMedia();
await sending;
check(submissions === 0, 'navigation retargeted an earlier send');
check(composerInput.value === 'belongs to B', 'late preprocessing modified B');
""")


def test_503_is_retried_automatically_with_the_same_command_identity():
    _run_pipeline(r"""
const commands = [];
runtimeScope.ConversationTurnStore.submitConversation = async (conv, payload, config, extra) => {
  commands.push(extra.commandId);
  if (commands.length === 1) throw Object.assign(new Error('storage unavailable'), {status: 503});
  return {submittedTurn: {turnId: 'ok'}};
};
composerInput.value = 'retry this';
await sendMessage();
check(commands.length === 2 && commands[0] === commands[1], '503 retry minted a duplicate command');
check(composerInput.value === '', 'accepted draft returned to the composer');
check(!toastEntries.some(x => x[1] === 'error'), 'a transient 503 still asked for a manual retry');
check(debugEntries.some(x => String(x[0]).startsWith('Send retry #1') && x[1] === 'warn'),
  'automatic retry left no diagnostic trace');
""")


def test_transient_preparation_failure_retries_without_a_manual_resend():
    _run_pipeline(r"""
const signals = [];
_buildConvSubmission = async (conv, workspace, options) => {
  signals.push(options?.signal);
  if (signals.length < 3) throw Object.assign(new Error('timeout'), {status: 0, code: 'timeout'});
  return {config: {autoTranslate: false}, settings: {}};
};
const labels = [];
_renderTranslatingBubble = (label) => labels.push(label);
const submitted = [];
runtimeScope.ConversationTurnStore.submitConversation = async (conv, payload) => {
  submitted.push(payload.text);
  return {submittedTurn: {turnId: 'ok'}};
};
composerInput.value = 'survive the stall';
await sendMessage();
check(signals.length === 3 && signals.every(signal => signal instanceof AbortSignal),
  'config resolution attempts did not own a signal');
check(new Set(signals).size === 3, 'resolution attempts shared one signal');
check(submitted.length === 1 && submitted[0] === 'survive the stall', 'draft was not submitted exactly once');
check(labels.includes('send.retrying'), 'automatic retry was invisible');
check(composerInput.value === '', 'accepted draft returned to the composer');
check(!toastEntries.some(x => x[1] === 'error'), 'transient preparation failure asked for a manual retry');
""")


def test_stop_during_automatic_retry_restores_the_draft_without_an_error():
    _run_pipeline(r"""
const collapsedRetry = retryTransientSendStartup;
retryTransientSendStartup = (options) => collapsedRetry({ ...options, retryDelayMs: () => 60000 });
let reportFailure;
const failedOnce = new Promise(resolve => { reportFailure = resolve; });
const signals = [];
_buildConvSubmission = async (conv, workspace, options) => {
  signals.push(options.signal);
  reportFailure();
  throw Object.assign(new Error('bad gateway'), {status: 502});
};
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  throw new Error('a stopped send reached the command write');
};
composerInput.value = 'stop me';
const sending = sendMessage();
await failedOnce;
await new Promise(resolve => setTimeout(resolve, 0));
const controller = composerConv._genStartCtrl;
check(controller, 'a retrying send released its stop scope');
composerConv._genStartStop = controller;
composerConv._genStartCtrl = null;
controller.abort();
await sending;
check(signals.length === 1, 'a stopped send kept retrying');
check(composerInput.value === 'stop me', 'stopped send lost its draft');
check(abortMarkers.includes(composerConv.id), 'stop did not reach the server abort marker');
check(!toastEntries.some(x => x[1] === 'error'), 'user stop surfaced as an error');
""")


def test_definitive_rejection_is_not_retried():
    _run_pipeline(r"""
let attempts = 0;
runtimeScope.ConversationTurnStore.submitConversation = async () => {
  attempts += 1;
  throw Object.assign(new Error('invalid request'), {status: 400});
};
composerInput.value = 'reject me';
await sendMessage();
check(attempts === 1, 'a definitive rejection was retried');
check(composerInput.value === 'reject me', 'rejected draft was not restored');
check(toastEntries.some(x => x[1] === 'error'), 'definitive rejection was silent');
""")


def test_document_settled_during_send_joins_attachments_and_failure_is_dropped():
    _run_pipeline(r"""
const uploading = { name: 'notes.txt', text: '', pages: 0, textLength: 0, method: 'parsing' };
const failing = { name: 'broken.txt', text: '', pages: 0, textLength: 0, method: 'parsing' };
pendingPdfTexts = [uploading, failing];
let submitted;
runtimeScope.ConversationTurnStore.submitConversation = async (conv, payload) => {
  submitted = payload;
  return {submittedTurn: {turnId: 'ok'}};
};
_waitForVlmParsing = async (draft) => {
  check(draft.pdfTexts.length === 2, 'in-flight uploads were not part of the send wait');
  Object.assign(uploading, { attachmentId: 'att-notes', kind: 'document', method: '.txt' });
  failing.method = 'failed';
};
composerInput.value = 'read these';
await sendMessage();
check(submitted, 'send did not submit');
check(submitted.attachments?.length === 1
  && submitted.attachments[0].attachmentId === 'att-notes', 'settled upload missed the attachment lane');
check(!submitted.pdfTexts, 'an uploaded or failed document reached the legacy text lane');
check(toastEntries.some(x => x[0] === 'upload.documentSkipped'), 'failed upload was dropped silently');
""")


def test_send_waits_for_archive_mount_and_never_sends_it_as_a_document():
    _run_pipeline(r"""
const archive = { name: 'src.zip', text: '', pages: 0, textLength: 9, _archive: true, method: 'parsing' };
pendingPdfTexts = [archive];
const order = [];
_waitForPendingArchives = async () => { order.push('archive'); archive.method = 'archive'; };
let submitted;
runtimeScope.ConversationTurnStore.submitConversation = async (conv, payload) => {
  order.push('submit');
  submitted = payload;
  return {submittedTurn: {turnId: 'ok'}};
};
composerInput.value = 'unzip and summarize';
await sendMessage();
check(order.join() === 'archive,submit', 'turn was captured before the archive mounted: ' + order.join());
check(!submitted.pdfTexts && !submitted.attachments, 'archive chip reached a document lane');
""")


def test_preparation_deadline_cannot_resume_into_a_late_submission():
    _run_pipeline(r"""
let complete;
let submitted = false;
const preparation = new Promise(resolve => { complete = resolve; });
const pending = awaitSendPreparation(preparation, 'preparation timed out', undefined, 5)
  .then(() => { submitted = true; }, error => {
    check(error.name === 'TimeoutError', 'timeout lost its classification');
  });
await pending;
complete();
await Promise.resolve();
check(!submitted, 'late preparation dispatched after timeout');
""")
