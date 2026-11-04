"""Project guidance costs context only for genuinely new rule text.

One spec per delivery requirement: each rule file's text is delivered at most
once while it stays in model-visible history (rounds and turns), identical
bytes at another path are a one-line scope note, new rules arrive
incrementally, changes supersede once, deletions retract briefly, a dropped
delivery returns only for root rules and retained work, scopes stay explicit
and an unchanged round does no guidance work. The end-to-end replay walks root -> lib/tasks_pkg -> a
linked worktree -> a second turn.
"""
from copy import deepcopy

import json

import pytest

from lib.project_mod.config import build_root_policy
from lib.project_mod.instructions import read_scoped_guidance, render_guidance
from lib.tasks_pkg import project_guidance
from lib.tasks_pkg.context_composer import (
    ComposeRequest, compact_context_snapshots, context_block,
)
from lib.tasks_pkg.context_composer._render import render_context
from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance

pytestmark = pytest.mark.unit

ROOT_BODY = 'ROOT-AGENTS rule. ' * 120
ENTRY_BODY = 'CLAUDE-ENTRY rule. ' * 40
SUB_BODY = 'SUB-RULES rule. ' * 90
OTHER_BODY = 'OTHER-RULES rule. ' * 70


def _project(tmp_path):
    root = tmp_path / 'project'
    for directory in ('sub', 'other', '.tofu/worktrees/x/sub'):
        (root / directory).mkdir(parents=True)
    (root / 'CLAUDE.md').write_text(ENTRY_BODY)
    (root / 'AGENTS.md').write_text(ROOT_BODY)
    (root / 'sub' / 'AGENTS.md').write_text(SUB_BODY)
    (root / 'other' / 'AGENTS.md').write_text(OTHER_BODY)
    worktree = root / '.tofu' / 'worktrees' / 'x'
    (worktree / 'AGENTS.md').write_text(ROOT_BODY)
    (worktree / 'sub' / 'AGENTS.md').write_text(SUB_BODY)
    return root, worktree


