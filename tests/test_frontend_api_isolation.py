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

Variable-URL bypass guard (added 2026-07-14)
--------------------------------------------
The inline-string ratchet above only sees ``fetch('/api/...')`` literals.
A call whose URL is a VARIABLE or expression — ``fetch(startUrl)``,
``fetch(url)``, ``fetch(apiUrl(u))``, ``fetch(_logCleanApiUrl('/api/...'))``
— slips past it entirely (``branch.js`` even documented this as a "silent
violation"). ``test_no_variable_url_api_fetches`` closes that hole: it counts
every ``fetch(`` whose first argument is not a plain string literal, and
fails unless the file is a documented carve-out in
``_ALLOWED_VARIABLE_FETCHES`` (external OAuth token endpoint, image-blob
hydration). Comments are stripped before scanning so a ``fetch(...)`` shown
in a comment is not counted.

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

# Matches a fetch( whose FIRST argument is NOT a plain string literal — i.e. a
# variable or expression URL (fetch(url) / fetch(startUrl) / fetch(apiUrl(u)) /
# fetch(_logCleanApiUrl('/api/...'))). These bypass the inline-string ratchet
# yet still hit the backend directly.
_VARIABLE_FETCH_RE = re.compile(r"\bfetch\(\s*(?![)'\"`])")

# Documented, LEGITIMATE variable-URL fetches that are NOT Tofu /api/* business
# calls — the only permitted carve-outs. Keyed by posix relpath under static/js.
_ALLOWED_VARIABLE_FETCHES = {
    # Cross-origin OAuth provider token endpoint (Anthropic/OpenAI), not /api/*.
    'settings/oauth.js': 1,
    # Image blob hydration: fetches img.url / img.preview (a static asset or
    # uploaded-image URL), not a JSON /api/* business endpoint. The helper was
    # extracted conversations.js → conv_image_hydrate.js by Epic-E slice 4
    # (2ba63a12); the carve-out follows the code.
    'core/conv_image_hydrate.js': 1,
    # (tofu-pet.js HAD a carve-out here for fetching its SVG frames; the
    # 2026-07-30 raster revamp loads frames with new Image() instead — zero
    # fetch() calls — so the entry was removed, tightening the ratchet.)
}

# Bundle output is generated; never count it. feature-*.js is the deferred
# bundle family (same generator, same rule).
def _is_generated(name: str) -> bool:
    return name.startswith(('bundle-', 'feature-')) and name.endswith('.js')


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


def _strip_comments(src: str) -> str:
    """Remove /* */ block and // line comments so a fetch( mentioned inside a
    comment (e.g. branch.js's documented 'silent violation' note) is not
    counted.

    Delegates to the SINGLE shared implementation (charter #24). This is an
    UPGRADE over the local ``re.sub(r'//[^\\n]*', '', s)`` it replaced, not mere
    parity: that regex treated the ``//`` inside a string literal as a comment,
    so ``if (path.startsWith('http://')) return path;`` in api.js lost
    everything from ``//`` onward — real code deleted before the scan saw it.
    Measured across all frontend JS: 27 files differ for that reason, and in
    every one the shared tokenizer preserves code the regex ate (zero cases of
    the reverse).

    Verified NOT to change THIS guard's verdict: the variable-URL fetch count is
    identical under both strippers for every scanned file (0 files differ), so
    the ratchet numbers below carry over unchanged.
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', inline=True)


def _scan_variable_fetches() -> dict[str, int]:
    """Count fetch() calls with a variable/expression URL per file (comments
    stripped). Same walk + skip rules as _scan_all()."""
    out: dict[str, int] = {}
    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js'):
                continue
            if _is_generated(name) or name in ALLOWED_FILES:
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except OSError:
                continue
            c = len(_VARIABLE_FETCH_RE.findall(_strip_comments(content)))
            if c > 0:
                rel = os.path.relpath(path, JS_DIR).replace(os.sep, '/')
                out[rel] = c
    return out


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


def test_no_variable_url_api_fetches():
    """A fetch() with a VARIABLE/expression URL bypasses the inline-string
    ratchet but still calls the backend directly. Only the documented non-/api
    carve-outs in _ALLOWED_VARIABLE_FETCHES are permitted."""
    suspects = _scan_variable_fetches()
    violations = {}
    for f, cnt in suspects.items():
        allowed = _ALLOWED_VARIABLE_FETCHES.get(f, 0)
        if cnt > allowed:
            violations[f] = (cnt, allowed)
    if violations:
        details = '\n'.join(
            f'  {f}: {c} variable-URL fetch(es), only {a} allowed'
            for f, (c, a) in sorted(violations.items())
        )
        pytest.fail(
            'Variable-URL fetch() calls bypass window.Api in static/js/api.js '
            '(the inline-string ratchet cannot see them):\n' + details +
            '\n\nFix: route those fetches through Api.<domain>.<method>(), or — '
            'if it is a genuine non-/api call — add it to '
            '_ALLOWED_VARIABLE_FETCHES with a reason.'
        )


def test_baseline_reflects_real_counts():
    """A stale BASELINE entry (file dropped below its budget, or vanished) must
    FAIL so the ratchet is tightened.

    This previously ``pytest.skip()``d, i.e. it could never report a problem —
    the one outcome it was written to detect produced a 'skipped' verdict. With
    ``BASELINE = {}`` it is currently a no-op; it earns its keep the moment a
    future endpoint family is granted a temporary budget here, by forcing that
    budget back down as the migration progresses instead of letting it linger.
    """
    actual = _scan_all()
    stale = [(n, b, actual.get(n, 0)) for n, b in BASELINE.items() if actual.get(n, 0) < b]
    assert not stale, (
        'BASELINE in tests/test_frontend_api_isolation.py is too generous — '
        'tighten it to the actual counts so the migrated files stay migrated:\n'
        + '\n'.join(f'  {n}: BASELINE={b}, actual={c}' for n, b, c in stale))
