"""Path version probes retain a static executable identity without expanding paths.

Directory globs and quoted HOME are shell-owned, not filesystem preflight. Real
private executables verify original-source execution, no replay, and the ordinary
managed process lifecycle under an unrelated held checkout writer.
"""
import os
import shlex
import subprocess
import threading
import uuid

import pytest

from lib.project_mod import run_command as commands, tools
from lib.project_mod import workspace_lease as leases
from lib.project_mod.command_setup import command_setup_view
from lib.project_mod.tool_admission import RecordedCommandSource, tool_admission_scope
from tests.project_mod.test_command_outer_writer_lease import _other_writer

pytestmark = pytest.mark.unit


def _probe(iterable, *, variable='candidate', body=None, guarded=True):
    reference = f'"${variable}"'
    body = body or f"printf '%s ' {reference}; {reference} --version;"
    if guarded:
        body = f'if [ -x {reference} ]; then {body} fi;'
    return f'for {variable} in {iterable}; do {body} done'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source.py').write_text('value = 1\n')
    home = tmp_path / "home with spaces; $data 'and glob*"
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')

    monkeypatch.delenv('CDPATH', raising=False)
    monkeypatch.setattr(commands, 'SHELL_PREFIX', '')
    return root


@pytest.mark.parametrize('command', [
    _probe('/opt/node*/bin/node /usr/local/bin/node /usr/bin/node '
           '"$HOME/.local/bin/node" "$HOME"/.local/share/fnm/node-versions/*/installation/bin/node'),
    _probe('/opt/node*/bin/node "${HOME}/.local/bin/node"', guarded=False),
    _probe('./runtimes/[ab]?/probe-tool "${HOME}"/.local/bin/probe-tool',
           body='"${candidate}" -V;'),
    _probe('/opt/node/bin/node /usr/bin/node', body='"$candidate" --version;'),
    _probe('./runtimes/*/node', body='echo "version for $candidate"; "$candidate" -v;'),
])
def test_invariant_path_leaf_probe_is_admitted_without_scanning(repo, monkeypatch, command):
    def no_preflight(*args, **kwargs):
        pytest.fail('path-probe admission must not execute, read HOME, or scan paths')

    with monkeypatch.context() as pure:
        pure.setattr(os, 'scandir', no_preflight)
        pure.setattr(subprocess, 'Popen', no_preflight)
        pure.setattr(os, 'getenv', no_preflight)
        view = command_setup_view(command)
        refused = commands.command_background_refusal(command)
    assert view != command
    assert refused is None
    assert commands.command_write_lease_claims(str(repo), command) == ()


def _private_probe(repo, tmp_path, monkeypatch):
    calls = tmp_path / 'calls'
    program = ("#!/bin/sh\n"
               f"printf '%s\\n' invoked >> {shlex.quote(str(calls))}\n"
               "printf '%s\\n' fake-v24.0.0\n")
    from pathlib import Path
    home = Path(os.environ['HOME'])
    for directory in (repo / 'bins' / 'alpha', repo / 'bins' / "beta space; $data 'literal",
                      home / '.local' / 'bin'):
        directory.mkdir(parents=True, exist_ok=True)
        executable = directory / 'probe-tool'
        executable.write_text(program)
        executable.chmod(0o755)
    inert = repo / 'not-executable' / 'probe-tool'
    inert.parent.mkdir()
    inert.write_text(program)
    inert.chmod(0o644)
    original = _probe('./bins/*/probe-tool ./missing*/probe-tool '
                      './not-executable/probe-tool "$HOME/.local/bin/probe-tool"')
    spawned = []
    popen = subprocess.Popen

    def observe(argv, **kwargs):
        if isinstance(argv, (list, tuple)) and original in str(argv[-1]):
            spawned.append((argv, kwargs))
        return popen(argv, **kwargs)

    monkeypatch.setattr(commands.subprocess, 'Popen', observe)
    return original, calls, spawned


