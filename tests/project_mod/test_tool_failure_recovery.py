"""Read-root failures stay item-local; legacy Git keeps compound shell source."""
from pathlib import Path
import shlex
import subprocess

import pytest

from lib.project_mod.config import build_root_policy, execution_root_scope
from lib.project_mod.tools import execute_tool

pytestmark = pytest.mark.unit


def test_unknown_namespace_preserves_valid_reads_and_explains_actual_roots(tmp_path):
    primary = tmp_path / 'autoharness'
    primary.mkdir()
    (primary / 'good.txt').write_text('expected registered content\n')
    other = tmp_path / 'tofu'
    other.mkdir()
    (other / 'good.txt').write_text('absolute read content\n')
    items = []
    with execution_root_scope('read-recovery', build_root_policy(str(primary))):
        result = execute_tool('read_files', {'reads': [
            {'path': 'tofu:good.txt'}, {'path': 'autoharness:good.txt'},
            {'path': str(other / 'good.txt')}, {'path': str(other / 'missing.txt')},
        ]}, str(primary), conv_id='read-recovery', result_projection_items=items)
    assert 'expected registered content' in result
    assert 'absolute read content' in result
    assert 'Unknown workspace root "tofu"' in result
    assert 'available: autoharness' in result
    assert 'absolute path' in result
    assert 'allow_outside_workspace=true' not in result
    assert len(items) == 4
    unknown = next(item for item in items if item['path'] == 'tofu:good.txt')
    assert unknown['status'] == 'error'


def test_all_unknown_namespaces_return_every_error_without_reading_primary(tmp_path):
    primary = tmp_path / 'autoharness'
    primary.mkdir()
    (primary / 'secret.txt').write_text('must not read fallback contents')
    with execution_root_scope('all-errors', build_root_policy(str(primary))):
        result = execute_tool('read_files', {'reads': [
            'tofu:secret.txt', {'path': 'other:secret.txt'}]},
            str(primary), conv_id='all-errors')
    assert 'Unknown workspace root "tofu"' in result
    assert 'Unknown workspace root "other"' in result
    assert 'must not read fallback contents' not in result


@pytest.mark.parametrize('interactive', [False, True])
def test_compound_command_with_host_git_preserves_pwd_and_index(tmp_path, interactive):
    from lib.project_mod.run_command import tool_run_command
    subprocess.run(['/usr/bin/git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'once').write_text('original')
    subprocess.run(['/usr/bin/git', '-C', str(tmp_path), 'add', 'once'], check=True)
    before = (tmp_path / '.git' / 'index').read_bytes()
    command = 'pwd; /usr/bin/git status --short'
    result = tool_run_command(str(tmp_path), command,
                              stdin_callback=(lambda *args: None) if interactive else None)
    assert '[exit code: 0]' in result
    assert str(tmp_path) in result
    assert 'once' in result
    assert (tmp_path / 'once').read_text() == 'original'
    assert (tmp_path / '.git' / 'index').read_bytes() == before
    assert 'pwd;: command not found' not in result
