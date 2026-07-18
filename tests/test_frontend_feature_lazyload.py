"""tests/test_frontend_feature_lazyload.py — the deferred feature-bundle
lazy-loader (static/js/feature-loader.js).

WHY
---
The 2.0 MB monolithic JS bundle shipped ~66 KB gzip of feature-gated code
(paper-reader / orchestration studio / task mode) at boot that most sessions
never open. Those three modules were split into a SEPARATE feature bundle
(lib/js_bundler.py `_DEFERRED_FILES` → feature-<hash>.js) fetched ON DEMAND the
first time the user opens the feature. feature-loader.js (in the CORE bundle)
installs a lazy stub for each entry point (openOrchestration / openTaskMode /
togglePaperMode) that: (1) injects the feature bundle <script> once, (2) once
loaded, dispatches to the REAL function the feature bundle defined (which by
then has overwritten the stub). Both bundles share window scope (plain
concatenated <script>s, NOT ES modules).

This harness loads the REAL shipped feature-loader.js under jsdom and drives
the mechanism directly:
  1. With __FEATURE_BUNDLE_SRC__ SET (production/bundled path): the entry-point
     names are installed as stubs; calling one injects the feature <script>
     exactly once; after the feature bundle "loads" (we define the real fn +
     fire onload) the real fn runs with the forwarded args; a second call goes
     straight to the real fn (no second injection).
  2. With __FEATURE_BUNDLE_SRC__ ABSENT (dev-fallback: individual <script>
     tags, real fns already on the page): NO stub is installed — a pre-existing
     real fn is NOT clobbered.

DOUBLE-NEUTER: a mutated COPY of feature-loader.js whose _installFeatureStub is
neutered to a no-op → the stub is never installed → clicking the entry point
never injects the feature bundle → the "injected once" assertion flips. The
shipped file is never touched.
"""

import os
import re

from tests._jsdom import run_harness, JS_DIR

_LOADER = os.path.join(JS_DIR, 'feature-loader.js')

