"""Every shipped JS source file must be in the bundle manifest.

WHY THIS EXISTS (2026-07-28): `static/js/paper/research.js` was committed,
`paper-reader.js` rendered a button calling into it, and the whole auto-research
UI was unreachable — the file was never added to `lib/js_bundler.py`'s manifest,
so the browser never received it and the click threw a bare ReferenceError.

The manifest is the ONLY thing that decides what reaches a browser. A .js file
on disk is not shipped code; a .js file in `_BUNDLE_FILES` / `_DEFERRED_FILES`
is. Nothing structurally coupled those two facts, so "write the module" and
"ship the module" were independent steps and one of them was silently skipped.

This ratchet asserts the RESULT ("no source file that other shipped code calls
into is missing from the manifest"), not the presence of any particular
filename — adding a new module keeps it green as long as the module is shipped.
"""

import ast
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(REPO, 'static', 'js')
BUNDLER = os.path.join(REPO, 'lib', 'js_bundler.py')

# Built outputs carry a content hash; they are products of the manifest, not
# inputs to it. Matched structurally so a new artifact prefix cannot sneak in.
_BUILT_ARTIFACT = re.compile(r'^(?:bundle|feature|i18n-(?:zh|en))-[0-9a-f]{6,}\.js$')

# Files deliberately NOT in index.html's bundle, each with a load path of its
# own. Keep this list SHORT and justified — it is the one way to make this
# ratchet a no-op, so every entry states who loads the file instead.
_INTENTIONALLY_UNBUNDLED = {
    # Loads only on the standalone /admin page (static/admin.html), which has
    # its own <script> tag; index.html never needs it.
    'relay-admin.js': 'static/admin.html',
}


def _manifest_lists():
    """Read the shipped-file lists out of lib/js_bundler.py via AST.

    AST, not regex: the manifest lists are long and comment-dense, and a regex
    that stops at the first ``]`` silently returns a fraction of the real list
    (measured: 8 entries instead of 131). A ratchet fed a truncated scan surface
    reports success while checking almost nothing.
    """
    tree = ast.parse(open(BUNDLER, encoding='utf-8').read())
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in ('_BUNDLE_FILES', '_DEFERRED_FILES'):
                continue
            if isinstance(node.value, (ast.List, ast.Tuple)):
                out[target.id] = [
                    e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
    return out


def _shipped():
    lists = _manifest_lists()
    return set(lists.get('_BUNDLE_FILES', [])) | set(lists.get('_DEFERRED_FILES', []))


def _source_files_on_disk():
    found = set()
    for dirpath, dirnames, filenames in os.walk(JS_DIR):
        dirnames[:] = [d for d in dirnames if d not in ('vendor', 'node_modules')]
        for fn in filenames:
            if not fn.endswith('.js') or _BUILT_ARTIFACT.match(fn):
                continue
            found.add(os.path.relpath(os.path.join(dirpath, fn), JS_DIR))
    return found


@pytest.mark.unit
def test_scan_surface_is_not_truncated():
    """Guard the guard: a truncated manifest read makes every other test here lie."""
    lists = _manifest_lists()
    assert '_BUNDLE_FILES' in lists and '_DEFERRED_FILES' in lists, (
        'manifest lists not found — js_bundler.py was restructured; re-point this guard'
    )
    # The real manifest is large. If a parsing change silently truncates it,
    # the coverage assertion below would pass while checking a handful of files.
    assert len(lists['_BUNDLE_FILES']) > 100, (
        'parsed only %d core entries — scan surface truncated, not a real shrink'
        % len(lists['_BUNDLE_FILES'])
    )
    assert len(_source_files_on_disk()) > 100


@pytest.mark.unit
def test_every_source_file_is_shipped_or_explicitly_excluded():
    """A .js file on disk that nothing ships is dead code the browser never sees."""
    missing = sorted(_source_files_on_disk() - _shipped() - set(_INTENTIONALLY_UNBUNDLED))
    assert not missing, (
        'These JS source files are on disk but in NO bundle list, so the browser '
        'never receives them. Add them to _BUNDLE_FILES / _DEFERRED_FILES in '
        'lib/js_bundler.py, or add an entry to _INTENTIONALLY_UNBUNDLED naming '
        'what else loads them:\n  ' + '\n  '.join(missing)
    )


@pytest.mark.unit
def test_manifest_has_no_entries_missing_from_disk():
    """Complement: a manifest entry with no file breaks the whole bundle build."""
    ghosts = sorted(f for f in _shipped()
                    if not os.path.isfile(os.path.join(JS_DIR, f)))
    assert not ghosts, 'manifest references nonexistent files: %s' % ghosts


@pytest.mark.unit
def test_exclusion_list_stays_justified():
    """Complement to the exclusion escape hatch: it must not become a dumping ground.

    Without this, the cheapest way to green the coverage test above is to paste
    the offending filename into _INTENTIONALLY_UNBUNDLED — which turns the whole
    ratchet into a no-op with no red signal.
    """
    for name, loader in _INTENTIONALLY_UNBUNDLED.items():
        assert os.path.isfile(os.path.join(JS_DIR, name)), (
            '%s is excluded but no longer exists — drop the entry' % name
        )
        assert loader and not loader.endswith('.js'), (
            '%s must name a non-bundle loader (an HTML page), got %r' % (name, loader)
        )
        assert os.path.isfile(os.path.join(REPO, loader)), (
            'declared loader %r for %s does not exist' % (loader, name)
        )
    assert len(_INTENTIONALLY_UNBUNDLED) <= 2, (
        'exclusion list is growing (%d) — it is the no-op path for this ratchet; '
        'ship the module instead' % len(_INTENTIONALLY_UNBUNDLED)
    )