def _assert_execution(repo, calls, spawned, source, result):
    assert '[exit code: 0]' in result, result
    assert 'fake-v24.0.0' in result
    assert calls.read_text().splitlines() == ['invoked'] * 3
    assert (repo / 'source.py').read_text() == 'value = 1\n'
    assert len(spawned) == 1
    argv, options = spawned[0]
    assert argv[-1] == source
    assert options['shell'] is False
    assert options['start_new_session'] is True
    assert options.get('pass_fds', ()) == ()
    assert not leases._registry
    assert not leases._active_descriptors


@pytest.mark.skipif(os.name != 'posix', reason='POSIX path probes and process-group contract')
@pytest.mark.parametrize('route', ['runner', 'dispatcher', 'recorded'])
def test_real_path_probe_preserves_original_execution_and_overlaps_writer(repo, tmp_path, monkeypatch, route):
    original, calls, spawned = _private_probe(repo, tmp_path, monkeypatch)
    output = tmp_path / 'record-output'
    source = f'( {original} ) > {shlex.quote(str(output))}' if route == 'recorded' else original
    binding = RecordedCommandSource(original, source) if route == 'recorded' else None
    with _other_writer(repo):
        with tool_admission_scope(command_source=binding):
            if route == 'runner':
                result = commands.tool_run_command(str(repo), source, timeout=10)
            else:
                result = tools.execute_tool('run_command', {'command': source}, str(repo),
                                            recorded_command_source=binding)
    if route == 'recorded':
        result += output.read_text() if output.exists() else ''
    _assert_execution(repo, calls, spawned, source, result)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX durable background record wrapper')
def test_real_background_probe_uses_original_source_and_managed_worker(repo, tmp_path, monkeypatch):
    from lib.tasks_pkg import background_commands as background

    original, calls, spawned = _private_probe(repo, tmp_path, monkeypatch)
    monkeypatch.setattr(background, '_records_root', lambda: str(tmp_path / 'records'))
    owner = {'id': uuid.uuid4().hex, 'convId': '', '_userId': 1}
    wrapped_sources = []

    def execute(task, context):
        wrapped = context['wrapped_command'] or original
        wrapped_sources.append(wrapped)
        binding = RecordedCommandSource(original, wrapped) if wrapped != original else None
        launched = False

        def note_spawn(*args):
            nonlocal launched
            launched = bool(task.get('_subprocess_pid'))
            context['note_spawn'](task.get('_subprocess_pid'), task.get('_subprocess_pgid'))

        result = tools.execute_tool('run_command', {'command': wrapped}, str(repo), task=task,
                                    on_spawn=note_spawn, recorded_command_source=binding)
        return context['build_result']() if launched and wrapped != original else result

    with _other_writer(repo):
        command_id = background.start_background_command(task=owner, command=original, execute=execute)
        info = background.check_background_command(command_id, user_id=owner['_userId'], wait_s=10)
    try:
        assert info['status'] == 'completed', info
        assert info['command'] == original
    finally:
        # Own only this command's worker/entry, never clear a shared registry.
        for worker in threading.enumerate():
            if worker.name == f'bg-cmd-{command_id[3:]}':
                worker.join(15)
                assert not worker.is_alive()
        with background._lock:
            background._registry.pop(command_id, None)
    assert len(wrapped_sources) == 1
    _assert_execution(repo, calls, spawned, wrapped_sources[0], info['result'])


