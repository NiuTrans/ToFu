"""Root guidance reaches the model before its first project command."""
import pytest

from lib.project_mod.config import build_root_policy, execution_root_scope
from lib.project_mod.indexer import get_context_for_prompt
from lib.tasks_pkg.context_composer import ComposeRequest, append_context_blocks, context_block
from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance

pytestmark = pytest.mark.unit


def _task(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'AGENTS.md').write_text('root instructions')
    task = {'id': 'bootstrap-guidance', '_userId': 7,
            'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}
    return task, root


def test_first_command_after_root_context_does_not_fail(tmp_path):
    task, root = _task(tmp_path)
    with execution_root_scope(task['id'], task['_workspaceRootPolicy']):
        rendered = get_context_for_prompt(str(root), conv_id=task['id'])
    messages = [{'role': 'user', 'content': 'check the project'}]
    append_context_blocks(messages, [context_block('project_rules', rendered)],
                          ComposeRequest(task=task, user_id=7))
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'run_command', {'command': 'git status'}) == ''
    assert 'root instructions' in str(messages)
    before = list(messages)
    refresh_project_guidance(task, messages, model='')
    assert messages == before
    sub = root / 'sub'
    sub.mkdir()
    (sub / 'AGENTS.md').write_text('nested instructions')
    args = {'command': 'printf changed > sub/file.py'}
    assert check_project_guidance(task, 'run_command', args) == ''
    assert not (sub / 'file.py').exists()
    refresh_project_guidance(task, messages, model='')
    assert 'nested instructions' in str(messages)
    assert check_project_guidance(task, 'run_command', args) == ''


@pytest.mark.parametrize('new_bytes', [b'root instructions\n', b'root instructions\xff'])
def test_raw_rule_change_refreshes_without_blocking_commands(tmp_path, new_bytes):
    from hashlib import sha256
    task, root = _task(tmp_path)
    messages = [{'role': 'user', 'content': 'check'}]
    refresh_project_guidance(task, messages, model='')
    previous = task['_projectGuidance']['delivered'].copy()
    (root / 'AGENTS.md').write_bytes(new_bytes)
    args = {'command': 'git status'}
    assert check_project_guidance(task, 'run_command', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert new_bytes.decode('utf-8', errors='replace').strip() in str(messages)
    assert task['_projectGuidance']['delivered'] != previous
    assert task['_projectGuidance']['delivered'][str(root / 'AGENTS.md')] == sha256(new_bytes).hexdigest()
    assert check_project_guidance(task, 'run_command', args) == ''


def test_bootstrap_open_batch_defers_and_closed_batch_recovers(tmp_path):
    task, _ = _task(tmp_path)
    messages = [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'open', 'type': 'function',
         'function': {'name': 'read_files', 'arguments': '{}'}}]}]
    refresh_project_guidance(task, messages, model='')
    assert len(messages) == 1
    assert check_project_guidance(task, 'run_command', {'command': 'git status'}) == ''
    messages.append({'role': 'tool', 'tool_call_id': 'open', 'content': 'read complete'})
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'run_command', {'command': 'git status'}) == ''


def test_bootstrap_extra_root_and_lost_carrier_recovery(tmp_path):
    task, root = _task(tmp_path)
    extra = tmp_path / 'reference'
    extra.mkdir()
    (extra / 'AGENTS.md').write_text('reference instructions')
    task['config']['projectPaths'] = [str(extra)]
    task['_workspaceRootPolicy'] = build_root_policy(str(root), [str(extra)])
    messages = [{'role': 'user', 'content': 'check'}]
    refresh_project_guidance(task, messages, model='')
    assert f'Applies only within: {extra}' in str(messages)
    args = {'command': 'git status', 'working_dir': str(extra)}
    assert check_project_guidance(task, 'run_command', args) == ''
    messages[:] = [{'role': 'user', 'content': 'compacted'}]
    refresh_project_guidance(task, messages, model='')
    assert 'reference instructions' in str(messages)
    assert check_project_guidance(task, 'run_command', args) == ''


@pytest.mark.parametrize('config_update', [{'project_remote': True}, {'projectPath': ''}])
def test_nonlocal_project_does_not_bootstrap(tmp_path, config_update):
    task, _ = _task(tmp_path)
    task['config'].update(config_update)
    messages = [{'role': 'user', 'content': 'check'}]
    refresh_project_guidance(task, messages, model='')
    assert '_projectGuidance' not in task
    assert len(messages) == 1


@pytest.mark.parametrize('tool_name', [
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents', 'run_command',
])
def test_new_nested_guidance_never_requires_operation_retry(tmp_path, tool_name):
    task, root = _task(tmp_path)
    sub = root / 'sub'
    sub.mkdir()
    guide = sub / 'AGENTS.md'
    guide.write_text('nested instructions')
    path = str(sub / 'file.txt')
    args = ({'command': 'printf changed > sub/file.txt'} if tool_name == 'run_command'
            else {'edits': [{'path': path}]} if tool_name.endswith('s')
            else {'path': path})
    assert check_project_guidance(task, tool_name, args) == ''
    assert not task['_projectGuidance']['delivered']
    messages = [{'role': 'user', 'content': 'implement'}]
    refresh_project_guidance(task, messages, model='')
    assert 'nested instructions' in str(messages)
    guide.write_text('updated nested instructions')
    assert check_project_guidance(task, tool_name, args) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'updated nested instructions' in str(messages)
    guide.unlink()
    assert check_project_guidance(task, tool_name, args) == ''
    refresh_project_guidance(task, messages, model='')
    assert str(guide) not in task['_projectGuidance']['delivered']


@pytest.mark.parametrize('command', [
    'cd "$PWD" && printf changed > file.txt',
    'target=file.txt; printf changed > "$target"',
    '(cd sub); printf changed > sub/file.txt',
])
def test_shell_discovery_uncertainty_does_not_refuse_execution(tmp_path, command):
    task, root = _task(tmp_path)
    (root / 'sub').mkdir()
    assert check_project_guidance(task, 'run_command', {'command': command}) == ''
    messages = [{'role': 'user', 'content': 'implement'}]
    refresh_project_guidance(task, messages, model='')
    assert 'root instructions' in str(messages)


@pytest.mark.parametrize('tool_name', ['write_file', 'run_command'])
def test_handler_executes_first_operation_before_nested_rule_delivery(tmp_path, monkeypatch, tool_name):
    from lib.project_mod.config import clear_conv_state
    from lib.tasks_pkg.handlers import project as handler
    task, root = _task(tmp_path)
    task.update(messages=[], toolRounds=[])
    sub = root / 'sub'
    sub.mkdir()
    (sub / 'AGENTS.md').write_text('nested instructions')
    args = ({'path': 'sub/file.txt', 'content': 'unlocked'} if tool_name == 'write_file'
            else {'command': 'printf unlocked > sub/file.txt'})
    outcomes = []
    monkeypatch.setattr(handler, '_finalize_tool_round',
                        lambda *args, **kwargs: outcomes.append((args[3], kwargs)))
    try:
        result = handler._handle_project_tool(
            task, {}, tool_name, 'unlock-call', args, 1, {},
            task['config'], str(root), True)
        assert (sub / 'file.txt').read_text() == 'unlocked'
        assert len(outcomes) == 1
        assert not outcomes[0][0][0].get('notRun')
        assert 'Project guidance changed' not in str(result)
        messages = [{'role': 'user', 'content': 'implement'}]
        refresh_project_guidance(task, messages, model='')
        assert 'nested instructions' in str(messages)
    finally:
        clear_conv_state(task['id'])


@pytest.mark.parametrize('prefetch', [False, True])
def test_task_preparation_delivers_project_rules_once_before_dispatch(tmp_path, monkeypatch, prefetch):
    from concurrent.futures import Future
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer._providers import _project_rules
    from lib.tasks_pkg.orchestrator import _context_inject as injection
    task, root = _task(tmp_path)
    messages = [{'role': 'user', 'content': 'check the project'}]
    if prefetch:
        future = Future()
        with execution_root_scope(task['id'], task['_workspaceRootPolicy']):
            future.set_result(get_context_for_prompt(str(root), conv_id=task['id'], include_guidance=False))
        task['_prefetch_project'] = future
    def compose(current, **kwargs):
        request = ComposeRequest(task=task, user_id=7, project_enabled=True,
                                 project_path=str(root))
        text = _project_rules(request)
        append_context_blocks(current, [context_block('project_rules', text)], request)
    monkeypatch.setattr(injection, 'compose_task_context', compose)
    injection.inject_context_and_emit_chips(task=task, messages=messages,
        cfg=task['config'], project_path=str(root), project_enabled=True,
        memory_enabled=False, search_enabled=False, has_real_tools=True,
        model='', tool_list=[], prefetch_executor=None, tid=task['id'], t_run_start=0)
    contents = '\n'.join(str(row.get('content') or '') for row in messages)
    assert contents.count('root instructions') == 1
    assert task['_projectGuidance']['delivered']
    before = [dict(row) for row in messages]
    refresh_project_guidance(task, messages, model='')
    assert messages == before
    (root / 'AGENTS.md').write_text('revised root instructions')
    check_project_guidance(task, 'read_files', {'path': str(root)})
    refresh_project_guidance(task, messages, model='')
    assert 'revised root instructions' in str(messages)
