"""Explicit shell data writes coordinate their targets, not an unrelated cwd."""
from contextlib import contextmanager
import os
import shlex
import subprocess
import sys
import threading

import pytest

from lib.project_mod import run_command as commands, tools, workspace_lease as leases
from lib.project_mod.command_heredoc import command_admission_view

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source').write_text('source contents\n')
    (root / 'other').write_text('preserve\n')
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    return root


@contextmanager
def held(path, file=False):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            api = leases.workspace_file_write_lease if file else leases.workspace_write_lease
            with api(path):
                ready.set()
                assert release.wait(15), 'holder release was not signalled'
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        yield
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors


def run(repo, route, source):
    if route == 'runner':
        return commands.tool_run_command(str(repo), source)
    return tools.execute_tool('run_command', {'command': source}, str(repo))


def fingerprint_command(repo, scratch, cargo_home):
    q = shlex.quote
    return (
        f'export CARGO_HOME={q(str(cargo_home))} PATH="$CARGO_HOME/bin:$PATH"\n'
        f'mkdir -p {q(str(scratch / "fp2/src"))}\n'
        f'cat > {q(str(scratch / "fp2/Cargo.toml"))} <<\'EOF\'\n'
        '[package]\nname = "fp2"\nversion = "0.0.0"\nedition = "2021"\n'
        '[dependencies]\nsha2 = "0.10"\ntoml = "1.1"\nEOF\n'
        f'{q(sys.executable)} - <<\'PYEOF\'\n'
        'from pathlib import Path\n'
        'src = Path("source").read_text()\n'
        f'Path({str(scratch / "fp2/src/main.rs")!r}).write_text(src)\n'
        'print("patched")\nPYEOF\n'
        f'cd {q(str(scratch / "fp2"))} && CARGO_MANIFEST_DIR={q(str(repo))} '
        f'cargo run --release --quiet 2>{q(str(scratch / "err"))} '
        f'>{q(str(scratch / "out"))}; echo "exit=$?"\n'
        f'grep \'^FINGERPRINT\' {q(str(scratch / "out"))}; '
        f'echo "RS keys: $(grep -c \'^K=\' {q(str(scratch / "out"))})"; '
        f'grep -i \'error\\|panic\' {q(str(scratch / "err"))} | head -3\n'
        f'grep \'^K=\' {q(str(scratch / "out"))} | sed \'s/^K=//\' | sort '
        f'>{q(str(scratch / "keys"))}\n'
        'echo "=== diff ==="; '
        f'diff {q(str(scratch / "expected"))} {q(str(scratch / "keys"))} | head -25\n'
    )


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_mixed_external_diagnostic_runs_while_checkout_writer_is_active(repo, tmp_path, monkeypatch, route):
    scratch = tmp_path / 'scratch space'
    scratch.mkdir()
    cargo_home = tmp_path / 'cargo-home'
    (cargo_home / 'bin').mkdir(parents=True)
    cargo = cargo_home / 'bin/cargo'
    # A local stub covers shell/admission behavior, not Rust or network behavior.
    cargo.write_text('#!/bin/sh\nprintf "FINGERPRINT=fixture\\nK=source\\n"\n')
    cargo.chmod(0o700)
    monkeypatch.setenv('PATH', f'{cargo_home / "bin"}:{os.environ["PATH"]}')

    # A single export expands PATH using the pre-export CARGO_HOME value.
    monkeypatch.setenv('CARGO_HOME', str(cargo_home))
    (scratch / 'expected').write_text('source\n')
    source = fingerprint_command(repo, scratch, cargo_home)
    claims = commands.command_write_lease_claims(str(repo), source)
    assert not any(path == str(repo) or path.startswith(str(repo) + os.sep) for _, path in claims), claims
    with held(repo):
        result = run(repo, route, source)
    assert '[workspace-busy]' not in result, result
    assert '[exit code: 0]' in result, result
    assert 'RS keys: 1' in result
    assert (scratch / 'keys').read_text() == 'source\n'
    assert (scratch / 'fp2/src/main.rs').read_text() == 'source contents\n'
    assert (repo / 'source').read_text() == 'source contents\n'
    assert not leases.workspace_lease_fds()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_literal_cat_heredoc_overlaps_other_file_but_not_same_target(repo, route):
    source = "cat > created <<'DATA'\n$() `touch victim` ; git stash\n' unmatched\nDATA\n"
    assert commands.command_write_lease_claims(str(repo), source) == (('file', str(repo / 'created')),)
    with held(repo / 'other', file=True):
        result = run(repo, route, source)
    assert '[exit code: 0]' in result, result
    assert (repo / 'created').read_text() == "$() `touch victim` ; git stash\n' unmatched\n"
    with held(repo / 'created', file=True):
        result = run(repo, route, source)
    assert '[workspace-busy]' in result
    assert not (repo / 'victim').exists()
    assert (repo / 'other').read_text() == 'preserve\n'
    assert not leases.workspace_lease_fds()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('git', ['git checkout -- source', 'git stash', 'git reset --hard', 'git clean -fd'])
