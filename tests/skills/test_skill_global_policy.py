"""Skills are owner-global (owner decision 2026-10-31).

Problem: a skill living in one project's ``.agents/skills`` was invisible to
every other conversation, and when a global copy existed the project copy
replaced it inside that project, so edits landed in a copy other
conversations never saw. Decision: the owner-global store wins every id
(``lib.skills.paths.skill_scan_dirs``); ``lib.skills.promote`` moves project
packages to it through the validated installer without touching the source.

Run:  pytest tests/skills/test_skill_global_policy.py -m unit
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.skills.test_skill_installation_contract import isolated as _isolated_fixture

pytestmark = pytest.mark.unit


@pytest.fixture(name='isolated')
def skill_workspace(tmp_path, monkeypatch):
    yield from _isolated_fixture.__wrapped__(tmp_path, monkeypatch)


def _pkg(root: Path, name: str, body: str, description: str | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description or name + " workflow"}\n---\n\n{body}\n',
        encoding='utf-8')
    return path


def _global_root(owner: int) -> Path:
    from lib.skills.paths import owner_global_skills_dir
    return Path(owner_global_skills_dir(owner))


def _install_into_project(root: Path, incoming: Path, **provenance) -> Path:
    """Install through the real installer so the project copy carries Tofu origin markers."""
    from lib.skills.installer import install_skill_package
    project = root / 'project'
    project.mkdir(exist_ok=True)
    result = install_skill_package(
        str(incoming), scope='project', project_path=str(project), **provenance)
    return Path(result['memory']['filepath']).parent


def _global_origin(owner: int, skill_id: str) -> dict:
    return json.loads((_global_root(owner) / skill_id / '.skill-origin.json').read_text(encoding='utf-8'))


def test_global_package_shadows_same_id_project_copy(isolated):
    from lib.skills import list_skills, load_skill
    project = isolated / 'project'
    _pkg(project / '.agents' / 'skills', 'shared', 'stale project copy')
    _pkg(project / '.agents' / 'skills', 'project-only', 'only here')
    _pkg(_global_root(7), 'shared', 'canonical global copy')

    rows = {row['id']: row for row in list_skills(str(project), owner_user_id=7)}
    assert rows['shared']['scope'] == 'global'
    assert rows['shared']['shadowed'] == 1
    assert rows['project-only']['scope'] == 'project'
    text = load_skill('shared', str(project), owner_user_id=7)
    assert 'canonical global copy' in text and 'stale project copy' not in text

    both = list_skills(str(project), owner_user_id=7, include_shadowed=True)
    copy = next(row for row in both if row['id'] == 'shared' and row['scope'] == 'project')
    assert copy['shadowed_by'] == rows['shared']['skill_ref']


def test_promote_installs_missing_then_skips_identical_and_keeps_source(isolated):
    from lib.skills import list_skills
    from lib.skills.promote import promote
    source = _pkg(isolated / 'project' / '.agents' / 'skills', 'sandbox-direct', 'version one')
    (source / 'scripts').mkdir()
    (source / 'scripts' / 'run.py').write_text('print(1)\n', encoding='utf-8')

    [row] = promote([str(source)], owner_user_id=7, apply=True, replace=False)
    assert (row['status'], row['action'], row['installed_id']) == ('missing', 'installed', 'sandbox-direct')
    target = _global_root(7) / 'sandbox-direct'
    assert 'version one' in (target / 'SKILL.md').read_text(encoding='utf-8')
    assert (target / 'scripts' / 'run.py').is_file()
    assert (source / 'SKILL.md').is_file(), 'promotion must never remove the project source'
    assert {r['id']: r['scope'] for r in list_skills(None, owner_user_id=7)}['sandbox-direct'] == 'global'

    [again] = promote([str(source)], owner_user_id=7, apply=True, replace=False)
    assert (again['status'], again['action']) == ('identical', 'none')


def test_promote_refuses_a_different_global_package_unless_replace(isolated):
    from lib.skills.promote import promote
    source = _pkg(isolated / 'project' / '.tofu' / 'skills', 'guide', 'project edit')
    _pkg(_global_root(7), 'guide', 'global original')

    [refused] = promote([str(source)], owner_user_id=7, apply=True, replace=False)
    assert (refused['status'], refused['action']) == ('differs', 'refused')
    assert 'global original' in (_global_root(7) / 'guide' / 'SKILL.md').read_text(encoding='utf-8')

    [replaced] = promote([str(source)], owner_user_id=7, apply=True, replace=True)
    assert (replaced['status'], replaced['action']) == ('differs', 'replaced')
    assert 'project edit' in (_global_root(7) / 'guide' / 'SKILL.md').read_text(encoding='utf-8')


def test_promote_carries_the_sealed_provenance_of_an_installed_project_package(isolated):
    from lib.skills.installer import canonical_skill_content_sha256
    from lib.skills.promote import promote
    incoming = _pkg(isolated / 'incoming', 'adapted', 'adapted upstream guide')
    provenance = {
        'catalog_id': 'curated.adapted',
        'source_revision': 'a' * 40,
        'source_registry': 'github-adapted',
        'source_url': 'https://github.com/example/skills/tree/' + 'a' * 40 + '/adapted',
    }
    source = _install_into_project(
        isolated, incoming, expected_content_sha256=canonical_skill_content_sha256(str(incoming)),
        **provenance)

    [row] = promote([str(source)], owner_user_id=7, apply=True, replace=False)
    assert (row['status'], row['action'], row['source_origin']) == ('missing', 'installed', 'sealed')
    assert row['provenance'] == provenance
    origin = _global_origin(7, 'adapted')
    assert {key: origin[key] for key in provenance} == provenance
    assert (origin['owner_user_id'], origin['content_sha256']) == (7, row['digest'])
    assert (_global_root(7) / 'adapted' / '.catalog_id').read_text(encoding='utf-8').strip() == 'curated.adapted'
    assert (source / '.skill-origin.json').is_file(), 'the project source keeps its own record'


def test_promote_carries_no_provenance_once_the_source_was_edited(isolated):
    from lib.skills.promote import promote
    source = _install_into_project(
        isolated, _pkg(isolated / 'incoming', 'local', 'upstream text'),
        source_registry='github-adapted', source_url='https://example.com/local')
    _pkg(source.parent, 'local', 'edited in the project after install')

    [row] = promote([str(source)], owner_user_id=7, apply=True, replace=False)
    assert (row['status'], row['action'], row['source_origin']) == ('missing', 'installed', 'edited')
    assert 'provenance' not in row
    origin = _global_origin(7, 'local')
    assert (origin['source_url'], origin['source_registry']) == ('', '')
    assert 'edited in the project' in (_global_root(7) / 'local' / 'SKILL.md').read_text(encoding='utf-8')


def test_replace_updates_from_a_newer_source_but_never_discards_global_edits(isolated):
    from lib.skills.promote import promote
    skills = isolated / 'project' / '.agents' / 'skills'
    source = _pkg(skills, 'guide', 'version one')
    promote([str(source)], owner_user_id=7, apply=True, replace=False)
    global_md = _global_root(7) / 'guide' / 'SKILL.md'

    _pkg(skills, 'guide', 'version two from the source')
    [updated] = promote([str(source)], owner_user_id=7, apply=True, replace=True)
    assert (updated['status'], updated['action']) == ('differs', 'replaced')
    assert 'version two from the source' in global_md.read_text(encoding='utf-8')

    _pkg(_global_root(7), 'guide', 'edited in the global store')
    [refused] = promote([str(source)], owner_user_id=7, apply=True, replace=True)
    assert (refused['status'], refused['action']) == ('global-edited', 'refused')
    assert 'would discard' in refused['error']
    assert 'edited in the global store' in global_md.read_text(encoding='utf-8')


def test_scan_reports_every_declared_root_and_applies_only_on_request(isolated, capsys):
    from lib.skills import list_skills
    from lib.skills.promote import main
    project = isolated / 'project'
    for subdir, name in (('.tofu/skills', 'a-tofu'), ('.agents/skills', 'b-agents'),
                         ('.codex/skills', 'c-codex'), ('bundles/meituan/skills', 'd-bundle')):
        _pkg(project / subdir, name, f'{name} body')
    (project / '.agents' / 'skills' / 'broken').mkdir()
    (project / '.agents' / 'skills' / 'broken' / 'SKILL.md').write_text('no frontmatter\n', encoding='utf-8')

    assert main(['--owner', '7', '--scan', str(project)]) == 1  # the invalid package is reported
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted(row.get('id', 'invalid') for row in rows) == [
        'a-tofu', 'b-agents', 'c-codex', 'd-bundle', 'invalid']
    assert all(row['action'] == 'none' for row in rows)
    assert not any(r['scope'] == 'global' for r in list_skills(None, owner_user_id=7))

    main(['--owner', '7', '--scan', str(project), '--apply'])
    capsys.readouterr()
    globals_ = {r['id'] for r in list_skills(None, owner_user_id=7) if r['scope'] == 'global'}
    assert {'a-tofu', 'b-agents', 'c-codex', 'd-bundle'} <= globals_


def test_library_installs_default_to_the_global_store(isolated):
    import io
    import zipfile
    from lib.skills.installer import install_skill_package
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('pkg/SKILL.md', '---\nname: defaulted\ndescription: d\n---\n\nbody\n')
    install_skill_package(buffer.getvalue(), owner_user_id=7)
    assert (_global_root(7) / 'defaulted' / 'SKILL.md').is_file()