def _task(root, name='turn-1'):
    return {'id': name, 'convId': 'delta-chat', '_userId': 7,
            'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}


def _texts(messages):
    texts = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(part.get('text') or '' for part in content or [] if isinstance(part, dict))
    return texts


def _round(task, messages, *targets):
    """Tool accesses of one model round, then the next round's preparation."""
    for target in targets:
        assert check_project_guidance(task, 'read_files', {'path': str(target)}) == ''
    before = len(messages)
    refresh_project_guidance(task, messages, model='')
    return '\n'.join(_texts(messages[before:]))


def _next_turn(messages, name='turn-2'):
    """A restored model continuation: earlier carriers keep their private fields."""
    return deepcopy(messages) + [{'role': 'user', 'content': f'{name} request'}]


def _visible(messages):
    return '\n'.join(_texts(messages))


def _tool_round(name, arguments, call_id='call-retained'):
    """One assistant tool call and its result, as a compaction retains them."""
    return [{'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': call_id, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': 'ok'}]


def _without(messages, *bodies):
    return [message for message in messages
            if not any(body.strip() in '\n'.join(_texts([message])) for body in bodies)]


def test_unchanged_rules_are_delivered_once_across_rounds(tmp_path):
    root, _ = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    first = _round(task, messages)
    assert ROOT_BODY.strip() in first and ENTRY_BODY.strip() in first
    nested = _round(task, messages, root / 'sub' / 'file.py')
    assert SUB_BODY.strip() in nested and ROOT_BODY.strip() not in nested
    for _ in range(5):
        assert _round(task, messages, root / 'sub' / 'file.py', root / 'file.py') == ''
    visible = _visible(messages)
    for body in (ROOT_BODY, ENTRY_BODY, SUB_BODY):
        assert visible.count(body.strip()) == 1


def test_second_turn_reuses_inherited_rule_blocks(tmp_path):
    root, _ = _project(tmp_path)
    first = _task(root)
    messages = [{'role': 'user', 'content': 'turn-1 request'}]
    _round(first, messages)
    _round(first, messages, root / 'sub' / 'file.py')
    messages = _next_turn(messages)
    second = _task(root, 'turn-2')
    assert _round(second, messages) == ''
    assert _round(second, messages, root / 'sub' / 'file.py') == ''
    visible = _visible(messages)
    for body in (ROOT_BODY, ENTRY_BODY, SUB_BODY):
        assert visible.count(body.strip()) == 1
    assert second['_projectGuidance']['delivered'][str(root / 'sub' / 'AGENTS.md')]


def test_worktree_copy_is_a_one_line_scope_note(tmp_path):
    root, worktree = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    _round(task, messages)
    note = _round(task, messages, worktree / 'src' / 'file.py')
    assert ROOT_BODY.strip() not in note
    sources = [line for line in note.splitlines() if line.startswith('Source: ')]
    assert sources == [f'Source: {worktree / "AGENTS.md"} — same bytes as {root / "AGENTS.md"}; '
                       f'that text applies within {worktree} too and is not repeated here.']
    assert len(note) < 300 + len(sources[0])
    assert _visible(messages).count(ROOT_BODY.strip()) == 1


def test_new_nested_rule_appends_only_its_own_block(tmp_path):
    root, _ = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    _round(task, messages)
    nested = _round(task, messages, root / 'sub' / 'file.py')
    assert SUB_BODY.strip() in nested
    assert ROOT_BODY.strip() not in nested and ENTRY_BODY.strip() not in nested
    framing = 400 + 2 * len(str(root / 'other' / 'AGENTS.md'))  # envelope, Source and scope lines
    assert len(nested) < len(SUB_BODY) + framing
    other = _round(task, messages, root / 'other' / 'file.py')
    assert OTHER_BODY.strip() in other and SUB_BODY.strip() not in other
    assert len(other) < len(OTHER_BODY) + framing


def test_changed_rule_supersedes_once_and_deleted_rule_is_retracted(tmp_path):
    root, _ = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    guide = root / 'sub' / 'AGENTS.md'
    _round(task, messages, guide.parent / 'file.py')
    revised = 'REVISED-SUB rule. ' * 300
    guide.write_text(revised)
    update = _round(task, messages, guide.parent / 'file.py')
    assert '[Context update: project_scoped_rules_' in update and 'replaces all earlier' in update
    assert revised.strip() in update
    assert SUB_BODY.strip() not in update and ROOT_BODY.strip() not in update
    assert _round(task, messages, guide.parent / 'file.py') == ''
    guide.unlink()
    retraction = _round(task, messages, guide.parent / 'file.py')
    assert (f'Source: {guide} — deleted; its earlier text no longer applies '
            f'within {guide.parent}.') in retraction
    assert revised.strip() not in retraction
    # Composer supersede notice and envelope plus one line, independent of rule size.
    assert len(retraction) < 600 + 2 * len(str(guide)) < len(revised)
    assert str(guide) not in task['_projectGuidance']['delivered']
    assert _round(task, messages, guide.parent / 'file.py') == ''
    # Compaction retires the superseded per-file versions, keeping the current one.
    projected = compact_context_snapshots(messages)
    assert revised.strip() not in _visible(projected)
    assert SUB_BODY.strip() not in _visible(projected)
    assert 'deleted; its earlier text' in _visible(projected)
    assert ROOT_BODY.strip() in _visible(projected)
    assert _round(task, projected) == ''


def test_compaction_drop_restores_only_rules_that_retained_work_needs(tmp_path):
    root, worktree = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    _round(task, messages)
    _round(task, messages, root / 'sub' / 'file.py')
    _round(task, messages, root / 'other' / 'file.py')
    _round(task, messages, worktree / 'file.py')
    # The summary replaced the sub/ and other/ accesses with their carriers;
    # one read of sub/ is the retained recent work.
    messages[:] = _without(messages, SUB_BODY, OTHER_BODY) + _tool_round(
        'read_files', {'reads': [{'path': str(root / 'sub' / 'file.py')}]})
    restored = _round(task, messages)
    assert restored.count(SUB_BODY.strip()) == 1
    assert OTHER_BODY.strip() not in restored
    assert ROOT_BODY.strip() not in restored and 'same bytes as' not in restored
    assert str(root / 'other' / 'AGENTS.md') not in task['_projectGuidance']['rules']
    assert _round(task, messages) == ''
    # Revisiting the summarized directory re-acquires its rule like a first touch.
    revisit = _round(task, messages, root / 'other' / 'file.py')
    assert revisit.count(OTHER_BODY.strip()) == 1
    # Root rules return without retained work; dropping the copy that holds
    # identical bytes restores that copy in full, and the surviving scope note
    # keeps pointing at it and is not re-sent.
    messages[:] = _without(messages, ROOT_BODY)
    restored = _round(task, messages)
    assert restored.count(ROOT_BODY.strip()) == 1 and ENTRY_BODY.strip() in restored
    assert 'same bytes as' not in restored and SUB_BODY.strip() not in restored
    assert _round(task, messages) == ''


@pytest.mark.parametrize('defect', ['hash', 'unowned', 'media', 'steering', 'extra_text',
                                  'undelivered', 'malformed_id', 'duplicate'])
def test_scope_retirement_requires_complete_owned_delivery(tmp_path, defect):
    root, _ = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'Keep the request'}]
    _round(task, messages, root / 'other' / 'file.py')
    carrier = messages[-1]
    if defect == 'hash':
        carrier['_contextBlocks'][-1]['hash'] = 'unverified'
    elif defect == 'unowned':
        carrier.pop('_contextComposer')
    elif defect == 'media':
        carrier['content'].append({'type': 'image_url', 'image_url': {'url': 'fixture://image'}})
    elif defect == 'steering':
        carrier['_containsHumanSteer'] = True
    elif defect == 'extra_text':
        carrier['content'].append({'type': 'text', 'text': 'Additional authored instruction'})
    elif defect == 'malformed_id':
        carrier['_contextBlocks'][-1]['id'] = []
    elif defect == 'duplicate':
        carrier['_contextBlocks'].append(dict(carrier['_contextBlocks'][-1]))
    else:
        task['_projectGuidance']['delivered'].clear()
    original = deepcopy(messages)
    assert project_guidance.compact_project_guidance(messages, task) == original
    assert messages == original


def test_scope_retirement_keeps_full_copy_referenced_by_retained_alias(tmp_path):
    root, worktree = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'Inspect the worktree'}]
    _round(task, messages, root / 'sub' / 'file.py', worktree / 'sub' / 'file.py')
    messages.extend(_tool_round('read_files', {'path': str(worktree / 'sub' / 'file.py')}))
    projected = project_guidance.compact_project_guidance(messages, task)
    assert SUB_BODY.strip() in _visible(projected)
    assert f'same bytes as {root / "sub" / "AGENTS.md"}' in _visible(projected)
    assert _round(task, projected) == ''


