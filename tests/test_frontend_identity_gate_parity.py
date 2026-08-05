#!/usr/bin/env python3
"""tests/test_frontend_identity_gate_parity.py — ONE identity predicate, and
every gate must delegate to it.

WHY THIS EXISTS
---------------
Four client entry points decide whether an inbound ``notify`` frame belongs to
this tab:

  * ``core/conv_state_reducer.js``  — ``applyRunningTaskIdsFrame`` /
                                       ``applyConvStateSnapshot``
  * ``core/cross_tab_sync.js``      — ``_onConvNotifyPush``
  * ``core/cross_tab_sync.js``      — ``_onFoldersChangedPush``
  * ``conv_sync_push.js``           — ``_onConvSyncPush``

The rule they enforce has FOUR clauses (no-window → unscoped; either side
null/undefined/'' → unscoped accept; otherwise String()-normalized equality).
It lives in exactly ONE place: ``_frameIsOurs`` in conv_state_reducer.js.

The normalization is load-bearing, not cosmetic. The server resolves a frame's
``userId`` through ``routes/common.py::_request_user_id()`` /
``lib/tasks_pkg/manager/_registry.py::task_user_id()``, both of which
int-coerce a numeric id::

    return int(uid) if str(uid).isdigit() else uid

So tenant ``'7'`` ships as ``frame.userId = 7`` (int) while a client that read
its identity from a JSON body holds ``'7'`` (str). Under a raw ``!==`` compare
``7 !== '7'`` → the tab **silently drops its own frames**: cross-device
conversation sync, folder sync and history-rewrite all go dead with no error
anywhere. That invisibility is exactly why this needs a guard rather than
manual review.

WHAT THIS SUITE PINS
--------------------
1. **Structural (whole-tree).** No file anywhere under ``static/js/`` may
   compare ``_currentUserId`` against a frame identity itself — every gate
   delegates. Scanning the whole tree (not a 3-file allowlist) is the point:
   a FIFTH gate added in a new file is caught, not silently admitted.
2. **Behavioural, per entry point.** Each of the four is driven with a
   type-skewed OWN frame (must be processed) and a foreign-tenant frame (must
   return early). Testing the predicate alone would not prove the gates
   actually call it.
3. **NEUTER, per delegation site.** Stripping any ONE delegation flips that
   entry point's skew face red — proving each call site is load-bearing
   individually, not just collectively.

Board: pt_679d064f68ac4dd6 follow-up. The ``_currentUserId`` INITIALIZER
itself (``core/current_user.js``, owner-chosen option B) is a sibling's
commit; this suite is orthogonal and holds under any wire that sets it.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

import pytest

from tests._jsdom import JS_DIR, ROOT, node_deps_available, run_harness

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HARNESS_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '_jsdom_harness.js')

_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')
_XTS = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
_CSP = os.path.join(JS_DIR, 'conv_sync_push.js')

# The delegation call every gate must use (whitespace-normalized match).
_DELEGATION = 'window._frameIsOurs(frame.userId)'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comment(line):
    """Return the CODE part of a JS line — drops ``//`` tails and any line
    that is visibly inside a block comment (``*`` continuation or ``/*``)."""
    stripped = line.lstrip()
    if stripped.startswith(('*', '/*')):
        return ''
    return line.split('//', 1)[0]


def _iter_js_files():
    """Every .js under static/js/, excluding vendored bundles/minified blobs."""
    for dirpath, dirnames, filenames in os.walk(JS_DIR):
        dirnames[:] = [d for d in dirnames if d != 'vendor']
        for fn in filenames:
            if not fn.endswith('.js'):
                continue
            if fn.startswith('bundle-') or fn.endswith('.min.js'):
                continue
            yield os.path.join(dirpath, fn)


# ══════════════════════════════════════════════════════════════════════
#  1. Structural — whole-tree, semantic (delegation), not syntactic
# ══════════════════════════════════════════════════════════════════════

def test_no_file_reimplements_the_identity_compare():
    """Scan ALL of static/js/: no file may compare ``_currentUserId`` against a
    frame identity itself. The ONLY legal reader is ``_frameIsOurs`` in
    conv_state_reducer.js (the single implementation) — everyone else calls it.

    This is deliberately whole-tree rather than an allowlist: a fifth gate in
    a brand-new file is the exact scenario an allowlist would wave through.

    ALIAS-AWARE. The realistic shape is not a one-liner — it stashes the
    identity in a local first::

        const myUser = window._currentUserId;      // line N
        if (frame.userId !== myUser) return;       // line N+3

    A same-line-only scan sails straight past that (verified: it did). So we
    track every local a file assigns ``_currentUserId`` into, then flag any
    ``===``/``!==`` involving that alias OR the global itself.
    """
    offenders = []
    for path in _iter_js_files():
        rel = os.path.relpath(path, PROJECT_ROOT)
        # conv_state_reducer.js is the ONE legal implementation site.
        if os.path.abspath(path) == os.path.abspath(_REDUCER):
            continue
        src = _read(path)
        lines = src.splitlines()

        # Pass 1 — collect locals that hold the identity.
        aliases = set()
        for line in lines:
            code = _strip_comment(line)
            if '_currentUserId' not in code:
                continue
            m = re.search(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', code)
            if m:
                aliases.add(m.group(1))

        # Pass 2 — flag comparisons on the global or any of its aliases.
        for i, line in enumerate(lines, 1):
            code = _strip_comment(line)
            if not re.search(r'[!=]==', code):
                continue
            reads_identity = '_currentUserId' in code or any(
                re.search(r'\b%s\b' % re.escape(a), code) for a in aliases)
            if not reads_identity:
                continue
            # Presence guards (…!== null / undefined / '' / "") are not
            # identity compares — only two identities against each other.
            probe = re.sub(r'[!=]==\s*(?:null|undefined|""|\'\')', '', code)
            probe = re.sub(r'(?:null|undefined|""|\'\')\s*[!=]==', '', probe)
            if not re.search(r'[!=]==', probe):
                continue
            offenders.append(f'{rel}:{i}: {line.strip()}')
    assert not offenders, (
        'file(s) re-implement the identity compare instead of delegating to '
        'window._frameIsOurs() — four normalization rules must live in ONE '
        'place:\n' + '\n'.join(offenders))


def test_all_three_consumer_gates_delegate():
    """The three non-reducer gates must each carry an explicit delegation
    call. Counting them pins that none silently loses its gate."""
    xts = _read(_XTS)
    assert xts.count(_DELEGATION) == 2, (
        'cross_tab_sync.js has TWO gates (_onConvNotifyPush + '
        f'_onFoldersChangedPush); expected 2 delegations, found '
        f'{xts.count(_DELEGATION)}')
    csp = _read(_CSP)
    assert csp.count(_DELEGATION) == 1, (
        f'conv_sync_push.js must delegate exactly once, found '
        f'{csp.count(_DELEGATION)}')


def test_delegation_is_fail_open_AND_reports():
    """If the predicate is unavailable the gate must ACCEPT the frame (today's
    pre-identity behaviour) — fail-closed would silently brick cross-device
    sync, strictly worse than briefly accepting a frame.

    BUT it must not be silent. A fail-open that leaves no trace is
    indistinguishable from a working gate, which is the property that let the
    original int/str skew sit dormant for months. Every delegation therefore
    pairs its ``typeof`` guard with an ``else`` branch that reports.
    """
    for path in (_XTS, _CSP):
        rel = os.path.relpath(path, PROJECT_ROOT)
        src = _read(path)
        n_deleg = src.count(_DELEGATION)
        assert n_deleg >= 1, f'{rel}: no delegation found'
        for m in re.finditer(re.escape(_DELEGATION), src):
            before = src[max(0, m.start() - 320):m.start()]
            assert 'typeof window._frameIsOurs === "function"' in before, (
                f'{rel}: delegation is not guarded by a typeof check — an '
                'unavailable predicate must fail OPEN, not throw')
        # Each delegation must be paired with a report on the else branch.
        n_report = src.count('reportIdentityGateUnavailable(')
        assert n_report == n_deleg, (
            f'{rel}: {n_deleg} delegation(s) but {n_report} '
            'reportIdentityGateUnavailable() call(s) — a silent fail-open is '
            'indistinguishable from a working gate; every fallback must leave '
            'a trace')


def test_reporter_exists_and_is_one_shot():
    """The tripwire must exist, be exported, and be latched — a missing
    predicate fires on EVERY inbound frame, so an unlatched warn would flood
    the console and bury the signal it exists to surface.

    Lives in core/identity_gate_tripwire.js, NOT the reducer: the degrade it
    reports is "the reducer failed to load"."""
    src = _read(os.path.join(JS_DIR, 'core', 'identity_gate_tripwire.js'))
    assert 'function reportIdentityGateUnavailable(' in src, (
        'the fail-open tripwire is missing from its module')
    assert 'window.reportIdentityGateUnavailable = ' in src, (
        'the tripwire must be exported for the consumer gates to call')
    body = src.split('function reportIdentityGateUnavailable(', 1)[1] \
              .split('\n}', 1)[0]
    assert '_identityGateWarned' in body, (
        'the tripwire must be one-shot latched, else it floods on every frame')
    # And it must NOT have been left behind in the reducer (a copy would
    # resurrect the self-referential blind spot).
    reducer = _read(_REDUCER)
    assert 'function reportIdentityGateUnavailable(' not in reducer, (
        'the tripwire is still implemented inside the reducer — it would then '
        'vanish exactly when the degrade it reports occurs')


def test_single_implementation_keeps_all_four_rules():
    """The one implementation must retain every clause. A copy-free design is
    only safer if the surviving original is complete."""
    src = _read(_REDUCER)
    body = src.split('function _frameIsOurs(', 1)[1].split('\n}', 1)[0]
    assert "typeof window !== 'undefined'" in body, (
        'no-window guard missing — the predicate is called from modules that '
        'may run outside a browser (node harnesses)')
    assert 'String(myRaw)' in body and 'String(userId)' in body, (
        'both sides must be String()-normalized')
    assert re.search(r"my === ''\s*\|\|\s*theirs === ''", body), (
        "unscoped-either-side rule ('' means accept-all) missing")
    assert 'window._frameIsOurs = _frameIsOurs' in src, (
        'the single implementation must be exported for the gates to delegate to')


# ══════════════════════════════════════════════════════════════════════
#  1b. BUILD-ORDER INVARIANT — the delegation's hidden dependency
# ══════════════════════════════════════════════════════════════════════

def _bundler_list(name):
    """Parse a top-level list literal out of lib/js_bundler.py.

    Reads the REAL source (not a hand-copied mirror) so the invariant below
    can never drift from what the bundler actually ships.
    """
    src = _read(os.path.join(PROJECT_ROOT, 'lib', 'js_bundler.py'))
    m = re.search(name + r'\s*=\s*[\[(]', src)
    if not m:
        return None
    start, depth, i = m.end(), 1, m.end()
    while depth > 0 and i < len(src):
        if src[i] in '[(':
            depth += 1
        elif src[i] in '])':
            depth -= 1
        i += 1
    return re.findall(r"'([^']+\.js)'", src[start:i])


def _build_order_violations(bundle, deferred):
    """Pure build-order invariant check (list of violation strings).

    Factored out of the test so the reverse-NEUTER below can drive it with
    SYNTHETIC bundle/deferred lists — proving the check distinguishes the
    safe direction from the dangerous one instead of merely keying on
    "consumer is deferred".
    """
    violations = []
    predicate = 'core/conv_state_reducer.js'
    consumers = ['core/cross_tab_sync.js', 'conv_sync_push.js']

    # THE DANGEROUS DIRECTION: predicate missing from the eager bundle (or
    # outright deferred). Every gate fails OPEN when _frameIsOurs is absent,
    # so any eager consumer whose intake is already wired can then receive a
    # frame it cannot scope → silent accept-all.
    if predicate not in bundle:
        violations.append(
            f'{predicate} is not in _BUNDLE_FILES — it defines _frameIsOurs, '
            'which every multi-user gate delegates to (fail-open when absent)')
    elif predicate in deferred:
        violations.append(
            f'{predicate} was moved into _DEFERRED_FILES. Deferring the '
            'PREDICATE is the dangerous direction: an eager consumer receives '
            'frames while _frameIsOurs is still undefined → gate degrades to '
            'accept-all with zero test signal.')

    if predicate in bundle:
        p_idx = bundle.index(predicate)
        for consumer in consumers:
            if consumer in deferred:
                # THE SAFE DIRECTION (Epic-E sub-3A, verified 2026-08-01):
                # deferring a CONSUMER while the predicate stays eager cannot
                # open an accept-all window, because the consumer's frame
                # intake is wired BY the deferred module itself — main.js's
                # boot call hits the feature-loader stub, which loads the
                # feature bundle and only then dispatches to the real
                # _wireConvSyncPush, whose pushSubscribe/BroadcastChannel
                # wiring is what lets frames in. Before that: zero frames
                # reachable. And the core bundle (predicate inside) executes
                # synchronously at boot, strictly before main.js runs — so
                # the predicate always exists before the first frame can
                # arrive. Deferring the consumer is therefore fine; deferring
                # the PREDICATE (checked above) is not.
                continue
            if consumer not in bundle:
                violations.append(
                    f'{consumer} is neither in _BUNDLE_FILES nor '
                    '_DEFERRED_FILES — the build-order invariant can no '
                    'longer be verified')
                continue
            c_idx = bundle.index(consumer)
            if p_idx >= c_idx:
                violations.append(
                    f'ORDER VIOLATION: {predicate} (idx {p_idx}) must load '
                    f'BEFORE {consumer} (idx {c_idx}). The consumer delegates '
                    'to _frameIsOurs and fails OPEN when it is undefined.')
    return violations


def test_predicate_loads_before_every_delegating_consumer():
    """BUILD-ORDER INVARIANT — the delegation's silent failure mode.

    ``window._frameIsOurs`` is a cross-file runtime lookup and the consumer
    gates fail OPEN when it is absent. The invariant is DIRECTIONAL:

      * predicate deferred / missing while a consumer is eager  → DANGEROUS:
        frames arrive with no predicate → silent accept-all (guarded here).
      * consumer deferred while the predicate stays eager        → SAFE: the
        deferred module wires its own intake (pushSubscribe / BroadcastChannel
        inside cross_tab_sync.js, reached only via the feature-loader stub
        loading the bundle first), so no frame can arrive before the module
        — and the predicate — exist. Epic-E sub-3A (8aa9a1c6) defers
        core/cross_tab_sync.js on exactly this argument; the pre-2026-08-01
        version of this test failed that SAFE direction by keying on
        "consumer in deferred" alone (drift, not a product bug — see
        pt_5f25b1d17c9048f1).
    """
    bundle = _bundler_list('_BUNDLE_FILES')
    deferred = _bundler_list('_DEFERRED_FILES') or []
    assert bundle, 'could not parse _BUNDLE_FILES from lib/js_bundler.py'
    violations = _build_order_violations(bundle, deferred)
    assert violations == [], '\n'.join(violations)


def test_build_order_direction_neuter():
    """Reverse NEUTER (by data): the invariant must distinguish directions.

    Drives _build_order_violations with synthetic lists. The first case is
    the EXACT production shape the old test wrongly failed — if someone
    "fixes" the check by keying on consumer-in-deferred again, this case
    goes red. The other two are the genuinely dangerous shapes the guard
    exists to catch.
    """
    predicate = 'core/conv_state_reducer.js'
    xts = 'core/cross_tab_sync.js'

    # SAFE direction: predicate eager, consumer deferred → NO violation.
    safe = _build_order_violations(
        ['core/identity_gate_tripwire.js', predicate, 'conv_sync_push.js'],
        [xts])
    assert safe == [], (
        f'the safe direction (consumer deferred, predicate eager) must pass: '
        f'{safe}')

    # DANGEROUS direction: predicate deferred, consumer eager → violation.
    dangerous = _build_order_violations(
        ['core/identity_gate_tripwire.js', xts, 'conv_sync_push.js'],
        [predicate])
    assert dangerous and predicate in dangerous[0], (
        'a deferred predicate with eager consumers must be flagged — that is '
        'the accept-all window this guard exists for')

    # ORDER violation: both eager but predicate AFTER the consumer.
    order = _build_order_violations(
        ['core/identity_gate_tripwire.js', xts, predicate,
         'conv_sync_push.js'], [])
    assert order and 'ORDER VIOLATION' in order[0], (
        'an eager predicate ordered after an eager consumer must be flagged')


def test_tripwire_loads_before_everything_it_watches():
    """The WATCHDOG's own build order.

    core/identity_gate_tripwire.js owns the fail-open latch, the reporter and
    the standalone flush. It exists as a separate file precisely so it does
    NOT share a fate with the reducer it watches — the degrade it reports is
    "the reducer failed to load". If it were deferred, or ordered after the
    reducer or a consumer, it would inherit the very blind spot splitting it
    out removed: nothing would be left to notice or ship the signal.

    THE RULE: the tripwire is eager and loads FIRST — before the predicate it
    watches and before every gate that reports to it.
    """
    bundle = _bundler_list('_BUNDLE_FILES')
    deferred = _bundler_list('_DEFERRED_FILES') or []
    assert bundle, 'could not parse _BUNDLE_FILES from lib/js_bundler.py'

    tripwire = 'core/identity_gate_tripwire.js'
    watched = ['core/conv_state_reducer.js', 'core/cross_tab_sync.js',
               'conv_sync_push.js']

    assert tripwire in bundle, (
        f'{tripwire} must be in _BUNDLE_FILES — it is the watchdog for the '
        'multi-user gate and cannot report anything if it never loads')
    assert tripwire not in deferred, (
        f'{tripwire} was deferred. A watchdog that arrives after the failure '
        'it watches for is not a watchdog.')
    t_idx = bundle.index(tripwire)
    for w in watched:
        if w not in bundle:
            continue
        assert t_idx < bundle.index(w), (
            f'ORDER VIOLATION: {tripwire} (idx {t_idx}) must load BEFORE '
            f'{w} (idx {bundle.index(w)}) — it must already exist when the '
            'thing it watches loads or fails to load.')


def test_tripwire_does_not_depend_on_what_it_watches():
    """Structural: the watchdog must not reference the reducer's symbols.

    The entire reason this module is separate is fate-independence. A single
    ``_frameIsOurs`` / ``buildSyncDigest`` / ``startSyncDriftProbe`` reference
    would re-couple it to the module whose absence it reports, silently
    restoring the blind spot.
    """
    src = _read(os.path.join(JS_DIR, 'core', 'identity_gate_tripwire.js'))
    # Strip comments AND string literals: both the module docstring and the
    # human-readable warning text NAME the symbols it deliberately avoids
    # (the warning tells an operator which file to fix — that is the point).
    # Only a real SYMBOL REFERENCE couples the watchdog to what it watches.
    code = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    code = '\n'.join(ln.split('//', 1)[0] for ln in code.splitlines())
    code = re.sub(r"'(?:[^'\\]|\\.)*'", "''", code)
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)
    code = re.sub(r'`(?:[^`\\]|\\.)*`', '``', code)
    for forbidden in ('_frameIsOurs', 'buildSyncDigest', 'startSyncDriftProbe',
                      '_authoritativeActiveTaskIds', 'applyRunningTaskIdsFrame'):
        assert forbidden not in code, (
            f'the tripwire REFERENCES {forbidden!r} in executable code — it '
            'must depend on NOTHING it watches, else it shares the reducer\'s '
            'fate and the self-referential blind spot is back')


def test_tripwire_has_its_own_delivery_path():
    """The watchdog must be able to ship the signal WITHOUT the drift probe.

    When the reducer is missing there is no probe to piggyback on (main.js
    guards it on ``typeof startSyncDriftProbe``, a reducer symbol). So the
    tripwire needs its own POST, and a claim flag so the two paths never
    double-report.
    """
    src = _read(os.path.join(JS_DIR, 'core', 'identity_gate_tripwire.js'))
    assert 'function flushIdentityGateDegraded' in src, (
        'no standalone flush — the reducer-missing case would still be '
        'unreportable, which is the whole reason this module exists')
    assert 'Api.conversations.reportSyncDigest' in src, (
        'the flush must reach the same endpoint the drift probe uses (no new '
        'endpoint, per the existing-channel constraint)')
    assert 'function markIdentityGateReported' in src, (
        'no claim seam — the probe path and the flush path would double-report')
    # The reducer's piggyback path must actually claim it.
    reducer = _read(_REDUCER)
    assert 'markIdentityGateReported' in reducer, (
        'the drift probe does not claim the report, so the standalone flush '
        'will fire a duplicate')


# ══════════════════════════════════════════════════════════════════════
#  2. Behavioural — drive all FOUR real entry points
# ══════════════════════════════════════════════════════════════════════

# Each entry point is driven twice:
#   skew  — our own frame whose userId type differs (int vs str) → MUST process
#   alien — a genuinely different tenant                          → MUST return
_FOUR_GATES_BODY = r"""
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
"""


def test_all_four_entry_points_honour_the_gate():
    """Drive the four REAL shipped handlers, not the predicate: a type-skewed
    own frame must be processed; a foreign tenant must be dropped."""
    run_harness(
        target_js=_REDUCER,
        body_js=_FOUR_GATES_BODY,
        extra_targets=[_XTS, _CSP],
        min_pass=16,
        label='four identity gates',
    )


# ══════════════════════════════════════════════════════════════════════
#  3. NEUTER — each delegation site individually load-bearing
# ══════════════════════════════════════════════════════════════════════

# (label, file, exact text to strip, which gate group must then fail)
_NEUTER_CASES = [
    ('notify', _XTS,
     '''if (typeof window._frameIsOurs === "function") {
      if (!window._frameIsOurs(frame.userId)) return;
    } else if (typeof window.reportIdentityGateUnavailable === "function") {
      window.reportIdentityGateUnavailable("_onConvNotifyPush");
    }

    const convId = frame.convId;''',
     '''const convId = frame.convId;'''),
    ('folders', _XTS,
     '''if (typeof window._frameIsOurs === "function") {
      if (!window._frameIsOurs(frame.userId)) return;
    } else if (typeof window.reportIdentityGateUnavailable === "function") {
      window.reportIdentityGateUnavailable("_onFoldersChangedPush");
    }

    const deletedId = frame.deletedFolderId;''',
     '''const deletedId = frame.deletedFolderId;'''),
    ('rewrite', _CSP,
     '''if (typeof window._frameIsOurs === "function") {
      if (!window._frameIsOurs(frame.userId)) return;
    } else if (typeof window.reportIdentityGateUnavailable === "function") {
      window.reportIdentityGateUnavailable("_onConvSyncPush");
    }''',
     ''''''),
]


_FAILOPEN_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const _timers = [];
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="convList"></div></body>',
  targets: [process.argv[2], process.argv[4]],   // consumers ONLY — no reducer
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
    _applySettingsToConv: () => {},
    _restoreConvToolState: () => {},
    _reconnectServerTaskIfIdle: () => false,
    updateSendButton: () => {},
    loadConversationMessages: async () => {},
    pushIsConnected: () => true,
    pushSubscribe: () => {},
  },
});
/* DRAIN TO A FIXED POINT: the debounced list-refresh re-schedules itself
 * (the first timer enqueues the one that actually calls
 * loadConversationsFromServer), so a single drain pass leaves the real
 * refresh pending. Bounded so a self-perpetuating timer cannot hang node. */
function fireTimers() {
  for (let round = 0; round < 10 && _timers.length; round++) {
    const t = _timers.splice(0);
    for (const fn of t) { try { fn(); } catch (e) {} }
  }
}

/* jsdom defaults visibilityState to 'prerender'; the debounced list-refresh
 * has an idle guard requiring 'visible', so without this the refresh never
 * runs and the fail-open assertion below would fail for an unrelated reason. */
Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });

