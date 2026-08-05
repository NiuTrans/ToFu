
const { setup } = require(process.env.JSDOM_HARNESS);

const _timers = [];
const { check, report } = setup({
  root: process.argv[3],
  /* ONLY the tripwire + the consumer gates. conv_state_reducer.js is NOT
   * loaded — this is a real build-order regression, not a simulation of one:
   * window._frameIsOurs, buildSyncDigest, reportSyncDigest and
   * startSyncDriftProbe are all genuinely absent. */
  targets: [process.argv[2], process.argv[4]],
  globals: {
    setTimeout: (fn, ms) => { _timers.push(fn); return _timers.length; },
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
    _applySettingsToConv: () => {},
    _restoreConvToolState: () => {},
    _reconnectServerTaskIfIdle: () => false,
    updateSendButton: () => {},
    loadConversationMessages: async () => {},
    pushIsConnected: () => true,
    pushSubscribe: () => {},
  },
});
function fireTimers() {
  for (let r = 0; r < 10 && _timers.length; r++) {
    const t = _timers.splice(0);
    for (const fn of t) { try { fn(); } catch (e) {} }
  }
}

let posted = [];
window.Api = global.Api = {
  conversations: {
    reportSyncDigest: (digests, extra) => {
      posted.push({ digests: digests, extra: extra });
      return Promise.resolve({ ok: true, checked: 0, divergences: [] });
    },
  },
};
Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };

(async () => {
  /* Preconditions: this IS the broken world. */
  check('precondition_predicate_absent', typeof window._frameIsOurs !== 'function');
  check('precondition_probe_absent', typeof window.startSyncDriftProbe !== 'function');
  check('precondition_digest_fn_absent', typeof window.reportSyncDigest !== 'function');
  /* …but the WATCHDOG survived, because it is a separate module. */
  check('tripwire_survived', typeof window.reportIdentityGateUnavailable === 'function');
  check('flush_survived', typeof window.flushIdentityGateDegraded === 'function');

  /* A frame arrives. The gate cannot evaluate identity → fail-open ACCEPT,
   * and the tripwire latches + schedules its own flush. */
  window._currentUserId = 'alice';
  window.conversations.push({ id: 'c1', _serverRev: 6, messages: [{}] });
  window.activeConvId = 'c1';
  _onConvNotifyPush({ type: 'conv_changed', convId: 'cNEW', rev: 1, userId: 'bob' });
  check('latched_after_frame', window.identityGateDegraded() === true);
  check('site_recorded', window.identityGateDegradedSite() === '_onConvNotifyPush');

  /* Nothing posted YET (the flush is deferred so the probe could have claimed
   * it — on this page there is no probe, so the flush is the only path). */
  check('not_posted_before_flush', posted.length === 0);

  /* Fire the deferred flush. THIS is the assertion the whole split exists
   * for: with the reducer gone, the degrade STILL reaches the server. */
  fireTimers();
  await settle();
  check('REDUCER_MISSING_still_reported', posted.length === 1);
  check('reducer_missing_flag_set',
        posted.length === 1 && posted[0].extra &&
        posted[0].extra.identityGateDegraded === true);
  check('reducer_missing_digest_empty',
        posted.length === 1 && Array.isArray(posted[0].digests) &&
        posted[0].digests.length === 0);
  check('reducer_missing_names_site',
        posted.length === 1 && posted[0].extra &&
        posted[0].extra.identityGateSite === '_onConvNotifyPush');

  /* Idempotent: a second flush must not double-post. */
  await window.flushIdentityGateDegraded();
  await settle();
  check('flush_is_idempotent', posted.length === 1);

  report();
  process.exit(0);
})();