def test_tree_destroying_git_tail_still_waits_before_any_write(repo, tmp_path, route, git):
    destination = tmp_path / 'external-output'
    source = f"cat > {shlex.quote(str(destination))} <<'DATA'\nsafe data\nDATA\n{git}"
    assert ('root', str(repo)) in commands.command_write_lease_claims(str(repo), source)
    with held(repo / 'other', file=True):
        result = run(repo, route, source)
    assert '[workspace-busy]' in result, result
    assert 'Command was not started' in result
    assert not destination.exists()
    assert (repo / 'source').read_text() == 'source contents\n'


@pytest.mark.parametrize('source', [
    "cat > created <<DATA\n$(touch victim)\nDATA\n",
    "cat > created <<'DATA'\nx\n",  # incomplete
    "sh <<'DATA'\ntouch victim\nDATA\n",
    "cat > created <<-'DATA'\n\tx\n\tDATA\n",
    "cat > created <<'DATA' <<'SECOND'\nx\nDATA\ny\nSECOND\n",
    "cat > created <<'DATA'\nx\nDATA\ntouch other",
    "echo \"$(grep -c x source; touch other)\"",
    "echo \"$(grep x source > other)\"",
    "echo \"$(grep $(touch other) source)\"",
    "export PATH=$(touch other)\necho ready",
    "export GIT_INDEX_FILE=other\ngit add source",
    "sed 's/x/y/w other' source",
    "sed 's/x/y/e' source",
    "sed -i 's/x/y/' source",
    "sed -f other source",
    "sed 's/x/y/;w other' source",
])
def test_unsupported_or_executable_data_shapes_keep_protection(repo, source):
    assert commands.command_write_lease_claims(str(repo), source)
    with held(repo):
        result = commands.tool_run_command(str(repo), source)
    assert '[workspace-busy]' in result, result
    assert (repo / 'other').read_text() == 'preserve\n'
    assert not (repo / 'victim').exists()


def test_literal_directory_targets_do_not_lease_reading_checkout(repo, tmp_path):
    outside = tmp_path / 'external' / 'new' / 'src'
    source = f'mkdir -p {shlex.quote(str(outside))}'
    assert commands.command_write_lease_claims(str(repo), source) == ()
    other = tmp_path / 'other-checkout'
    other.mkdir()
    subprocess.run(['git', 'init', '-q', str(other)], check=True)
    source = f'mkdir -p {shlex.quote(str(other / "new"))}'
    with held(other):
        assert '[exit code: 0]' in commands.tool_run_command(str(repo), source)
    assert (other / 'new').is_dir()


def test_external_symlink_into_source_is_not_an_exemption(repo, tmp_path):
    alias = tmp_path / 'external-alias'
    alias.symlink_to(repo, target_is_directory=True)
    source = f"mkdir -p {alias / 'child'}; cat > {alias / 'output'} <<'DATA'\nx\nDATA\n"
    with held(repo):
        assert '[workspace-busy]' in commands.tool_run_command(str(repo), source)
    assert not (repo / 'child').exists()
    assert not (repo / 'output').exists()


def test_admission_view_preserves_header_writes_and_real_tail(repo):
    source = "export PATH=$PATH\ncat > created <<'DATA' && touch other\n$() > payload\nDATA\necho $?"
    view = command_admission_view(source)
    assert len(view) == len(source)
    assert 'cat > created' in view and 'touch other' in view
    assert '$() > payload' not in view
    claims = commands.command_write_lease_claims(str(repo), source)
    assert ('file', str(repo / 'created')) in claims
    assert ('file', str(repo / 'other')) in claims


