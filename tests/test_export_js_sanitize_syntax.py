#!/usr/bin/env python3
"""Regression test for the sanitizer-induced invalid-JS class.

Background — the `your-provider` unquoted-key bug (distinct from the
corrupt-source / merge-conflict class guarded by
tests/test_bundle_corruption_guard.py):

  export.py's opensource sanitizer did a blind
  ``content.replace('meituan', 'your-provider')`` on every non-brand-asset
  file. Brand registries like ``static/js/settings/branding.js`` and
  ``static/js/settings/visibility_defaults.js`` carry ``meituan`` as an
  UNQUOTED object key (``meituan: '<svg…>'`` / ``meituan:'Meituan'``). The
  rewrite turned it into ``your-provider:`` which JS parses as subtraction
  (``your - provider``) → "Uncaught SyntaxError: Unexpected token '-'",
  white-screening the whole exported app at
  ``bundle-XXXXXXXX.js:NNNNN``.

  The `_is_brand_asset` filename allowlist (settings.js / main.js / .css)
  did NOT cover these files — the settings.js monolith split (2026-05-28)
  orphaned the brand registries into settings/*.js and drifted the list.

Fix (two layers, both exercised here):
  1. Class fix — the replacement token is now a valid JS identifier
     (``yourprovider``), so it's a safe bareword key wherever ``meituan``
     was, file-agnostically.
  2. Safety net — export.py::_verify_exported_js_syntax runs ``node --check``
     over every exported .js and FAILS the export loudly on invalid JS.

These tests run the REAL sanitize transform over the REAL repo JS files and
assert the output parses with ``node --check``. skipif-node-absent; no DB.
"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# export.py is the maintainer's release tool; not shipped in opensource builds.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

_NODE = shutil.which('node')

# The brand-registry files that carry `meituan` as an unquoted object key —
# the exact files orphaned by the settings.js split. Kept explicit so the test
# names the known offenders; test_all_js_with_meituan_key_sanitizes_clean below
# additionally sweeps the WHOLE tree so a newly-orphaned file can't slip by.
_KNOWN_KEY_FILES = [
    'static/js/settings/branding.js',
    'static/js/settings/visibility_defaults.js',
]


def _node_check(source: str, tmp_path, name: str):
    """Write `source` to a temp .js and return (ok, detail) from node --check."""
    p = tmp_path / name
    p.write_text(source, encoding='utf-8')
    r = subprocess.run([_NODE, '--check', str(p)],
                       capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    return r.returncode == 0, (r.stderr or r.stdout or '').strip()


def _sanitize(rel: str) -> str:
    """Run the real opensource sanitize transform over a repo file's content."""
    from export import _sanitize_defaults_for_export, _sanitize_source_opensource
    src = (open(os.path.join(ROOT, rel), encoding='utf-8').read())
    out = _sanitize_defaults_for_export(src, rel, version='0.5.0')
    out = _sanitize_source_opensource(out, rel)
    return out


@pytest.mark.skipif(_NODE is None, reason='node not installed')
@pytest.mark.parametrize('rel', _KNOWN_KEY_FILES)
def test_known_brand_registry_sanitizes_to_valid_js(rel, tmp_path):
    """The known brand-registry files must still parse after sanitization."""
    if not os.path.exists(os.path.join(ROOT, rel)):
        pytest.skip(f'{rel} absent in this tree')
    sanitized = _sanitize(rel)
    # Precondition: the sanitizer actually touched the brand token (otherwise
    # the test would be vacuously green if the token were renamed in source).
    assert 'meituan' not in sanitized, f'{rel}: sanitizer left raw brand token'
    ok, detail = _node_check(sanitized, tmp_path, os.path.basename(rel))
    assert ok, f'sanitized {rel} FAILED node --check: {detail}'


@pytest.mark.skipif(_NODE is None, reason='node not installed')
def test_no_hyphenated_bareword_key_after_sanitize(tmp_path):
    """Belt-and-braces class assertion: the replacement token must be a valid
    identifier, so no ``<token>:`` bareword key can be hyphenated. Directly
    encodes the fix (independent of node) AND node-checks the output."""
    from export import _sanitize_source_opensource
    # A minimal object literal with meituan as an unquoted key — the exact
    # shape branding.js / visibility_defaults.js use.
    src = "const M = {\n  meituan: '<svg></svg>',\n  meituan_alt:'Meituan',\n};\n"
    out = _sanitize_source_opensource(src, 'static/js/settings/branding.js')
    assert 'your-provider' not in out, (
        'replacement token must not be the hyphenated form (invalid bareword key)'
    )
    ok, detail = _node_check(out, tmp_path, 'branding.js')
    assert ok, f'sanitized minimal object literal FAILED node --check: {detail}'


@pytest.mark.skipif(_NODE is None, reason='node not installed')
def test_all_js_with_meituan_key_sanitizes_clean(tmp_path):
    """Tree-wide sweep: EVERY static/js file containing an unquoted ``meituan``
    key must sanitize to valid JS. Catches a file the settings.js split (or a
    future refactor) newly orphaned from any exclusion — file-agnostic, so it
    doesn't depend on _KNOWN_KEY_FILES staying complete."""
    import re
    js_root = os.path.join(ROOT, 'static', 'js')
    key_re = re.compile(r'^\s*meituan\s*:', re.MULTILINE)
    swept = 0
    for dirpath, _, filenames in os.walk(js_root):
        for fn in filenames:
            if not fn.endswith('.js') or fn.startswith('bundle-'):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
            src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
            if not key_re.search(src):
                continue
            swept += 1
            sanitized = _sanitize(rel)
            ok, detail = _node_check(sanitized, tmp_path, fn)
            assert ok, f'sanitized {rel} FAILED node --check: {detail}'
    assert swept >= 2, (
        f'expected to sweep >=2 files with an unquoted meituan: key, swept {swept} '
        '— the known offenders (branding.js, visibility_defaults.js) went missing; '
        'update the sweep or the source moved'
    )


def test_verify_exported_js_syntax_raises_on_broken(tmp_path):
    """The export-time safety net must RAISE (fail loudly) when an exported .js
    file does not parse — no node dependency for the raise path itself, but the
    detection needs node, so skip when absent."""
    if _NODE is None:
        pytest.skip('node not installed')
    from export import _verify_exported_js_syntax, ExportSyntaxError
    js_dir = tmp_path / 'static' / 'js'
    js_dir.mkdir(parents=True)
    (js_dir / 'ok.js').write_text('const a = 1;\n', encoding='utf-8')
    # The exact corruption the sanitizer used to produce.
    (js_dir / 'bad.js').write_text("const M = { your-provider: 1 };\n", encoding='utf-8')
    with pytest.raises(ExportSyntaxError):
        _verify_exported_js_syntax(tmp_path)


def test_verify_exported_js_syntax_passes_on_clean(tmp_path):
    """The safety net must NOT raise when all exported JS parses."""
    if _NODE is None:
        pytest.skip('node not installed')
    from export import _verify_exported_js_syntax
    js_dir = tmp_path / 'static' / 'js'
    js_dir.mkdir(parents=True)
    (js_dir / 'a.js').write_text('const a = 1;\n', encoding='utf-8')
    (js_dir / 'b.js').write_text("const M = { yourprovider: 1 };\n", encoding='utf-8')
    _verify_exported_js_syntax(tmp_path)  # must not raise
