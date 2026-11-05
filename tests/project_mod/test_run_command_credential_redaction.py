"""Vault credentials injected into a run_command child never reach model-visible
output: results, streamed progress, truncated captures and background-command
results all carry an exact-value redaction instead of the secret. A background
result rebuilt from its durable record after a server restart is redacted the
same way, or withheld when the injected value can no longer be proven."""
from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

SECRET = 'ghp_RedactMe0123456789abcdefSECRET'
MARKER = '[redacted credential: GITHUB_TOKEN]'


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    import lib.credentials_vault as vault
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    vault.set_entry('github_token', SECRET)
    return vault


def _python(code: str) -> str:
    return f'{sys.executable} -c "{code}"'


def test_result_and_streamed_progress_never_contain_the_value(vault, tmp_path):
    from lib.project_mod.run_command import tool_run_command
    chunks = []
    command = _python(
        "import os, sys, time; t = os.environ['GITHUB_TOKEN']; "
        "sys.stdout.write('token=' + t[:9]); sys.stdout.flush(); time.sleep(0.4); "
        "sys.stdout.write(t[9:] + chr(10)); sys.stdout.flush(); "
        "sys.stderr.write('auth ' + t + chr(10))")
    out = tool_run_command(str(tmp_path), command, credentials=['github_token'],
                           on_chunk=lambda stream, text: chunks.append((stream, text)))
    streamed = {name: ''.join(text for stream, text in chunks if stream == name)
                for name in ('stdout', 'stderr')}
    for text in (out, *streamed.values()):
        assert SECRET not in text, text
    assert 'token=' + MARKER in out and 'auth ' + MARKER in out, out
    assert streamed['stdout'] == 'token=' + MARKER + '\n', chunks
    assert streamed['stderr'] == 'auth ' + MARKER + '\n', chunks


def test_truncated_capture_keeps_no_fragment_of_the_value(vault, tmp_path):
    from lib.project_mod.config import MAX_COMMAND_OUTPUT
    from lib.project_mod.run_command import tool_run_command
    head = MAX_COMMAND_OUTPUT * 3 // 4
    # The value straddles the capture's head/tail cut; then output overflows.
    command = _python(
        "import os, sys; t = os.environ['GITHUB_TOKEN']; "
        f"sys.stdout.write('x' * {head - 9} + t + 'y' * {MAX_COMMAND_OUTPUT})")
    out = tool_run_command(str(tmp_path), command, credentials=['github_token'])
    assert 'truncated' in out
    for start in range(len(SECRET) - 7):
        assert SECRET[start:start + 8] not in out, SECRET[start:start + 8]


def test_commands_without_credentials_are_unchanged(vault, tmp_path):
    from lib.project_mod.run_command import tool_run_command
    out = tool_run_command(str(tmp_path), "printf '%s' 'ghp_RedactMe'")
    assert 'ghp_RedactMe' in out and 'redacted' not in out


def test_background_command_result_is_redacted(vault, tmp_path, monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import project
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path / 'records'))
    monkeypatch.setattr(project, '_finalize_tool_round', Mock())
    task = {'id': 'task-bg-secret', 'convId': '', '_userId': 7, 'config': {}}
    args = {'command': 'printf "%s\\n" "$GITHUB_TOKEN"; printf "%s" "$GITHUB_TOKEN" >&2',
            'background': True, 'credentials': ['github_token']}
    result = project._handle_project_tool(
        task, {}, 'run_command', 'tc-bg', args, 1,
        {'toolName': 'run_command', 'toolCallId': 'tc-bg'}, {}, str(tmp_path), True)
    command_id = next(word for word in str(result[1]).replace(':', ' ').replace('.', ' ').split()
                      if word.startswith('bg_'))
    snapshot = background_commands.check_background_command(command_id, user_id=7, wait_s=30)
    assert snapshot['status'] == 'completed', snapshot
    assert SECRET not in snapshot['result'], snapshot['result']
    assert snapshot['result'].count(MARKER) == 2, snapshot['result']


def _command_id(tool_result) -> str:
    return next(word for word in str(tool_result[1]).replace(':', ' ').replace('.', ' ').split()
                if word.startswith('bg_'))


def _assert_no_fragment(text: str) -> None:
    for start in range(len(SECRET) - 7):
        assert SECRET[start:start + 8] not in text, (SECRET[start:start + 8], text)