def test_external_git_metadata_directory_keeps_source_checkout_barrier(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0.04')
    root, metadata = tmp_path / 'separate-repo', tmp_path / 'metadata'
    subprocess.run(['git', 'init', '-q', '--separate-git-dir', str(metadata), str(root)], check=True)
    source = f'mkdir -p {shlex.quote(str(metadata / "objects/new"))}'
    assert ('tree', str(root)) in commands.command_write_lease_claims(str(root), source)
    with held(root):
        assert '[workspace-busy]' in commands.tool_run_command(str(root), source)
    assert not (metadata / 'objects/new').exists()


@pytest.mark.parametrize('source', [
    'mkdir -p "$DESTINATION"', 'mkdir -m 700 source/new',
    'mkdir -p source/new > other', 'mkdir --unknown-option source/new',
])
def test_ambiguous_directory_creation_retains_cwd_protection(repo, source):
    assert ('tree', str(repo)) in commands.command_write_lease_claims(str(repo), source)


@pytest.mark.parametrize('source', [
    "echo '\ncat > created <<'DATA'\n'; touch other\nDATA\n",
    'echo "\ncat > created <<\'DATA\'\n$(touch other)\nDATA\n"\n',
    "echo \\\ncat > created <<'DATA'\nx\nDATA\ntouch other\n",
])
def test_cross_line_shell_context_cannot_hide_writes_as_heredoc_data(repo, source):
    assert command_admission_view(source) == source
    with held(repo):
        assert '[workspace-busy]' in commands.tool_run_command(str(repo), source)
    assert (repo / 'other').read_text() == 'preserve\n'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('tail', [
    'printf changed > other', 'printf changed > other &',
    'git checkout -- source', 'git stash', 'git reset --hard', 'git clean -fd',
])
def test_comment_parenthesis_cannot_hide_substitution_writes(repo, route, tail):
    source = 'echo "$(grep #)\n' + tail + '\n)"'
    assert commands.command_write_lease_claims(str(repo), source)
    with held(repo):
        result = run(repo, route, source)
    assert 'Command was not started' in result or 'command was not started' in result, result
    assert (repo / 'other').read_text() == 'preserve\n'
    assert (repo / 'source').read_text() == 'source contents\n'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('tail', [
    'printf changed > {peer}/other', 'printf changed > {peer}/other &',
    'git -C {peer} checkout -- source', 'git -C {peer} stash',
    'git -C {peer} reset --hard', 'git -C {peer} clean -fd',
])
def test_legacy_backticks_cannot_hide_other_checkout_writer(repo, tmp_path, route, tail):
    peer = tmp_path / 'peer'
    peer.mkdir()
    subprocess.run(['git', 'init', '-q', str(peer)], check=True)
    (peer / 'other').write_text('peer preserve\n')
    (peer / 'source').write_text('peer source\n')
    mutation = tail.format(peer=shlex.quote(str(peer)))
    source = "echo `\ncat <<'DATA'\n`; " + mutation + '; echo `\nDATA\necho `\n'
    assert command_admission_view(source) == source
    with held(peer):
        result = run(repo, route, source)
    assert 'Command was not started' in result or 'command was not started' in result, result
    assert (peer / 'other').read_text() == 'peer preserve\n'
    assert (peer / 'source').read_text() == 'peer source\n'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('operand', ['transient/../../outside', '.git/transient/../../outside'])
def test_mkdir_parent_traversal_keeps_intermediate_checkout_writes_protected(repo, tmp_path, route, operand):
    source = 'mkdir -p ' + operand
    assert ('tree', str(repo)) in commands.command_write_lease_claims(str(repo), source)
    with held(repo):
        result = run(repo, route, source)
    assert '[workspace-busy]' in result, result
    assert not (repo / 'transient').exists()
    assert not (repo / '.git/transient').exists()
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('absolute', [False, True])
def test_mkdir_traversal_protects_intermediate_peer_checkout(repo, tmp_path, route, absolute):
    peer = tmp_path / 'peer'
    peer.mkdir()
    subprocess.run(['git', 'init', '-q', str(peer)], check=True)
    operand = str(peer) + '/transient/../../outside' if absolute else '../peer/transient/../../outside'
    source = 'mkdir -p ' + shlex.quote(operand)
    claims = commands.command_write_lease_claims(str(repo), source)
    assert ('tree', str(peer / 'transient')) in claims, claims
    with held(peer):
        result = run(repo, route, source)
    assert '[workspace-busy]' in result, result
    assert not (peer / 'transient').exists()
    assert not (tmp_path / 'outside').exists()
