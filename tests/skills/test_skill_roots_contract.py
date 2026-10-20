"""Contract: the declared skill-root table matches runtime and export behaviour.

``lib/skills/paths.py`` names one table (``PROJECT_SKILL_ROOTS``) as the single
source of truth for where skill packages live, and annotates each row with the
export modes that carry it. Both halves of that claim are executable here:

* every declared root is actually scanned (runtime), and
* the declared ``exported_in`` set equals what ``export_pkg._should_exclude``
  really does with a file inside that root (export).

Without the second half the annotation is just a comment: a skills directory
could start shipping in the opensource mirror and this table would still claim
otherwise. That drift is exactly what leaked internal platform paths before.
"""
from __future__ import annotations

import pytest

from lib.skills.paths import PROJECT_SKILL_ROOTS, skill_scan_dirs

pytestmark = pytest.mark.unit

MODES = ('personal', 'internal', 'opensource')


def _sample_path(subdir: str, skill_id: str = 'contract-skill') -> str:
    return f'{subdir}/{skill_id}/SKILL.md'


@pytest.mark.parametrize('subdir,visibility,exported_in', PROJECT_SKILL_ROOTS)
def test_declared_root_is_actually_scanned(subdir, visibility, exported_in):
    roots = dict(skill_scan_dirs('/proj', None, owner_user_id=1))
    # Global roots share the 'global' scope key; project rows are keyed by path.
    scanned = {path for path, _scope in skill_scan_dirs('/proj', None, owner_user_id=1)}
    assert f'/proj/{subdir}' in scanned, (
        f'{subdir} is declared in PROJECT_SKILL_ROOTS but skill_scan_dirs '
        f'never visits it — the table and the scanner have drifted'
    )
    assert roots  # sanity: the call returns a mapping-like list at all


@pytest.mark.parametrize('subdir,visibility,exported_in', PROJECT_SKILL_ROOTS)
def test_declared_export_visibility_matches_exporter(subdir, visibility, exported_in):
    from export_pkg._excludes import _should_exclude

    relpath = _sample_path(subdir)
    for mode in MODES:
        excluded = _should_exclude(relpath, 'SKILL.md', mode) is not None
        expected_to_ship = mode in exported_in
        assert excluded is not expected_to_ship, (
            f'{relpath}: table says exported_in={exported_in or "()"} '
            f'(mode {mode!r} -> ship={expected_to_ship}) but the exporter '
            f'{"excludes" if excluded else "ships"} it. '
            f'Update PROJECT_SKILL_ROOTS *and* the exporter together.'
        )


def test_no_duplicate_subdirs():
    subdirs = [subdir for subdir, _v, _e in PROJECT_SKILL_ROOTS]
    assert len(subdirs) == len(set(subdirs)), f'duplicate skill roots: {subdirs}'


def test_every_row_declares_valid_visibility_and_modes():
    allowed_modes = set(MODES)
    for subdir, visibility, exported_in in PROJECT_SKILL_ROOTS:
        assert visibility, f'{subdir} must declare a visibility'
        assert set(exported_in) <= allowed_modes, (
            f'{subdir} declares unknown export modes: {set(exported_in) - allowed_modes}'
        )


@pytest.mark.parametrize('subdir,visibility,exported_in', PROJECT_SKILL_ROOTS)
def test_per_machine_roots_never_reach_opensource(subdir, visibility, exported_in):
    """Per-machine agent state must not be published.

    ``.tofu`` and ``.codex`` hold this machine's skills/undo history. They are
    machine-local install artifacts, not product content: shipping them to the
    opensource mirror would leak one operator's skill catalogue.
    """
    if not visibility.endswith('-runtime') and visibility != 'project-codex-dev':
        pytest.skip('root is repository content, not per-machine state')
    assert 'opensource' not in exported_in, (
        f'{subdir} is per-machine state but is exported to opensource'
    )


def test_public_dev_root_is_the_only_one_in_every_mode():
    """A root that ships to opensource must be public-safe by construction.

    Today only the git-tracked development guides qualify. If another root is
    ever added with full export visibility, this assertion forces the author to
    justify it rather than silently widening the public surface.
    """
    full = {subdir for subdir, _v, exported in PROJECT_SKILL_ROOTS
            if set(exported) == set(MODES)}
    assert full == {'.agents/skills'}, (
        f'roots exported to every mode changed: {sorted(full)}. '
        f'Opensource must not carry internal platform paths — '
        f'see docs/modules/skills.md.'
    )
