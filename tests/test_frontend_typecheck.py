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
BASELINE = 464

# Bundle output is generated + gitignored; never count it.
_BUNDLE_RE = re.compile(r'(^|/)bundle-[0-9a-f]+\.js')
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
    """Soft notice: if the count dropped below BASELINE, nudge to lower it
    so future regressions are caught at the tighter bound."""
    if not _tsc_available():
        pytest.skip('tsc not available — cannot check baseline tightness.')
    count = len(_run_tsc())
    if count < BASELINE:
        pytest.skip(
            f'BASELINE in tests/test_frontend_typecheck.py is loose: '
            f'actual={count} < BASELINE={BASELINE}. Please lower BASELINE '
            f'to {count} to lock in the improvement.'
        )