# Common harness prologue: jsdom + a captured-appendChild that records injected
# <script> tags instead of actually loading them.
_PROLOGUE = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const fs = require('fs');
const SRC = process.argv[4];               // path to feature-loader.js (or a mutated copy)  [run_harness: argv[4:]=extra_targets]
const FEATURE_SET = process.argv[5] === '1';
const injected = [];
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [],                             // we eval SRC manually after setting globals
  globals: {
    debugLog: function(){},
    toast: function(){},
  },
});
if (FEATURE_SET) window.__FEATURE_BUNDLE_SRC__ = 'static/js/feature-xyz.js';
// Capture injected <script> tags without loading them; expose .onload/.onerror.
document.head.appendChild = function(node){ injected.push(node); return node; };
// eval the loader source in global scope (window-concat semantics)
(0, eval)(fs.readFileSync(SRC, 'utf8'));
'''


def _run(body, *, feature_set, min_pass, label, src=_LOADER):
    run_harness(
        target_js=_LOADER,           # argv[2] (unused target; we eval SRC ourselves)
        body_js=_PROLOGUE + body,
        extra_targets=[src, '1' if feature_set else '0'],
        min_pass=min_pass,
        label=label,
    )


def test_lazy_load_then_dispatch():
    body = r'''
    // Stubs installed for the three deferred entry points.
    check('stub installed openTaskMode', typeof window.openTaskMode === 'function');
    check('stub installed openOrchestration', typeof window.openOrchestration === 'function');
    check('stub installed togglePaperMode', typeof window.togglePaperMode === 'function');

    // Call the stub → should inject the feature bundle <script> exactly once.
    window.openTaskMode('ARG1');
    check('feature script injected once', injected.length === 1);
    check('injected src is feature bundle', injected.length === 1 && /feature-xyz\.js/.test(injected[0].src));

    // Simulate the feature bundle loading: it defines the REAL fn (overwriting
    // the stub) then the <script> onload fires.
    let realArgs = null;
    window.openTaskMode = function(){ realArgs = Array.prototype.slice.call(arguments); };
    if (injected[0].onload) injected[0].onload();
    // The loader's .then() runs on the resolved promise microtask.
    setTimeoutFlush();
    check('real fn dispatched after load', realArgs && realArgs[0] === 'ARG1');

    // A second call goes straight to the (now real) fn — no new injection.
    const before = injected.length;
    window.openTaskMode('ARG2');
    check('no second injection', injected.length === before);
    report();
    '''
    # The harness neuters setTimeout, but the loader uses Promise microtasks.
    # Provide a synchronous microtask flush shim.
    body = body.replace('setTimeoutFlush();',
                        'await new Promise(r=>r()); await new Promise(r=>r());')
    body = 'exports.__unused=0; (async () => {\n' + body + '\n})();'
    _run(body, feature_set=True, min_pass=7, label='lazy-load')


def test_dev_fallback_no_stub_clobber():
    body = r'''
    // Dev-fallback: __FEATURE_BUNDLE_SRC__ absent BEFORE loader eval. But the
    // prologue only sets it when FEATURE_SET; here it is not set. A real fn
    // pre-defined on the page must survive (loader must not stub over it).
    // NOTE: define the real fn BEFORE loading the loader to mimic the dev path
    // where the feature files loaded as individual <script>s earlier.
    // (Re-eval the loader with the real fn present.)
    let called = false;
    window.openTaskMode = function(){ called = true; };
    (0, eval)(fs.readFileSync(SRC, 'utf8'));   // re-run install with no __FEATURE_BUNDLE_SRC__
    check('real fn preserved (not stubbed)', typeof window.openTaskMode === 'function');
    window.openTaskMode();
    check('real fn actually runs (no stub)', called === true);
    check('no injection in dev fallback', injected.length === 0);
    report();
    '''
    _run(body, feature_set=False, min_pass=3, label='dev-fallback')


def test_neuter_install_stub_regresses():
    """Double-neuter: a mutated COPY whose _installFeatureStub body is a no-op
    → no stub installed → calling the entry point injects nothing. Proves the
    stub install is load-bearing. The shipped file is never touched."""
    src = open(_LOADER, encoding='utf-8').read()
    # Neuter: make _installFeatureStub return immediately (skip stub install).
    neutered = src.replace(
        'function _installFeatureStub(name) {',
        'function _installFeatureStub(name) {\n  return; // NEUTERED',
        1,
    )
    assert neutered != src, 'neuter pattern did not match _installFeatureStub'
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=os.path.dirname(_LOADER),
                                     delete=False, encoding='utf-8') as fh:
        mutated = fh.name
        fh.write(neutered)
    try:
        body = r'''
        // With the stub install neutered, the entry point is NOT a loader stub.
        // Calling it must NOT inject the feature bundle.
        if (typeof window.openTaskMode === 'function') { try { window.openTaskMode(); } catch(e){} }
        check('neutered: no feature script injected', injected.length === 0);
        report();
        '''
        _run(body, feature_set=True, min_pass=1, label='neuter', src=mutated)
    finally:
        try:
            os.remove(mutated)
        except OSError:
            pass


# Static invariant (no node needed): the JS loader's entry-point list must
# match the bundler's _DEFERRED_ENTRY_POINTS so the two never drift.
def test_entry_points_match_bundler():
    from lib.js_bundler import _DEFERRED_ENTRY_POINTS as py_eps
    js = open(_LOADER, encoding='utf-8').read()
    m = re.search(r'_DEFERRED_ENTRY_POINTS\s*=\s*\[([^\]]*)\]', js)
    assert m, 'could not find _DEFERRED_ENTRY_POINTS in feature-loader.js'
    js_eps = set(re.findall(r"'([^']+)'", m.group(1)))
    assert js_eps == set(py_eps), (
        f'entry-point drift: JS {sorted(js_eps)} vs bundler {sorted(py_eps)}')
