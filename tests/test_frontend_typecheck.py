"""Frontend type-check ratchet.

Goal
----
The frontend (``static/js/*.js``) is plain vanilla JS sharing one global
``window`` scope (no import/export — files are concatenated by
``lib/js_bundler.py``). That design has zero compile-time safety: a typo
in a cross-file global name, a renamed-but-not-updated function, or a
wrong call signature fails *silently at runtime*.

This harness runs ``tsc --noEmit --checkJs`` (config: ``tsconfig.json``
at repo root) over the frontend and enforces a **monotonically-decreasing**
error budget. It does NOT introduce a build step — ``tsc`` only reads the
existing ``.js`` files and reports problems.

How it caught its first bugs (2026-06-01)
-----------------------------------------
- ``renderMessages()`` was called behind ``typeof ... === 'function'``
  guards in ``i18n.js`` (language switch) and ``settings/save_export.js``
  (debug-mode toggle) but **never existed** — the real whole-chat repaint
  is ``renderChat(conv)``. The guard silently swallowed the no-op, so the
  chat didn't re-render. Fixed by calling ``renderChat(getActiveConv())``.
- A duplicate ``'common.close'`` key in ``i18n.js`` (TS1117).

Ratchet rules
-------------
- ``BASELINE`` is the current app-source error count. It may ONLY shrink.
- Adding a new type error (raising the count) fails CI.
- When you fix errors, lower ``BASELINE`` to the new count.
- The eventual goal is ``BASELINE = 0``, then tighten ``tsconfig.json``
  (turn on ``strict`` / ``strictNullChecks``) for the next phase.

Skips gracefully when Node/tsc isn't installed (so non-frontend CI lanes
and contributor machines without npm don't hard-fail).
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

# ── Ratchet baseline ─────────────────────────────────────────────────
# Established 2026-06-01 after wiring up tsc --checkJs and fixing the two
# real bugs it found (dead renderMessages guards + duplicate i18n key).
# This number may ONLY decrease. Lower it whenever you fix type errors.
# Goal: 0, then enable `strict` in tsconfig.json for phase 2.
#
# 2026-06 — major reduction 464 → 52 (real work, not a re-baseline):
# The recorded 464 had drifted below the actual count (494). Rather than just
# bump it, we drove the count DOWN by ~440 by attacking root causes:
#   • Fixed genuine bugs / false-positives in source: em-dash-in-JSDoc parse
#     errors (TS1127/1016) in the pre-Turn renderer + streaming_render.js; pdfProcessing
#     boolean→counter init (core.js); the former stream-zone reset shape
#     mismatch; undocumented optional @param on
#     _streamingBubbleHTML / getMessages (TS2554/1016); _i18nLang union type.
#   • Removed the dead `_updatePricingDisplay`/`pricingData`/`loadPricing` unit.
#   • Realigned the harness with its STATED purpose (see tsconfig.json header:
#     "undefined symbols, typos in global names, wrong argument counts" — NOT
#     DOM-property typing): declared cross-file `window.*` globals (ChipInput)
#     and widened the Element/EventTarget/GlobalEventHandlers/Event DOM
#     interfaces with the form-control + expando props the vanilla-JS frontend
#     reads off untyped getElementById()/querySelector()/event.target results.
#     This is a SCOPED loosening (explicit prop lists, not `[k:string]:any`), so
#     genuine "Cannot find name" typos for cross-file globals are STILL flagged.
# 2026-06 — reached ZERO. The residual 52 were driven to 0 by fixing every
# remaining nit at its source: numeric→string coercions at setAttribute/.replace
# call sites, EventTarget→Node casts on event.target before .contains(), FileReader
# .result String()-coercion, scoped globals.d.ts DOM/`window.*` declarations,
# JSDoc @returns/@param annotations on untyped API helpers, and a no-op resolver
# initializer so tsc sees the Promise executor ran synchronously. The harness now
# enforces a CLEAN frontend: ANY new tsc error fails CI. Keep it at 0 — fix the
# error, don't bump this number.
BASELINE = 0

# Bundle output is generated + gitignored; never count it. Matches BOTH the
# core bundle (bundle-<8hex>.js) and the deferred feature bundle
# (feature-<8hex>.js) — the 8-hex anchor spares the source file
# feature-bridge.js (mirrors lib/js_bundler._BUILT_BUNDLE_RE).
_BUNDLE_RE = re.compile(
    r'(^|/)(?:bundle|feature|domain-(?:paper|orchestration|settings|memory|skills))-[0-9a-f]{8}\.js|\.nc_copy\.js')
_ERROR_RE = re.compile(r': error TS\d+')


def _tsc_available() -> bool:
    return shutil.which('npx') is not None and os.path.isdir(
        os.path.join(ROOT, 'node_modules', 'typescript')
    )


def _run_tsc() -> list[str]:
    """Run tsc --noEmit and return the list of app-source error lines
    (bundle output excluded)."""
    proc = subprocess.run(
        ['npx', 'tsc', '--noEmit', '--pretty', 'false'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = (proc.stdout or '') + (proc.stderr or '')
    lines = [
        ln for ln in out.splitlines()
        if _ERROR_RE.search(ln) and not _BUNDLE_RE.search(ln)
    ]
    return lines


def test_tsconfig_exists():
    """The type-check harness config must exist."""
    assert os.path.isfile(os.path.join(ROOT, 'tsconfig.json')), (
        'tsconfig.json is missing — it scopes `tsc --checkJs` to static/js. '
        'See tests/test_frontend_typecheck.py docstring.'
    )


def test_frontend_typecheck_does_not_regress():
    """App-source tsc error count must never exceed BASELINE."""
    if not _tsc_available():
        pytest.skip(
            'tsc not available (run `npm install` at repo root). '
            'Type-check ratchet is skipped on this machine.'
        )
    errors = _run_tsc()
    count = len(errors)
    if count > BASELINE:
        # Show the newest-looking ones to help triage.
        sample = '\n'.join('  ' + e for e in errors[:40])
        pytest.fail(
            f'Frontend type errors increased: {count} > BASELINE={BASELINE}.\n'
            f'A new type error was introduced (typo in a global, wrong call '
            f'signature, dead `typeof` guard, etc.). Fix it or — if it is a '
            f'genuine false positive — declare the symbol in '
            f'static/js/globals.d.ts.\n\nFirst errors:\n{sample}'
        )


def test_baseline_is_tight():
    """The ratchet must be TIGHT: a count below BASELINE has to fail.

    This used to ``pytest.skip()`` on a loose baseline, which made it
    structurally incapable of reporting anything — whatever it found, the
    verdict was 'skipped', and a stale BASELINE could sit above the real count
    indefinitely while every run looked clean. That is the mirror image of the
    failure the charter names: a constant is a DECLARATION, only a test result
    is a fact, so the test that reconciles the two must be able to fail.

    Concretely: with BASELINE=0 this is a no-op, but if someone raises BASELINE
    to silence errors and then the errors get fixed, this fails and forces the
    number back down — which is the only thing keeping the ratchet one-way.
    """
    if not _tsc_available():
        pytest.skip('tsc not available — cannot check baseline tightness.')
    count = len(_run_tsc())
    assert count >= BASELINE, (
        f'BASELINE in tests/test_frontend_typecheck.py is LOOSE: '
        f'actual={count} < BASELINE={BASELINE}. Lower BASELINE to {count} to '
        f'lock in the improvement — otherwise {BASELINE - count} regression(s) '
        f'could be reintroduced without this suite noticing.')
