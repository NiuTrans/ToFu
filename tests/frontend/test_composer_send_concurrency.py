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


def test_503_retry_preserves_command_identity_because_commit_is_uncertain():
    _run_pipeline(r"""
const commands = [];
runtimeScope.ConversationTurnStore.submitConversation = async (conv, payload, config, extra) => {
  commands.push(extra.commandId);
  if (commands.length === 1) throw Object.assign(new Error('storage unavailable'), {status: 503});
  return {submittedTurn: {turnId: 'ok'}};
};
composerInput.value = 'retry this';
await sendMessage();
await sendMessage();
check(commands.length === 2 && commands[0] === commands[1], '503 minted a duplicate command');
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
