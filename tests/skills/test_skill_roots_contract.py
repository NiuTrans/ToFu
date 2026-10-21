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

import os

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


def test_internal_carry_pass_matches_scanned_roots():
    """The internal-only carry pass must restore roots the scanner searches.

    ``export_pkg._internal._copy_installed_skills`` is the one deliberate
    exception to "internal exports strip ``.tofu*`` and root ``data/``": user
    skills live there, so the export restores them one package at a time after
    the bulk copy. It is a second export surface, so it needs its own lock:
    a destination path that the scanner does not read would ship files nobody
    can use, and a source path that is not a scan root would carry nothing.
    """
    from export_pkg._internal import _SKILL_STATE_ROOTS, _carried_skill_state_roots
    from lib.memory.storage._dirs import SERVER_GLOBAL_SKILLS_SUBPATH
    from lib.skills.paths import OWNER_GLOBAL_SKILLS_SUBPATH

    scanned = {os.path.normpath(path) for path, _scope in
               skill_scan_dirs('/proj', None, owner_user_id=1)}
    declared = {os.path.normpath(f'/proj/{subdir}')
                for subdir, _v, _e in PROJECT_SKILL_ROOTS}
    # Global packages live OUTSIDE any project: they sit under the server data
    # root, which the export destination carries as ``data/``. Both global
    # subpaths must therefore share that one carried root.
    project_reaches = declared | scanned
    global_skills_rel = os.path.commonpath(
        [SERVER_GLOBAL_SKILLS_SUBPATH, OWNER_GLOBAL_SKILLS_SUBPATH])
    global_carried_rel = os.path.normpath(os.path.join('data', global_skills_rel))

    for src_rel, dst_rel in _SKILL_STATE_ROOTS:
        assert src_rel == dst_rel, (
            f'{src_rel} is restored to {dst_rel}: the destination must keep '
            f'the exact relative layout the scanner reads')
        reachable = (
            os.path.normpath(f'/proj/{src_rel}') in project_reaches
            or os.path.normpath(src_rel) == global_carried_rel)
        assert reachable, (
            f'{src_rel} is carried by the internal export but is neither a '
            f'project scan root nor the server data root\'s '
            f'``{global_skills_rel}`` store — the destination would never see '
            f'those packages')

    assert _carried_skill_state_roots('internal') == tuple(
        src for src, _dst in _SKILL_STATE_ROOTS)


def test_carry_pass_never_reaches_public_modes():
    """Personal and opensource exports must not carry a skill catalogue."""
    from export_pkg._internal import _carried_skill_state_roots

    assert _carried_skill_state_roots('opensource') == ()
    assert _carried_skill_state_roots('personal') == ()


def test_carry_pass_merges_and_skips_transient_lock(tmp_path, monkeypatch):
    """Restoring skills must not destroy the destination's own packages."""
    from export_pkg import _internal

    source = tmp_path / 'source'
    (source / '.tofu' / 'skills' / 'carried-skill').mkdir(parents=True)
    (source / '.tofu' / 'skills' / 'carried-skill' / 'SKILL.md').write_text('# carried')
    (source / '.tofu' / 'skills' / 'carried-skill' / 'skill.sig').write_text('sig')
    (source / '.tofu' / 'skills' / '.install.lock').write_text('lock')
    monkeypatch.setattr(_internal, 'ROOT', source)
    monkeypatch.setattr(_internal, '_SKILL_STATE_ROOTS', (('.tofu/skills', '.tofu/skills'),))

    dest = tmp_path / 'dest'
    own = dest / '.tofu' / 'skills' / 'dest-only-skill'
    own.mkdir(parents=True)
    (own / 'SKILL.md').write_text('# dest only')
    stale = dest / '.tofu' / 'skills' / 'carried-skill'
    stale.mkdir(parents=True)
    (stale / 'STALE.md').write_text('# must not survive')

    _internal._copy_installed_skills(dest, 'internal')

    skills = dest / '.tofu' / 'skills'
    assert (skills / 'carried-skill' / 'SKILL.md').exists()
    assert (skills / 'carried-skill' / 'skill.sig').exists(), 'integrity marker dropped'
    assert not (skills / 'carried-skill' / 'STALE.md').exists(), \
        'a replaced package kept files from the previous export'
    assert (own / 'SKILL.md').exists(), 'destination-only skill was destroyed'
    assert not (skills / '.install.lock').exists(), 'transient install lock shipped'

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
