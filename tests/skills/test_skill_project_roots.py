"""Attached workspace records remain usable by every skill disclosure path."""

import threading

import pytest

from tests.skills.test_skill_activation import _package
from tests.skills.test_skill_installation_contract import isolated as _isolated

pytestmark = pytest.mark.unit


@pytest.fixture(name='isolated')
def skill_workspace(tmp_path, monkeypatch):
    yield from _isolated.__wrapped__(tmp_path, monkeypatch)


def _compose(task, primary, *, enabled=True):
    from lib.tasks_pkg.context_composer import compose_task_context

    messages = [{'role': 'user', 'content': 'Use $secondary-guide.'}]
    compose_task_context(
        messages, user_id=7, model='test', task=task, has_real_tools=True,
        project_path=str(primary), project_enabled=enabled)
    return '\n'.join(str(m.get('content')) for m in messages)


@pytest.mark.parametrize('shape', ['strings', 'records', 'mixed'])
def test_attached_roots_supply_index_and_explicit_instructions(isolated, shape):
    primary, extra = isolated / 'primary', isolated / 'extra'
    _package(primary, '.agents/skills', 'primary-guide', 'PRIMARY WORKFLOW')
    _package(extra, '.agents/skills', 'secondary-guide', 'SECONDARY WORKFLOW')
    paths = [str(primary), str(extra)]
    if shape == 'records':
        paths = [{'path': paths[0], 'access': 'rw'},
                 {'path': paths[1], 'access': 'ro'}]
    elif shape == 'mixed':
        paths[1] = {'path': paths[1], 'access': 'ro'}
    task = {'config': {'projectPaths': paths}, '_userId': 7}
    text = _compose(task, primary)
    assert 'primary-guide' in task['_skillsIndexSnapshot']
    assert 'secondary-guide' in task['_skillsIndexSnapshot']
    assert 'SECONDARY WORKFLOW' in text
    assert task['_selectedSkillsSnapshot']['receipts'][0]['status'] == 'loaded'
    assert str(extra) not in task['_selectedSkillsSnapshot']['content']
    assert task['config']['projectPaths'] == paths


def test_malformed_workspace_entries_cannot_hide_valid_packages(isolated):
    primary, extra = isolated / 'primary', isolated / 'extra'
    _package(extra, '.agents/skills', 'secondary-guide', 'VALID WORKFLOW')
    paths = [None, {}, {'path': {}}, {'path': False}, 42, '', '   ',
             {'path': str(extra), 'access': 'ro'}, str(extra)]
    task = {'config': {'projectPaths': paths}, '_userId': 7}
    assert 'VALID WORKFLOW' in _compose(task, primary)
    assert task['_skillsIndexSnapshot'].count('- secondary-guide (project)') == 1


def test_disabled_project_has_only_owner_global_guides(isolated):
    primary, extra = isolated / 'primary', isolated / 'extra'
    _package(primary, '.agents/skills', 'primary-guide', 'PRIVATE PROJECT BODY')
    _package(extra, '.agents/skills', 'secondary-guide', 'DISABLED ROOT BODY')
    _package(isolated / 'data', 'skills/users/7', 'owner-guide', 'OWNER BODY')
    _package(isolated / 'data', 'skills/users/8', 'foreign-guide', 'FOREIGN BODY')
    task = {'config': {'projectPaths': [{'path': str(extra), 'access': 'ro'}]},
            '_userId': 7}
    text = _compose(task, primary, enabled=False)
    index = task['_skillsIndexSnapshot']
    assert 'owner-guide' in index
    assert 'primary-guide' not in index and 'secondary-guide' not in index
    assert 'foreign-guide' not in index
    assert 'DISABLED ROOT BODY' not in text
    assert not any(r['status'] == 'loaded'
                   for r in task['_selectedSkillsSnapshot']['receipts'])


@pytest.mark.parametrize('enabled', [True, False])
def test_model_skill_load_uses_the_same_attached_root_visibility(isolated, enabled):
    from lib.tasks_pkg.handlers.skills import _handle_skill_tool

    primary, extra = isolated / 'primary', isolated / 'extra'
    _package(extra, '.agents/skills', 'secondary-guide', 'MODEL READ WORKFLOW')
    config = {'projectPaths': [{'path': str(primary), 'access': 'rw'},
                              {'path': str(extra), 'access': 'ro'}]}
    task = {'id': 'workspace-skill-task', '_userId': 7,
            'toolRounds': [], 'events': [], 'events_lock': threading.Lock(),
            '_suppressEvents': True}
    entry = {'query': 'Read a project guide'}
    call_id, content, _ = _handle_skill_tool(
        task, None, 'load_skill', 'read-guide', {'skill_id': 'secondary-guide'},
        0, entry, config, str(primary), enabled)
    assert call_id == 'read-guide'
    assert entry['status'] == ('done' if enabled else 'error')
    assert ('MODEL READ WORKFLOW' in content) is enabled
    assert task['_skillUsage'][-1]['status'] == ('loaded' if enabled else 'unavailable')


def test_attached_record_cannot_escape_project_with_a_skill_root_symlink(isolated):
    primary, extra, outside = [isolated / p for p in ['primary', 'extra', 'outside']]
    _package(outside, '.agents/skills', 'secondary-guide', 'ESCAPED BODY')
    extra.mkdir()
    (extra / '.agents').symlink_to(outside / '.agents', target_is_directory=True)
    task = {'config': {'projectPaths': [{'path': str(extra), 'access': 'ro'}]},
            '_userId': 7}
    text = _compose(task, primary)
    assert 'secondary-guide' not in task['_skillsIndexSnapshot']
    assert 'ESCAPED BODY' not in text
