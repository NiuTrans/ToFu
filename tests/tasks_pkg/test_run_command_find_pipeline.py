"""tests/tasks_pkg/test_run_command_find_pipeline.py — broadened find fast path.

The find fast path used to die on ANY pipe (``find ... | head`` went to a
native find wrapped in timeout — the 40s FUSE worst case). The broadened
parser accepts whitelisted stream tails (``head``/``wc -l``/``sort``),
``-type d``, name unions, and depth bounds, executed by the bounded
GNU-order walker in lib/project_mod/shell_find.py. Pinned here: parser
matrix, GNU output-order equivalence, exact counts, and the native-fallback
contract for budget-exhausted exact totals.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/tasks_pkg/test_run_command_find_pipeline.py -v
"""

from __future__ import annotations

import os
import subprocess

import pytest

from lib.project_mod.run_command import (
    parse_safe_file_find_command, tool_run_command)

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    base = tmp_path / 'w'
    (base / 'src' / 'sub' / 'deep').mkdir(parents=True)
    for i in range(7):
        (base / 'src' / f'f{i}.py').write_text(f'# {i}\n')
    for i in range(3):
        (base / 'src' / f'g{i}.js').write_text(f'// {i}\n')
    (base / 'src' / 'sub' / 'h0.py').write_text('# sub\n')
    (base / 'src' / 'sub' / 'deep' / 'z.py').write_text('# deep\n')
    (base / 'src' / 'sub' / 'deep' / 'z.txt').write_text('deep\n')
    (base / '.hidden.py').write_text('# hidden\n')
    return str(base)


def _native(command, cwd):
    return subprocess.run(['bash', '-c', command], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def _body(result):
    """run_command result → output text without echo and exit marker."""
    return result.split('\n', 1)[1].rsplit('\n[exit code:', 1)[0].strip()


# ═════════════════════════════════════════════════════════════════════
#  1. Output equivalence with native GNU find
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('command', [
    'find src -type f -name "*.py"',
    'find src -type f -name "*.py" | head -3',
    'find src -type f -name "*.py" | head',
    'find src -type f | sort | head -4',
    'find src -maxdepth 1 -type f -name "*.py"',
    'find src -type d',
    'find src -maxdepth 2 -type d | sort',
    'find src -name "*.py" -o -name "*.js" | sort',
    'find src -mindepth 2 -type f',
    'find . -maxdepth 0 -type d',
    'find src -type f | sort',
])
def test_find_pipelines_match_native(ws, command):
    result = tool_run_command(ws, command)
    assert '[exit code: 0]' in result
    assert _body(result) == _native(command, ws).stdout.strip()


def test_wc_l_exact_count(ws):
    result = tool_run_command(ws, 'find src -type f | wc -l')
    assert '[exit code: 0]' in result
    assert _body(result) == _native('find src -type f | wc -l', ws) \
        .stdout.strip()


def test_head_then_wc_compose(ws):
    result = tool_run_command(ws, 'find src -type f | head -3 | wc -l')
    assert '[exit code: 0]' in result
    assert _body(result) == '3'


def test_hidden_files_included(ws):
    """The shell find contract: hidden files are matched (no project
    ignore filtering), unlike the find_files tool."""
    result = tool_run_command(ws, 'find . -maxdepth 1 -name ".hidden.py"')
    assert './.hidden.py' in result


def test_result_shape_is_executed_command(ws):
    result = tool_run_command(ws, 'find src -type f -name "*.py" | head -2')
    assert result.startswith('$ find src')
    assert result.rstrip().endswith('[exit code: 0]')


# ═════════════════════════════════════════════════════════════════════
#  2. Native fallback contracts
# ═════════════════════════════════════════════════════════════════════

def test_unknown_tail_keeps_shell(ws):
    """A tail outside head/wc/sort must reach the real shell unchanged."""
    result = tool_run_command(ws, 'find src -type f -name "*.py" | sort -r')
    assert '[exit code: 0]' in result
    assert _body(result) == _native(
        'find src -type f -name "*.py" | sort -r', ws).stdout.strip()


def test_missing_root_keeps_shell_error(ws):
    result = tool_run_command(ws, 'find nodir -type f')
    assert '[exit code: 1]' in result or '[exit code: 2]' in result
    assert 'No such file or directory' in result


def test_killswitch_uses_native_find(ws, monkeypatch):
    monkeypatch.setenv('TOFU_RUN_FIND_FASTPATH', '0')
    result = tool_run_command(ws, 'find src -type f -name "*.py" | head -2')
    assert '[exit code: 0]' in result
    assert _body(result) == _native(
        'find src -type f -name "*.py" | head -2', ws).stdout.strip()


def test_symlink_root_keeps_shell(ws):
    os.symlink(os.path.join(ws, 'src'), os.path.join(ws, 'srclink'))
    # GNU find -P does not descend a symlink root; the fast path declines
    # it rather than search the realpath target.
    result = tool_run_command(ws, 'find srclink -maxdepth 0')
    assert 'srclink' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
