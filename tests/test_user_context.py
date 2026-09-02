"""Contracts for structured, always-on My Context."""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def context_data_dir(tmp_path, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs

    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(tmp_path))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(tmp_path))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    yield tmp_path
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def test_legacy_profile_migrates_to_three_type_store(context_data_dir):
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
    up.save_context_items([
        {'type': 'identity', 'text': 'Works at Meituan'},
        {'type': 'response_preference', 'text': 'Reply in Chinese'},
        {'type': 'work_rule', 'condition': 'using internal documentation',
         'action': 'use the xuecheng MCP'},
    ])
    block = up.render_profile_block()
    assert block is not None
    assert '[USER CONTEXT]' in block
    assert 'Works at Meituan' in block
    assert 'Reply in Chinese' in block
    assert 'using internal documentation' in block
    assert 'use the xuecheng MCP' in block
    assert 'ask before using an alternative' in block


def test_consolidation_adds_structured_rule_with_undo(context_data_dir,
                                                       monkeypatch):
    import lib.memory.profile_consolidate as pc
    import lib.memory.user_profile as up

    response = {'actions': [{
        'kind': 'new', 'type': 'work_rule',
        'condition': 'submitting jobs on our cluster',
        'action': 'use hope MCP',
        'evidence': 'On our cluster, always use hope MCP when submitting jobs.',
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


def test_short_explicit_preference_is_reviewed_and_grounded(context_data_dir,
                                                            monkeypatch):
    """Useful context is not required to be padded past the old 200-char gate."""
    import lib.memory.profile_consolidate as pc
    import lib.memory.user_profile as up

    user_text = '我偏好简洁的中文回答。'
    response = {'actions': [{
        'kind': 'new',
        'type': 'response_preference',
        'text': '默认使用简洁的中文回答',
        'evidence': user_text,
    }]}
    calls = []

    def fake_dispatch(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(response), {}

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', fake_dispatch)

    learned = pc.run_profile_consolidation([
        {'role': 'user', 'content': user_text},
    ])

    assert len(calls) == 1
    assert learned and learned[0]['type'] == 'response_preference'
    assert up.load_context()['items'][0]['text'] == '默认使用简洁的中文回答'


def test_latest_explicit_fact_survives_a_large_historical_turn(context_data_dir,
                                                               monkeypatch):
    """The bounded learner surface must spend its budget on the current turn."""
    import lib.memory.profile_consolidate as pc
    import lib.memory.user_profile as up

    latest = '我在美团担任后端工程师。'
    response = {'actions': [{
        'kind': 'new', 'type': 'identity', 'text': '在美团担任后端工程师',
        'evidence': latest,
    }]}
    monkeypatch.setattr(
        'lib.llm_dispatch.dispatch_chat',
        lambda *args, **kwargs: (json.dumps(response), {}),
    )

    learned = pc.run_profile_consolidation([
        {'role': 'user', 'content': 'x' * 7000},
        {'role': 'assistant', 'content': 'done'},
        {'role': 'user', 'content': latest},
    ])

    assert learned and learned[0]['type'] == 'identity'
    assert up.load_context()['items'][0]['text'] == '在美团担任后端工程师'


def test_short_one_off_request_does_not_call_learner(context_data_dir,
                                                     monkeypatch):
    """The short-turn signal gate avoids cost and accidental one-off learning."""
    import lib.memory.profile_consolidate as pc

    calls = []
    monkeypatch.setattr(
        'lib.llm_dispatch.dispatch_chat',
        lambda *args, **kwargs: calls.append(1) or ('{}', {}),
    )

    assert pc.run_profile_consolidation([
        {'role': 'user', 'content': '请把这个按钮改成蓝色。'},
    ]) == []
    assert calls == []


def test_consolidation_rejects_ungrounded_or_fluffy_output(context_data_dir,
                                                           monkeypatch):
    """A model cannot invent evidence or save conversational framing."""
    import lib.memory.profile_consolidate as pc
    import lib.memory.user_profile as up

    user_text = (
        'I prefer concise answers in every conversation. This is an explicit '
        'long-term response preference that should remain useful in future '
        'conversations rather than applying only to this task.'
    )
    responses = iter((
        {'actions': [{
            'kind': 'new', 'type': 'response_preference',
            'text': 'Use lots of decorative headings',
            'evidence': 'I always ask for decorative headings',
        }]},
        {'actions': [{
            'kind': 'new', 'type': 'response_preference',
            'text': 'The user said they prefer concise answers because it is efficient',
            'evidence': 'I prefer concise answers in every conversation.',
        }]},
        {'actions': [{
            'kind': 'new', 'type': 'response_preference',
            'text': 'Use decorative headings in every answer',
            'evidence': 'I prefer concise answers in every conversation.',
        }]},
    ))
    monkeypatch.setattr(
        'lib.llm_dispatch.dispatch_chat',
        lambda *args, **kwargs: (json.dumps(next(responses)), {}),
    )

    messages = [{'role': 'user', 'content': user_text}]
    assert pc.run_profile_consolidation(messages) == []
    assert pc.run_profile_consolidation(messages) == []
    assert pc.run_profile_consolidation(messages) == []
    assert up.load_context()['items'] == []


def test_consolidation_input_excludes_assistant_reasoning(context_data_dir,
                                                           monkeypatch):
    import lib.memory.profile_consolidate as pc

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
    from lib.memory.storage import clear_memories, create_memory
    import lib.memory.user_profile as up

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
