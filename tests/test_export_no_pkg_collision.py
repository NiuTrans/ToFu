#!/usr/bin/env python3
"""Guard tests for export.py directory-exclusion collisions.

Background — the `lib/paper` drop bug:
  export.py excludes a top-level junk dir named ``paper`` (an academic
  paper draft). The tar / walk exclusion matches by BASENAME ANYWHERE in
  the tree, so an unanchored ``paper`` exclude also matched ``lib/paper``
  — silently stripping the core package while ``routes/paper.py`` (a file,
  not a dir) survived. The destination then died at startup with
  ``ModuleNotFoundError: No module named 'lib.paper'``.

  The fix was ``_TOP_LEVEL_ONLY_EXCLUDE_DIRS`` (currently {'paper',
  'tools'}), anchored as ``--exclude=./paper`` so nested dirs of the same
  name survive. This test makes the whole class of bug a red test instead
  of a silent broken export: it fails the day a new unanchored exclude
  name collides with a nested git-tracked package.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/paper/report_engine.py'}

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# export.py is the maintainer's release tool; it is intentionally NOT shipped
# in the opensource build, so these guard tests (which import it) can only run
# in the source tree.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _nested_tracked_dirs() -> set[str]:
    """Basenames of every git-tracked directory that is NOT at depth 0."""
    files = subprocess.check_output(
        ['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    nested = set()
    for f in files:
        parts = f.split('/')
        # parts[:-1] are dir components; index 0 is the top-level component.
        for i, p in enumerate(parts[:-1]):
            if i >= 1:
                nested.add(p)
    return nested


def test_no_unanchored_exclude_collides_with_nested_pkg():
    """Every unanchored exclude-dir name must NOT also name a nested dir.

    If it does, that nested package would be silently stripped on export
    (this is exactly how lib/paper was lost). The fix is to add the name
    to _TOP_LEVEL_ONLY_EXCLUDE_DIRS — or, if the strip is deliberate, to
    _INTENTIONAL_NESTED_STRIP with a documented reason.
    """
    from export import (PERSONAL_EXCLUDE_DIRS, ALWAYS_EXCLUDE_DIRS,
                        OPENSOURCE_EXTRA_EXCLUDE_DIRS,
                        _TOP_LEVEL_ONLY_EXCLUDE_DIRS)

    # Unanchored exclude names whose collision with a nested tracked dir is
    # INTENTIONAL — the nested dir IS the strip target and dropping it on
    # export is correct, so anchoring it top-level-only would defeat its
    # purpose. Each entry MUST document the nested dir it strips + why. The
    # guard still fires for any OTHER (accidental) collision.
    _INTENTIONAL_NESTED_STRIP = {
        # static/icons/pet/_candidates/ — raw AI-gen asset candidates + proof
        # strips (multi-MB, review-only). No top-level `_candidates` package
        # exists to protect; unlike lib/paper this nested dir is NOT a runtime
        # package, so the unanchored strip is the intended behavior.
        '_candidates',
    }
    unanchored = ((PERSONAL_EXCLUDE_DIRS | ALWAYS_EXCLUDE_DIRS
                   | OPENSOURCE_EXTRA_EXCLUDE_DIRS)
                  - _TOP_LEVEL_ONLY_EXCLUDE_DIRS
                  - _INTENTIONAL_NESTED_STRIP)
    collide = sorted(unanchored & _nested_tracked_dirs())
    assert not collide, (
        'Unanchored export-exclude dir name(s) collide with a nested '
        f'git-tracked package: {collide}. These dirs would be SILENTLY '
        'stripped on export (the lib/paper drop bug). Add each to '
        '_TOP_LEVEL_ONLY_EXCLUDE_DIRS in export.py so it is anchored to '
        'the project root only.')
    _ok('no unanchored exclude name collides with a nested tracked package')


def test_top_level_only_names_are_not_in_global_sets():
    """A top-level-only name must NOT also live in a global exclude set.

    If it did, the global (unanchored) entry would win and re-introduce
    the collision the anchoring was meant to prevent.
    """
    from export import (PERSONAL_EXCLUDE_DIRS, ALWAYS_EXCLUDE_DIRS,
                        OPENSOURCE_EXTRA_EXCLUDE_DIRS,
                        _TOP_LEVEL_ONLY_EXCLUDE_DIRS)

    leaked = sorted(_TOP_LEVEL_ONLY_EXCLUDE_DIRS & (
        PERSONAL_EXCLUDE_DIRS | ALWAYS_EXCLUDE_DIRS
        | OPENSOURCE_EXTRA_EXCLUDE_DIRS))
    assert not leaked, (
        f'Name(s) {leaked} are in _TOP_LEVEL_ONLY_EXCLUDE_DIRS AND a global '
        'exclude set — the global entry makes them unanchored again. Remove '
        'them from the global set(s).')
    _ok('top-level-only names are not duplicated in any global exclude set')


def test_should_exclude_keeps_nested_pkgs():
    """_should_exclude must NOT prune lib/paper or lib/tools in any mode."""
    from export import _should_exclude

    for mode in ('personal', 'internal', 'opensource'):
        assert _should_exclude('lib/paper/report_engine.py',
                               'report_engine.py', mode) is None, \
            f'lib/paper wrongly excluded in {mode} mode'
        assert _should_exclude('lib/tools/project.py',
                               'project.py', mode) is None, \
            f'lib/tools wrongly excluded in {mode} mode'
    _ok('_should_exclude keeps lib/paper and lib/tools in all modes')


def test_should_exclude_still_prunes_top_level():
    """The top-level junk dirs the anchoring protects must still be pruned."""
    from export import _should_exclude

    for mode in ('personal', 'internal', 'opensource'):
        assert _should_exclude('paper/draft.tex', 'draft.tex', mode), \
            f'top-level paper/ not excluded in {mode} mode'
        assert _should_exclude('tools/md2cards.js', 'md2cards.js', mode), \
            f'top-level tools/ not excluded in {mode} mode'
    _ok('_should_exclude still prunes top-level paper/ and tools/')


def main():
    print()
    print(_color('═══ export.py exclusion-collision Guard Tests ═══', '36'))
    print()
    tests = [
        test_no_unanchored_exclude_collides_with_nested_pkg,
        test_top_level_only_names_are_not_in_global_sets,
        test_should_exclude_keeps_nested_pkgs,
        test_should_exclude_still_prunes_top_level,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
