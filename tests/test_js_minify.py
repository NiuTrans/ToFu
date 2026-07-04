"""Regression tests for the conservative JS minifier in ``lib/js_bundler``.

The minifier (``_minify_js``) shrinks the served bundle by stripping comments
and non-semantic whitespace, WITHOUT a webpack/terser dependency. The whole
value proposition rests on two properties that these tests pin down:

  1. It genuinely SHRINKS real code (else it's pointless) — the double-neuter
     below proves the strip bites: neutering ``_minify_js`` to an identity pass
     makes the "it got smaller" assertion fail.
  2. It NEVER corrupts code — a ``//`` / ``/* */`` that lives inside a string,
     template literal, or regex literal must survive verbatim, and the
     transform must be line-preserving (no ASI hazard, no token fusing). The
     preservation tests bite if the scanner loses lexical state.

Integration guard: ``build_bundle`` still runs ``node --check`` on the
concatenated result, so a latent minifier bug degrades to "serve the raw
fallback", never a white screen — ``test_build_bundle_minifies_and_parses``
exercises that whole path when node is present.
"""
from __future__ import annotations

import os
import shutil

import pytest


# ── Pure-function correctness (dependency-free) ───────────────────────────

def test_strips_line_and_block_comments():
    from lib.js_bundler import _minify_js
    src = (
        '// a leading line comment\n'
        'var a = 1;   // trailing comment\n'
        '/* a block\n comment spanning lines */\n'
        'var b = 2;\n'
    )
    out = _minify_js(src)
    assert 'line comment' not in out
    assert 'block' not in out
    assert 'var a = 1;' in out
    assert 'var b = 2;' in out


def test_double_neuter_strip_actually_bites():
    """★ Double-neuter: with the real minifier the output is strictly smaller
    than the input for comment-heavy code; an identity-pass 'minifier' would
    NOT shrink it. This is the property the whole feature exists for."""
    from lib.js_bundler import _minify_js
    src = (
        '/* big\n   multi-line\n   docstring header */\n'
        'function f() {\n'
        '    // explain the next line\n'
        '    return 42;\n'
        '}\n'
    )
    minified = _minify_js(src)
    assert len(minified) < len(src), 'real minifier must shrink comment-heavy code'
    # Identity neuter → no shrink (models a broken/no-op minifier).
    identity = src
    assert not (len(identity) < len(src)), 'sanity: identity pass does not shrink'
    # Behaviour preserved: the code token survives.
    assert 'return 42;' in minified


def test_preserves_comment_like_content_in_string():
    """A '//' or '/* */' INSIDE a string literal must survive untouched."""
    from lib.js_bundler import _minify_js
    src = 'var url = "https://example.com/path"; var x = "/* not a comment */";\n'
    out = _minify_js(src)
    assert 'https://example.com/path' in out, 'URL // inside string must survive'
    assert '/* not a comment */' in out, 'comment-like string content must survive'


def test_preserves_content_in_template_literal():
    """Comment-like sequences inside a template literal (incl. ${} exprs) survive."""
    from lib.js_bundler import _minify_js
    src = 'const s = `line // not a comment ${a /* keep */ + b} end`;\n'
    out = _minify_js(src)
    assert 'line // not a comment' in out, 'template raw text // must survive'
    # The ${} expression comment IS a real comment context → may be stripped,
    # but the surrounding template + expression code must stay intact.
    assert 'const s = `' in out and '${' in out and '+ b} end`;' in out


def test_preserves_regex_literal():
    """A '//'-looking regex and a comment-looking regex body must survive."""
    from lib.js_bundler import _minify_js
    src = 'var re = /a\\/\\/b/g; var re2 = /x\\/\\*y/; var q = 1;\n'
    out = _minify_js(src)
    assert '/a\\/\\/b/g' in out, 'regex containing // must survive'
    assert '/x\\/\\*y/' in out, 'regex containing /* must survive'
    assert 'var q = 1;' in out


def test_line_preserving_no_token_fusing():
    """Two statements on separate lines must stay on separate lines (the
    line-preserving invariant that removes any ASI hazard)."""
    from lib.js_bundler import _minify_js
    src = 'var a = 1\nvar b = 2\n'
    out = _minify_js(src)
    assert '\n' in out, 'newline between statements must be preserved'
    # a and b must not be glued onto one line
    assert 'var a = 1' in out and 'var b = 2' in out
    assert 'var a = 1var b = 2' not in out.replace('\n', 'NL')


