#!/usr/bin/env python3
"""tests/test_identity_gate_degraded_telemetry.py — the fail-open degrade must
reach the SERVER, not just a browser console.

WHY
---
``conv_state_reducer::_frameIsOurs`` is the ONE multi-user identity predicate;
three consumer gates delegate to it and fail OPEN when it is missing (a
build-order regression). 25dd7b19 made that degrade visible via a one-shot
``console.warn`` — but console output is BROWSER-LOCAL. Nobody operating the
server ever learns that a page is accepting every notify frame unscoped.

Every other invariant in this subsystem already reports home: the P5 sync-drift
probe POSTs a digest every 60s and the server WARN-logs divergences. The one
security-relevant degrade was the only signal that did not use that path.

So the flag rides the EXISTING probe — no new endpoint, no new cadence, no new
failure mode, and the degrade lands in ``logs/app.log`` beside every other
drift signal.

THE SUBTLE PART (and the reason this file exists)
-------------------------------------------------
``reportSyncDigest`` used to bail on ``if (!digests.length) return null``. A
page whose bundle order broke can easily have ZERO authoritative markers —
arguably the LIKELIEST shape of the bug, since a missing reducer means nothing
ever wrote ``_authoritativeActiveTaskIds``. Gating the POST on a non-empty
digest list would therefore have suppressed the signal on exactly the page it
exists to catch. The send condition is now ``digests.length || degraded``, and
the server reads the flag BEFORE validating ``digests`` for the same reason.

Telemetry only: the flag never influences an accept/reject decision — the
fail-open is already decided by the time it is set.

TWO DELIVERY PATHS (owner-directed, 2026-07-26)
-----------------------------------------------
The tripwire originally lived INSIDE conv_state_reducer.js, which made it
structurally unable to fire on its own trigger: when the reducer is missing,
the latch, the flag reader AND the 60s probe timer that would ship it all
vanish together (main.js guards the probe on ``typeof startSyncDriftProbe``,
another reducer symbol). The flag therefore covered every cause of the degrade
EXCEPT the only cause that exists.

So the watchdog now lives in ``core/identity_gate_tripwire.js``, loads BEFORE
everything it watches, and depends on nothing it watches:

  * reducer PRESENT → the drift probe piggybacks the flag onto the digest it
    already POSTs, then calls ``markIdentityGateReported()``.
  * reducer MISSING → no probe exists; the tripwire's own one-shot flush POSTs
    to the same endpoint with an empty digest list.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')
_TRIPWIRE = os.path.join(JS_DIR, 'core', 'identity_gate_tripwire.js')
_XTS = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')


# ══════════════════════════════════════════════════════════════════════
#  Client — the flag rides the existing digest POST
# ══════════════════════════════════════════════════════════════════════

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2], process.argv[4]],
  globals: { activeStreams: new Map(), conversations: [], debugLog: () => {} },
});

let posted = [];
window.Api = global.Api = {
  conversations: {
    reportSyncDigest: (digests, extra) => {
      posted.push({ digests: digests, extra: extra });
      return Promise.resolve({ ok: true, checked: digests.length, divergences: [] });
    },
  },
};

const settle = () => new Promise((r) => setImmediate(r));

(async () => {
  /* ── 1. Healthy gate + real conv state → digest sent, NO flag ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  const convs = [{ id: 'c1', _serverRev: 4,
                   _authoritativeActiveTaskIds: new Set(['t1']),
                   _authoritativeActiveTaskIdsRev: [10, 'r'] }];
  await window.reportSyncDigest(convs);
  await settle();
  check('healthy_posted', posted.length === 1);
  check('healthy_has_digest', posted[0] && posted[0].digests.length === 1);
  check('healthy_no_flag',
        !posted[0].extra || posted[0].extra.identityGateDegraded !== true);

  /* ── 2. Healthy gate + NO conv state → nothing sent (unchanged: the
         probe must stay silent on an idle page) ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  await window.reportSyncDigest([]);
  await settle();
  check('healthy_empty_silent', posted.length === 0);

  /* ── 3. DEGRADED + real conv state → flag rides the digest ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  window.reportIdentityGateUnavailable('_onConvNotifyPush');
  check('tripwire_latched', window.identityGateDegraded() === true);
  await window.reportSyncDigest(convs);
  await settle();
  check('degraded_posted', posted.length === 1);
  check('degraded_flag_set',
        posted[0].extra && posted[0].extra.identityGateDegraded === true);
  check('degraded_still_carries_digest', posted[0].digests.length === 1);

  /* ── 4. THE CRITICAL CASE: DEGRADED + EMPTY digest → STILL reports.
         A broken-bundle page usually has zero authoritative markers, because
         the missing reducer never wrote any. If the empty-digest early
         return still gated the POST, the signal would never fire on exactly
         the page this telemetry exists for. ── */
  window.resetIdentityGateWarnedForTests();
  posted = [];
  window.reportIdentityGateUnavailable('_onConvNotifyPush');
  await window.reportSyncDigest([]);
  await settle();
  check('degraded_empty_STILL_reports', posted.length === 1);
  check('degraded_empty_flag_set',
        posted.length === 1 && posted[0].extra &&
        posted[0].extra.identityGateDegraded === true);
  check('degraded_empty_digest_is_empty',
        posted.length === 1 && posted[0].digests.length === 0);

  /* ── 5. Telemetry only — reading the flag must not disturb the latch. ── */
  check('flag_read_is_idempotent',
        window.identityGateDegraded() === true &&
        window.identityGateDegraded() === true);

  report();
  process.exit(0);
})();
"""


