"""Frontend API-isolation guard.

Goal
----
The frontend has a single entry point for backend HTTP calls:
``static/js/api.js``. Every other JS file MUST call into ``Api.*`` and
MUST NOT issue a raw ``fetch('/api/...')`` or ``fetch(apiUrl('/api/...'))``.

End state reached 2026-05-28
----------------------------
``BASELINE = {}`` — every JS file has been migrated. The hard rule is
now in steady state: ``test_no_new_files_call_api_directly`` fails CI
the moment any file outside ``api.js`` adds a raw ``fetch('/api/...')``.

The ``BASELINE`` ratchet machinery is preserved in case a future
endpoint family lands in legacy form before the matching ``Api.*``
domain is added — populating ``BASELINE`` with the new file gives a
documented decrescendo path back to zero.

Adding a new endpoint
---------------------
1. Add a method to the relevant domain in ``static/js/api.js``.
2. Call it from feature JS via ``Api.<domain>.<method>(...)``.
3. Run this test — it must stay green with ``BASELINE = {}``.
"""

from __future__ import annotations

import os
import re

import pytest


# ── Configuration ────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.normpath(os.path.join(HERE, '..', 'static', 'js'))

# Matches:
#   fetch('/api/foo'      fetch("/api/foo"      fetch(`/api/foo`
#   fetch(apiUrl('/api/foo'))   fetch(apiUrl(`/api/${x}`))
_LEGACY_FETCH_RE = re.compile(
    r"""fetch\(\s*                       # fetch(
        (?:apiUrl\(\s*)?                 # optional apiUrl(
        ['"`]                            # opening quote
        /?api/                           # /api/ prefix (slash optional for tagged-template trims)
    """,
    re.VERBOSE,
)

# api.js is the ONE file allowed to call /api/ directly.
ALLOWED_FILES = {'api.js'}

# Bundle output is generated; never count it.
def _is_generated(name: str) -> bool:
    return name.startswith('bundle-') and name.endswith('.js')


# ── Per-file ratchet baseline ───────────────────────────────────────
# Established 2026-05-28 right after creating api.js + migrating folders.
# Numbers MUST monotonically decrease toward {}. Do NOT raise a value here.
BASELINE: dict[str, int] = {}


# ── Helpers ─────────────────────────────────────────────────────────
def _count_legacy_calls(path: str) -> int:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return 0
    return len(_LEGACY_FETCH_RE.findall(content))


def _scan_all() -> dict[str, int]:
    """Walk the entire static/js tree (incl. subdirs like ui/) for legacy
    fetch call sites. Subdirectory files use posix paths in the result
    dict so BASELINE entries can be e.g. ``'ui/sse_pipeline.js'``.
    """
    out: dict[str, int] = {}
    for root, dirs, files in os.walk(JS_DIR):
        # Skip any caches if present
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js'):
                continue
            if _is_generated(name) or name in ALLOWED_FILES:
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            c = _count_legacy_calls(path)
            if c > 0:
                # Use posix-style relative path so the BASELINE key is
                # stable across OSes and matches the index.html layout.
                rel = os.path.relpath(path, JS_DIR).replace(os.sep, '/')
                out[rel] = c
    return out


# ── Tests ───────────────────────────────────────────────────────────
def test_api_js_exists():
    """api.js — the unified frontend API client — must exist."""
    path = os.path.join(JS_DIR, 'api.js')
    assert os.path.isfile(path), (
        'static/js/api.js is missing. It is the single entry point for '
        'all frontend backend calls. Recreate it before any other change.'
    )


def test_no_new_files_call_api_directly():
    """A file that currently has zero legacy calls must STAY at zero."""
    actual = _scan_all()
    new_violators = sorted(set(actual) - set(BASELINE))
    if new_violators:
        details = '\n'.join(f'  {n}: {actual[n]} call(s)' for n in new_violators)
        pytest.fail(
            'New files are calling /api/* directly instead of going through '
            'window.Api in static/js/api.js:\n' + details +
            '\n\nFix: route those fetches through Api.<domain>.<method>().'
        )


def test_legacy_fetch_count_only_decreases():
    """Each known-legacy file must decrease its raw fetch count, never grow.

    When you migrate calls, run this test and paste the new actual
    counts into BASELINE above (lower numbers only).
    """
    actual = _scan_all()
    regressions = []
    for name, baseline in BASELINE.items():
        cur = actual.get(name, 0)
        if cur > baseline:
            regressions.append((name, baseline, cur))
    if regressions:
        msg = '\n'.join(
            f'  {n}: baseline={b}, now={c} (+{c - b})'
            for n, b, c in regressions
        )
        pytest.fail(
            'Frontend legacy fetch count increased — new direct calls to /api/* '
            'must instead go through window.Api in static/js/api.js:\n' + msg
        )


def test_baseline_reflects_real_counts():
    """If BASELINE entries are stale (file dropped to zero or is missing)
    or BASELINE is too generous (file is below baseline), nudge the user
    to update BASELINE so future regressions are caught at the tighter
    bound. This is a soft notice, not a failure."""
    actual = _scan_all()
    stale = [(n, b, actual.get(n, 0)) for n, b in BASELINE.items() if actual.get(n, 0) < b]
    if stale:
        msg = '\n'.join(f'  {n}: BASELINE={b}, actual={c}' for n, b, c in stale)
        pytest.skip(
            'BASELINE in tests/test_frontend_api_isolation.py is too generous '
            '— please tighten it to actual counts:\n' + msg
        )