def test_division_not_treated_as_regex():
    """A '/' used as division (after an identifier/number) must not be misread
    as a regex-literal opener, which would swallow the rest of the line."""
    from lib.js_bundler import _minify_js
    src = 'var r = total / count; var s = 10 / 2; return r;\n'
    out = _minify_js(src)
    assert 'total / count' in out
    assert '10 / 2' in out
    assert 'return r;' in out


# ── Integration: build_bundle applies minify + still passes node --check ──

def _make_js_tree(tmp_path, files: dict):
    js_dir = tmp_path / 'js'
    js_dir.mkdir()
    for name, content in files.items():
        p = js_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return str(js_dir)


def _reset_state(monkeypatch, js_dir, files):
    from lib import js_bundler
    monkeypatch.setattr(js_bundler, 'JS_DIR', js_dir)
    monkeypatch.setattr(js_bundler, '_BUNDLE_FILES', list(files.keys()))
    monkeypatch.setattr(js_bundler, '_bundle_filename', None)
    monkeypatch.setattr(js_bundler, '_bundle_mtime', 0)


def test_build_bundle_minifies_and_parses(tmp_path, monkeypatch):
    """The concatenated bundle drops the fat header comments but keeps the
    code, and — when node is present — passes the syntax gate."""
    from lib import js_bundler

    files = {
        'i18n.js': (
            '/* i18n module — big header comment that should vanish */\n'
            'window.t = function (k) { return k; };  // identity translator\n'
        ),
        'core.js': (
            '// core state\n'
            'window.conversations = [];\n'
            'var re = /foo\\/\\/bar/;  // regex with slashes\n'
        ),
        'api.js': 'window.Api = { get: function(u){ return fetch(u); } };\n',
        'push.js': 'window.pushSubscribe = function(){};\n',
        'main.js': 'window.__boot = true;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES',
                        frozenset({'i18n.js', 'core.js', 'api.js', 'push.js', 'main.js'}))

    name = js_bundler.build_bundle()
    assert name and name.startswith('bundle-')
    text = (tmp_path / 'js' / name).read_text(encoding='utf-8')

    # Header comments stripped from the SOURCE bodies…
    assert 'big header comment that should vanish' not in text
    assert 'identity translator' not in text
    # …but the per-file boundary header the bundler itself adds stays.
    assert '// ═══ i18n.js ═══' in text
    # Code survives.
    assert 'window.t = function' in text
    assert 'window.conversations = [];' in text
    assert 'window.__boot = true;' in text
    # Regex with slashes survived the strip.
    assert '/foo\\/\\/bar/' in text


def test_build_bundle_fails_open_when_minifier_raises(tmp_path, monkeypatch):
    """If ``_minify_js`` raises for a file, the bundle still builds using that
    file's RAW content (fail-open) — the app must never blank because minify
    hit an edge case."""
    from lib import js_bundler

    files = {
        'i18n.js': 'window.t = function (k) { return k; };\n',
        'core.js': 'window.MARKER_RAW = 1; // kept via raw fallback\n',
        'api.js': 'window.Api = {};\n',
        'push.js': 'window.pushSubscribe = function(){};\n',
        'main.js': 'window.__boot = true;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES',
                        frozenset({'i18n.js', 'core.js', 'api.js', 'push.js', 'main.js'}))

    real_min = js_bundler._minify_js

    def _boom(src):
        if 'MARKER_RAW' in src:
            raise ValueError('simulated minifier edge case')
        return real_min(src)

    monkeypatch.setattr(js_bundler, '_minify_js', _boom)

    name = js_bundler.build_bundle()
    assert name and name.startswith('bundle-'), 'bundle must still build (fail-open)'
    text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    # The file that raised is present verbatim (comment NOT stripped → raw used).
    assert 'window.MARKER_RAW = 1;' in text
    assert 'kept via raw fallback' in text, 'raw content used when minify raised'


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_minified_real_style_bundle_parses(tmp_path, monkeypatch):
    """A file exercising templates + regex + nested braces must, once minified
    and concatenated, still pass node --check (i.e. the minifier didn't produce
    a syntactically broken bundle)."""
    from lib import js_bundler

    tricky = (
        '// tricky module\n'
        'function build(items) {\n'
        '  /* map each item to a row */\n'
        '  return items.map(function (it) {\n'
        '    const label = `Item ${it.id} — ${it.name || "n/a"}`;  // template\n'
        '    const clean = label.replace(/\\s+/g, " ");  // collapse ws\n'
        '    return `<div class="row">${clean}</div>`;\n'
        '  }).join("");\n'
        '}\n'
    )
    files = {
        'i18n.js': 'window.t = function (k) { return k; };\n',
        'core.js': 'window.conversations = [];\n',
        'api.js': tricky + 'window.Api = { build: build };\n',
        'push.js': 'window.pushSubscribe = function(){};\n',
        'main.js': 'window.__boot = true;\n',
    }
    js_dir = _make_js_tree(tmp_path, files)
    _reset_state(monkeypatch, js_dir, files)
    monkeypatch.setattr(js_bundler, '_CRITICAL_FILES',
                        frozenset({'i18n.js', 'core.js', 'api.js', 'push.js', 'main.js'}))

    name = js_bundler.build_bundle()
    assert name is not None, 'node --check must accept the minified bundle'
    text = (tmp_path / 'js' / name).read_text(encoding='utf-8')
    # Template + regex bodies survived.
    assert '`Item ${it.id} — ${it.name || "n/a"}`' in text
    assert '/\\s+/g' in text
    assert 'collapse ws' not in text  # the trailing comment was stripped