@pytest.fixture()
def restart(vault, tmp_path, monkeypatch):
    """Background launches whose server process dies before publishing.

    The launching worker parks at publication: its process is gone and never
    publishes. ``restart`` then plays the next process, with an empty registry
    and a recovery sweep over the records the old process left on disk.
    """
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import project
    records = tmp_path / 'records'
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(records))
    monkeypatch.setattr(background_commands, 'REATTACH_POLL_S', 0.05)
    monkeypatch.setattr(background_commands, 'wake_completed_background_batch',
                        lambda *args: None)
    monkeypatch.setattr(project, '_finalize_tool_round', Mock())
    released, parked = threading.Event(), {}
    delivered: dict[str, str] = {}
    delivered_signal = threading.Condition()

    def deliver_completion(*, task, config, command_id, command, result):
        if threading.current_thread().name == f'bg-cmd-{command_id[3:]}':
            parked[command_id].set()
            released.wait(60)
            return 'failed'
        with delivered_signal:
            delivered[command_id] = result
            delivered_signal.notify_all()
        return 'queued'

    monkeypatch.setattr(background_commands, 'deliver_completion', deliver_completion)
    launched: list[str] = []

    def launch(command: str) -> str:
        task = {'id': 'task-bg-restart', 'convId': 'conv-bg-restart', '_userId': 7,
                'config': {}}
        args = {'command': command, 'background': True, 'credentials': ['github_token']}
        command_id = _command_id(project._handle_project_tool(
            task, {}, 'run_command', 'tc-bg', args, 1,
            {'toolName': 'run_command', 'toolCallId': 'tc-bg'}, {}, str(tmp_path), True))
        parked[command_id] = threading.Event()
        launched.append(command_id)
        return command_id

    def wait_spawned(command_id: str) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                record = json.loads((records / command_id / 'record.json').read_text())
            except (OSError, ValueError):
                record = {}
            if record.get('pid'):
                return record
            time.sleep(0.02)
        raise AssertionError('background command never recorded its spawn')

    def wait_parked(command_id: str) -> None:
        assert parked[command_id].wait(30), 'the launching worker never finished'

    def restart_server() -> None:
        with background_commands._lock:
            for command_id in launched:
                background_commands._registry.pop(command_id, None)
        background_commands.recover_background_commands_on_startup()

    def recovered(command_id: str) -> str:
        with delivered_signal:
            assert delivered_signal.wait_for(lambda: command_id in delivered, 30), delivered
            return delivered[command_id]

    yield SimpleNamespace(
        launch=launch, wait_spawned=wait_spawned, wait_parked=wait_parked,
        restart=restart_server, recovered=recovered, records=records,
        background_commands=background_commands)
    released.set()
    for thread in threading.enumerate():
        if any(thread.name.endswith(command_id[3:]) for command_id in launched):
            thread.join(10)


_PRINT_TOKEN = 'printf "%s\\n" "$GITHUB_TOKEN"; printf "%s" "$GITHUB_TOKEN" >&2'


@pytest.mark.parametrize('vault_after_launch', ['unchanged', 'changed', 'deleted'])
def test_result_of_a_command_that_finished_while_the_server_was_down(
        restart, vault, vault_after_launch):
    command_id = restart.launch(_PRINT_TOKEN)
    restart.wait_parked(command_id)
    if vault_after_launch == 'changed':
        vault.set_entry('github_token', 'ghp_RotatedAfterLaunch000000000000')
    elif vault_after_launch == 'deleted':
        vault.delete_entry('github_token')

    restart.restart()
    result = restart.recovered(command_id)

    _assert_no_fragment(result)
    assert result.endswith('\n[exit code: 0]'), result
    if vault_after_launch == 'unchanged':
        assert result.count(MARKER) == 2, result
    else:
        assert 'Output withheld (credential_unresolvable)' in result, result
        assert f"'github_token' was {vault_after_launch}" in result, result
        assert MARKER not in result, result
    assert not (restart.records / command_id).exists()


@pytest.mark.parametrize('vault_after_launch', ['unchanged', 'changed'])
def test_result_of_a_command_reattached_after_restart(
        restart, vault, tmp_path, vault_after_launch):
    gate = tmp_path / 'gate'
    command_id = restart.launch(
        f'while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.05; done; {_PRINT_TOKEN}')
    restart.wait_spawned(command_id)
    if vault_after_launch == 'changed':
        vault.set_entry('github_token', 'ghp_RotatedAfterLaunch000000000000')

    restart.restart()
    running = restart.background_commands.check_background_command(command_id, user_id=7)
    assert running['status'] == 'running', running
    gate.write_text('go')
    polled = restart.background_commands.check_background_command(
        command_id, user_id=7, wait_s=30)
    result = restart.recovered(command_id)

    assert polled['status'] == 'completed', polled
    for text in (polled['result'], result):
        _assert_no_fragment(text)
        if vault_after_launch == 'unchanged':
            assert text.count(MARKER) == 2, text
        else:
            assert 'Output withheld (credential_unresolvable)' in text, text


def test_a_record_without_credential_evidence_withholds_its_output(restart):
    """A record that does not say which credentials its command received
    (written before credentials were recorded) cannot be scrubbed."""
    bc = restart.background_commands
    paths = bc._record_paths(str(restart.records / 'bg_legacy00001'))
    os.makedirs(paths['dir'])
    bc._write_record(paths, {
        'command_id': 'bg_legacy00001', 'command': 'printf "%s" "$GITHUB_TOKEN"',
        'conv_id': 'conv-bg-restart', 'user_id': 7, 'started_at': time.time() - 1,
        'pid': None, 'pgid': None, 'pid_starttime': None})
    with open(paths['stdout'], 'w', encoding='utf-8') as stream:
        stream.write(SECRET)
    with open(paths['exit'], 'w', encoding='ascii') as stream:
        stream.write('0')

    bc.recover_background_commands_on_startup()
    result = restart.recovered('bg_legacy00001')

    _assert_no_fragment(result)
    assert 'Output withheld (credential_record_missing)' in result, result
    assert result.endswith('\n[exit code: 0]'), result


@pytest.mark.parametrize('lifetime', ['live', 'recovered'])
def test_record_read_window_keeps_no_fragment_of_a_split_value(
        restart, monkeypatch, lifetime):
    """Values straddling the record read's head and tail cuts leave no part."""
    bc = restart.background_commands
    monkeypatch.setattr(bc, '_RECORD_READ_CAP', 2000)
    half, size = 1000, len(SECRET)
    # One value spans the head cut and one spans the tail cut by nine bytes.
    command = _python(
        "import os, sys; t = os.environ['GITHUB_TOKEN']; "
        f"sys.stdout.write('x' * {half - 9} + t + 'y' * 500 + t + 'z' * {half + 9 - size})")
    command_id = restart.launch(command)
    if lifetime == 'live':
        result = bc.check_background_command(command_id, user_id=7, wait_s=30)['result']
    else:
        restart.wait_parked(command_id)
        restart.restart()
        result = restart.recovered(command_id)

    assert 'middle omitted' in result, result
    _assert_no_fragment(result)