def test_scope_semantics_survive_notes_and_retractions(tmp_path):
    root, worktree = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    _round(task, messages, root / 'sub' / 'file.py', root / 'other' / 'file.py',
           worktree / 'sub' / 'file.py')
    visible = _visible(messages)
    assert f'Applies only within: {root}\n' in visible
    assert f'Applies only within: {root / "sub"}\n' in visible
    assert f'Applies only within: {root / "other"}\n' in visible
    assert f'that text applies within {worktree} too' in visible
    assert f'that text applies within {worktree / "sub"} too' in visible
    assert f'same bytes as {root / "sub" / "AGENTS.md"}' in visible
    (root / 'other' / 'AGENTS.md').unlink()
    retraction = _round(task, messages, root / 'other' / 'file.py')
    assert f'no longer applies within {root / "other"}.' in retraction
    assert str(root / 'sub') not in retraction


def test_unchanged_round_does_no_guidance_work(tmp_path, monkeypatch):
    from lib.tasks_pkg import context_composer
    root, _ = _project(tmp_path)
    task = _task(root)
    messages = [{'role': 'user', 'content': 'implement'}]
    _round(task, messages, root / 'sub' / 'file.py')
    calls = []
    for name in ('verified_block_hashes', 'append_context_blocks'):
        real = getattr(context_composer, name)
        monkeypatch.setattr(context_composer, name, lambda *args, _real=real, _name=name, **kwargs:
                            calls.append(_name) or _real(*args, **kwargs))
    for _ in range(10):
        before = len(messages)
        refresh_project_guidance(task, messages, model='')
        assert len(messages) == before
    assert calls == []
    # A compactor that copies a carrier forces verification, not a re-send.
    index = next(i for i, message in enumerate(messages) if SUB_BODY.strip() in str(message))
    messages[index] = deepcopy(messages[index])
    refresh_project_guidance(task, messages, model='')
    assert calls == ['verified_block_hashes'] and len(messages) == before
    # An in-place edit that damages the delivered text is detected and repaired.
    calls.clear()
    part = messages[index]['content'][0]
    part['text'] = part['text'].replace('SUB-RULES', 'SUB-RULEZ', 1)
    refresh_project_guidance(task, messages, model='')
    repaired = '\n'.join(_texts(messages[before:]))
    assert calls == ['verified_block_hashes', 'append_context_blocks']
    assert SUB_BODY.strip() in repaired and ROOT_BODY.strip() not in repaired


def test_next_turn_revalidates_inherited_rules(tmp_path):
    root, _ = _project(tmp_path)
    first = _task(root)
    messages = [{'role': 'user', 'content': 'turn-1 request'}]
    _round(first, messages, root / 'sub' / 'file.py', root / 'other' / 'file.py')
    revised = 'REVISED-SUB rule. ' * 30
    (root / 'sub' / 'AGENTS.md').write_text(revised)
    (root / 'other' / 'AGENTS.md').unlink()
    messages = _next_turn(messages)
    second = _task(root, 'turn-2')
    update = _round(second, messages)
    assert revised.strip() in update and '[Context update: project_scoped_rules_' in update
    assert (f'Source: {root / "other" / "AGENTS.md"} — deleted; its earlier text '
            f'no longer applies within {root / "other"}.') in update
    assert ROOT_BODY.strip() not in update and ENTRY_BODY.strip() not in update
    assert _round(second, messages, root / 'sub' / 'file.py') == ''


