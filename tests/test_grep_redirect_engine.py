"""Runtime grep delegation and shared streaming-engine contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from lib.project_mod.config import IGNORE_DIRS
from lib.project_mod.grep_engine import (
    SearchRequest,
    _raw_grep_rg_argv,
    run_search,
)
from lib.project_mod.grep_redirect import plan_grep_redirect

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    base = tmp_path / 'w'
    (base / 'sub' / 'src').mkdir(parents=True)
    (base / 'sub' / 'logs').mkdir(parents=True)
    (base / 'a.txt').write_text('alpha\nbeta\nALPHA gamma\n')
    (base / 'sub' / 'src' / 'b.txt').write_text('beta\ndelta\n')
    (base / 'sub' / 'logs' / 'l.txt').write_text('hit\n')
    (base / 'single.txt').write_text('x\ny\n')
    (base / 'bin.dat').write_bytes(b'ab\x00cd\nbeta\n')
    return str(base)


def run_rewritten(command, cwd, env=None):
    plan = plan_grep_redirect(command, cwd)
    assert plan is not None and plan.rewritten is not None
    return subprocess.run(
        ['bash', '-c', plan.rewritten], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=10,
    )


def run_gnu_hardened(command, cwd):
    argv = __import__('shlex').split(command)
    exclusions = []
    for dirname in sorted(IGNORE_DIRS):
        exclusions.extend(('--exclude-dir', dirname))
    return subprocess.run(
        ['grep', *exclusions, *argv[1:]], cwd=cwd,
        capture_output=True, text=True,
    )


@pytest.mark.skipif(not shutil.which('grep'), reason='GNU grep required')
@pytest.mark.parametrize('command', [
    'grep beta a.txt',
    'grep -n beta a.txt sub/src/b.txt',
    'grep -i BETA a.txt',
    'grep -v beta a.txt',
    'grep -w beta a.txt',
    'grep -x beta sub/src/b.txt',
    'grep -o b.t a.txt',
    'grep -m1 -n beta a.txt sub/src/b.txt',
    'grep -C1 -n beta a.txt sub/src/b.txt',
    'grep -l beta a.txt single.txt',
    'grep -L beta a.txt single.txt',
    'grep -c beta a.txt sub/src/b.txt',
    'grep beta missing.txt a.txt',
    'grep -q beta a.txt',
    'grep -q zzz a.txt',
    "grep 'alp\\|bet' a.txt",
    "grep -E 'alp(a|ha)' a.txt",
    'grep -rn beta sub',
    'grep -rl beta sub',
    'grep -r beta',
    'grep --include=*.txt -rn delta sub',
    'grep beta bin.dat',
    'grep -I beta bin.dat',
    'grep -P b.ta a.txt',
])
def test_native_gnu_byte_parity(ws, command):
    expected = run_gnu_hardened(command, ws)
    actual = run_rewritten(command, ws)
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        expected.returncode, expected.stdout, expected.stderr)


@pytest.mark.parametrize('command', [
    'grep -n beta a.txt | wc -l',
    'grep beta a.txt sub/src/b.txt > out.txt && cat out.txt',
    'grep beta missing.txt a.txt 2>/dev/null || echo ERR',
    'x=$(grep -c beta a.txt); echo count=$x',
    'if grep -q beta a.txt; then echo YES; fi',
    '( grep beta a.txt )',
    'grep beta a.txt | grep -c beta',
])
def test_shell_shapes_execute_at_runtime(ws, command):
    result = run_rewritten(command, ws)
    assert result.returncode in (0, 1, 2)
    assert 'grep_search helper' not in result.stderr


def test_preceding_write_is_visible(ws):
    result = run_rewritten("printf 'fresh\\n' > new.txt; grep -n fresh new.txt", ws)
    assert result.returncode == 0
    assert result.stdout == '1:fresh\n'


def test_planning_does_not_read_or_create_temp_results(ws):
    plan = plan_grep_redirect('grep beta a.txt', ws)
    assert plan and plan.rewritten
    assert 'tofu_gred_' not in plan.rewritten
    with open(os.path.join(ws, 'a.txt'), 'w', encoding='utf-8') as handle:
        handle.write('changed\n')
    result = subprocess.run(
        ['bash', '-c', plan.rewritten], cwd=ws, capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout == ''


def test_stream_grep_is_not_rewritten(ws):
    assert plan_grep_redirect('printf "a\\nb\\n" | grep a', ws) is None


def test_only_proven_common_subset_translates_to_rg(ws):
    fast = _raw_grep_rg_argv(
        'grep', ['-rln', 'beta', '--include=*.txt', 'sub'], ws,
        ['node_modules'])
    assert fast and fast[0] == 'rg'
    assert '--files-with-matches' in fast and '--glob' in fast
    assert not _raw_grep_rg_argv(
        'grep', ['-P', 'b.ta', 'a.txt'], ws, ['node_modules'])
    assert not _raw_grep_rg_argv(
        'grep', ['-rc', 'beta', '--include=*.txt', 'sub'], ws,
        ['node_modules'])


def test_head_closure_stops_backend(ws, tmp_path, monkeypatch):
    backend = tmp_path / 'many-lines.sh'
    backend.write_text(
        '#!/bin/sh\ni=0\nwhile [ "$i" -lt 100 ]; do\n'
        '  printf "hit-%s\\n" "$i"\n  i=$((i+1))\n  sleep 0.05\ndone\n')
    backend.chmod(0o755)
    monkeypatch.setenv('TOFU_GREP_EXECUTABLE', str(backend))
    started = time.monotonic()
    result = run_rewritten('grep hit a.txt | head -1', ws, os.environ.copy())
    assert time.monotonic() - started < 2
    assert result.stdout == 'hit-0\n'


def test_timeout_keeps_only_complete_lines_and_kills_tree(ws, tmp_path, monkeypatch):
    backend = tmp_path / 'slow.sh'
    child_file = tmp_path / 'child.pid'
    backend.write_text(
        '#!/bin/sh\nsleep 30 &\necho $! > "$TOFU_TEST_CHILD_PID"\n'
        'printf "complete\\ntorn"\nsleep 30\n')
    backend.chmod(0o755)
    monkeypatch.setenv('TOFU_GREP_EXECUTABLE', str(backend))
    monkeypatch.setenv('TOFU_GREP_REDIRECT_DEADLINE_S', '0.15')
    monkeypatch.setenv('TOFU_TEST_CHILD_PID', str(child_file))
    result = run_rewritten('grep hit a.txt', ws, os.environ.copy())
    assert result.returncode == 124
    assert result.stdout == 'complete\n'
    assert 'partial results above' in result.stderr
    child_pid = int(child_file.read_text().strip())
    for _ in range(20):
        if not os.path.exists(f'/proc/{child_pid}'):
            break
        time.sleep(0.02)
    assert not os.path.exists(f'/proc/{child_pid}')


def test_shared_engine_enforces_global_limit(tmp_path):
    backend = tmp_path / 'emit.sh'
    marker = tmp_path / 'finished'
    backend.write_text(
        '#!/bin/sh\nprintf "a\\nb\\nc\\nd\\n"\nsleep 5\ntouch "$1"\n')
    backend.chmod(0o755)
    request = SearchRequest(
        cwd=str(tmp_path),
        gnu_argv=(str(backend), str(marker)),
        preferred_backend='gnu',
        max_results=3,
        timeout=2,
        match_line=lambda _line: True,
    )
    result = run_search(request)
    assert result.limit_reached and not result.timed_out
    assert result.stdout == b'a\nb\nc\n'
    assert not marker.exists()


def test_public_grep_search_limit_is_global(tmp_path):
    from lib.project_mod.read_tools import tool_grep

    for index in range(5):
        (tmp_path / f'f{index}.txt').write_text(
            ''.join(f'needle {line}\n' for line in range(10)))
    result = tool_grep(
        str(tmp_path), 'needle', '.', include='*.txt', max_results=3)
    assert '\u2014 3 matches:' in result
    match_lines = [line for line in result.splitlines()
                   if __import__('re').match(r'.+?:\d+:', line)]
    assert len(match_lines) == 3
