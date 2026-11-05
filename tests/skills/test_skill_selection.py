"""Explicit workflow selections share Chat's loader and provenance boundary."""
import pytest

from tests.skills.test_skill_activation import skill_workspace as _isolated, _package

isolated = _isolated

pytestmark = pytest.mark.unit


def test_bound_selection_is_loaded_without_a_dollar_mention_and_pins_the_guide(isolated):
    from lib.skills.activation import bind_skill_selection, explicit_skill_context
    project = isolated / 'project'
    package = _package(project, '.tofu/skills', 'novelty', 'Require a falsifier.')
    bindings = bind_skill_selection(['novelty'], project_path=str(project), owner_user_id=7)
    args = dict(task={}, project_path=str(project), extra_paths=[], owner_user_id=7,
                required=bindings, model='test')
    selected = explicit_skill_context('Run the next experiment.', **args)
    assert 'Require a falsifier.' in selected['content']
    assert selected['receipts'][0]['trigger'] == 'bound'
    assert selected['receipts'][0]['content_hash'] == bindings[0]['content_hash']
    (package / 'SKILL.md').write_text('---\nname: novelty\n---\nChanged procedure.')
    with pytest.raises(ValueError, match='changed'):
        explicit_skill_context('Next stage', **{**args, 'task': {}})


def test_unavailable_selected_guide_never_becomes_an_empty_success(isolated):
    from lib.skills.activation import bind_skill_selection
    with pytest.raises(ValueError, match='missing'):
        bind_skill_selection(['missing'], owner_user_id=7)


def test_required_selection_cannot_be_silently_deferred(isolated):
    from lib.skills.activation import bind_skill_selection, explicit_skill_context
    project = isolated / 'project'
    _package(project, '.tofu/skills', 'large', '复杂操作说明' * 5000)
    bindings = bind_skill_selection(['large'], project_path=str(project), owner_user_id=7)
    with pytest.raises(ValueError, match='context budget'):
        explicit_skill_context('', task={}, project_path=str(project), extra_paths=[],
                               owner_user_id=7, required=bindings, model='test')


def test_bound_review_receives_criteria_without_project_or_write_authority(isolated):
    from lib.skills.activation import bind_skill_selection
    from lib.tasks_pkg.context_composer import compose_task_context
    project = isolated / 'project'
    _package(project, '.tofu/skills', 'novelty', 'Require a falsifier.')
    binding = bind_skill_selection(['novelty'], project_path=str(project), owner_user_id=7)
    task = {'config': {'skillSelection': binding, 'skillSelectionMode': 'review',
                       'skillSelectionProjectPath': str(project)}, '_userId': 7}
    messages = [{'role': 'user', 'content': 'Review the evidence.'}]
    compose_task_context(messages, task=task, user_id=7, project_enabled=False,
                         has_real_tools=True, model='test')
    body = '\n'.join(str(m.get('content')) for m in messages)
    assert 'Require a falsifier.' in body
    assert 'Do not execute' in body
    assert task['_selectedSkillsSnapshot']['receipts'][0]['status'] == 'loaded'


def test_required_provider_failure_stops_composition(isolated, monkeypatch):
    from lib.tasks_pkg.context_composer import compose_task_context
    from lib.tasks_pkg.context_composer import _providers
    def fail(*args, **kwargs):
        raise OSError('read failed')
    monkeypatch.setattr(_providers, '_skill_context', fail)
    task = {'config': {'skillSelection': [{'skill_id': 'novelty', 'content_hash': 'a' * 16}]}}
    with pytest.raises(ValueError, match='skill'):
        compose_task_context([{'role': 'user', 'content': 'Run'}], task=task,
                             user_id=7, has_real_tools=True, model='test')


def test_required_review_has_only_shared_skill_reads_and_rejects_changed_guide(isolated):
    import threading
    from types import SimpleNamespace
    from lib.skills.activation import bind_skill_selection
    from lib.tools.registry._build import _build_skills, _build_skill_install
    from lib.tasks_pkg.handlers.skills import _handle_skill_tool
    project = isolated / 'project'
    package = _package(project, '.tofu/skills', 'novelty', 'Original check')
    binding = bind_skill_selection(['novelty'], project_path=str(project), owner_user_id=7)
    cfg = {'skillSelection': binding, 'skillSelectionProjectPath': str(project)}
    ctx = SimpleNamespace(lean=False, has_base_tools=False, cfg=cfg)
    assert {tool['function']['name'] for tool in _build_skills(ctx)} == {'search_skills', 'load_skill', 'read_skill_resource'}
    assert not _build_skill_install(ctx)
    task = {'id': 'skill-test-task', '_userId': 7, 'toolRounds': [], 'events': [],
            'events_lock': threading.Lock(), '_suppressEvents': True}
    row = {'query': 'Load the selected guide'}
    _, content, _ = _handle_skill_tool(task, None, 'load_skill', 'one', {'skill_id': 'novelty'},
                                     0, row, cfg, None, False)
    assert 'Original check' in content and row['status'] == 'done'
    (package / 'SKILL.md').write_text('---\nname: novelty\n---\nReplacement check')
    row = {'query': 'Reload the selected guide'}
    _, content, _ = _handle_skill_tool(task, None, 'load_skill', 'two', {'skill_id': 'novelty'},
                                     1, row, cfg, None, False)
    assert row['status'] == 'error' and 'changed' in content and 'Replacement check' not in content
