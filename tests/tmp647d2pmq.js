
const { setup } = require(process.env.JSDOM_HARNESS);

const _timers = [];
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="convList"></div></body>',
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    setTimeout: (fn) => { _timers.push(fn); return _timers.length; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    _editingMsgIdx: null,
    activeStreams: new Map(),
    activeConvId: null,
    conversations: [],
    debugLog: () => {},
    saveConversations: () => {},
    renderConversationList: () => {},
    ConvCache: { put: () => {}, remove: () => {}, get: async () => null },
    renderChat: () => {},
    _applySettingsToConv: () => {},
    _restoreConvToolState: () => {},
    _reconnectServerTaskIfIdle: () => false,
    updateSendButton: () => {},
    loadConversationMessages: async () => {},
    pushIsConnected: () => true,
    pushSubscribe: () => {},
  },
});
function fireTimers() { const t = _timers.splice(0); for (const fn of t) { try { fn(); } catch (e) {} } }

// Observable side-effects per gate.
let listRefreshCalls = 0;
let convGetCalls = [];
let folderLoadCalls = 0;
global.loadConversationsFromServer = window.loadConversationsFromServer =
  async () => { listRefreshCalls++; };
global.Api = window.Api = { conversations: { get: async (id) => { convGetCalls.push(id); return null; } } };
let _folders = [{ id: 'f-del', name: 'Doomed', order: 0 }];
global.getFolders = window.getFolders = () => _folders;
global.loadFolders = window.loadFolders = () => { folderLoadCalls++; return Promise.resolve(_folders); };
Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });

const NEUTER = process.env.NEUTER || '';
function reset() {
  listRefreshCalls = 0; convGetCalls = []; folderLoadCalls = 0;
  _timers.splice(0);
  window.conversations.length = 0;
  window.activeConvId = null;
  _folders = [{ id: 'f-del', name: 'Doomed', order: 0 }];
}

/* ── GATE 1: reducer applyRunningTaskIdsFrame ───────────────────────────
   Observable: conv._authoritativeActiveTaskIds is written (or not). */
function gate1(myId, frameUserId) {
  reset();
  window._currentUserId = myId;
  const conv = { id: 'c1' };
  window.conversations.push(conv);
  window.applyRunningTaskIdsFrame(window.conversations, {
    convId: 'c1', runningTaskIds: ['t1'], runningTaskIdsRev: [10, 'r'],
    userId: frameUserId,
  });
  return !!(conv._authoritativeActiveTaskIds && conv._authoritativeActiveTaskIds.size > 0);
}
if (NEUTER === '' || NEUTER === 'reducer') {
  check('g1_skew_int_id_str_frame_processed', gate1(7, '7') === true);
  check('g1_skew_str_id_int_frame_processed', gate1('7', 7) === true);
  check('g1_alien_tenant_dropped',           gate1('alice', 'bob') === false);
  check('g1_unscoped_accepts',               gate1(null, 'anything') === true);
}

/* ── GATE 2: cross_tab_sync _onConvNotifyPush ───────────────────────────
   Observable: an unknown-conv frame schedules a debounced list refresh. */
function gate2(myId, frameUserId) {
  reset();
  window._currentUserId = myId;
  window.conversations.push({ id: 'c1', _serverRev: 6, messages: [{}] });
  window.activeConvId = 'c1';
  _onConvNotifyPush({ type: 'conv_changed', convId: 'cNEW', rev: 1, userId: frameUserId });
  fireTimers();
  return listRefreshCalls > 0;
}
if (NEUTER === '' || NEUTER === 'notify') {
  check('g2_skew_int_id_str_frame_processed', gate2(7, '7') === true);
  check('g2_skew_str_id_int_frame_processed', gate2('7', 7) === true);
  check('g2_alien_tenant_dropped',           gate2('alice', 'bob') === false);
  check('g2_unscoped_accepts',               gate2(null, 'anything') === true);
}

/* ── GATE 3: cross_tab_sync _onFoldersChangedPush ───────────────────────
   Observable: a delete frame drops the folder from the tree. */
function gate3(myId, frameUserId) {
  reset();
  window._currentUserId = myId;
  _onFoldersChangedPush({ type: 'folders_changed', deletedFolderId: 'f-del', userId: frameUserId });
  return _folders.some((f) => f.id === 'f-del') === false;
}
if (NEUTER === '' || NEUTER === 'folders') {
  check('g3_skew_int_id_str_frame_processed', gate3(7, '7') === true);
  check('g3_skew_str_id_int_frame_processed', gate3('7', 7) === true);
  check('g3_alien_tenant_dropped',           gate3('alice', 'bob') === false);
  check('g3_unscoped_accepts',               gate3(null, 'anything') === true);
}

/* ── GATE 4: conv_sync_push _onConvSyncPush ─────────────────────────────
   Observable: the handler issues Api.conversations.get for the conv. */
function gate4(myId, frameUserId) {
  reset();
  window._currentUserId = myId;
  window.conversations.push({ id: 'c1', messages: [{}] });
  _onConvSyncPush({ kind: 'history_rewrite', convId: 'c1', rev: 5, userId: frameUserId });
  return convGetCalls.length > 0;
}
if (NEUTER === '' || NEUTER === 'rewrite') {
  check('g4_skew_int_id_str_frame_processed', gate4(7, '7') === true);
  check('g4_skew_str_id_int_frame_processed', gate4('7', 7) === true);
  check('g4_alien_tenant_dropped',           gate4('alice', 'bob') === false);
  check('g4_unscoped_accepts',               gate4(null, 'anything') === true);
}

report();
/* Explicit exit: loading conv_sync_push.js leaves a pending handle (the
 * async _applyHistoryRewrite's un-awaited Api promise, plus jsdom's own
 * timers), so node would print every PASS line and then sit on a live event
 * loop until the subprocess timeout. Mirrors the other conv-push harnesses. */
process.exit(0);
