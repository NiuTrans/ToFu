#!/usr/bin/env python3
"""Tests for lib.runtime_layout — the single-source install-tree runtime-state
registry that self_update (and, later, export.py + .gitignore generation)
derive from.

Two guarantees:

  1. GOLDEN byte-identity — the derived ``RUNTIME_STATE_PREFIXES`` /
     ``OVERLAY_SKIP_PREFIXES`` tuples exactly reproduce the literal tuples
     self_update.py hard-coded before the extraction, so re-keying the updater
     onto the registry is provably a no-op for the classify/skip decisions.
  2. Classification correctness + NEUTER — user/runtime state (data/, logs/,
     .tofu memories, uploads, bundle, .git) is recognised; real source (lib/,
     routes/, server.py, requirements.txt) is NOT; and a poisoned registry
     (drop the data/ entry) makes the classifier wrongly wave the DB through —
     proving the assertions have teeth.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/.project_sessions/abc/modifications.json', 'lib/foo.py',
    'routes/chat.py', 'static/vite/manifest.json',
}

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# The EXACT literal tuples self_update.py carried before the extraction. If the
# registry ever diverges from these, the updater's classify/skip behaviour
# changed — this golden makes that change loud and deliberate.
_GOLDEN_RUNTIME_STATE = (
    '.tofu/',
    'data/',
    'logs/',
    'uploads/',
    'outputs/',
    'overleaf_cache/',
    'lib/.project_sessions/',
)
_GOLDEN_OVERLAY_SKIP = _GOLDEN_RUNTIME_STATE + (
    '.git/', '.venv/', 'venv/', 'node_modules/', '__pycache__/',
    '.update_backup/',
)


def test_golden_byte_identity():
    from lib.runtime_layout import OVERLAY_SKIP_PREFIXES, RUNTIME_STATE_PREFIXES
    assert RUNTIME_STATE_PREFIXES == _GOLDEN_RUNTIME_STATE, (
        f'RUNTIME_STATE_PREFIXES drifted:\n'
        f'  got:  {RUNTIME_STATE_PREFIXES}\n  want: {_GOLDEN_RUNTIME_STATE}')
    assert OVERLAY_SKIP_PREFIXES == _GOLDEN_OVERLAY_SKIP, (
        f'OVERLAY_SKIP_PREFIXES drifted:\n'
        f'  got:  {OVERLAY_SKIP_PREFIXES}\n  want: {_GOLDEN_OVERLAY_SKIP}')
    _ok('derived prefixes are byte-identical to the historical self_update literals')


def test_self_update_reexports_match():
    """self_update must re-export the SAME tuples (its dirty-classify + overlay
    decisions are the registry's now)."""
    import lib.runtime_layout as rl
    import lib.self_update as su
    assert su._RUNTIME_STATE_PREFIXES == rl.RUNTIME_STATE_PREFIXES
    assert su._OVERLAY_SKIP_PREFIXES == rl.OVERLAY_SKIP_PREFIXES
    _ok('self_update re-exports the registry tuples (single source)')


def test_classification():
    from lib.runtime_layout import is_overlay_skipped, is_runtime_state
    # Mutable runtime/user state — tolerated as dirty, preserved by overlay.
    state = ['data/tofu.db', 'data/config/server_config.json', 'logs/app.log',
             'uploads/images/x.png', 'outputs/run1/report.md',
             'overleaf_cache/proj/main.tex',
             '.tofu/skills/mine.md', '.tofu/file-history/x',
             '.tofu_trash/y', './data/tofu.db',
             # nested undo store (multi-segment registry prefix)
             'lib/.project_sessions/abc/modifications.json',
             'data/project_sessions/abc/modifications.json']
    for p in state:
        assert is_runtime_state(p), f'{p} should be runtime state'
        assert is_overlay_skipped(p), f'{p} should be overlay-skipped'
    # Real shippable source — must be updated, never treated as user state.
    source = ['server.py', 'lib/foo.py', 'routes/chat.py', 'requirements.txt',
              'frontend/src/main.ts', 'static/styles.css', 'VERSION',
              'static/vite/manifest.json']
    for p in source:
        assert not is_runtime_state(p), f'{p} must NOT be runtime state'
        assert not is_overlay_skipped(p), f'{p} must NOT be overlay-skipped'
    # VCS/build-only: overlay-skipped but NOT "runtime state" (a .git edit is a
    # blocking dirty change, not tolerable churn).
    for p in ['.git/config', '.venv/pyvenv.cfg', '__pycache__/x.pyc',
              '.update_backup/20260101/x']:
        assert is_overlay_skipped(p), f'{p} should be overlay-skipped'
        assert not is_runtime_state(p), f'{p} must NOT be runtime state'
    _ok('classification: state skipped, source copied, VCS overlay-only')


def test_neuter_drop_data_entry():
    """Poison the registry (drop the data/ entry) → is_runtime_state must wrongly
    wave the DB through, proving the positive assertion has teeth."""
    import lib.runtime_layout as rl
    orig = rl.RUNTIME_STATE_PREFIXES
    try:
        rl.RUNTIME_STATE_PREFIXES = tuple(p for p in orig if p != 'data/')
        # data/ no longer classified as state → the guard is defeated.
        assert not rl.is_runtime_state('data/tofu.db'), (
            'neuter failed: data/ still matched after removal — the classifier '
            'is not actually reading RUNTIME_STATE_PREFIXES')
    finally:
        rl.RUNTIME_STATE_PREFIXES = orig
    # Restored.
    assert rl.is_runtime_state('data/tofu.db')
    _ok('neuter: dropping data/ from the registry defeats the DB guard (has teeth)')


def test_gitignore_lines():
    from lib.runtime_layout import gitignore_lines
    lines = gitignore_lines()
    joined = '\n'.join(lines)
    # Every INSTALL_STATE dir + the .tofu* glob must appear.
    for expect in ('data/', 'logs/', 'uploads/', 'outputs/',
                   'overleaf_cache/', '.tofu*'):
        assert expect in joined, f'gitignore block missing {expect!r}'
    # Each ignore entry is preceded by a comment line.
    assert lines.count('data/') == 1
    _ok('gitignore_lines: renders every runtime-state dir + the .tofu* glob')


# ── Drift-guard helpers ──────────────────────────────────────────────────

def _registry_dir_prefixes():
    """The INSTALL_STATE entries that name a directory (trailing '/').

    These are the "mutable user state" dirs whose coverage in the export
    exclude-sets and .gitignore we drift-guard. Filename-prefix entries
    (e.g. ``static/js/bundle-``) are covered by an export GLOB / a .gitignore
    glob, not a dir-name membership check, so they're asserted separately.
    """
    from lib.runtime_layout import INSTALL_STATE
    return [e.prefix.rstrip('/') for e in INSTALL_STATE if e.prefix.endswith('/')]


def test_export_covers_registry_internal_opensource():
    """DRIFT-GUARD: every runtime-state DIR in the registry must be excluded by
    export.py's internal + opensource modes (the modes that strip runtime
    state). If someone adds ``data/foo/`` to the registry but forgets export,
    this fails — closing the loop the objective is about.

    Axis note — personal mode is DELIBERATELY excluded from this assertion.
    export.py documents personal as a full same-user backup that KEEPS
    ``data/config`` credentials + ``uploads/`` (only chat-history *.db/pgdata
    are skipped), so ``data``/``uploads`` are intentionally ABSENT from
    ``PERSONAL_EXCLUDE_DIRS``. Asserting personal-mode coverage would encode a
    false expectation and break a real, intended behaviour.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    export = pytest.importorskip('export', reason='export.py not shipped in opensource')
    # The dir set that internal/opensource _should_exclude keys on. bundle-*
    # is a GLOB (ALWAYS_EXCLUDE_GLOBS), so dir membership is the right check
    # only for the trailing-'/' entries.
    internal_dirs = export.ALWAYS_EXCLUDE_DIRS | export.ALWAYS_EXCLUDE_ROOT_ONLY_DIRS
    opensource_dirs = (export.ALWAYS_EXCLUDE_DIRS | export.ALWAYS_EXCLUDE_ROOT_ONLY_DIRS
                       | export.OPENSOURCE_EXTRA_EXCLUDE_DIRS)
    missing = []
    for d in _registry_dir_prefixes():
        # export.py prunes by path-COMPONENT membership (``part in
        # ALWAYS_EXCLUDE_DIRS`` over Path(rel).parts), so a nested registry
        # prefix like ``lib/.project_sessions`` is excluded via its basename
        # ``.project_sessions``. Compare on the final segment to mirror that.
        seg = os.path.basename(d)
        if seg not in internal_dirs:
            missing.append(('internal', seg))
        if seg not in opensource_dirs:
            missing.append(('opensource', seg))
    assert not missing, (
        'export exclude-sets DRIFTED from the runtime_layout registry — these '
        'registry dirs are not excluded by export:\n  ' +
        '\n  '.join(f'{mode}: {d!r}' for mode, d in missing) +
        '\n→ add each to export.ALWAYS_EXCLUDE_DIRS (see CLAUDE.md / runtime_layout).')
    _ok('export internal+opensource exclude every registry runtime-state dir')


def test_neuter_export_guard_has_teeth():
    """Poison the registry with a fake dir → the export drift-guard must fail,
    proving it actually cross-checks the registry against export (not a
    tautology)."""
    pytest.importorskip('export', reason='export.py not shipped in opensource')
    import lib.runtime_layout as rl
    orig = rl.INSTALL_STATE
    try:
        rl.INSTALL_STATE = orig + (
            rl.RuntimeEntry('__nonexistent_state_dir__/', 'data', False, 'fake'),
        )
        raised = False
        try:
            test_export_covers_registry_internal_opensource()
        except AssertionError:
            raised = True
        assert raised, (
            'neuter failed: adding a fake registry dir did NOT trip the export '
            'drift-guard — the guard is not reading INSTALL_STATE')
    finally:
        rl.INSTALL_STATE = orig
    _ok('neuter: a fake registry dir trips the export drift-guard (has teeth)')


def _live_gitignore_text():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, '.gitignore'), encoding='utf-8') as fh:
        return fh.read()


def test_gitignore_covers_registry():
    """DRIFT-GUARD: the live .gitignore must ignore every runtime-state dir in
    the registry (so state never leaks into version control) + the bundle glob
    + the .tofu* glob. Matches an ignore entry whether written bare (``data/``)
    or root-anchored (``/data/``)."""
    text = _live_gitignore_text()
    lines = {ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith('#')}

    def _ignored(dirname: str) -> bool:
        # Covered if the dir itself is ignored (bare or root-anchored) OR any
        # ignore rule targets a path UNDER it. The latter accepts the common
        # keep-the-dir-drop-its-contents pattern (e.g. ``uploads/images/*`` +
        # a tracked ``uploads/images/.gitkeep``) as legitimate coverage — the
        # registry's "runtime state" intent is met by content-level rules. A
        # registry dir with ZERO .gitignore mention (the neuter) still trips.
        cands = {dirname, dirname + '/', '/' + dirname, '/' + dirname + '/'}
        if cands & lines:
            return True
        return any(ln.lstrip('/').startswith(dirname + '/') for ln in lines)

    missing = [d for d in _registry_dir_prefixes() if not _ignored(d)]
    assert not missing, (
        '.gitignore DRIFTED from the runtime_layout registry — these registry '
        f'runtime-state dirs are NOT gitignored: {missing}\n→ add them (or run '
        'the generated block from runtime_layout.gitignore_lines()).')
    # Filename-prefix + agent-artifact globs.
    assert any('bundle-' in ln for ln in lines), '.gitignore missing bundle-* glob'
    assert '.tofu*' in lines, '.gitignore missing the .tofu* agent-artifact glob'
    _ok('.gitignore ignores every registry runtime-state dir (+bundle +.tofu* globs)')


def test_neuter_gitignore_guard_has_teeth():
    """Poison the registry with a fake dir → the .gitignore drift-guard must
    fail (the fake dir is not in the real .gitignore), proving it cross-checks
    reality."""
    import lib.runtime_layout as rl
    orig = rl.INSTALL_STATE
    try:
        rl.INSTALL_STATE = orig + (
            rl.RuntimeEntry('__nonexistent_state_dir__/', 'data', False, 'fake'),
        )
        raised = False
        try:
            test_gitignore_covers_registry()
        except AssertionError:
            raised = True
        assert raised, (
            'neuter failed: a fake registry dir did NOT trip the .gitignore '
            'drift-guard')
    finally:
        rl.INSTALL_STATE = orig
    _ok('neuter: a fake registry dir trips the .gitignore drift-guard (has teeth)')


def main():
    print()
    print(_color('═══ runtime_layout registry Tests ═══', '36'))
    print()
    tests = [
        test_golden_byte_identity,
        test_self_update_reexports_match,
        test_classification,
        test_neuter_drop_data_entry,
        test_gitignore_lines,
        test_export_covers_registry_internal_opensource,
        test_neuter_export_guard_has_teeth,
        test_gitignore_covers_registry,
        test_neuter_gitignore_guard_has_teeth,
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
