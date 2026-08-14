#!/usr/bin/env python3
"""Guard: export skips untracked, NON-gitignored ROOT dirs (agent scratch).

Why this exists
---------------
``export.py`` decides what to copy via three layers:
  1. ``_gitignored_excludes`` — everything git IGNORES.
  2. hand-maintained ``ALWAYS_EXCLUDE_*`` sets — known product dirs.
  3. ``_untracked_root_dirs`` / ``_untracked_root_excludes`` — the gap this
     test guards. Both the real tar-copy path AND the ``--dry-run`` preview
     walk consult the same set, so preview == reality.

A directory that is BOTH untracked AND not gitignored (a code-exec run's
``module1/`` / ``module2/`` at the repo root, a stray ``scratchpad/``, a
``.pytest_cache/``) falls through layers 1 & 2 and used to be tar-copied
verbatim into the export — leaking non-product junk and (for opensource)
breaking the ruff gate on unformatted scratch Python.

The subtle correctness constraint: a NEW source file inside a TRACKED dir
(``lib/foo.py``, ``tests/test_x.py``) is ALSO untracked-and-not-ignored, but it
MUST be copied. ``git ls-files -o --directory`` distinguishes them: it collapses
a fully-untracked directory to one ``dir/`` entry but lists individual files
inside tracked dirs. The guard below pins exactly that boundary.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/core.py', 'lib/new_feature.py'}

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path):
    """A throwaway git repo with the four relevant kinds of path."""
    if not subprocess.run(['git', '--version'],
                          capture_output=True).returncode == 0:
        pytest.skip('git not available')
    repo = tmp_path / 'proj'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 't@t')
    _git(repo, 'config', 'user.name', 't')

    # (a) a tracked source dir with a committed file
    (repo / 'lib').mkdir()
    (repo / 'lib' / 'core.py').write_text('x = 1\n')
    (repo / '.gitignore').write_text('ignored_junk/\n*.local_bak\n')
    _git(repo, 'add', 'lib/core.py', '.gitignore')
    _git(repo, 'commit', '-qm', 'init')

    # (b) stray untracked-and-NOT-ignored ROOT dirs → SHOULD be excluded
    (repo / 'module1').mkdir()
    (repo / 'module1' / 'a.py').write_text('bad=1\n')
    (repo / 'scratchpad').mkdir()
    (repo / 'scratchpad' / 'n.txt').write_text('notes\n')

    # (c) new untracked source file INSIDE a tracked dir → MUST NOT be excluded
    (repo / 'lib' / 'new_feature.py').write_text('y = 2\n')

    # (d) a gitignored dir → NOT this function's job (layer 1 handles it)
    (repo / 'ignored_junk').mkdir()
    (repo / 'ignored_junk' / 'z.py').write_text('junk=1\n')

    return repo


def test_stray_root_dirs_excluded_source_kept(tmp_path):
    export = pytest.importorskip('export', reason='export.py not shipped')
    repo = _make_repo(tmp_path)
    excludes = export._untracked_root_excludes(repo)

    # (b) stray root dirs are excluded, anchored at ./
    assert '--exclude=./module1' in excludes
    assert '--exclude=./scratchpad' in excludes

    # (c) a new source file inside a tracked dir is NEVER excluded
    assert not any('new_feature' in e for e in excludes)
    assert not any(e == '--exclude=./lib' for e in excludes)

    # (d) gitignored dir is out of scope here (git ls-files -o omits it)
    assert not any('ignored_junk' in e for e in excludes)


def test_pure_scan_returns_names_only(tmp_path):
    """The pure query both paths share: names only, no ./ prefix / trailing /."""
    export = pytest.importorskip('export', reason='export.py not shipped')
    repo = _make_repo(tmp_path)
    strays = export._untracked_root_dirs(repo)
    assert strays == {'module1', 'scratchpad'}
    # new source inside a tracked dir and gitignored dirs are NOT in the set
    assert 'lib' not in strays and 'ignored_junk' not in strays


def test_dry_run_preview_matches_real_run(tmp_path, monkeypatch, capsys):
    """--dry-run preview must report stray root dirs as EXCLUDED, not "will
    copy" — otherwise the pre-flight lies about what the real tar copy drops."""
    export = pytest.importorskip('export', reason='export.py not shipped')
    repo = _make_repo(tmp_path)
    # The dry-run walk enumerates the module-level ROOT; point it at our repo.
    monkeypatch.setattr(export, 'ROOT', repo)
    dest = tmp_path / 'out'

    export.export_project('opensource', dest, dry_run=True, push=False)
    out = capsys.readouterr().out

    # stray dirs and their contents are NOT previewed as "would copy"
    assert 'module1/a.py' not in out
    assert 'scratchpad/n.txt' not in out
    # they ARE attributed to the untracked-root exclusion reason
    assert 'untracked non-gitignored root dir' in out
    # genuine tracked/new source IS still previewed as copied
    assert 'lib/core.py' in out or 'lib/new_feature.py' in out


def test_neuter_guard_has_teeth(tmp_path):
    """If the stray dirs weren't created, the function must return nothing for
    them — proving the assertion tracks real git state, not a constant."""
    export = pytest.importorskip('export', reason='export.py not shipped')
    if not subprocess.run(['git', '--version'],
                          capture_output=True).returncode == 0:
        pytest.skip('git not available')
    repo = tmp_path / 'clean'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 't@t')
    _git(repo, 'config', 'user.name', 't')
    (repo / 'lib').mkdir()
    (repo / 'lib' / 'core.py').write_text('x = 1\n')
    _git(repo, 'add', 'lib/core.py')
    _git(repo, 'commit', '-qm', 'init')

    # No stray root dirs → no module1/scratchpad excludes.
    excludes = export._untracked_root_excludes(repo)
    assert not any('module1' in e or 'scratchpad' in e for e in excludes)


def test_non_git_dir_is_best_effort_empty(tmp_path):
    export = pytest.importorskip('export', reason='export.py not shipped')
    plain = tmp_path / 'not_a_repo'
    plain.mkdir()
    (plain / 'stray').mkdir()
    # Not a git repo → best-effort empty, never raises.
    assert export._untracked_root_excludes(plain) == []


def main():
    print('\n═══ untracked-root-excludes guard ═══\n')
    import tempfile
    from pathlib import Path
    for fn in (test_stray_root_dirs_excluded_source_kept,
               test_pure_scan_returns_names_only,
               test_neuter_guard_has_teeth,
               test_non_git_dir_is_best_effort_empty):
        try:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
            print('  \033[32m✓\033[0m', fn.__name__)
        except Exception as e:  # noqa: BLE001 — standalone runner surfaces all
            print('  \033[31m✗\033[0m', fn.__name__, '—', e)
            sys.exit(1)
    print('\n═══ ALL PASSED ═══\n')


if __name__ == '__main__':
    main()
