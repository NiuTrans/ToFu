"""Static ratchets for the 2026-08-04 "force-refresh, sidebar clicks dead" incident.

An interrupted agent edit left `static/js/ui/chat_render.js` brace-unbalanced
(a duplicated `function renderMessage(` line) and `index.html` carrying the
`stream_reducer.js` <script> tag TWICE. Two latent gaps made it a production
outage instead of a CI red:

  * nothing in the test suite ever asserted that every file the bundler SHIPS
    actually parses — the corruption scanner only knows conflict markers and
    NUL bytes, so a syntax-broken source sailed all the way to users;
  * nothing asserted index.html's script tags are unique — and a classic
    script loaded twice re-runs its top-level `const`/`let` declarations,
    throwing "Identifier has already been declared", which kills the whole
    file in dev-fallback mode (measured: stream_reducer.js, same incident).

Also pinned here: the LoadGuard capability assertion must keep covering
`renderMessage` (added to `_loadBearingCaps` in the incident fix) — its
absence was a SILENT cripple (page "booted", sidebar painted, every click
threw) precisely because no guard knew message rendering was load-bearing.

These tests are deliberately static (no server, no browser): they run in CI
and in every sibling conversation's guard ring, so the next interrupted edit
fails the suite BEFORE it can be served.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
INDEX_HTML = os.path.join(ROOT, 'index.html')


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_every_shipped_source_file_parses():
    """Every file in _BUNDLE_FILES + _DEFERRED_FILES must pass node --check.

    This is the incident's root ratchet: the bundler ships exactly these
    files (core eagerly, deferred via the feature bundle), so "parses" is the
    floor every shipped file must hold. A file failing here breaks BOTH serve
    modes (the bundle concatenates it; the dev-fallback serves it raw), so it
    must never reach production — fail here instead.
    """
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    names = list(_BUNDLE_FILES) + list(_DEFERRED_FILES)
    assert len(names) > 50, 'manifest sanity — an empty parse list would vacuously pass'
    node = shutil.which('node')

    def check(name):
        path = os.path.join(JS_DIR, name)
        if not os.path.isfile(path):
            return (name, 'MISSING on disk')
        proc = subprocess.run(
            [node, '--check', path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            return (name, None)
        return (name, (proc.stderr or proc.stdout or '').strip()[:300])

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check, names))
    bad = [(n, d) for n, d in results if d]
    assert not bad, (
        'shipped source file(s) failing node --check (fix or revert before '
        'serving — this exact shape killed sidebar clicks on 2026-08-04):\n'
        + '\n'.join(f'  {n}: {d}' for n, d in bad)
    )


def test_index_html_script_srcs_unique():
    """No <script src> may appear twice in index.html (query string ignored).

    A duplicate classic script re-executes the file: every top-level
    `const`/`let` re-declares → SyntaxError, and the file is dead in
    dev-fallback mode (the bundle strips duplicates via its own manifest, so
    the landmine only arms itself exactly when the fallback is already
    serving a degraded page)."""
    with open(INDEX_HTML, encoding='utf-8') as f:
        html = f.read()
    srcs = re.findall(r'<script\b[^>]*?\bsrc="([^"]+)"', html)
    assert srcs, 'sanity — index.html must reference scripts'
    paths = [s.split('?')[0] for s in srcs]
    dups = sorted({p for p in paths if paths.count(p) > 1})
    assert not dups, (
        'duplicate <script> tag(s) in index.html — a classic script loaded '
        'twice re-runs its top-level const/let and dies with "Identifier '
        'already declared" (stream_reducer.js, 2026-08-04): %s' % dups
    )


def test_load_bearing_capability_assertion_covers_message_rendering():
    """index.html's _loadBearingCaps must keep asserting renderMessage.

    The 2026-08-04 incident was SILENT because the guard's capability list
    stopped at t/apiUrl/pushSubscribe/loadConversation: chat_render.js failed
    to parse, the page "booted", and every sidebar click threw with no
    banner. If a future refactor defers/renames renderMessage, this ratchet
    forces the guard list to move with it instead of re-opening the silent-
    cripple window."""
    with open(INDEX_HTML, encoding='utf-8') as f:
        html = f.read()
    m = re.search(r'_loadBearingCaps\s*=\s*\[(.*?)\];', html, re.S)
    assert m, '_loadBearingCaps capability assertion not found in index.html'
    body = m.group(1)
    for fn in ('t', 'apiUrl', 'pushSubscribe', 'loadConversation', 'renderMessage'):
        assert "fn: '%s'" % fn in body, (
            "_loadBearingCaps lost its '%s' entry — a boot that lacks it must "
            "raise the banner, not dead-click silently" % fn
        )
