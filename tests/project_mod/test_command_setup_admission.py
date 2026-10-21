"""Setup and server recording syntax must not invent checkout write ownership."""
import shlex
import subprocess
import sys
import threading
import time

import pytest

from lib.project_mod import run_command as commands
from lib.project_mod.command_setup import command_setup_view
from lib.project_mod.tool_admission import RecordedCommandSource, tool_admission_scope
from lib.project_mod import workspace_lease as leases
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source.py').write_text('value = 1\n')
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setattr(_ops, '_salvage_root', lambda: str(tmp_path / 'salvage'))
    return root


@pytest.mark.parametrize('command', [
    'PATH=/usr/bin:$PATH python3 -m pytest; git status --short',
    'PATH=/usr/bin:$PATH python3 -m pytest -q',
    'PATH="/usr/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 python3 -m pytest',
    'PYTHON=$(command -v python3); PATH=/usr/bin:$PATH "$PYTHON" -m pytest',
    'PYTHON="$(command -v python3)" && "${PYTHON}" -m pytest',
    'RUNTIME=/usr/bin/python3; "$RUNTIME" -m pytest',
    'NAME="a literal $(not executable)" python3 -m pytest',
    "NAME='$(rm dangerous)' python3 -m pytest",
])
def test_understood_setup_requests_no_checkout_lease(repo, command):
    # The double-quoted unknown substitution is executable and must not qualify.
    if command.startswith('NAME="'):
        assert commands.command_write_lease_claims(str(repo), command)
    else:
        assert commands.command_write_lease_claims(str(repo), command) == ()


@pytest.mark.parametrize('command', [
    'PYTHON=$(command -v python3; touch victim); "$PYTHON" -m pytest',
    'PYTHON=$(command -v $(touch victim)); "$PYTHON" -m pytest',
    'PATH=$(touch victim) python3 -m pytest',
    'PATH=`touch victim` python3 -m pytest',
    'PYTHON=${PYTHON:-$(touch victim)}; "$PYTHON" -m pytest',
    'PYTHON=$(command -v python3) || "$PYTHON" -m pytest',
    'PYTHON=$(command -v python3) | "$PYTHON" -m pytest',
    'PYTHON=$(command -v python3); read PYTHON; "$PYTHON" target',
    'PYTHON=rm; "$PYTHON" -f source.py',
    'PYTHON=git; "$PYTHON" reset --hard',
    'PYTHON=sh; "$PYTHON" -c "touch victim"',
    'PYTHON=python3; eval "$PYTHON -m pytest"',
    'IFS=x; PYTHON=$(command -v python3); "$PYTHON" -m pytest',
    'PYTHON=$(command -v python3); "$PYTHON" -m pytest > result',
    'PYTHON=$(command -v python3); "$PYTHON" -m pytest; touch source.py',
    'PYTHON="$(command -v python3)""$(touch victim)"; "$PYTHON" -m pytest',
])
def test_unproven_or_real_writes_keep_admission(repo, command):
    assert commands.command_write_lease_claims(str(repo), command)


@pytest.mark.parametrize('command', [
    'for p in /a/node /b/node; do echo "== $p"; "$p" -v 2>&1 | head -2; done',
    'for p in a b; do "${p}" -v; done',
    'for p in a b\ndo "$p" -v\ndone',
    'for p in a b; do "$p" -v; done && ls',
])
def test_literal_loop_binds_each_plain_word_and_claims_nothing(repo, command):
    """A bounded literal loop is unrolled in admission only, never executed."""
    assert command_setup_view(command) != command
    assert commands.command_background_refusal(command) is None
    assert commands.command_write_lease_claims(str(repo), command) == ()


def test_loop_body_keeps_per_command_classification(repo):
    assert 'background-unsupported' in commands.command_background_refusal(
        'for p in a b; do setsid -v; done')
    assert commands.command_write_lease_claims(
        str(repo), 'for p in a b; do touch x; done') == (('file', str(repo / 'x')),)


@pytest.mark.parametrize('command', [
    'for p in $(ls); do "$p" -v; done',
    'for p in *.py; do "$p" -v; done',
    'for p in "a b"; do "$p" -v; done',
    'for p in a b; do "$UNKNOWN" -v; done',
    'for p in a b; do for q in c; do "$q"; done; done',
    'for p in a b c d e f g h i; do "$p" -v; done',
    'for p in a b; do sleep 1 & done',
    'for p in a b; do "$p" -v',
])
def test_unprovable_body_head_keeps_the_conservative_refusal(repo, command):
    assert command_setup_view(command) == command
    assert 'command was not started' in commands.command_background_refusal(command).lower()


def test_literal_head_loop_with_unprovable_iterable_stays_admitted(repo):
    command = 'for f in *.py; do wc -l "$f"; done'
    assert command_setup_view(command) == command
    assert commands.command_background_refusal(command) is None
    assert commands.command_write_lease_claims(str(repo), command) == (('tree', str(repo)),)


def test_static_mutator_is_not_an_opaque_runtime(repo):
    claim = commands.command_write_lease_claims(str(repo), 'COPY=cp; "$COPY" source.py copied.py')
    assert claim == (('file', str(repo / 'copied.py')),)


def test_normalization_is_bounded_and_preserves_unsupported_source():
    source = 'X=python3; "$X" ' + 'x' * 16_384
    assert command_setup_view(source) == source
    source = '; '.join(f'V{i}=python3' for i in range(33)) + '; "$V32" -m pytest'
    assert command_setup_view(source) == source
    assert command_setup_view('PATH="$PATH python3') == 'PATH="$PATH python3'


