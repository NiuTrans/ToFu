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
    'JOURNAL.md',                                # internal evolution journal (public history ships via CHANGELOG.md)
    'android/JOURNAL.md',                        # same internal-journal contract as the root JOURNAL.md
    'static/provider_templates/meituan.json',    # functional internal provider template
    # Dir-placeholder: export strips ALL of uploads/ (runtime data), but this
    # 0-byte marker stays tracked so the dir exists in a fresh checkout. It is
    # export-excluded (uploads/ is an ALWAYS_EXCLUDE dir) yet legitimately
    # tracked — the canonical keep-the-dir/drop-its-contents pattern.
    'uploads/images/.gitkeep',
    # ── Internal cost-audit tools (scripts/ is opensource-excluded) ──────────
    # These are NOT in export._OPENSOURCE_KEEP_FILES on purpose: that set means
    # "restore into the PUBLIC build", and neither tool is something the public
    # build runs — they read our own rotated logs and quote our own gateway.
    # They ARE deliberately tracked, with the reasoning recorded in .gitignore
    # right beside their `!/scripts/…` negations: the measurement DISCIPLINE
    # each one encodes is the deliverable, and an untracked copy means the next
    # cost audit goes back to throwaway one-liners and re-makes the errors that
    # took two passes to find. So: tracked, private, and registered here.
    'scripts/cache_waste_report.py',   # cache-waste distribution; pinned by
                                       # tests/test_cache_waste_report.py
    'scripts/cache_ab_probe.py',       # second-path A/B control; names our own
                                       # gateway + an internal report doc
    # Maintainer-only settings panel (cost A/B experiment —
    # lib/settings_panels.MAINTAINER_ONLY_TABS). Export-excluded so the public
    # tree never carries the markup, but it MUST stay tracked here: it is the
    # panel the maintainer's own builds render every day, and
    # tests/test_settings_panels_parity.py guards its marker↔fragment loop.
    # The render-time strip (TOFU_OPENSOURCE_BUILD) is the second line of
    # defense if the file ever ships, same contract as the MCP internal_only
    # catalog filter.
    'static/settings_panels/experiments.html',
}

# ── The pet's RAW MASTER ART: tracked on purpose, never published ────────────
# static/icons/_gen/tofu-pet/_candidates/ai/*.png — the 1024² AI-generated poses
# that process_ai_frames.py turns into the 22 shipped frames.
#
# WHY THEY MUST STAY TRACKED (the constraint that rules out `git rm --cached`):
# they are the pipeline's ONLY input. The 2026-07-31 size-constancy fix works by
# deriving ONE global scale + a shared body/foot anchor from measurements taken
# across ALL masters — so the invariant is only reproducible while they exist.
# Untrack them and the pipeline cannot be re-run from a clean clone: nobody can
# regenerate a frame, verify `--check`, or re-derive the scale, and the guards in
# tests/test_frontend_pet_light_direction.py lose their subject permanently
# rather than skipping in one tree.
#
# WHY THEY MUST NOT BE PUBLISHED: 5.7MB of review-only master art (export.py
# lists `_candidates` in ALWAYS_EXCLUDE_DIRS for exactly this reason). The public
# build ships the finished frames; it has no use for the raws.
#
# Both constraints hold at once, which is precisely what this keeper list is for.
# Registered as a PREFIX rather than 16 hand-copied paths: adding or re-rendering
# a pose must not require editing this file, and every path under the prefix
# carries the identical justification. The prefix is anchored to the one subtree
# (not a bare `_candidates` glob), so it cannot silently absorb an unrelated dir.
_KEEPER_PREFIXES = (
    'static/icons/_gen/tofu-pet/_candidates/',
    # The export toolchain itself. Tracked in the private repo because it IS
    # the release tool, but never published: export_pkg/_lists.py carries
    # private markers (real API keys, Feishu secrets, internal
    # usernames/domains) as sanitization patterns, so the whole package is in
    # OPENSOURCE_EXTRA_EXCLUDE_DIRS. Same contract as the export.py keeper
    # above (ALWAYS_EXCLUDE_FILES) — registered as a prefix so adding a
    # module to the package never requires editing this list.
    'export_pkg/',
)


def _tracked_files():
    """Every tracked path present in this worktree.

    ``git ls-files`` retains an unstaged deletion, but that path is absent from
    both the worktree being tested and the eventual commit. Ignore only those
    absent paths so local release gates match clean-checkout behavior.
    """
    out = subprocess.run(
        ['git', 'ls-files', '-z'], cwd=_ROOT,
        capture_output=True, text=True)
    return [
        path for path in out.stdout.split('\0')
        if path and os.path.isfile(os.path.join(_ROOT, path))
    ]


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
    if parts[0] in export.ALWAYS_EXCLUDE_ROOT_ONLY_DIRS:
        return True
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


def _is_keeper(export, rel):
    """True if ``rel`` is a sanctioned tracked-but-export-excluded file.

    Exact paths come from ``_EXTRA_KEEPERS`` + ``export._OPENSOURCE_KEEP_FILES``;
    ``_KEEPER_PREFIXES`` covers whole subtrees whose members all share one
    justification (see the pet-master note). Prefixes are matched on the '/'-
    terminated form so 'a/b/' can never match a sibling file named 'a/bc'.
    """
    if rel in _keeper_set(export):
        return True
    return any(rel.startswith(p) for p in _KEEPER_PREFIXES)