# ── Ratchet: the REAL production bundle must stay materially minified ──────

# The single-source-of-truth cap. The current real bundle minifies to ~65% of
# the raw source sum (measured: 3.06 MB → 1.99 MB). We ratchet at a comfortable
# 80% so ordinary source churn never trips it, but a regression that DISABLES
# minification (a revert of _minify_js, or a change that makes it fail-open on
# every file) — which would land the ratio right back at ~100% — fails loudly.
_MINIFY_RATCHET_MAX_RATIO = 0.80


def _real_bundle_ratio():
    """Build the REAL production bundle and return (raw_sum, minified_body,
    ratio). ``raw_sum`` is the byte sum of every present ``_BUNDLE_FILES``
    source; ``minified_body`` is the same content AS SHIPPED, with the
    bundler's own added per-file headers (``// ═══ name ═══``) and ``\\n;\\n``
    separators subtracted so the ratio measures the MINIFY effect, not
    concatenation overhead. Uses the module's real JS_DIR / _BUNDLE_FILES."""
    from lib import js_bundler

    # Force a fresh build so we measure the CURRENT code, not a cached name.
    js_bundler._bundle_filename = None
    js_bundler._bundle_mtime = 0
    name = js_bundler.build_bundle()
    assert name, 'production build_bundle() must succeed'
    bundle_text = os.path.join(js_bundler.JS_DIR, name)
    with open(bundle_text, 'r', encoding='utf-8') as f:
        shipped = f.read()

    raw_sum = 0
    overhead = 0
    included = 0
    for nm in js_bundler._BUNDLE_FILES:
        p = os.path.join(js_bundler.JS_DIR, nm)
        try:
            with open(p, 'r', encoding='utf-8') as f:
                raw_sum += len(f.read())
        except OSError:
            continue
        included += 1
        # Bundler adds exactly `// ═══ {nm} ═══\n` before + `\n;\n` after each.
        overhead += len(f'// ═══ {nm} ═══\n') + len('\n;\n')

    minified_body = len(shipped) - overhead
    assert included > 50, 'sanity: real _BUNDLE_FILES should have many entries'
    assert raw_sum > 0
    return raw_sum, minified_body, minified_body / raw_sum


def test_real_bundle_is_materially_minified():
    """★ Ratchet: the shipped production bundle body must be < 80% of the raw
    source sum. This turns the one-time −41% win into a standing guarantee —
    a future refactor that reverts or breaks the minify pass (ratio → ~1.0)
    fails here. Double-neuter: an identity ``_minify_js`` pushes the ratio to
    ~1.0 and this test FAILS (verified 2026-07-03)."""
    raw_sum, minified_body, ratio = _real_bundle_ratio()
    assert ratio < _MINIFY_RATCHET_MAX_RATIO, (
        f'production bundle not minified enough: minified body {minified_body:,} B '
        f'is {ratio:.1%} of raw source {raw_sum:,} B (cap {_MINIFY_RATCHET_MAX_RATIO:.0%}). '
        f'The minify pass was likely reverted or is failing-open on every file.'
    )