def test_record_binding_is_internal_exact_and_does_not_exempt_git(repo):
    original = 'python3 -m pytest'
    wrapped = f'( {original} ) > /tmp/record-out; code=$?; printf "%s" "$code" > /tmp/record-status'
    assert commands.command_write_lease_claims(str(repo), wrapped)
    with tool_admission_scope(command_source=RecordedCommandSource(original, wrapped)):
        assert commands.command_write_lease_claims(str(repo), wrapped) == ()
        with pytest.raises(ValueError, match='wrapper changed'):
            commands.command_write_lease_claims(str(repo), wrapped + '; touch victim')
    with pytest.raises(ValueError, match='Invalid recorded'):
        with tool_admission_scope(command_source={'original': original, 'wrapped': wrapped}):
            pass
    for original in ('git status --short', 'git reset --hard', 'touch source.py'):
        wrapped = f'( {original} ) > /tmp/record-out'
        with tool_admission_scope(command_source=RecordedCommandSource(original, wrapped)):
            assert commands.command_write_lease_claims(str(repo), wrapped)


@pytest.mark.parametrize('recorded', [False, True])
def test_actual_setup_worker_overlaps_actual_file_edit_before_completion(repo, tmp_path, recorded):
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    script = tmp_path / 'worker.py'
    script.write_text(
        'from pathlib import Path\nimport time\n'
        f'Path({str(ready)!r}).touch()\n'
        f'while not Path({str(release)!r}).exists(): time.sleep(0.01)\n')
    original = (f'PYTHON=$(command -v {shlex.quote(sys.executable)}); '
                f'PATH=/usr/bin:$PATH "$PYTHON" {shlex.quote(str(script))}')
    output = tmp_path / 'record-out'
    wrapped = f'( {original} ) > {shlex.quote(str(output))}' if recorded else original
    done, errors, result = threading.Event(), [], []

    def run():
        try:
            binding = RecordedCommandSource(original, wrapped) if recorded else None
            with tool_admission_scope(tool_name='run_command', command_source=binding):
                result.append(commands.tool_run_command(str(repo), wrapped, timeout=10))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and not done.is_set() and time.monotonic() < deadline:
            done.wait(0.01)
        assert ready.exists(), (errors, result)
        assert not done.is_set()
        edited = _ops.tool_apply_diff(str(repo), 'source.py', 'value = 1', 'value = 2')
        assert edited['ok'], edited
        assert not done.is_set(), 'edit was merely delayed until the command finished'
    finally:
        release.touch()
        worker.join(15)
    assert not worker.is_alive()
    assert not errors
    assert '[exit code: 0]' in result[0]
    assert not leases._registry
    assert not leases._active_descriptors


@pytest.mark.parametrize('source', [
    'X=setsid; # ; X=python3\n"$X" sleep 1',
    'X=setsid; # ignored assignment\n"$X" sleep 1',
    '"$UNKNOWN_EXECUTABLE" sleep 1',
])
def test_comments_and_unresolved_executable_do_not_borrow_false_detach_proof(repo, source):
    assert command_setup_view(source) == source
    assert 'command was not started' in commands.command_background_refusal(source).lower()
    wrapped = f'( {source} ) > /tmp/record-output'
    with tool_admission_scope(command_source=RecordedCommandSource(source, wrapped)):
        assert commands.command_background_refusal(wrapped)


@pytest.mark.parametrize('source', [
    'CDPATH=/; cd tmp && touch victim',
    'export CDPATH=/; cd tmp && touch victim',
    'CDPATH=/; # comment\ncd tmp && touch victim',
])
def test_command_owned_cdpath_cannot_claim_the_wrong_checkout(repo, source):
    with pytest.raises(ValueError, match='CDPATH'):
        commands.command_write_lease_claims(str(repo), source)
    refused = commands.tool_run_command(str(repo), source)
    assert 'workspace-cwd-ambiguous' in refused
    assert 'command was not started' in refused.lower()
    assert not (repo / 'victim').exists()


def test_explicit_cd_target_is_not_cdpath_ambiguous(repo, monkeypatch):
    monkeypatch.setenv('CDPATH', '/')
    child = repo / 'child'
    child.mkdir()
    source = 'CDPATH=/; cd ./child && touch victim'
    assert commands.command_background_refusal(source) is None
    assert commands.command_write_lease_claims(str(repo), source) == (('file', str(child / 'victim')),)
    with pytest.raises(ValueError, match='CDPATH'):
        commands.command_write_lease_claims(str(repo), 'cd tmp && touch victim')


def test_recorded_file_mutator_tracks_original_targets_not_server_spool(repo, tmp_path, monkeypatch):
    from lib.project_mod import tools
    scopes = []
    snapshot_scope = tools._snapshot_scope_rel_targets

    def observe(targets):
        scopes.append(targets)
        return snapshot_scope(targets)

    monkeypatch.setattr(tools, '_snapshot_scope_rel_targets', observe)
    original = 'cp source.py copied.py'
    wrapped = f'( {original} ) > {shlex.quote(str(tmp_path / "server-record"))}'
    result = tools.execute_tool('run_command', {'command': wrapped}, str(repo),
                                recorded_command_source=RecordedCommandSource(original, wrapped))
    assert '[exit code: 0]' in result
    assert (repo / 'copied.py').read_text() == 'value = 1\n'
    assert scopes == [{'copied.py'}]