def test_degraded_flag_rides_the_existing_digest():
    """Drive the REAL shipped reportSyncDigest: the flag travels on the
    existing probe, and a degraded gate reports even with an empty digest."""
    run_harness(
        target_js=_TRIPWIRE,
        body_js=_BODY,
        extra_targets=[_REDUCER],
        min_pass=12,
        label='identity-gate degraded telemetry',
    )


def test_NEUTER_empty_digest_short_circuit_hides_the_signal():
    """NEUTER: restore the old ``if (!digests.length) return null`` gate →
    face 4 goes red. Proves the send-condition change (not the assertion) is
    what makes a broken-bundle page report at all."""
    import subprocess
    import tempfile

    from tests._jsdom import ROOT, node_deps_available

    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')

    with open(_REDUCER, encoding='utf-8') as f:
        src = f.read()
    live = "  if (!digests.length && !degraded) return null;"
    assert live in src, 'send-condition anchor not found — did reportSyncDigest change?'
    neutered = src.replace(live, '  if (!digests.length) return null;', 1)
    assert neutered != src

    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(_REDUCER), delete=False,
            encoding='utf-8',
        ) as fh:
            npath = fh.name
            fh.write(neutered)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8',
        ) as hf:
            harness = hf.name
            hf.write(_BODY)
        tmp.append(harness)
        proc = subprocess.run(
            ['node', harness, _TRIPWIRE, ROOT, npath],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(
                     os.path.dirname(os.path.abspath(__file__)),
                     '_jsdom_harness.js')},
        )
        out = (proc.stdout or '').strip()
        assert 'FAIL degraded_empty_STILL_reports' in out, (
            'NEUTER did not bite — with the old empty-digest short circuit '
            'restored, a degraded gate on a marker-less page should go '
            f'unreported:\n{out}')
    finally:
        for p in tmp:
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


# ══════════════════════════════════════════════════════════════════════
#  THE CASE THAT MATTERS — reducer absent, signal still escapes the page
# ══════════════════════════════════════════════════════════════════════

_REDUCER_MISSING_BODY = r"""
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
"""


def test_reducer_missing_still_reports_to_server():
    """THE decisive case. Load the tripwire + the consumer gates but NOT the
    reducer — a genuine build-order regression, where _frameIsOurs,
    reportSyncDigest and startSyncDriftProbe are all really absent.

    The degrade must STILL escape the page via the tripwire's own flush. This
    is the only test that distinguishes the split-out watchdog from the
    previous self-referential version, which could not report its own trigger.
    """
    run_harness(
        target_js=_TRIPWIRE,
        body_js=_REDUCER_MISSING_BODY,
        extra_targets=[_XTS],
        min_pass=13,
        label='reducer-missing degrade escapes',
    )


