"""Transparent wrappers and read-only redirections keep their narrow claim.

A transparent wrapper (``env``/``command``/``exec``) only adjusts the child's
environment or argv[0]; the workspace effect is the wrapped program's. An
input redirect only reads and an unquoted glob only produces words, so none of
them may escalate a proven reader into a whole-checkout writer. Measured
2026-10-22: one ``env -u VAR python3 …`` background command held the canonical
``tree:`` root for 479 s and 2852 of 3948 recorded waits were 60 s or longer,
blocking every other conversation's Edit and write_file in that checkout.

Only private temporary Git checkouts are used; contenders synchronize with
bounded joins/events rather than sleeps or the live workspace's lock.
"""
from contextlib import contextmanager
import subprocess
import threading

import pytest

from lib.project_mod import run_command as commands
from lib.project_mod import workspace_lease
from lib.project_mod.command_analysis import (
    command_segment_dynamic_kinds,
    command_segment_wrapper_view,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '1')
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source').write_text('new contents\n')
    (root / 'target').write_text('old\n')
    return root


@contextmanager
def _other_tree_writer(root):
    """Hold the same whole-checkout tree claim the slow commands held."""
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_lease.workspace_tree_write_lease(root):
                ready.set()
                if not release.wait(10):
                    raise AssertionError('tree writer release was not signalled')
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        yield
    finally:
        release.release() if hasattr(release, 'release') else release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors


# ── Transparent wrappers ───────────────────────────────────────────────


def test_wrapper_view_keeps_the_wrapped_program_only():
    view = command_segment_wrapper_view(
        'env -u PYTHONHOME -u TOFU_STORAGE_TOKEN python3 script.py')
    assert view == 'python3 script.py'
    assert command_segment_wrapper_view('env -i prog args') == 'prog args'
    assert command_segment_wrapper_view('command rm -rf build') == 'rm -rf build'
    assert command_segment_wrapper_view('exec prog args') == 'prog args'


def test_wrapper_view_keeps_unproven_wrappers_visible():
    # `env -S` hides the program inside a quoted string; that stays opaque.
    assert command_segment_wrapper_view("env -S 'prog args'") == "env -S 'prog args'"
    assert command_segment_wrapper_view('env --unknown prog') == 'env --unknown prog'
    assert command_segment_wrapper_view('command -v prog') == 'command -v prog'
    assert command_segment_wrapper_view('env') == 'env'
    assert command_segment_wrapper_view('sudo rm -rf build') == 'sudo rm -rf build'
    assert command_segment_wrapper_view('plain command') == 'plain command'


# ── Redirections and expansions ─────────────────────────────────────────


def test_dynamic_kinds_separate_reading_from_writing_tokens():
    assert command_segment_dynamic_kinds('cat < README.md') == frozenset({'redirect_in'})
    assert command_segment_dynamic_kinds('cat > README.md') == frozenset({'redirect_out'})
    assert command_segment_dynamic_kinds('cat <<EOF') == frozenset({'heredoc'})
    assert command_segment_dynamic_kinds('grep x *.py') == frozenset({'glob'})
    assert command_segment_dynamic_kinds('echo {a,b}') == frozenset({'brace'})
    assert command_segment_dynamic_kinds('echo "$VAR"') == frozenset({'substitution'})
    assert command_segment_dynamic_kinds("echo '$(rm -rf x)'") == frozenset()
    assert command_segment_dynamic_kinds('grep "*.py"') == frozenset()


# ── The user-visible symptom ───────────────────────────────────────────


def test_wrapped_reader_is_not_blocked_by_a_whole_checkout_writer(repo):
    with _other_tree_writer(repo):
        result = commands.tool_run_command(
            str(repo), 'env -u PYTHONHOME python3 -c "print(1)"')
    assert '[workspace-busy]' not in result
    assert 'print(1)' in result or result.strip() == ''


def test_effect_free_shell_body_is_not_blocked_by_a_whole_checkout_writer(repo):
    with _other_tree_writer(repo):
        result = commands.tool_run_command(str(repo), 'bash -c "echo hi"')
    assert '[workspace-busy]' not in result
    assert 'hi' in result