@pytest.mark.parametrize('command', [
    _probe('$(printf /a/node)'),
    _probe('`printf /a/node`'),
    _probe('"$UNKNOWN/bin/node"'),
    _probe('$HOME/bin/node'),
    _probe('"${HOME:-/a}/bin/node"'),
    _probe('/opt/node*/*'),
    _probe('*.py'),
    _probe('/opt/*/node', body='"$UNKNOWN" --version;'),
    _probe('/opt/*/node', body='$candidate --version;'),
    _probe('/opt/*/node', body='candidate=setsid; "$candidate" --version;'),
    _probe('/opt/*/node', body='read candidate; "$candidate" --version;'),
    _probe('/opt/*/node', body='printf -v candidate setsid; "$candidate" --version;'),
    _probe('/opt/*/node', body='"$candidate" --version > result;'),
    _probe('/opt/*/node', body='"$candidate" --version &'),
    _probe('/opt/*/node', body='"$candidate" $(touch victim);'),
    _probe('/opt/*/node', body='for other in /a/node; do "$other" --version; done;'),
    _probe('/opt/*/node', variable='PATH'),

    _probe('/opt/*/node', variable='_'),
    _probe('/opt/*/node', body='printf "%n" candidate; "$candidate" --version;'),
    _probe('/opt/*/node', body='printf "%ln" candidate; "$candidate" --version;'),
    _probe('/opt/*/node', body='printf "%s" "$candidate" > result; "$candidate" --version;'),
    _probe('/opt/*/node', body='"$candidate" --version | head -1;'),
    _probe('/opt/*/node', body='"$candidate" --version; "$candidate" --version;'),
    _probe('/opt/*/node', body='[ -x "$candidate" ]; "$candidate" --version;'),
    _probe('/opt/*/node') + ' &',
    _probe('/a/node', guarded=False) + ' &',
    _probe(' '.join('/opt/version*/node' for _ in range(9))),
    _probe('/opt/*/node', body='echo path; ' * 64 + '"$candidate" --version;'),
])
def test_unproved_probe_retains_refusal_and_original_source(repo, command):
    assert command_setup_view(command) == command
    refused = commands.command_background_refusal(command)
    assert refused and 'command was not started' in refused.lower()
    assert commands.command_write_lease_claims(str(repo), command)


@pytest.mark.parametrize('head', ['setsid', 'nohup', 'disown'])
def test_candidate_basename_cannot_hide_detacher(repo, head):
    command = _probe(f'/opt/*/{head} "$HOME/.local/bin/{head}"')
    refused = commands.command_background_refusal(command)
    assert refused and 'background-unsupported' in refused
    assert 'command was not started' in refused.lower()


@pytest.mark.parametrize('head', ['git', 'touch', 'rm', 'sh', 'env'])
def test_candidate_basename_and_neighbouring_writes_keep_normal_claims(repo, head):
    command = _probe(f'/opt/*/{head} "$HOME/.local/bin/{head}"')
    assert commands.command_write_lease_claims(str(repo), command)
    command = _probe('/opt/*/node') + '; touch source.py'
    assert commands.command_write_lease_claims(str(repo), command) == (('file', str(repo / 'source.py')),)


@pytest.mark.parametrize('body', [
    'candidate=setsid; "$candidate" sleep 1;',
    'export candidate=setsid; "$candidate" sleep 1;',
    'read candidate; "$candidate" sleep 1;',
    'unset candidate; "$candidate" sleep 1;',
    'printf -v candidate setsid; "$candidate" sleep 1;',

    'printf "%n" candidate; "$candidate" sleep 1;',
    'printf "%ln" candidate; "$candidate" sleep 1;',
    'cd /tmp; "$candidate" sleep 1;',
    'eval candidate=setsid; "$candidate" sleep 1;',
    '"$candidate" $(touch victim);',
    'echo "it\'s $UNKNOWN"; "$candidate" sleep 1;',
])
def test_literal_loop_does_not_guess_binding_after_body_mutation(repo, body):
    command = _probe('/a/node', body=body, guarded=False)
    assert command_setup_view(command) == command
    refused = commands.command_background_refusal(command)
    assert refused and 'command was not started' in refused.lower()


@pytest.mark.parametrize('command', [
    'if true; then "$UNKNOWN" sleep 1; fi',
    'if true; then "/usr/bin/setsid" sleep 1; fi',
    'if true; then NAME=setsid "$NAME" sleep 1; fi',

    'if false; then true; else "$UNKNOWN" sleep 1; fi',
    'if false; then true; elif "/usr/bin/setsid" sleep 1; then true; fi',
    'if false; then true; else sudo "/usr/bin/setsid" sleep 1; fi',
    'while "$UNKNOWN" sleep 1; do true; done',
    'for p in /a/node*; do if [ -x "$p" ]; then "$p" --version; fi; done',
    'for p in ~; do "$p" --version; done',
])
def test_control_prefix_or_tilde_does_not_hide_unproved_executable(repo, command):
    refused = commands.command_background_refusal(command)
    assert refused and 'command was not started' in refused.lower()


