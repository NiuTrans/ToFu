#!/usr/bin/env python3
"""DRIFT-GUARD: git-tracked files must not leak through the opensource export gate.

Why this exists
---------------
Tofu publishes via ``export.py`` (the opensource mode strips personal /
internal / not-part-of-the-product paths), and ``.gitignore`` is the SECOND
line of defense — it stops those same paths from being committed in the first
place. Historically the two lists were maintained by hand and DRIFTED: a path
was added to ``export.py``'s exclude sets but nobody added a matching
``.gitignore`` rule, so it sat *tracked* in the repo. Nothing broke while
publication went through export (it got stripped there), but the moment anyone
pushed the working repo directly to a public remote, it leaked
(``uploads/`` papers, ``overleaf_cache/`` unpublished theses, ``.tofu/skills``
personal notes, ``scripts/`` / ``benchmarks/`` with hardcoded internal paths…).

The root cause is exactly this drift. This guard closes it by asserting the
invariant DIRECTLY against the live git index + the live ``export.py`` rules,
so the two can never silently diverge again:

    Every git-tracked file MUST be shippable in an ``opensource`` export,
    except a small, explicitly documented allow-list of keepers.

Sibling of ``tests/test_runtime_layout.py::test_gitignore_covers_registry``,
which guards the *mutable-user-state* axis (data/ logs/ uploads/ … from the
``runtime_layout`` registry). That test's docstring deliberately scopes ITSELF
out of the "not part of the public product" axis (scripts/ benchmarks/ debug/
promo/ …). THIS test covers that second axis, keyed on ``export.py`` instead of
the runtime_layout registry — together they cover both.

Keepers (tracked on purpose, even though export excludes them)
--------------------------------------------------------------
A few files are export-excluded yet MUST stay tracked in the private repo:
  * ``export.py`` / ``CLAUDE.md`` — the export infra + agent-rules doc; excluded
    from the *published* tree but are real source in the working repo.
  * ``static/provider_templates/meituan.json`` — a functional internal provider
    template (export sanitizes/strips it for the public build).
  * ``export._OPENSOURCE_KEEP_FILES`` — files that live inside an
    opensource-excluded dir but the public build restores verbatim
    (e.g. ``scripts/gen_desktop_icons.py`` for CI icon generation).
Each keeper is a project-root-relative path and is the ONLY sanctioned way to
keep a tracked-but-export-excluded file: add it here + document why.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── The keeper allow-list: tracked-on-purpose despite being export-excluded. ──
# Everything here is a project-root-relative path. Adding an entry is the
# deliberate, reviewable act of saying "yes, this export-excluded file stays in
# the private repo" — the guard fails loudly for anything NOT listed.
_EXTRA_KEEPERS = {
    'export.py',                                 # export infra (sanitization patterns)
    'CLAUDE.md',                                 # agent-rules doc (stripped from public)
    'static/provider_templates/meituan.json',    # functional internal provider template
    # Dir-placeholder: export strips ALL of uploads/ (runtime data), but this
    # 0-byte marker stays tracked so the dir exists in a fresh checkout. It is
    # export-excluded (uploads/ is an ALWAYS_EXCLUDE dir) yet legitimately
    # tracked — the canonical keep-the-dir/drop-its-contents pattern.
    'uploads/images/.gitkeep',
}


def _tracked_files():
    """Every git-tracked path (project-root-relative, '/'-separated)."""
    out = subprocess.run(
        ['git', 'ls-files', '-z'], cwd=_ROOT,
        capture_output=True, text=True)
    return [p for p in out.stdout.split('\0') if p]


def _keeper_set(export):
    return _EXTRA_KEEPERS | set(export._OPENSOURCE_KEEP_FILES)


def _opensource_excluded(export, rel):
    """True if ``export.py`` would strip ``rel`` from an OPENSOURCE export.

    Mirrors ``export._should_exclude(rel, mode='opensource')`` exactly:
    path-COMPONENT membership in ALWAYS/OPENSOURCE exclude dirs, basename
    membership in the exclude file sets, then the ALWAYS glob set on the
    basename. Kept in lock-step with export.py on purpose — if that logic
    changes, update here (the neuter proves this predicate is load-bearing).
    """
    from fnmatch import fnmatch
    parts = rel.split('/')
    filename = parts[-1]
    for part in parts:
        if part in export.ALWAYS_EXCLUDE_DIRS:
            return True
        if part in export.OPENSOURCE_EXTRA_EXCLUDE_DIRS:
            return True
    if filename in export.ALWAYS_EXCLUDE_FILES:
        return True
    if filename in export.OPENSOURCE_EXTRA_EXCLUDE_FILES:
        return True
    for glob_pat in export.ALWAYS_EXCLUDE_GLOBS:
        if fnmatch(filename, glob_pat):
            return True
    return False


def _offenders(export, tracked):
    keepers = _keeper_set(export)
    return [f for f in tracked
            if f not in keepers and _opensource_excluded(export, f)]


def _require_git_repo():
    if not os.path.isdir(os.path.join(_ROOT, '.git')):
        pytest.skip('not a git checkout (e.g. running inside an exported tree)')


def test_no_tracked_file_is_opensource_excluded():
    """DRIFT-GUARD: no git-tracked file may be stripped by the opensource export
    gate, except the documented keepers. A failure means export.py and
    .gitignore have drifted — a path was excluded from the public build but
    left tracked, so a direct ``git push`` of the working repo would leak it.
    """
    _require_git_repo()
    export = pytest.importorskip(
        'export', reason='export.py not shipped in opensource tree')
    tracked = _tracked_files()
    assert tracked, 'git ls-files returned nothing — not a real checkout?'

    offenders = _offenders(export, tracked)
    assert not offenders, (
        f'{len(offenders)} git-tracked file(s) would be STRIPPED by the '
        'opensource export but are NOT gitignored — export.py and .gitignore '
        'have DRIFTED (a leak path). First 30:\n  ' +
        '\n  '.join(offenders[:30]) +
        '\n→ Either add a .gitignore rule + `git rm --cached` the path, or (if '
        'it must stay tracked) add it to _EXTRA_KEEPERS / '
        'export._OPENSOURCE_KEEP_FILES with a documented reason.')


def test_keepers_are_actually_tracked_and_excluded():
    """Sanity: every keeper is (a) genuinely tracked and (b) genuinely
    export-excluded. A keeper that is no longer tracked, or no longer excluded,
    is dead allow-list entry that masks nothing and should be removed — this
    keeps the allow-list honest so it can't quietly grow into a blanket bypass.
    """
    _require_git_repo()
    export = pytest.importorskip('export', reason='export.py not shipped')
    tracked = set(_tracked_files())
    stale = []
    for k in _keeper_set(export):
        if k not in tracked:
            stale.append((k, 'not tracked'))
        elif not _opensource_excluded(export, k):
            stale.append((k, 'not export-excluded (keeper unnecessary)'))
    assert not stale, (
        'keeper allow-list is stale — these entries no longer serve a purpose:\n  ' +
        '\n  '.join(f'{k}: {why}' for k, why in stale))


def test_neuter_guard_has_teeth():
    """Poison the keeper set to EMPTY → the guard must fail (export.py + CLAUDE.md
    are genuinely tracked-and-excluded). Proves the assertion cross-checks the
    real index against real export rules, not a tautology that always passes."""
    _require_git_repo()
    export = pytest.importorskip('export', reason='export.py not shipped')
    tracked = _tracked_files()

    real_extra = _EXTRA_KEEPERS.copy()
    real_keep = set(export._OPENSOURCE_KEEP_FILES)
    globals()['_EXTRA_KEEPERS'] = set()
    export._OPENSOURCE_KEEP_FILES = set()
    try:
        offenders = _offenders(export, tracked)
        assert offenders, (
            'neuter failed: emptying the keeper set produced NO offenders — the '
            'guard is not actually reading the keeper allow-list (or export.py '
            'no longer excludes export.py/CLAUDE.md).')
    finally:
        globals()['_EXTRA_KEEPERS'] = real_extra
        export._OPENSOURCE_KEEP_FILES = real_keep
    # Restored: real run is clean again.
    assert not _offenders(export, tracked)


def main():
    print('\n═══ export-exclude drift-guard ═══\n')
    for fn in (test_no_tracked_file_is_opensource_excluded,
               test_keepers_are_actually_tracked_and_excluded,
               test_neuter_guard_has_teeth):
        try:
            fn()
            print('  \033[32m✓\033[0m', fn.__name__)
        except Exception as e:  # noqa: BLE001 — standalone runner surfaces all
            print('  \033[31m✗\033[0m', fn.__name__, '—', e)
            sys.exit(1)
    print('\n═══ ALL PASSED ═══\n')


if __name__ == '__main__':
    main()