/* Simulate the BUILD-ORDER REGRESSION: the reducer never loaded, so the
 * predicate is absent. This is what deferring conv_state_reducer.js (or
 * ordering it after a consumer) produces in a real browser. */
check('precondition_predicate_absent', typeof window._frameIsOurs !== 'function');

let warned = [];
window.reportIdentityGateUnavailable = (site) => { warned.push(site); };

let listRefreshCalls = 0;
global.loadConversationsFromServer = window.loadConversationsFromServer =
  async () => { listRefreshCalls++; };

/* A frame from a DIFFERENT tenant. With the predicate gone the gate cannot
 * evaluate identity, so it must still ACCEPT (fail-open — refusing would
 * brick sync) …but it must REPORT that it did so. */
window._currentUserId = 'alice';
window.conversations.push({ id: 'c1', _serverRev: 6, messages: [{}] });
window.activeConvId = 'c1';
_onConvNotifyPush({ type: 'conv_changed', convId: 'cNEW', rev: 1, userId: 'bob' });
fireTimers();

check('failopen_frame_still_accepted', listRefreshCalls > 0);
check('failopen_was_reported', warned.length >= 1);
check('failopen_names_the_site', warned[0] === '_onConvNotifyPush');

report();
process.exit(0);
"""


def test_fail_open_is_observable_not_silent():
    """Load the consumers WITHOUT the reducer — the exact shape of a
    build-order regression. The frame must still be accepted (fail-open),
    and the miss must be REPORTED. A silent degrade is indistinguishable
    from a working gate."""
    run_harness(
        target_js=_XTS,
        body_js=_FAILOPEN_BODY,
        extra_targets=[_CSP],
        min_pass=4,
        label='observable fail-open',
    )


def test_NEUTER_silent_fail_open_is_caught():
    """NEUTER: strip the report call from the fallback → the fail-open goes
    silent again. Proves the tripwire (not the assertion) is what makes the
    degraded state visible."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')

    src = _read(_XTS)
    report_call = ('} else if (typeof window.reportIdentityGateUnavailable === "function") {\n'
                   '      window.reportIdentityGateUnavailable("_onConvNotifyPush");\n'
                   '    }')
    assert report_call in src, 'reporter anchor not found in _onConvNotifyPush'
    neutered_src = src.replace(report_call, '}', 1)
    assert neutered_src != src

    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(_XTS), delete=False,
            encoding='utf-8',
        ) as fh:
            npath = fh.name
            fh.write(neutered_src)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8',
        ) as hf:
            harness = hf.name
            hf.write(_FAILOPEN_BODY)
        tmp.append(harness)
        proc = subprocess.run(
            ['node', harness, npath, ROOT, _CSP],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': _HARNESS_JS},
        )
        out = (proc.stdout or '').strip()
        assert 'FAIL failopen_was_reported' in out, (
            'NEUTER did not bite — with the report call stripped the '
            f'fail-open should go silent:\n{out}')
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass


@pytest.mark.parametrize('label,path,strip_text,replacement', _NEUTER_CASES,
                         ids=[c[0] for c in _NEUTER_CASES])
def test_NEUTER_each_delegation_is_load_bearing(label, path, strip_text,
                                                replacement):
    """Strip ONE delegation → that entry point stops dropping a foreign
    tenant's frame. Proves each call site individually carries the gate
    (a collective test could pass while one site was dead)."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')

    src = _read(path)
    assert strip_text in src, (
        f'{label}: neuter anchor not found — did the delegation change shape?')
    neutered_src = src.replace(strip_text, replacement, 1)
    assert neutered_src != src

    tmp_paths = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(path), delete=False,
            encoding='utf-8',
        ) as fh:
            neutered_path = fh.name
            fh.write(neutered_src)
        tmp_paths.append(neutered_path)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8',
        ) as hf:
            harness = hf.name
            hf.write(_FOUR_GATES_BODY)
        tmp_paths.append(harness)

        # Swap the neutered copy in for its real file, keep the other two.
        targets = [_REDUCER,
                   neutered_path if path == _XTS else _XTS,
                   neutered_path if path == _CSP else _CSP]
        proc = subprocess.run(
            ['node', harness, targets[0], ROOT, targets[1], targets[2]],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': _HARNESS_JS, 'NEUTER': label},
        )
        out = (proc.stdout or '').strip()
        gate_no = {'notify': 'g2', 'folders': 'g3', 'rewrite': 'g4'}[label]
        assert f'FAIL {gate_no}_alien_tenant_dropped' in out, (
            f'NEUTER({label}) did not bite — with its delegation stripped, '
            f'{gate_no} should stop dropping a foreign tenant frame:\n{out}')
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass
