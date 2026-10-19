"""Path-scoped rules stay complete and isolated without blocking execution."""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _project(tmp_path):
    project = tmp_path / 'project'
    target = project / 'src' / 'sub' / 'new.py'
    target.parent.mkdir(parents=True)
    (tmp_path / 'AGENTS.md').write_text('foreign server rules')
    (project / 'AGENTS.md').write_text('root rules')
    (project / 'src' / 'AGENTS.md').write_text('src rules')
    (project / 'src' / 'sub' / 'AGENTS.md').write_text('sub rules')
    sibling = project / 'other'
    sibling.mkdir()
    (sibling / 'AGENTS.md').write_text('sibling rules')
    return project, target


def test_nearest_guidance_chain_excludes_siblings_and_server_ancestors(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance, render_guidance
    project, target = _project(tmp_path)
    rows = read_scoped_guidance(str(project), str(target))
    assert [row['content'] for row in rows] == ['root rules', 'src rules', 'sub rules']
    rendered = render_guidance(rows)
    assert 'foreign server rules' not in rendered and 'sibling rules' not in rendered
    assert str(project / 'src' / 'sub' / 'AGENTS.md') in rendered
    assert [row['content'] for row in read_scoped_guidance(str(project), str(project))] == ['root rules']


def test_root_and_extra_root_guidance_are_labeled_not_inferred(tmp_path):
    from lib.project_mod.config import build_root_policy, execution_root_scope
    from lib.project_mod.indexer import get_context_for_prompt
    project, _ = _project(tmp_path)
    reference = tmp_path / 'reference'
    reference.mkdir()
    (reference / 'AGENTS.md').write_text('reference rules')
    with execution_root_scope('owned', build_root_policy(str(project), [str(reference)], [str(reference)])):
        rendered = get_context_for_prompt(str(project), conv_id='owned')
    assert 'reference rules' in rendered and 'root rules' in rendered
    assert f'Applies only within: {reference}' in rendered
    assert 'src rules' not in rendered


def test_outside_and_symlinked_rules_fail_closed(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    with pytest.raises(PermissionError):
        read_scoped_guidance(str(project), str(tmp_path / 'foreign.py'))
    guide = project / 'src' / 'sub' / 'AGENTS.md'
    guide.unlink()
    guide.symlink_to(tmp_path / 'AGENTS.md')
    with pytest.raises(PermissionError): read_scoped_guidance(str(project), str(target))


def test_partial_read_failure_is_not_absence(tmp_path, monkeypatch):
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    import os
    original = os.open
    def fail(path, flags, *args, **kwargs):
        directory = kwargs.get('dir_fd')
        if (path == 'AGENTS.md' and directory is not None and
                Path(f'/proc/self/fd/{directory}').resolve() == project / 'src'):
            raise PermissionError('unavailable')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', fail)
    with pytest.raises(PermissionError): read_scoped_guidance(str(project), str(target))


def test_rule_change_is_fingerprinted_and_oversize_never_clipped(tmp_path):
    from lib.project_mod.instructions import read_root_guidance, MAX_GUIDANCE_BYTES
    project, _ = _project(tmp_path)
    before = read_root_guidance(str(project))[0]['hash']
    (project / 'AGENTS.md').write_text('updated root rules')
    assert read_root_guidance(str(project))[0]['hash'] != before
    (project / 'AGENTS.md').write_text('x' * (MAX_GUIDANCE_BYTES + 1))
    with pytest.raises(ValueError): read_root_guidance(str(project))


def test_mutation_proceeds_while_guidance_refreshes_on_change_and_removal(tmp_path):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, target = _project(tmp_path)
    task = {'id': 'scoped-rule-task', 'convId': 'scope-chat', '_userId': 7,
            'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    args = {'path': str(target), 'content': 'new code'}
    assert check_project_guidance(task, 'write_file', args) == ''
    assert not target.exists()
    messages = [{'role': 'user', 'content': 'implement'}]
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' in str(messages)
    assert check_project_guidance(task, 'write_file', args) == ''
    guide = project / 'src' / 'sub' / 'AGENTS.md'
    guide.write_text('updated subtree rules')
    assert check_project_guidance(task, 'write_file', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'write_file', args) == ''
    guide.unlink()
    assert check_project_guidance(task, 'write_file', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'write_file', args) == ''


def test_open_tool_batch_defers_guidance_without_blocking_mutation(tmp_path):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, target = _project(tmp_path)
    task = {'id': 'scope-open-task', 'convId': 'scope-chat', '_userId': 7,
            'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    args = {'path': str(target)}
    assert check_project_guidance(task, 'write_file', args) == ''
    messages = [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'unfinished', 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{}'}}]}]
    refresh_project_guidance(task, messages, model='')
    assert len(messages) == 1
    assert check_project_guidance(task, 'write_file', args) == ''


@pytest.mark.parametrize('command', ['printf changed > src/sub/new.py', 'cd src/sub && printf changed > new.py'])
def test_shell_target_acquires_nested_guidance_without_blocking(tmp_path, command):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, _ = _project(tmp_path)
    task = {'id': 'shell-guidance', 'convId': 'shell-chat', '_userId': 7,
            'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    messages = [{'role': 'user', 'content': 'work'}]
    assert check_project_guidance(task, 'run_command', {'command': 'git status'}) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' not in str(messages)
    args = {'command': command}
    assert check_project_guidance(task, 'run_command', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' in str(messages)
    assert check_project_guidance(task, 'run_command', args) == ''


def test_two_sibling_guides_retain_subtree_applicability(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance, render_guidance
    project, target = _project(tmp_path)
    rows = read_scoped_guidance(str(project), str(target))
    other = read_scoped_guidance(str(project), str(project / 'other' / 'new.py'))
    combined = list({row['path']: row for row in [*rows, *other]}.values())
    rendered = render_guidance(combined)
    assert f'Applies only within: {project / "src" / "sub"}' in rendered
    assert f'Applies only within: {project / "other"}' in rendered
    assert next(row for row in rows if row['content'] == 'sub rules')['scope'] != str(project)


def test_rule_swap_between_validation_and_open_cannot_import_foreign_bytes(tmp_path, monkeypatch):
    import os
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    guide = project / 'src' / 'sub' / 'AGENTS.md'
    original = os.open
    def swap(path, flags, *args, **kwargs):
        directory = kwargs.get('dir_fd')
        if (path == 'AGENTS.md' and directory is not None and
                Path(f'/proc/self/fd/{directory}').resolve() == guide.parent):
            guide.unlink()
            guide.symlink_to(tmp_path / 'AGENTS.md')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap)
    with pytest.raises((OSError, PermissionError)):
        read_scoped_guidance(str(project), str(target))


@pytest.mark.parametrize('command', [
    'cd -P src/sub && printf changed > new.py',
    '(cd other); printf changed > src/sub/new.py',
    'printf "%s" "$HOME" > src/sub/new.py',
])
def test_shell_structure_or_expansion_cannot_hide_nested_write(tmp_path, command):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, _ = _project(tmp_path)
    task = {'id': 'literal-target-task', '_userId': 7, 'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    messages = [{'role': 'user', 'content': 'implement'}]
    check_project_guidance(task, 'run_command', {'command': 'git status'})
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'run_command', {'command': command}) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' in str(messages)
    assert check_project_guidance(task, 'run_command', {'command': command}) == ''


def test_guidance_stops_before_opening_files_beyond_remaining_budget(tmp_path, monkeypatch):
    import os
    from lib.project_mod.instructions import read_scoped_guidance
    directories = [tmp_path]
    for index in range(5):
        directories.append(directories[-1] / f'sub{index}')
        directories[-1].mkdir()
    for directory in directories:
        (directory / 'AGENTS.md').write_text('x' * 300000)
    opened = []
    real_open = os.open
    def count(path, flags, *args, **kwargs):
        if path == 'AGENTS.md' and kwargs.get('dir_fd') is not None:
            opened.append(path)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', count)
    with pytest.raises(ValueError):
        read_scoped_guidance(str(tmp_path), str(directories[-1] / 'file.py'))
    assert len(opened) == 3  # The fourth file exceeds remaining bytes before open.


def test_verified_deleted_rule_is_pruned_before_candidate_bound_check(tmp_path):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    root = tmp_path / 'project'
    sub = root / 'sub'
    sub.mkdir(parents=True)
    guide = root / 'AGENTS.md'
    guide.write_text('r' * 600000)
    task = {'id': 'removed-rule-task', '_userId': 7, 'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}
    messages = [{'role': 'user', 'content': 'implement'}]
    assert check_project_guidance(task, 'write_file', {'path': str(root / 'file.py')}) == ''
    refresh_project_guidance(task, messages, model='')
    guide.unlink()
    (sub / 'AGENTS.md').write_text('s' * 600000)
    args = {'path': str(sub / 'file.py')}
    assert check_project_guidance(task, 'write_file', args) == ''
    assert str(guide) not in task['_projectGuidance']['rules']
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'write_file', args) == ''


def test_foreground_command_keeps_guidance_cwd_when_background_changes_sticky_cwd(tmp_path, monkeypatch):
    import threading
    from lib.project_mod import tools
    from lib.project_mod.config import build_root_policy, get_conv_cwd, clear_conv_state
    from lib.tasks_pkg import workspace_delivery
    from lib.tasks_pkg.background_commands import BackgroundCommandTask
    from lib.tasks_pkg.handlers import project as handler
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    root, target = _project(tmp_path)
    task = {'id': 'foreground-cwd-task', 'convId': 'cwd-chat', '_userId': 7,
            'messages': [], 'toolRounds': [], 'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}
    args = {'command': 'printf changed > src/sub/new.py'}
    messages = [{'role': 'user', 'content': 'implement'}]
    check_project_guidance(task, 'run_command', args)
    refresh_project_guidance(task, messages, model='')
    entered, changed = threading.Event(), threading.Event()
    observed, worker_errors = [], []
    def capture(cwd, command, **kwargs):
        observed.append((command, cwd))
        return f'$ {command}\n[exit code: 0]'
    monkeypatch.setattr(tools, '_exec_run_command_with_writer', capture)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *_args, **_kwargs: None)
    original = workspace_delivery.rebind_project_tool_arguments
    def pause_after_guidance(current, arguments):
        result = original(current, arguments)
        entered.set()
        assert changed.wait(3)
        return result
    monkeypatch.setattr(workspace_delivery, 'rebind_project_tool_arguments', pause_after_guidance)
    def background():
        try:
            assert entered.wait(3)
            tools.execute_tool('run_command', {'command': 'printf background',
                'working_dir': str(root / 'other')}, str(root), task=BackgroundCommandTask(task),
                task_id=task['id'])
            assert get_conv_cwd(task['id']) == str(root / 'other')
        except BaseException as error:
            worker_errors.append(error)
        finally:
            changed.set()
    worker = threading.Thread(target=background)
    worker.start()
    try:
        handler._handle_project_tool(task, {}, 'run_command', 'cwd-call', args, 1, {},
            task['config'], str(root), True)
        worker.join(3)
        assert not worker.is_alive() and not worker_errors
        assert observed == [('printf background', str(root / 'other')), (args['command'], str(root))]
        assert not target.exists()  # Executor observation only; no mutation test stunt.
    finally:
        changed.set()
        worker.join(3)
        clear_conv_state(task['id'])


@pytest.mark.parametrize('option', ['', '-P '])
def test_symlink_cd_parent_hop_preserves_shell_logical_and_physical_scope(tmp_path, option):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    root = tmp_path / 'project'
    destination = root / 'physical' / 'deep'
    destination.mkdir(parents=True)
    (root / 'link').symlink_to(destination, target_is_directory=True)
    for parent, text in ((root, 'root guide'), (root / 'logical', 'logical target guide'),
                         (root / 'physical' / 'logical', 'physical target guide')):
        parent.mkdir(exist_ok=True)
        (parent / 'AGENTS.md').write_text(text)
    task = {'id': 'symlink-cd-task', '_userId': 7, 'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}
    messages = [{'role': 'user', 'content': 'implement'}]
    args = {'command': f'cd {option}link && cd .. && printf changed > logical/new.py'}
    assert check_project_guidance(task, 'run_command', args) == ''
    refresh_project_guidance(task, messages, model='')
    expected = 'physical target guide' if option else 'logical target guide'
    assert expected in str(messages)
    assert check_project_guidance(task, 'run_command', args) == ''
