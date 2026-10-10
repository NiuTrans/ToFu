"""Real request selection, project-source isolation, and bounded skill state."""
import pytest

from tests.skills.test_skill_installation_contract import isolated as _isolated_fixture, _skill_text

pytestmark = pytest.mark.unit


@pytest.fixture(name='isolated')
def skill_workspace(tmp_path, monkeypatch):
    yield from _isolated_fixture.__wrapped__(tmp_path, monkeypatch)


def _package(project, subdir, name, body='unique workflow instructions'):
    path = project / subdir / name
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(_skill_text(name, body), encoding='utf-8')
    return path


def test_project_sources_precedence_and_escape(isolated):
    from lib.skills import list_skills, load_skill
    project = isolated / 'project'
    _package(project, '.agents/skills', 'shared', 'agent guide')
    _package(project, '.tofu/skills', 'shared', 'tofu override')
    _package(project, '.codex/skills', 'codex-guide')
    assert {s['id'] for s in list_skills(str(project), owner_user_id=7)} == {
        'shared', 'codex-guide'}
    assert 'tofu override' in load_skill('shared', str(project), owner_user_id=7)
    other = isolated / 'other'
    _package(other, '.agents/skills', 'foreign')
    escaped = isolated / 'escaped'
    escaped.mkdir()
    (escaped / '.agents').symlink_to(other / '.agents', target_is_directory=True)
    assert list_skills(str(escaped), owner_user_id=8) == []


def test_explicit_request_discloses_body_and_receipt_before_model_actions(isolated):
    from lib.tasks_pkg.context_composer import compose_task_context
    project = isolated / 'project'
    _package(project, '.agents/skills', 'ui-guide', 'Measure the composer overflow.')
    messages = [{'role': 'user', 'content': 'Use $ui-guide to fix this interface.'}]
    task = {'config': {}, '_userId': 7}
    compose_task_context(messages, user_id=7, project_path=str(project),
                         project_enabled=True, has_real_tools=True,
                         task=task, model='test')
    text = '\n'.join(str(m.get('content')) for m in messages)
    assert 'Measure the composer overflow.' in text
    assert 'instruction-only' in text
    assert 'workspace files' in text
    assert text.count('<selected_skills>') == 1
    snapshot = task['_selectedSkillsSnapshot']
    assert snapshot['receipts'][0]['status'] == 'loaded'
    assert snapshot['receipts'][0]['content_hash']
    assert str(project) not in snapshot['content']
    assert any(row.get('id') == 'selected_skills' for row in task['_contextManifest'])


def test_explicit_only_skill_answers_dollar_mention(isolated):
    """allow_implicit_invocation=false leaves the index but keeps $id."""
    from lib.skills.activation import explicit_skill_context
    project = isolated / 'project'
    path = _package(project, '.agents/skills', 'vault-guide',
                    'EXPLICIT ONLY BODY')
    skill_md = path / 'SKILL.md'
    text = skill_md.read_text(encoding='utf-8')
    skill_md.write_text(
        text.replace('---\n\n',
                     'allow_implicit_invocation: false\n---\n\n', 1),
        encoding='utf-8')
    result = explicit_skill_context(
        'Open $vault-guide', task={}, project_path=str(project),
        extra_paths=[], owner_user_id=7)
    assert 'EXPLICIT ONLY BODY' in result['content']
    assert result['receipts'][0]['status'] == 'loaded'


def test_selection_honors_disabled_missing_and_owner_boundaries(isolated):
    from lib.skills.activation import explicit_skill_context
    from lib.skills import set_skill_enabled
    project = isolated / 'project'
    _package(project, '.agents/skills', 'off-guide', 'DO NOT DISCLOSE')
    set_skill_enabled('off-guide', False, str(project), owner_user_id=7)
    _package(isolated / 'data', 'skills/users/7', 'private-guide', 'PRIVATE BODY')
    result = explicit_skill_context(
        '$off-guide $private-guide $missing', task={}, project_path=str(project),
        extra_paths=[], owner_user_id=8)
    assert 'DO NOT DISCLOSE' not in result['content']
    assert 'PRIVATE BODY' not in result['content']
    assert all(r['status'] == 'unavailable' for r in result['receipts'])


def test_selection_snapshot_and_large_pages_are_bounded(isolated):
    from lib.skills.activation import explicit_skill_context
    from lib.skills.injection import _count_tokens
    project = isolated / 'project'
    for name in ('one', 'two', 'three', 'four'):
        _package(project, '.agents/skills', name, '复杂操作说明' * 2000)
    task = {}
    kwargs = dict(task=task, project_path=str(project), extra_paths=[],
                  owner_user_id=7, model='test')
    first = explicit_skill_context('$one $two $three $four', **kwargs)
    assert len(first['receipts']) == 3
    assert _count_tokens(first['content'], 'test') < 8000
    assert first['content'].endswith('</selected_skills>')
    assert explicit_skill_context('$one $two $three $four', **kwargs) is first
    assert explicit_skill_context('Now explain the result.', **kwargs)['content'] == ''


@pytest.mark.parametrize('query', [
    'software architecture plan review', 'UI visual hierarchy design critique',
    'frontend visual design interaction animation',
])
def test_generic_words_cannot_route_into_unrelated_catalog(isolated, query):
    from lib.skills.discovery import search_skill_catalog
    matches = search_skill_catalog(query, owner_user_id=7)
    assert not {m.get('catalog_id') for m in matches} & {
        'pptx-skill', 'general-video', 'hyperframes-design'}


@pytest.mark.parametrize('query', [
    '帮我开发一个翻译项目,要有 agent 能力和高水平的页面设计',
    '新建一个企业项目的管理页面 UI',
    'build a new project page with the tofu design system',
])
def test_new_project_ui_routes_to_design_system_skill(isolated, query):
    """One-sentence project requests must land on the design-system skill,
    not on a blank stylesheet (docs/DESIGN_SYSTEM.md#agent-driven)."""
    from pathlib import Path

    from lib.skills.discovery import search_skill_catalog
    repo_root = Path(__file__).resolve().parents[2]
    matches = search_skill_catalog(
        query, project_path=str(repo_root), owner_user_id=7)
    assert any(m.get('skill_id') == 'tofu-design-system' for m in matches), (
        f'query {query!r} did not route to tofu-design-system; '
        f'top matches: {[(m.get("skill_id"), m.get("score")) for m in matches[:5]]}')


def test_receipts_do_not_grow_with_long_running_tasks():
    from lib.skills.activation import record_skill_use
    task = {}
    for index in range(100):
        record_skill_use(task, {'skill_id': str(index), 'status': 'loaded'})
    assert len(task['_skillUsage']) == 32
    assert task['_skillUsage'][-1]['skill_id'] == '99'


def test_shell_examples_do_not_activate_skills_and_snapshots_keep_owner(isolated):
    from lib.skills.activation import explicit_skill_context
    _package(isolated / 'data', 'skills/users/7', 'private-guide', 'PRIVATE GUIDE')
    task = {}
    kwargs = dict(task=task, project_path=None, extra_paths=[], owner_user_id=7)
    assert explicit_skill_context('Explain echo $HOME', **kwargs)['content'] == ''
    assert explicit_skill_context('Inspect:\n```sh\necho $private-guide\n```',
                                  **kwargs)['content'] == ''
    assert 'PRIVATE GUIDE' in explicit_skill_context('$private-guide', **kwargs)['content']
    kwargs['owner_user_id'] = 8
    assert 'PRIVATE GUIDE' not in explicit_skill_context('$private-guide', **kwargs)['content']