def _offenders(export, tracked):
    return [f for f in tracked
            if not _is_keeper(export, f) and _opensource_excluded(export, f)]


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


def test_keeper_prefixes_are_live_and_narrow():
    """A keeper PREFIX must still cover real tracked-and-excluded files.

    A prefix is a blanket permission, so it needs the same staleness discipline
    as an exact path — otherwise a subtree that was deleted (or that export.py
    stopped excluding) leaves behind a rule that would silently absorb anything
    later dropped at that path. Asserting both halves keeps it honest: the
    prefix must match at least one tracked file, and every file it matches must
    genuinely be export-excluded (i.e. the prefix is not over-reaching into
    shippable territory).
    """
    _require_git_repo()
    export = pytest.importorskip('export', reason='export.py not shipped')
    tracked = _tracked_files()
    for p in _KEEPER_PREFIXES:
        assert p.endswith('/'), (
            f'keeper prefix {p!r} must end with "/" — an unterminated prefix '
            'also matches sibling paths that merely start with the same text')
        covered = [f for f in tracked if f.startswith(p)]
        assert covered, (
            f'keeper prefix {p!r} covers no tracked file — dead blanket '
            'permission; remove it rather than leaving it to absorb whatever '
            'lands at that path next')
        shippable = [f for f in covered if not _opensource_excluded(export, f)]
        assert not shippable, (
            f'keeper prefix {p!r} covers files that are NOT export-excluded, so '
            f'it is granting permission it does not need: {shippable[:5]}')


def test_neuter_guard_has_teeth():
    """Poison the keeper set to EMPTY → the guard must fail (export.py + CLAUDE.md
    are genuinely tracked-and-excluded). Proves the assertion cross-checks the
    real index against real export rules, not a tautology that always passes.

    The prefix list is emptied TOO. Leaving it populated would let the neuter
    pass while the guard was in fact still consulting a live blanket permission
    — i.e. the neuter would prove less than it claims, which is how a poisoned
    run comes back green for the wrong reason.
    """
    _require_git_repo()
    export = pytest.importorskip('export', reason='export.py not shipped')
    tracked = _tracked_files()

    real_extra = _EXTRA_KEEPERS.copy()
    real_keep = set(export._OPENSOURCE_KEEP_FILES)
    real_prefixes = tuple(_KEEPER_PREFIXES)
    globals()['_EXTRA_KEEPERS'] = set()
    globals()['_KEEPER_PREFIXES'] = ()
    export._OPENSOURCE_KEEP_FILES = set()
    try:
        offenders = _offenders(export, tracked)
        assert offenders, (
            'neuter failed: emptying the keeper set produced NO offenders — the '
            'guard is not actually reading the keeper allow-list (or export.py '
            'no longer excludes export.py/CLAUDE.md).')
    finally:
        globals()['_EXTRA_KEEPERS'] = real_extra
        globals()['_KEEPER_PREFIXES'] = real_prefixes
        export._OPENSOURCE_KEEP_FILES = real_keep
    # Restored: real run is clean again.
    assert not _offenders(export, tracked)


def test_neuter_keeper_prefix_is_load_bearing():
    """Empty ONLY the prefix list → every prefix-covered subtree must resurface
    as offenders (pet masters, export_pkg/ toolchain).

    Distinct from the neuter above, which empties everything at once and so
    cannot tell WHICH mechanism did the work. This proves the prefixes
    specifically are what cover those tracked files, so a future edit that
    drops one fails here with a pointed message instead of silently re-opening
    the drift. Offenders must be exactly the union of the registered prefixes'
    subtrees — anything else means the exact-path keepers drifted.
    """
    _require_git_repo()
    export = pytest.importorskip('export', reason='export.py not shipped')
    tracked = _tracked_files()

    real_prefixes = tuple(_KEEPER_PREFIXES)
    globals()['_KEEPER_PREFIXES'] = ()
    try:
        offenders = _offenders(export, tracked)
        assert offenders, (
            'emptying the keeper PREFIX list produced no offenders — the prefix '
            'mechanism is not load-bearing (are the masters still tracked?)')
        assert all(any(o.startswith(p) for p in real_prefixes) for o in offenders), (
            'the prefixes should cover exactly their documented subtrees; other '
            'offenders appeared, so the exact-path keepers have drifted: '
            f'{offenders[:5]}')
    finally:
        globals()['_KEEPER_PREFIXES'] = real_prefixes
    assert not _offenders(export, tracked)


def main():
    print('\n═══ export-exclude drift-guard ═══\n')
    for fn in (test_no_tracked_file_is_opensource_excluded,
               test_keepers_are_actually_tracked_and_excluded,
               test_keeper_prefixes_are_live_and_narrow,
               test_neuter_guard_has_teeth,
               test_neuter_keeper_prefix_is_load_bearing):
        try:
            fn()
            print('  \033[32m✓\033[0m', fn.__name__)
        except Exception as e:  # noqa: BLE001 — standalone runner surfaces all
            print('  \033[31m✗\033[0m', fn.__name__, '—', e)
            sys.exit(1)
    print('\n═══ ALL PASSED ═══\n')


if __name__ == '__main__':
    main()