def test_inherited_rules_do_not_count_toward_the_task_acquisition_bound(tmp_path, monkeypatch):
    root, _ = _project(tmp_path)
    first = _task(root)
    messages = [{'role': 'user', 'content': 'turn-1 request'}]
    _round(first, messages, root / 'sub' / 'file.py', root / 'other' / 'file.py')
    monkeypatch.setattr(project_guidance, 'MAX_GUIDANCE_FILES', 3)
    messages = _next_turn(messages)
    second = _task(root, 'turn-2')
    assert _round(second, messages) == ''
    assert _round(second, messages, root / 'sub' / 'file.py') == ''
    with pytest.raises(ValueError):
        check_project_guidance(second, 'read_files', {'path': str(root / 'other' / 'file.py')})


def test_legacy_combined_snapshot_is_retired_once(tmp_path):
    root, _ = _project(tmp_path)
    messages = [{'role': 'user', 'content': 'old turn'}]
    legacy = render_guidance(read_scoped_guidance(str(root), str(root / 'sub' / 'file.py')))
    render_context(messages, [context_block('project_scoped_rules', legacy)], ComposeRequest())
    messages = _next_turn(messages)
    task = _task(root, 'upgraded-turn')
    migration = _round(task, messages)
    assert 'Superseded: project guidance now arrives as one project_scoped_rules_<id>' in migration
    assert SUB_BODY.strip() in migration and ROOT_BODY.strip() in migration
    assert _round(task, messages, root / 'sub' / 'file.py') == ''
    projected = compact_context_snapshots(messages)
    assert _visible(projected).count(ROOT_BODY.strip()) == 1
    assert _round(task, projected) == ''


def test_replay_root_package_worktree_and_second_turn_costs_only_new_rules(tmp_path):
    """End-to-end replay of a realistic path walk; appended bytes stay proportional."""
    root = tmp_path / 'tofu'
    worktree = root / '.tofu' / 'worktrees' / 'x'
    bodies = {'CLAUDE.md': ENTRY_BODY, 'AGENTS.md': ROOT_BODY,
              'lib/AGENTS.md': 'LIB rule. ' * 160,
              'lib/tasks_pkg/AGENTS.md': 'TASKS rule. ' * 300,
              'lib/tasks_pkg/context_composer/AGENTS.md': 'COMPOSER rule. ' * 60}
    for relative, body in bodies.items():
        for base in (root, worktree):
            if base is worktree and relative == 'CLAUDE.md':
                continue
            (base / relative).parent.mkdir(parents=True, exist_ok=True)
            (base / relative).write_text(body)
    turn_one = [
        [root / 'lib' / 'tasks_pkg' / 'project_guidance.py'],
        [root / 'lib' / 'tasks_pkg' / 'context_composer' / '_render.py'],
        [worktree],
        [worktree / 'lib' / 'tasks_pkg' / 'context_composer' / '_render.py'],
        [worktree / 'lib' / 'tasks_pkg' / 'project_guidance.py'],
    ]
    turn_two = [[root / 'lib' / 'tasks_pkg' / 'context_composer' / '_render.py'],
                [worktree / 'lib' / 'tasks_pkg' / 'project_guidance.py']]
    messages = [{'role': 'user', 'content': 'turn-1 request'}]
    task = _task(root)
    appended = [len(_round(task, messages))]
    appended += [len(_round(task, messages, *targets)) for targets in turn_one]
    messages = _next_turn(messages)
    task = _task(root, 'turn-2')
    second = [len(_round(task, messages))]
    second += [len(_round(task, messages, *targets)) for targets in turn_two]
    distinct = sum(len(body.strip()) for body in bodies.values())
    blocks = len(bodies) + (len(bodies) - 1)  # every root file plus one note per worktree copy
    # Framing per block: envelope plus header lines that name up to three paths.
    framing = 400 + 3 * len(str(worktree / 'lib' / 'tasks_pkg' / 'context_composer' / 'AGENTS.md'))
    assert sum(second) == 0
    assert distinct < sum(appended) <= distinct + blocks * framing
    visible = _visible(messages)
    for body in bodies.values():
        assert visible.count(body.strip()) == 1
