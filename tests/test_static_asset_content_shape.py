"""tests/test_static_asset_content_shape.py — a file must contain its own language.

WHY THIS FILE EXISTS (pt_b62849184ede40d8)
------------------------------------------
``static/styles.css`` was once overwritten wholesale with PYTHON source — a
tool-execution pipeline module, ``# HOT_PATH`` banner and all. Measured at the
time: 22291 lines → 1048, ``data-theme`` selectors 1680 → 0, and the file
happily ``ast.parse``-d. 87 test files read that stylesheet, so the whole band
went red at once.

WHAT MADE IT EXPENSIVE WAS NOT THE BREAKAGE, IT WAS THE DIAGNOSIS
-----------------------------------------------------------------
Every one of those 87 suites failed with a message like ``no
[data-theme=tofu] .folder-badge rule found``. That points at a MISSING
SELECTOR — so the natural reading is "someone deleted a CSS rule", and the
investigation starts in the wrong place entirely. Nothing anywhere said the
plainer truth: *this file is not CSS any more*. Two separate sessions burned
time on that misdirection (it also silently invalidated an equivalence probe
that had been comparing two comment-strippers on what it believed was CSS).

WHY THE EXISTING GUARD IS NOT THIS GUARD
----------------------------------------
``tests/test_write_freshness_gate.py`` ends with
``assert os.path.getsize(root / 'static/styles.css') > 512 * 1024``. That WOULD
have caught this particular clobber (the corrupt file was ~42 KB), but its own
comment says what it is for: *"Sanity the guard itself reads the real tree"* —
it is a fixture check for the freshness threshold, not a corruption tripwire.
It is also size-only, so the mirror case — a clobber of roughly the right SIZE
but the wrong LANGUAGE — sails straight through it.

So this file asserts the property directly: an asset must look like the
language its extension claims. Cheap, O(files), and it fails with a message
that names the actual problem.

DELIBERATELY NARROW
-------------------
This is a shape check, not a linter. It asserts only what is unambiguous:

  * a ``.css`` file must contain CSS-ish structure (declaration blocks) and
    must NOT parse as a Python module;
  * a ``.js`` file must not parse as a Python module either.

It does NOT try to validate CSS grammar or JS syntax — ``node --check`` and the
bundler guards already cover JS, and a half-written CSS parser here would be
the "second hand-written implementation" charter #24 exists to prevent.
"""

from __future__ import annotations

import ast
import os

import pytest

from lib.vite_assets import VITE_OUT_DIR, validate_vite_artifact

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

#: Stylesheets that are load-bearing for the frontend guard suites. Listed
#: explicitly rather than globbed so a new file cannot silently opt out, and so
#: a failure names the file a human recognises.
CSS_ASSETS = [
    'static/styles.css',
    'static/settings.css',
]


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def _parses_as_python_module(text):
    """True iff ``text`` is a plausible Python MODULE, not merely parseable.

    ``ast.parse`` is far too permissive on its own: most CSS files happen to be
    syntactically valid Python expressions-ish nonsense or raise, and a bare
    ``ast.parse`` success would flag innocent files. What actually distinguishes
    the clobber is that the content is a real module — it has imports, or
    function/class definitions at the top level.
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return False
    return any(
        isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                          ast.AsyncFunctionDef, ast.ClassDef))
        for node in tree.body)


@pytest.mark.parametrize('rel', CSS_ASSETS)
def test_css_asset_is_css_not_another_language(rel):
    """A ``.css`` file must not be Python (or anything else) wearing a .css name.

    This is the direct assertion the 87-suite outage lacked. When it fires, the
    message says the file is the wrong LANGUAGE — which is the fact that was
    missing while everyone read "missing selector" and looked for a deleted
    rule.
    """
    src = _read(rel)

    assert not _parses_as_python_module(src), (
        '%s parses as a PYTHON MODULE (top-level imports and/or defs). It has '
        'been overwritten with source from another file — almost certainly a '
        'write whose target path was wrong. Recover with:\n'
        '    git checkout HEAD -- %s\n'
        'This is the failure that once took 87 test files red while every one '
        'of them reported only "missing CSS rule".' % (rel, rel))

    # Positive half: it must actually look like a stylesheet. Without this, an
    # empty or truncated file would pass the negative check above.
    assert '{' in src and '}' in src and ':' in src, (
        '%s contains no CSS declaration block at all — it is empty or '
        'truncated, not a stylesheet' % rel)


def test_styles_css_still_carries_its_theme_selectors():
    """The load-bearing content 87 suites depend on, asserted once, centrally.

    Individually those suites assert "MY rule exists", so a wholesale loss shows
    up as 87 unrelated-looking "missing rule" failures. One assertion on the
    BULK property turns that into a single unambiguous signal.

    The floor is deliberately loose (an order of magnitude below the ~1680
    present today): this must catch a wholesale loss, never fail because
    somebody legitimately refactored a few dozen theme rules away.
    """
    src = _read('static/styles.css')
    n = src.count('data-theme')
    assert n > 100, (
        'static/styles.css carries only %d occurrences of `data-theme` — the '
        'theme layer has been lost wholesale (healthy trees carry >1600). '
        'Check whether the file was overwritten rather than edited; '
        '`git checkout HEAD -- static/styles.css` restores it.' % n)


def test_js_asset_is_not_python():
    """Mirror case: a ``.js`` file overwritten with Python source.

    Same class of accident, same invisibility — ``node --check`` would catch it,
    but only for files a JS-syntax guard actually visits. This covers the two
    largest, most-scanned entry points cheaply.
    """
    manifest = validate_vite_artifact()
    rels = {
        entry['file']
        for entry in manifest.values()
        if entry.get('isEntry') and entry.get('file', '').endswith(('.js', '.mjs'))
    }
    assert rels, 'Vite manifest exposes no JavaScript entry assets'
    for rel in sorted(rels):
        full_path = os.path.join(VITE_OUT_DIR, *rel.split('/'))
        with open(full_path, encoding='utf-8') as source:
            src = source.read()
        assert not _parses_as_python_module(src), (
            '%s parses as a Python module — it has been overwritten with '
            'source from another file. Rebuild the Vite artifact containing %s'
            % (rel, rel))
