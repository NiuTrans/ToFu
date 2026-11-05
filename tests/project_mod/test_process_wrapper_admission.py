"""Process wrappers are transparent to admission, risk and isolation analysis.

`timeout`, `nice`, `nohup`, `stdbuf`, `time`, `command` and `exec` run their
operand with the caller's cwd, environment and descriptors, so the operand
decides the workspace effect. Judging the wrapper word admitted
`timeout 900 git switch` with no lease and `nice git reset --hard` past the
shared-checkout guard (observed 2026-10-25).
"""
import subprocess

import pytest

from lib.project_mod.command_analysis import (
    _command_checkout_mutation, command_segment_wrapper_view, git_command_words,
    process_wrapper_operand, unwrap_process_wrappers,
)
from lib.project_mod.git_guard import classify

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'notes.txt').write_text('a\n')
    return root


@pytest.mark.parametrize('command', [
    'timeout 5 git reset --hard HEAD',
    'nice git clean -fdx',
    'stdbuf -o0 git checkout -- .',
    'command git reset --hard',
    'sudo timeout 5 git reset --hard',
])
def test_the_shared_checkout_guard_sees_through_wrappers(repo, command):
    assert classify(command) == 'tree_destroying'
    assert _command_checkout_mutation(command, str(repo))


@pytest.mark.parametrize('command', [
    'command -v git',            # describes the command, runs nothing
    'nice',                      # prints the niceness
    'timeout git status',        # no duration: timeout fails before running
    'timeout $T git reset --hard',  # duration computed by the shell
    'time -o report.txt git status',  # writes a report file
    'exec -c git status',        # empty environment
    'stdbuf git status',         # stdbuf refuses to run without a mode
    'nohup --version',
])
def test_unproven_wrapper_forms_stay_opaque(command):
    assert command_segment_wrapper_view(command) == command


def test_wrapper_operand_grammar():
    assert process_wrapper_operand(['timeout', '10', 'git']) == 2
    assert process_wrapper_operand(['timeout', 'inf', 'git']) == 2
    assert process_wrapper_operand(['timeout', '--signal=TERM', '3s', 'git']) == 3
    assert process_wrapper_operand(['nice', '-n', '-5', 'git']) == 3
    assert process_wrapper_operand(['nice', '--adjustment=3', 'git']) == 2
    assert process_wrapper_operand(['nohup', '--', 'git']) == 2
    assert process_wrapper_operand(['stdbuf', '--output=L', 'git']) == 2
    assert process_wrapper_operand(['/usr/bin/time', '-f', '%e', 'git']) == 3
    assert process_wrapper_operand(['git', 'status']) is None
    assert process_wrapper_operand(['timeout', '10']) is None
    assert unwrap_process_wrappers(['sudo', 'nice', 'timeout', '5', 'git', 'log']) == ['git', 'log']
    assert git_command_words(['env', 'A=1', 'timeout', '5', 'git', 'log']) == ['git', 'log']
    assert command_segment_wrapper_view(
        'FOO=1 timeout --foreground 5 nice -n 2 git log > out') == 'git log > out'
    # `sudo -i` changes the program's cwd, so the view keeps privilege wrappers.
    assert command_segment_wrapper_view('sudo timeout 5 rm -rf x') == 'sudo timeout 5 rm -rf x'


def test_isolation_attributes_a_wrapped_git_target(tmp_path):
    from lib.tasks_pkg.workspace_delivery import command_workspace_refusal
    canonical = tmp_path / 'canonical'
    owned = tmp_path / 'owned'
    for root in (canonical, owned):
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
    task = {'id': 'wrapped-isolation', 'config': {'projectPath': str(owned)},
            '_workspaceExecution': {'repositories': [
                {'canonicalRoot': str(canonical), 'workspacePath': str(owned)}]}}
    for command in (f'git -C {canonical} reset --hard',
                    f'timeout 60 git -C {canonical} reset --hard'):
        assert 'workspace-isolation' in command_workspace_refusal(task, command, str(owned))
