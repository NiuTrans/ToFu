"""Contracts for structured, always-on My Context."""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def context_data_dir(tmp_path, monkeypatch):
    from lib.memory import storage
    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(tmp_path))
    return tmp_path


def test_legacy_profile_migrates_to_three_type_store(context_data_dir):
    from lib.memory import user_profile as up
    path = up.profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('## Preferences\n- Reply in Chinese\n'
                     '## About the user\n- Works at Meituan\n'
                     '## Old custom heading\n- Uses an internal cluster\n')

    status = up.context_status()
    assert [item['type'] for item in status['items']] == [
        'response_preference', 'identity', 'identity']
    assert all(item['source'] == 'legacy_migration'
               for item in status['items'])
    assert os.path.isfile(up.context_path())


def test_work_rule_roundtrip_and_hard_cap(context_data_dir):
    from lib.memory import user_profile as up
    result = up.create_context_item({
        'type': 'work_rule',
        'condition': 'submitting a job on our cluster',
        'action': 'use the hope MCP',
    })
    item = result['item']
    assert item['id'].startswith('ctx_')
    assert up.load_context()['items'][0]['condition'].startswith('submitting')

    with pytest.raises(up.ContextValidationError):
        up.create_context_item({
            'type': 'identity',
            'text': 'x' * (up.CONTEXT_CHAR_CAP + 10),
        })


def test_assistant_change_can_be_undone_and_conflicts_are_safe(context_data_dir):
    from lib.memory import user_profile as up
    added = up.create_context_item(
        {'type': 'identity', 'text': 'Works at Meituan'},
        source='assistant', record_change=True)
    assert added['change_id'].startswith('chg_')
    assert up.undo_context_change(added['change_id'])['undone']
    assert up.load_context()['items'] == []

    added = up.create_context_item(
        {'type': 'response_preference', 'text': 'Reply in Chinese'},
        source='assistant', record_change=True)
    up.update_context_item(added['item']['id'], {'text': 'Reply concisely'})
    with pytest.raises(up.ContextConflictError):
        up.undo_context_change(added['change_id'])


def test_all_context_categories_are_injected_for_unrelated_query(context_data_dir):
    from lib.memory import user_profile as up
    up.save_context_items([
        {'type': 'identity', 'text': 'Works at Meituan'},
        {'type': 'response_preference', 'text': 'Reply in Chinese'},
        {'type': 'work_rule', 'condition': 'using internal documentation',
         'action': 'use the xuecheng MCP'},
    ])
    core, detail = up.render_profile_tiers(query='unrelated CSS question')
    assert detail is None
    assert '[USER CONTEXT]' in core
    assert 'Works at Meituan' in core
    assert 'Reply in Chinese' in core
    assert 'using internal documentation' in core
    assert 'use the xuecheng MCP' in core
    assert 'ask before using an alternative' in core


def test_consolidation_adds_structured_rule_with_undo(context_data_dir,
                                                       monkeypatch):
    from lib.memory import profile_consolidate as pc
    from lib.memory import user_profile as up

    response = {'actions': [{
        'kind': 'new', 'type': 'work_rule',
        'condition': 'submitting jobs on our cluster',
        'action': 'use hope MCP',
        'evidence': 'the user explicitly said so',
    }]}
    monkeypatch.setattr(
        'lib.llm_dispatch.dispatch_chat',
        lambda *args, **kwargs: (json.dumps(response), {}))
    messages = [
        {'role': 'user', 'content':
         'On our cluster, always use hope MCP when submitting jobs. This is '
         'a durable internal rule that I want you to remember in future '
         'conversations, rather than a one-off instruction for only this task.'},
        {'role': 'assistant', 'content':
         'Understood. I will apply that explicit rule in future conversations.'},
    ]
    learned = pc.run_profile_consolidation(messages)
    assert len(learned) == 1
    assert learned[0]['change_id'].startswith('chg_')
    assert up.load_context()['items'][0]['type'] == 'work_rule'
    assert up.undo_context_change(learned[0]['change_id'])['undone']


def test_consolidation_input_excludes_assistant_reasoning(context_data_dir,
                                                           monkeypatch):
    from lib.memory import profile_consolidate as pc

    captured = {}

    def fake_dispatch(messages, **kwargs):
        captured['surface'] = messages[1]['content']
        return (json.dumps({'actions': []}), {})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', fake_dispatch)
    pc.run_profile_consolidation([
        {'role': 'user', 'content':
         'I explicitly work at Meituan and want that durable fact remembered. '
         'This message is intentionally long enough for the context learner '
         'to run while keeping the evidence entirely in a real user message. '
         'Please retain it for future conversations and unrelated projects.'},
        {'role': 'assistant', 'content':
         'SECRET MODEL REASONING: use a speculative workaround forever'},
        {'role': 'tool', 'content': 'SECRET TOOL OUTPUT'},
        {'role': 'user', '_isMeta': True,
         'content': '[PROJECT CO-PILOT MODE] SECRET SYNTHETIC CONTEXT'},
    ])
    assert 'I explicitly work at Meituan' in captured['surface']
    assert 'SECRET MODEL REASONING' not in captured['surface']
    assert 'SECRET TOOL OUTPUT' not in captured['surface']
    assert 'SECRET SYNTHETIC CONTEXT' not in captured['surface']


def test_clear_memories_preserves_context_and_skill_packages(context_data_dir,
                                                              tmp_path):
    from lib.memory import clear_memories, create_memory
    from lib.memory import user_profile as up

    project = tmp_path / 'project'
    project.mkdir()
    global_memory = create_memory(
        'Global lesson', body='A verified reusable lesson for future tasks.',
        scope='global')
    project_memory = create_memory(
        'Project lesson', body='A verified project-specific failure pattern.',
        scope='project', project_path=str(project))
    up.create_context_item({'type': 'identity', 'text': 'Works at Meituan'})

    skill = context_data_dir / 'skills' / 'global' / 'demo-skill'
    skill.mkdir(parents=True)
    skill_file = skill / 'SKILL.md'
    skill_file.write_text(
        '---\nname: Demo skill\ndescription: A reusable installed skill package\n'
        '---\n\n# Demo\n', encoding='utf-8')

    preview = clear_memories(project_path=str(project), dry_run=True)
    assert preview['total'] == 2
    assert preview['global'] == 1 and preview['project'] == 1
    result = clear_memories(project_path=str(project))
    assert set(result['deleted_ids']) == {
        global_memory['id'], project_memory['id']}
    assert result['failed_ids'] == []
    assert up.load_context()['items'][0]['text'] == 'Works at Meituan'
    assert skill_file.is_file()
