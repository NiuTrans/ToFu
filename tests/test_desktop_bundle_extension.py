"""tests/test_desktop_bundle_extension.py — the frozen desktop app must ship
the browser extension it offers to hand out.

THE DEFECT THIS PINS
--------------------
The Local Control modal shows two rows: "Browser tabs" (with a *Download
extension ZIP* button) and "This computer". The download is served by
``routes/browser.py::browser_download``, which zips the directory
``BASE_DIR/browser_extension`` **at request time** and returns 404 when it is
absent. ``routes/api_v1/browser.py::browser_status`` reads the same directory
to publish ``extensionPath``.

``tofu.spec`` listed ``static/``, ``index.html``, ``VERSION`` and
``.env.example`` in ``datas`` — but not ``browser_extension``. So in the frozen
build the directory does not exist, the button 404s, and ``extensionPath`` is
permanently null. The desktop installer was therefore the ONE distribution in
which the user could not obtain the extension — and it is the distribution
whose users are least able to fall back on "clone the repo and load the folder".

WHY A BEHAVIOURAL ASSERTION AND NOT ``'browser_extension' in tofu.spec``
------------------------------------------------------------------------
A substring check passes on a commented-out line, on a typo'd destination, and
on an entry whose source path does not resolve. The property that actually
matters is: *after the spec's own filtering logic runs, is there a datas entry
whose SOURCE is the real extension directory and whose DEST resolves to the
place the request handlers look?*

So these tests execute the spec's real datas-construction logic and inspect the
result, and separately pin the two path derivations the handlers use — because
the bundle being right is worthless if the handler computes a different path.

``datas`` entries are filtered by ``os.path.exists`` before being appended (a
missing source is a HARD PyInstaller error, so the spec drops absentees and logs
them). That filter is precisely why a silent omission is possible, and why the
test evaluates the post-filter list rather than the literal source text.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = _ROOT / 'tofu.spec'

# The directory name all three sites must agree on.
_EXT_DIRNAME = 'browser_extension'


def _spec_datas() -> list[tuple[str, str]]:
    """Rebuild the spec's post-filter ``datas`` list without running PyInstaller.

    The spec is not importable (``SPECPATH`` and the PyInstaller classes are
    injected by the tool), so we evaluate just the ``_candidate_datas`` literal
    and re-apply the same ``os.path.exists`` filter the spec applies. That keeps
    this test honest about the filter — the mechanism that makes an omission
    silent — instead of reading the source text.
    """
    tree = ast.parse(_SPEC.read_text(encoding='utf-8'))
    node = None
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == '_candidate_datas':
                    node = stmt.value
    assert node is not None, 'tofu.spec no longer defines _candidate_datas'

    root = str(_ROOT)

    def _literal(n):
        """Evaluate os.path.join(ROOT, 'x', ...) / plain string constants."""
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Call):
            parts = [_literal(a) for a in n.args]
            parts = [root if p == '__ROOT__' else p for p in parts]
            return os.path.join(*parts)
        if isinstance(n, ast.Name) and n.id == 'ROOT':
            return '__ROOT__'
        raise AssertionError(f'unsupported datas expression: {ast.dump(n)}')

    out = []
    for elt in node.elts:
        src, dst = elt.elts
        s = _literal(src)
        s = root if s == '__ROOT__' else s
        out.append((s, _literal(dst)))
    # Same filter the spec applies.
    return [(s, d) for s, d in out if os.path.exists(s)]


def test_frozen_bundle_ships_the_browser_extension():
    """A surviving datas entry must carry the real extension directory."""
    entries = _spec_datas()
    hits = [(s, d) for s, d in entries
            if os.path.basename(os.path.normpath(s)) == _EXT_DIRNAME]

    assert hits, (
        'tofu.spec ships no browser_extension entry that survives its own '
        'os.path.exists filter. The frozen app will 404 on '
        '/api/browser/download and report extensionPath=null, so desktop '
        'users cannot obtain the extension at all.\n'
        f'Surviving datas destinations: {[d for _, d in entries]}'
    )

    src, dst = hits[0]
    assert os.path.isdir(src), f'datas source {src} is not a directory'
    assert (Path(src) / 'manifest.json').is_file(), (
        f'{src} has no manifest.json — the bundled folder is not a loadable '
        'unpacked extension'
    )


def test_bundled_extension_lands_where_the_handlers_look():
    """The datas DEST must match the path the request handlers derive.

    Both handlers compute ``<app root>/browser_extension`` from their own
    ``__file__``. In a PyInstaller --onedir build that app root is
    ``_internal/``, which is also where a datas entry with dest
    ``'browser_extension'`` is placed. A dest of ``'.'`` would instead splat the
    extension's files loose into the app root, leaving no directory to zip — the
    bundle would look populated while the endpoint still 404s.
    """
    hits = [(s, d) for s, d in _spec_datas()
            if os.path.basename(os.path.normpath(s)) == _EXT_DIRNAME]
    assert hits, 'no browser_extension datas entry (see the sibling test)'
    _, dst = hits[0]

    assert os.path.normpath(dst) == _EXT_DIRNAME, (
        f"browser_extension is bundled to dest {dst!r}, but the handlers look "
        f"for a DIRECTORY named {_EXT_DIRNAME!r} under the app root. A dest of "
        "'.' scatters the files instead of creating that directory, and the "
        'download endpoint would still 404.'
    )


def test_handlers_still_resolve_the_directory_they_zip():
    """Pin the two derivations, so a relocation cannot silently break the bundle.

    If either module moves to a different nesting depth, its ``os.path.dirname``
    chain changes and it starts looking somewhere the bundle does not populate.
    The bundle entry above would still be present and this suite would still be
    green — which is the same shape of blindness that let the missing entry
    survive in the first place.
    """
    legacy = (_ROOT / 'routes' / 'browser.py').read_text(encoding='utf-8')
    assert "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))" in legacy, (
        'routes/browser.py no longer derives BASE_DIR as <repo>/ from '
        'routes/browser.py — re-verify that BASE_DIR/browser_extension still '
        'matches the tofu.spec datas destination.'
    )
    assert f"'{_EXT_DIRNAME}'" in legacy, (
        f'routes/browser.py no longer references {_EXT_DIRNAME}'
    )

    v1 = (_ROOT / 'routes' / 'api_v1' / 'browser.py').read_text(encoding='utf-8')
    assert v1.count('os.path.dirname(') >= 3, (
        'routes/api_v1/browser.py no longer walks three levels up to the repo '
        'root — extensionPath may now point outside the bundle.'
    )
    assert f"'{_EXT_DIRNAME}'" in v1, (
        f'routes/api_v1/browser.py no longer references {_EXT_DIRNAME}'
    )
