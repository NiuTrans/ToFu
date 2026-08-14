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
import re
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
#
# HISTORICAL NOTE (measured 2026-07-31): `visibility_defaults.js` no longer
# contains the token at all — the 0d3293da brand-grouping refactor moved the
# per-provider visibility map to a shape that does not name providers as
# unquoted keys. It stays listed because the per-file test SKIPS a file that is
# absent and its precondition assert (`'meituan' not in sanitized`) would
# otherwise pass vacuously; keeping the name records that this file WAS a
# carrier, so a refactor re-introducing the key here is covered from day one.
# The authoritative coverage is the tree-wide sweep below, which DISCOVERS
# carriers instead of trusting this list.
_KNOWN_KEY_FILES = ['frontend/src/runtime/app-runtime.js']
_AUDIT_SYNTHETIC_REPO_PATHS = {'static/js/settings/branding.js'}

# ── The instrument itself, hoisted to module scope so it can be ASSERTED ON ──
# Matches an UNQUOTED `meituan:` object key anywhere — deliberately NOT anchored
# to a line start. `test_the_key_pattern_sees_a_midline_key` pins its capability
# directly, because a scan pattern is the one thing a sweep cannot validate by
# using itself.
_UNQUOTED_KEY_RE = re.compile(r"(?<!['\"])\bmeituan\s*:")


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
    src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
    # The precondition must be checked on the SOURCE, not only the output.
    # `assert 'meituan' not in sanitized` alone is satisfied both by "the
    # sanitizer did its job" AND by "the token was never here" — measured
    # 2026-07-31: visibility_defaults.js stopped carrying the token entirely
    # (0d3293da), so this case was passing while asserting nothing about the
    # sanitizer. A file with nothing to rewrite is a skip, not a green tick.
    if 'meituan' not in src:
        pytest.skip(
            f'{rel} no longer contains the brand token, so there is nothing for '
            'the sanitizer to rewrite here — the tree-wide sweep is what keeps '
            'coverage honest when a carrier moves')
    sanitized = _sanitize(rel)
    # Now non-vacuous: the token WAS present, so its absence proves the rewrite.
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
    future refactor) newly orphaned from any exclusion.

    TWO THINGS THIS USED TO GET WRONG (both fixed 2026-07-31, pt_715f5283):

    1. **The pattern was anchored to the start of a line** (``^\\s*meituan\\s*:``
       with re.MULTILINE), so it could only see a key that opens its own line.
       It therefore could NOT see ``static/js/core/model_group.js:54``, which
       packs the brand map several keys to a line::

           mistral: 'Mistral', glm: 'GLM', meituan: 'Meituan', kimi: 'Kimi',

       That is the exact shape this guard exists to catch — an unquoted bareword
       key the sanitizer rewrites — and formatting decided whether the guard
       could see it. A guard whose coverage depends on where a comma fell is
       not covering the class.

    2. **The floor was a hard-coded count** (``swept >= 2``) naming two specific
       files. ``visibility_defaults.js`` legitimately stopped carrying the key
       (the 0d3293da brand-grouping refactor), so the guard went red for a
       SOURCE IMPROVEMENT while the class invariant still held perfectly. A
       count floor pinned to a file layout produces a false red on every move.

    The property being guarded has nothing to do with how many files there are:
    *whatever* set of files carries the key, every one of them must sanitize to
    parseable JS. So the set is DISCOVERED and each member asserted; the only
    count assertion left is liveness (see below), which is about the guard not
    silently becoming a no-op.
    """
    js_root = os.path.join(ROOT, 'frontend', 'src')
    swept = []
    for dirpath, _, filenames in os.walk(js_root):
        for fn in sorted(filenames):
            if not fn.endswith('.js') or fn.startswith('bundle-'):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
            src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
            if not _UNQUOTED_KEY_RE.search(src):
                continue
            swept.append(rel)
            sanitized = _sanitize(rel)
            ok, detail = _node_check(sanitized, tmp_path, fn)
            assert ok, f'sanitized {rel} FAILED node --check: {detail}'

    # LIVENESS, not a layout assertion: the sweep must still be looking at
    # something. If the brand token is renamed everywhere this guard becomes a
    # vacuous pass, and a vacuous pass is how the whole class quietly stops
    # being covered — so require at least one carrier, WITHOUT caring which
    # files or how many. Deleting every carrier is a real event that should be
    # noticed once, not silently absorbed.
    assert swept, (
        'no frontend source carries an unquoted `meituan:` key any more, so this '
        'sweep asserted nothing. If the brand token was renamed, retarget '
        '_UNQUOTED_KEY_RE at the new token; if the keys were all quoted, say so '
        'and delete this guard deliberately rather than leaving it vacuously green.'
    )


def test_the_key_pattern_sees_a_midline_key():
    """The SCAN PATTERN's own capability, asserted on fixed strings.

    This exists because the sweep above CANNOT validate its own instrument: it
    uses the pattern both to pick files and to decide whether to check them, so
    a pattern that stops seeing a shape simply examines fewer files and stays
    green. Measured 2026-07-31 — re-anchoring the pattern to line starts left
    the whole suite passing while coverage silently halved (2 carriers → 1).
    A weakened pattern must fail HERE, on inputs that cannot move or be
    reformatted, rather than being detected by a count that legitimately drifts.

    Both directions are pinned: the dangerous shapes must match (that is the
    coverage), and the already-safe QUOTED key must not (or the sweep inflates
    itself with files that were never at risk).
    """
    must_match = {
        'own line':      "const M = {\n  meituan: 'x',\n};\n",
        'packed midline': "const M = { glm: 'GLM', meituan: 'Meituan', kimi: 'K' };\n",
        'no space':      "const M = {meituan:'x'};\n",
        'space before':  "const M = { meituan : 'x' };\n",
    }
    for label, src in must_match.items():
        assert _UNQUOTED_KEY_RE.search(src), (
            f'the key pattern cannot see an unquoted key {label!r} — that shape '
            'is exactly what the sanitizer rewrites into a bareword key, so it '
            'must be swept regardless of how the source is formatted')

    must_not_match = {
        'single-quoted': "const M = { 'meituan': 'x' };\n",
        'double-quoted': 'const M = { "meituan": "x" };\n',
    }
    for label, src in must_not_match.items():
        assert not _UNQUOTED_KEY_RE.search(src), (
            f'the key pattern matched a {label} key, which is already valid JS '
            'after rewriting — counting it would dilute the sweep with files '
            'that were never in this failure class')

    # WHY a quoted key is excluded, stated accurately because I first got this
    # wrong: it is NOT the lookbehind doing the work. In `'meituan':` the
    # CLOSING quote sits between the name and the colon, so `meituan\s*:` cannot
    # match at all — the two cases above are excluded by the shape of the text,
    # and would be excluded even with the lookbehind removed. The lookbehind
    # only changes the verdict on a leading quote with no closing one (malformed
    # JS). It is kept as cheap belt-and-braces, and this comment exists so the
    # next reader does not mistake it for the load-bearing part; the assertions
    # above are about the PROPERTY (quoted keys stay out), not its mechanism.
    assert not _UNQUOTED_KEY_RE.search("{ 'meituan: 1 }"), (
        'a leading quote should suppress the match')


def test_verify_exported_js_syntax_raises_on_broken(tmp_path):
    """The export-time safety net must RAISE (fail loudly) when an exported .js
    file does not parse — no node dependency for the raise path itself, but the
    detection needs node, so skip when absent."""
    if _NODE is None:
        pytest.skip('node not installed')
    from export import _verify_exported_js_syntax, ExportSyntaxError
    js_dir = tmp_path / 'frontend' / 'src'
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
    js_dir = tmp_path / 'frontend' / 'src'
    js_dir.mkdir(parents=True)
    (js_dir / 'a.js').write_text('const a = 1;\n', encoding='utf-8')
    (js_dir / 'b.js').write_text("const M = { yourprovider: 1 };\n", encoding='utf-8')
    _verify_exported_js_syntax(tmp_path)  # must not raise