def test_whole_probe_refused_before_snapshot_or_spawn(repo, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unsafe probe reached snapshots or spawn')

    monkeypatch.setattr(tools, '_snapshot_targets', forbidden)
    monkeypatch.setattr(tools, '_snapshot_project_files', forbidden)
    monkeypatch.setattr(commands.subprocess, 'Popen', forbidden)
    original = _probe('/opt/*/node', body='"$UNKNOWN" --version;')
    wrapped = f'( {original} ) > /tmp/server-record'
    binding = RecordedCommandSource(original, wrapped)
    for source, record in ((original, None), (wrapped, binding)):
        result = tools.execute_tool('run_command', {'command': source}, str(repo),
                                    recorded_command_source=record)
        assert 'command was not started' in result.lower()
        assert 'exit code' not in result
        if record is not None:
            with tool_admission_scope(command_source=record):
                with pytest.raises(ValueError, match='wrapper changed'):
                    commands.command_background_refusal(source + '; touch source.py')


def test_path_probe_bounds_cover_whole_syntax_without_limiting_glob_results(repo):
    command = _probe(' '.join('/opt/version*/node' for _ in range(8)),
                     body='echo path; ' * 7 + '"$candidate" --version;')
    assert command_setup_view(command) != command
    assert commands.command_background_refusal(command) is None
    assert commands.command_write_lease_claims(str(repo), command) == ()
    command = _probe('/opt/*/node', body='echo "' + 'x' * 16_384 + '"; "$candidate" --version;')
    assert command_setup_view(command) == command
    assert commands.command_background_refusal(command)


def test_quoted_control_word_is_an_executable_not_a_control_prefix():
    assert commands.command_background_refusal('"then" "$ARGUMENT"') is None
    assert commands.command_background_refusal('\\then "$ARGUMENT"') is None


@pytest.mark.parametrize(('setup', 'body'), [
    ("alias echo='touch source.py; echo'", 'echo probe; "$candidate" --version;'),
    ("alias printf='candidate=setsid; printf'", 'printf "%s" "$candidate"; "$candidate" --version;'),
])
def test_prior_alias_cannot_borrow_inert_probe_body(repo, setup, body):
    command = setup + '\n' + _probe('/opt/*/node', body=body, guarded=False)
    assert command_setup_view(command) == command
    refused = commands.command_background_refusal(command)
    assert refused and 'command was not started' in refused.lower()
    assert commands.command_write_lease_claims(str(repo), command)


@pytest.mark.parametrize('command', [
    'for p in node; do >/dev/null p=setsid; "$p" sleep 1; done',
    'for p in node; do 2>/dev/null p=setsid; "$p" sleep 1; done',
    'for p in /opt/*/setsid; do >/dev/null "$p" sleep 1; done',
    'if true; then 2>/dev/null "$UNKNOWN" sleep 1; fi',
    'if true; then >/dev/null "/usr/bin/setsid" sleep 1; fi',
])
def test_leading_redirect_cannot_hide_binding_or_executable(repo, command):
    if command.startswith('for'):
        assert command_setup_view(command) == command
    refused = commands.command_background_refusal(command)
    assert refused and 'command was not started' in refused.lower()
    assert commands.command_write_lease_claims(str(repo), command)


def test_leading_discard_redirect_does_not_invent_a_control_keyword():
    command = '> /dev/null "then" "$ARGUMENT"'
    assert commands.command_background_refusal(command) is None


@pytest.mark.parametrize('command', [
    'for p in node; do ! p=setsid; "$p" sleep 1; done',
    'for p in node; do >/dev/null read p; "$p" sleep 1; done',
    'shopt -s expand_aliases\nfor p in /opt/*/node; do "$p" --version; done',
    '>/dev/null IFS=x\nfor p in /opt/*/node; do "$p" --version; done',
])
def test_shell_state_prefix_never_leaves_a_guessed_loop_binding(repo, command):
    assert command_setup_view(command) == command
    refused = commands.command_background_refusal(command)
    assert refused and 'command was not started' in refused.lower()