def test_NEUTER_no_standalone_flush_restores_the_blind_spot():
    """NEUTER: strip the tripwire's flush scheduling → with the reducer gone
    the degrade never leaves the page again. Proves the standalone delivery
    path (not the assertion) is what closes the self-referential blind spot.
    """
    import subprocess
    import tempfile

    from tests._jsdom import ROOT, node_deps_available

    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')

    with open(_TRIPWIRE, encoding='utf-8') as f:
        src = f.read()
    anchor = '  _scheduleIdentityGateFlush();'
    assert anchor in src, 'flush-schedule anchor not found in the tripwire'
    neutered = src.replace(anchor, '  /* NEUTERED: no self-flush */', 1)
    assert neutered != src

    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(_TRIPWIRE), delete=False,
            encoding='utf-8',
        ) as fh:
            npath = fh.name
            fh.write(neutered)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8',
        ) as hf:
            harness = hf.name
            hf.write(_REDUCER_MISSING_BODY)
        tmp.append(harness)
        proc = subprocess.run(
            ['node', harness, npath, ROOT, _XTS],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(
                     os.path.dirname(os.path.abspath(__file__)),
                     '_jsdom_harness.js')},
        )
        out = (proc.stdout or '').strip()
        assert 'FAIL REDUCER_MISSING_still_reported' in out, (
            'NEUTER did not bite — without its own flush the tripwire should '
            f'be unable to report a missing reducer:\n{out}')
    finally:
        for p in tmp:
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


# ══════════════════════════════════════════════════════════════════════
#  Server — the flag is WARN-logged like every other drift signal
# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
#  Server — the flag is WARN-logged like every other drift signal
# ══════════════════════════════════════════════════════════════════════

def test_server_warns_on_degraded_flag(flask_client, caplog):
    """The endpoint must WARN-log the degrade with greppable context."""
    import logging
    caplog.set_level(logging.WARNING)
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': [],
                                   'identityGateDegraded': True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    joined = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'IDENTITY GATE DEGRADED' in joined, (
        'degrade was not WARN-logged — it would stay invisible server-side:\n'
        + joined)
    assert 'SyncDrift' in joined, (
        'the degrade must carry the SyncDrift prefix so it greps alongside '
        'the divergence warnings it rides with')


def test_server_warns_even_with_empty_digests(flask_client, caplog):
    """The flag must be read BEFORE the digests validation. A broken-bundle
    page typically sends an empty list; validating first would swallow the
    one signal that matters."""
    import logging
    caplog.set_level(logging.WARNING)
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': [],
                                   'identityGateDegraded': True})
    assert resp.status_code == 200
    joined = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'IDENTITY GATE DEGRADED' in joined, joined


def test_server_silent_without_the_flag(flask_client, caplog):
    """A healthy client must not produce the degrade warning — otherwise the
    signal is noise and gets tuned out."""
    import logging
    caplog.set_level(logging.WARNING)
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': []})
    assert resp.status_code == 200
    joined = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'IDENTITY GATE DEGRADED' not in joined, joined


def test_flag_does_not_change_probe_semantics(flask_client):
    """Telemetry only: the flag must not alter the probe's response shape or
    its divergence computation."""
    import time
    conv_id = f'test-degraded-{time.time_ns()}'
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, created_at, '
        'updated_at, settings, msg_count, search_text, rev) '
        'VALUES (?, 1, ?, ?, ?, ?, ?, 0, ?, ?)',
        (conv_id, 't', '[]', now, now, '{}', '', 5))
    db.commit()

    digest = [{'convId': conv_id, 'taskIds': ['ghost'], 'rev': 5}]
    without = flask_client.post('/api/v1/conversations/sync-digest',
                                json={'digests': digest}).get_json()
    with_flag = flask_client.post(
        '/api/v1/conversations/sync-digest',
        json={'digests': digest, 'identityGateDegraded': True}).get_json()
    assert without['checked'] == with_flag['checked']
    assert without['divergences'] == with_flag['divergences'], (
        'the telemetry flag changed the drift verdict — it must be inert')
