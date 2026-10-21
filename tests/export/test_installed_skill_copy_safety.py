"""Installed-skill export must never dereference source or destination links."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['file', 'directory', 'broken', 'root'])
def test_source_link_refuses_before_replacing_existing_package(tmp_path, monkeypatch, kind):
    from export_pkg import _internal

    source = tmp_path / 'source'
    skills = source / '.tofu' / 'skills'
    package = skills / 'demo'
    package.mkdir(parents=True)
    (package / 'SKILL.md').write_text('# new package')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_text('synthetic outside content')
    if kind == 'file':
        (package / 'linked').symlink_to(outside / 'private.txt')
    elif kind == 'directory':
        (package / 'linked').symlink_to(outside, target_is_directory=True)
    elif kind == 'broken':
        (package / 'linked').symlink_to(outside / 'missing')
    else:
        skills.rename(source / '.tofu' / 'real-skills')
        skills.symlink_to(source / '.tofu' / 'real-skills', target_is_directory=True)
    destination = tmp_path / 'destination'
    old = destination / '.tofu' / 'skills' / 'demo'
    old.mkdir(parents=True)
    (old / 'SKILL.md').write_text('# existing package')
    monkeypatch.setattr(_internal, 'ROOT', source)
    monkeypatch.setattr(_internal, '_SKILL_STATE_ROOTS', (('.tofu/skills', '.tofu/skills'),))

    with pytest.raises(RuntimeError, match='symlink'):
        _internal._copy_installed_skills(destination, 'internal')

    assert (old / 'SKILL.md').read_text() == '# existing package'
    assert not (old / 'linked').exists()
    assert (outside / 'private.txt').read_text() == 'synthetic outside content'


@pytest.mark.parametrize('component', ['.tofu', '.tofu/skills', '.tofu/skills/demo', '.tofu/skills/metadata.json'])
def test_destination_link_never_writes_outside_export(tmp_path, monkeypatch, component):
    from export_pkg import _internal

    source = tmp_path / 'source'
    skills = source / '.tofu' / 'skills'
    package = skills / 'demo'
    package.mkdir(parents=True)
    (package / 'SKILL.md').write_text('# package')
    (skills / 'metadata.json').write_text('{}')
    destination = tmp_path / 'destination'
    link = destination / component
    link.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = outside / 'marker'
    marker.write_text('unchanged')
    target = marker if component.endswith('.json') else outside
    link.symlink_to(target, target_is_directory=target.is_dir())
    monkeypatch.setattr(_internal, 'ROOT', source)
    monkeypatch.setattr(_internal, '_SKILL_STATE_ROOTS', (('.tofu/skills', '.tofu/skills'),))

    with pytest.raises(RuntimeError, match='symlink'):
        _internal._copy_installed_skills(destination, 'internal')

    assert marker.read_text() == 'unchanged'
    assert sorted(path.name for path in outside.iterdir()) == ['marker']
