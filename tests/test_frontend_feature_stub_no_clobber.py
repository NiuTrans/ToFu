"""Failing-first guard for the mixed-shape stub-clobber fix
(pt_3879f00e sub-part 3 slice B, 2026-08-01 production incident).

Incident: a transitional bundle set — OLD-shape core (cross_tab_sync.js
still inline, so the REAL `_wireConvSyncPush` exists) + NEW
feature-loader.js (with `_wireConvSyncPush` in its stub list) — makes
`_installFeatureStub` CLOBBER the real function with a lazy stub,
because its only guard checks `window.__FEATURE_BUNDLE_SRC__` (the
dev-fallback case), never whether the real fn is already defined.
When the deferred feature bundle then 404s (or simply lacks the
not-yet-deferred module), the wiring the stub replaced is dead:
conv-sync push never subscribes.

The comment above `_installFeatureStub` already PROMISED "If the real
function is ALREADY present ... do not clobber it with a stub" — the
guard only implemented the dev-fallback half of that promise. This
suite pins the other half:

  1. The guard MUST skip installation when `window[name]` is already a
     function (mixed-shape transition safety), AND
  2. still install when the name is absent (the intended deferred
     shape), AND
  3. still skip when __FEATURE_BUNDLE_SRC__ is absent (dev fallback).

Guards 1-3 are source-level (the module runs in a browser; a JSDOM
behavioural harness would duplicate test_frontend_feature_stub_behaviour
-style exec, which the source guard makes redundant for a one-line
predicate). Guard 4 is a node-driven BEHAVIOURAL check when node is
available (skipped otherwise): real fn + stub list → real fn survives;
absent fn → stub installs.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / 'static' / 'js' / 'feature-loader.js'


# ---------------------------------------------------------------------------
# 1. the skip-guard exists: never clobber an already-defined real fn
# ---------------------------------------------------------------------------
def test_install_stub_skips_when_real_fn_present():
    src = LOADER_JS.read_text()
    # The guard must live INSIDE _installFeatureStub, before window[name]
    # is assigned the stub. Accept either typeof-window[name] or
    # typeof-window.name spelling.
    m = re.search(
        r'function\s+_installFeatureStub\s*\(name\)\s*\{(?P<body>.*?)\n\}',
        src, re.DOTALL)
    assert m, '_installFeatureStub not found'
    body = m.group('body')
    assert re.search(
        r"typeof\s+window\[name\]\s*===\s*['\"]function['\"]\s*\)?\s*return",
        body), (
        '_installFeatureStub must `return` early when window[name] is '
        'already a function — otherwise a mixed-shape bundle (real fn in '
        'core + stub entry in feature-loader) clobbers the real wiring '
        '(2026-08-01 conv-sync production incident)')
    # …and the skip must be evaluated BEFORE the assignment.
    assert body.index('typeof window[name]') < body.index('(window)[name] = stub'), (
        'the skip-guard must run before `(window)[name] = stub`')


# ---------------------------------------------------------------------------
# 2. the stub still installs when the name is absent (intended shape)
# ---------------------------------------------------------------------------
def test_install_stub_still_installs_when_absent():
    src = LOADER_JS.read_text()
    assert '(window)[name] = stub' in src, (
        'the stub assignment must remain — deferral depends on it')
    # The skip must be conditional, not a bare return (a bare return
    # would disable every stub).
    m = re.search(
        r'function\s+_installFeatureStub\s*\(name\)\s*\{(?P<body>.*?)\n\}',
        src, re.DOTALL)
    body = m.group('body')
    assert 'if' in body, 'the skip must be a conditional, not a bare return'


# ---------------------------------------------------------------------------
# 3. dev-fallback guard unchanged
# ---------------------------------------------------------------------------
def test_dev_fallback_guard_unchanged():
    src = LOADER_JS.read_text()
    assert 'if (!window.__FEATURE_BUNDLE_SRC__) return;' in src, (
        'the dev-fallback guard (no __FEATURE_BUNDLE_SRC__ → no stubs) '
        'must be preserved verbatim')


# ---------------------------------------------------------------------------
# 4. BEHAVIOURAL (node, skip-if-absent): real fn survives; absent fn stubbed
# ---------------------------------------------------------------------------
def test_behaviour_stub_never_clobbers_real_fn(tmp_path):
    node = shutil.which('node')
    if not node:
        import pytest
        pytest.skip('node unavailable — source guards 1-3 cover the predicate')
    src = LOADER_JS.read_text()
    m = re.search(
        r'function\s+_installFeatureStub\s*\(name\)\s*\{(?P<body>.*?)\n\}',
        src, re.DOTALL)
    body = m.group('body')
    # Extract _installFeatureStub + a minimal _loadFeatureBundle/window shim.
    harness = (
        'const window = { __FEATURE_BUNDLE_SRC__: "static/js/feature-x.js" };\n'
        'function _loadFeatureBundle() { return Promise.resolve(false); }\n'
        'function _installFeatureStub(name) {' + body + '}\n'
        # case A: real fn already present → must NOT be replaced
        'window._wireConvSyncPush = function real() { return 42; };\n'
        '_installFeatureStub("_wireConvSyncPush");\n'
        'if (window._wireConvSyncPush.name !== "real") {\n'
        '  console.error("CLOBBERED: real fn replaced by stub");\n'
        '  process.exit(1);\n'
        '}\n'
        # case B: name absent → stub MUST install
        '_installFeatureStub("openTaskMode");\n'
        'if (typeof window.openTaskMode !== "function") {\n'
        '  console.error("MISSING: stub not installed for absent name");\n'
        '  process.exit(2);\n'
        '}\n'
        'console.log("OK: real fn preserved, absent name stubbed");\n'
    )
    probe = tmp_path / 'probe.js'
    probe.write_text(harness)
    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0, (
        f'behavioural harness failed:\n{proc.stdout}\n{proc.stderr}')
    assert 'OK: real fn preserved, absent name stubbed' in proc.stdout
