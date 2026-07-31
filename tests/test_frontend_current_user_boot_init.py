"""tests/test_frontend_current_user_boot_init.py — pt_679d064f68ac4dd6.

Owner picked option **B — fetch users/me on boot** (2026-07-25).

WHAT THIS CLOSES
----------------
Four client-side multi-user gates read ``window._currentUserId``:

  * static/js/core/conv_state_reducer.js::_frameIsOurs
  * static/js/core/cross_tab_sync.js::_onConvNotifyPush
  * static/js/core/cross_tab_sync.js::_onFoldersChangedPush
  * static/js/conv_sync_push.js::_onConvSyncPush

Until this commit NO JS ever WROTE that global, so every gate was
structurally inert (``myUser === null`` → accept-all). That is correct for
a personal install but means the moment auth lands with real tenant
user_ids, a tab has no identity to compare against and cross-tenant frames
would be accepted.

WIRE (owner-picked B)
---------------------
``GET /api/v1/users/me`` is a PUBLIC endpoint (routes/api_v1/users.py:266,
``@api_meta(..., public=True)``) that already returns everything needed:

  * multi-tenant login  → ``{authenticated: true, user: {id, email, ...}}``
  * personal install    → ``{authenticated: true, user: null,
                              principal: {name, key_id, scopes}}``
  * unauthenticated     → ``{authenticated: false, user: null}``

So the initializer needs ZERO server change. ``Api.users.me()`` is added to
the unified client (CLAUDE.md §3.2.0 forbids raw fetch outside api.js) and
main.js boot resolves the identity BEFORE the push subscribers are wired
(main.js:1214-1226), so the very first frame is already scoped.

BYTE-IDENTICAL SINGLE-USER DEFAULT
----------------------------------
Personal install yields ``user: null`` → we write ``''`` (empty string).
Every gate treats ``myUser === ''`` as "no identity established" and
accepts all frames — the pre-commit behaviour, preserved exactly. Only a
REAL tenant id (non-empty) turns the gates live.

Coverage (failing-first; NEUTER-verified):

  1. Api.users.me exists on the unified client and hits /api/v1/users/me.
  2. initCurrentUserId() writes a real tenant id from ``data.user.id``.
  3. Personal install (``user: null``) writes ``''`` → gates stay inert.
  4. Unauthenticated (``authenticated: false``) writes ``''``.
  5. Network failure never throws and leaves ``''`` (fail-open: a boot
     hiccup must not brick the sidebar by rejecting every frame).
  6. Numeric tenant ids survive as-is; the gates String()-normalize both
     sides so int-vs-str can never mis-scope (already hardened in HEAD).
  7. main.js boot calls initCurrentUserId BEFORE _wireConvSyncPush /
     _wireConvHistoryRewritePush / startSyncDriftProbe — ordering is the
     whole point (a frame arriving before identity resolves would be
     accepted unscoped).
  8. The initializer is idempotent (a second call does not clobber a
     resolved id with '').
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ═════════════════════════════════════════════════════════════════════
#  Static wiring assertions (no node needed)
# ═════════════════════════════════════════════════════════════════════

def test_api_client_exposes_users_domain():
    """CLAUDE.md §3.2.0: every backend call goes through Api.<domain>.
    A raw fetch('/api/v1/users/me') in main.js would violate the
    frontend-API-isolation ratchet, so the endpoint MUST be reachable as
    Api.users.me()."""
    src = open(os.path.join(JS_DIR, 'api.js'), encoding='utf-8').read()
    assert 'const users = {' in src, (
        'api.js must define a `users` domain object')
    assert '/api/v1/users/me' in src, (
        'the users domain must own the /api/v1/users/me URL')
    # It has to be exported in the public namespace, else callers can't see it.
    ns = re.search(r'const Api = \{(.+?)\n  \};', src, re.S)
    assert ns, 'could not locate the Api public-namespace literal'
    assert 'users' in ns.group(1), (
        '`users` must be listed in the Api public namespace')


def test_boot_initializes_before_push_subscribers():
    """ORDERING IS THE CONTRACT. A notify/conv frame that lands before the
    identity resolves would pass the gate unscoped. initCurrentUserId must
    therefore be awaited BEFORE _wireConvSyncPush /
    _wireConvHistoryRewritePush / startSyncDriftProbe."""
    src = open(os.path.join(JS_DIR, 'main.js'), encoding='utf-8').read()
    assert 'initCurrentUserId' in src, (
        'main.js boot must call initCurrentUserId')
    init_at = src.index('initCurrentUserId')
    for later in ('_wireConvSyncPush()',
                  '_wireConvHistoryRewritePush()',
                  'startSyncDriftProbe()'):
        assert later in src, f'main.js should still wire {later}'
        # EVERY occurrence must follow the identity probe, not just the
        # first — a stray earlier call would re-open the unscoped window
        # this whole ticket exists to close.
        first_use = src.index(later)
        assert init_at < first_use, (
            f'initCurrentUserId must run BEFORE the FIRST {later} — '
            'otherwise the first frame is evaluated with no identity')
        assert src.count(later) == 1, (
            f'{later} should be wired exactly once; found '
            f'{src.count(later)} call sites — a duplicate outside the '
            'identity-ready chain would bypass the ordering guarantee')


def test_initializer_module_is_bundled():
    """CLAUDE.md §3.2.1: a top-level static/js/*.js not listed in
    _BUNDLE_FILES loads as a SILENT no-op in production (the script tag is
    stripped but never re-added)."""
    bundler = open(os.path.join(ROOT, 'lib', 'js_bundler.py'),
                   encoding='utf-8').read()
    assert 'core/current_user.js' in bundler, (
        'core/current_user.js must be registered in _BUNDLE_FILES or it '
        'silently never loads in production')
    # Must load BEFORE main.js (which calls it) — main.js is always last.
    assert bundler.index('core/current_user.js') < bundler.index("'main.js'"), (
        'core/current_user.js must be bundled before main.js')


# ═════════════════════════════════════════════════════════════════════
#  Behavioural harness (node)
# ═════════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const jsDir = process.argv[2];
const src = fs.readFileSync(path.join(jsDir, 'core', 'current_user.js'), 'utf8');

// Minimal Api double — the initializer must go through Api.users.me().
let meCalls = 0;
let meImpl = async () => ({ authenticated: true, user: { id: 'alice' } });
global.Api = { users: { me: async () => { meCalls++; return meImpl(); } } };
global.debugLog = () => {};

(0, eval)(src);

check('exposes_initCurrentUserId', typeof initCurrentUserId === 'function');

(async () => {
  // ── Face 2: real tenant id lands verbatim ──
  window._currentUserId = undefined;
  meImpl = async () => ({ authenticated: true, user: { id: 'alice' } });
  await initCurrentUserId();
  check('tenant_id_written', window._currentUserId === 'alice');
  check('went_through_api_client', meCalls === 1);

  // ── Face 3: personal install (user:null) → '' so gates stay inert ──
  window._currentUserId = undefined;
  resetCurrentUserIdForTests();
  meImpl = async () => ({ authenticated: true, user: null,
                          principal: { name: 'local', key_id: 'k1' } });
  await initCurrentUserId();
  check('personal_install_empty_string', window._currentUserId === '');

  // ── Face 4: unauthenticated → '' ──
  window._currentUserId = undefined;
  resetCurrentUserIdForTests();
  meImpl = async () => ({ authenticated: false, user: null });
  await initCurrentUserId();
  check('unauthenticated_empty_string', window._currentUserId === '');

  // ── Face 5: network failure is fail-open, never throws ──
  window._currentUserId = undefined;
  resetCurrentUserIdForTests();
  meImpl = async () => { throw new Error('network down'); };
  let threw = false;
  try { await initCurrentUserId(); } catch (e) { threw = true; }
  check('network_failure_does_not_throw', threw === false);
  check('network_failure_leaves_empty', window._currentUserId === '');

  // ── Face 6: numeric tenant id preserved (gates String()-normalize) ──
  window._currentUserId = undefined;
  resetCurrentUserIdForTests();
  meImpl = async () => ({ authenticated: true, user: { id: 7 } });
  await initCurrentUserId();
  check('numeric_id_preserved', window._currentUserId === 7);

  // ── Face 8: idempotent — a second call must not clobber a resolved id ──
  window._currentUserId = undefined;
  resetCurrentUserIdForTests();
  meImpl = async () => ({ authenticated: true, user: { id: 'bob' } });
  await initCurrentUserId();
  const callsAfterFirst = meCalls;
  meImpl = async () => ({ authenticated: true, user: null });   // would blank it
  await initCurrentUserId();
  check('idempotent_keeps_resolved_id', window._currentUserId === 'bob');
  check('idempotent_skips_second_fetch', meCalls === callsAfterFirst);

  // ── Face 9: a TRANSIENT failure must stay retryable, never latch '' ──
  //    Setting the latch before the await would pin the failure's '' for
  //    the life of the page — every gate accept-all forever, which is the
  //    exact cross-tenant window this module exists to close.
  window._currentUserId = undefined;
  resetCurrentUserIdForTests();
  meImpl = async () => { throw new Error('transient'); };
  await initCurrentUserId();
  check('transient_failure_yields_empty', window._currentUserId === '');
  meImpl = async () => ({ authenticated: true, user: { id: 'carol' } });
  await initCurrentUserId();
  check('identity_recovers_after_transient_failure',
        window._currentUserId === 'carol');

  console.log(out.join('\n'));})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_current_user_initializer_behaviour():
    driver = os.path.join(HERE, '_current_user_boot_harness.js')
    with open(driver, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', driver, JS_DIR],
                              capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.remove(driver)
        except OSError:
            pass
    assert proc.returncode == 0, f'node harness failed: {proc.stderr}'
    output = proc.stdout.strip()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'initializer failures:\n' + output
    passes = [ln for ln in output.splitlines() if ln.startswith('PASS')]
    assert len(passes) >= 12, f'expected >=10 PASS, got {len(passes)}:\n{output}'


# ═════════════════════════════════════════════════════════════════════
#  Gate integration — a resolved identity actually scopes the frames
# ═════════════════════════════════════════════════════════════════════

_GATE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const jsDir = process.argv[2];
global.debugLog = () => {};
global.saveConversations = () => {};
global.activeStreams = new Map();
(0, eval)(fs.readFileSync(path.join(jsDir, 'core', 'conv_state_reducer.js'), 'utf8'));

// With a REAL tenant identity, a foreign frame must be dropped and our own
// frame (including the int-vs-str mismatch the server can emit) accepted.
{
  window._currentUserId = 'alice';
  const convs = [{ id: 'c1' }];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t-bob'],
                                    runningTaskIdsRev: [10, 'r0'], userId: 'bob' });
  check('foreign_frame_dropped_when_identity_set',
        !convs[0]._authoritativeActiveTaskIds
        || !convs[0]._authoritativeActiveTaskIds.has('t-bob'));

  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t-alice'],
                                    runningTaskIdsRev: [20, 'r0'], userId: 'alice' });
  check('own_frame_accepted_when_identity_set',
        convs[0]._authoritativeActiveTaskIds
        && convs[0]._authoritativeActiveTaskIds.has('t-alice'));
}
// Personal install ('' identity) keeps the pre-commit accept-all behaviour.
{
  window._currentUserId = '';
  const convs = [{ id: 'c2' }];
  applyRunningTaskIdsFrame(convs, { convId: 'c2', runningTaskIds: ['t-any'],
                                    runningTaskIdsRev: [30, 'r0'], userId: 42 });
  check('empty_identity_accepts_all',
        convs[0]._authoritativeActiveTaskIds
        && convs[0]._authoritativeActiveTaskIds.has('t-any'));
}
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_resolved_identity_scopes_reducer_frames():
    """The initializer's whole purpose: once a real id is present the gates
    stop being inert. Also pins that '' (personal install) preserves the
    accept-all default byte-identically."""
    driver = os.path.join(HERE, '_current_user_gate_harness.js')
    with open(driver, 'w', encoding='utf-8') as f:
        f.write(_GATE_HARNESS)
    try:
        proc = subprocess.run(['node', driver, JS_DIR],
                              capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.remove(driver)
        except OSError:
            pass
    assert proc.returncode == 0, f'node harness failed: {proc.stderr}'
    output = proc.stdout.strip()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'gate-integration failures:\n' + output


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
