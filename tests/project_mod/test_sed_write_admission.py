"""Literal substitutions coordinate files without claiming a shared temp parent."""
import shlex

import pytest

from lib.project_mod import run_command as commands, tools, workspace_lease as leases
from lib.project_mod.command_analysis import command_segment_targets, is_literal_sed_filter
from tests.project_mod.test_command_writer_scope import _other_file_writer, _run, repo

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_literal_substitution_can_edit_beside_internal_lease_storage(repo, monkeypatch, route):
    namespace = repo / 'internal-leases'
    namespace.mkdir(mode=0o700)
    monkeypatch.setattr(leases, '_tree_lock_namespace', lambda: namespace)
    target = repo / 'jterm.py'
    target.write_text("path=f'/terminals/websocket/{name}'\n")
    source = (
        f'cd {shlex.quote(str(repo))} && '
        '''sed -i "s|path=f'/terminals/websocket/{name}'|path=f'/terminals/websocket/{name}?token={TOK}'|" jterm.py'''
        " && python3 -c 'print(\"done; still data | no shell pipeline\")'"
    )
    assert commands.command_write_lease_claims(str(repo), source) == (('file', str(target)),)
    with _other_file_writer(repo / 'source'):
        result = _run(repo, route, source)
    assert '[exit code: 0]' in result, result
    assert 'done; still data | no shell pipeline' in result
    assert target.read_text() == "path=f'/terminals/websocket/{name}?token={TOK}'\n"
    assert not leases.workspace_lease_fds()


@pytest.mark.parametrize('invocation', [
    "-i 's/old/new/'",
    "--in-place 's|old|new|g'",
    "--in-place= 's#old#new#'",
    "-Ei 's@old@new@'",
    "-i -E -e 's/old/new/'",
    "-i --expression='s/old/new/'",
    "-i --expression 's/old/middle/' -e 's/middle/new/'",
    "-i -es,old,new,",
    "-i -ne 's:old:new:p'",
    "-i -- 's;old;new;'",
])
def test_supported_substitution_forms_have_real_file_only_effects(repo, invocation):
    source = f'sed {invocation} target'
    assert command_segment_targets(source).complete
    assert commands.command_write_lease_claims(str(repo), source) == (('file', str(repo / 'target')),)
    with _other_file_writer(repo / 'source'):
        result = _run(repo, 'runner', source)
    assert '[exit code: 0]' in result, result
    assert (repo / 'target').read_text() == 'new\n'
    assert (repo / 'other').read_text() == 'other\n'
    assert not leases.workspace_lease_fds()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_all_files_are_admitted_before_any_substitution(repo, monkeypatch, route):
    second = repo / 'second target'
    second.write_text('old\n')
    source = "sed -i -e 's/old/new/' target 'second target'"
    assert set(commands.command_write_lease_claims(str(repo), source)) == {
        ('file', str(repo / 'target')), ('file', str(second))}

    def forbidden(*_args, **_kwargs):
        pytest.fail('conflicting sed writer reached snapshot or process creation')

    with monkeypatch.context() as protected:
        protected.setattr(commands.subprocess, 'Popen', forbidden)
        protected.setattr(tools, '_snapshot_targets', forbidden)
        protected.setattr(tools, '_snapshot_project_files', forbidden)
        with _other_file_writer(second):
            result = _run(repo, route, source)
        assert '[workspace-busy]' in result, result
        assert 'retry after the conflicting lease finishes' in result
        assert (repo / 'target').read_text() == second.read_text() == 'old\n'
        assert not leases.workspace_lease_fds()
    with _other_file_writer(repo / 'source'):
        result = _run(repo, route, source)
    assert '[exit code: 0]' in result, result
    assert (repo / 'target').read_text() == second.read_text() == 'new\n'
    assert not leases.workspace_lease_fds()


def test_external_target_and_option_terminator_keep_exact_files(repo, tmp_path):
    target = tmp_path / '-target'
    target.write_text('old\n')
    source = f"sed -i -e 's|old|new|' -- {shlex.quote(str(target))}"
    assert commands.command_write_lease_claims(str(repo), source) == (('file', str(target)),)
    source = "sed -i -e 's|old|new|' -- -target"
    assert commands.command_write_lease_claims(str(tmp_path), source) == (('file', str(target)),)
    result = _run(tmp_path, 'runner', source)
    assert '[exit code: 0]' in result, result
    assert target.read_text() == 'new\n'


@pytest.mark.parametrize('arguments', [
    ['s|old|new|g', 'target'],
    ['-E', '-e', 's#old#new#', 'target'],
    ['--expression=s/old/middle/', '-e', 's/middle/new/', 'target'],
])
def test_readonly_and_inplace_share_the_same_substitution_proof(repo, arguments):
    assert is_literal_sed_filter(arguments)
    assert commands.command_write_lease_claims(str(repo), shlex.join(['sed', *arguments])) == ()
    assert not is_literal_sed_filter(['-i', *arguments])
    assert commands.command_write_lease_claims(
        str(repo), shlex.join(['sed', '-i', *arguments])) == (('file', str(repo / 'target')),)


@pytest.mark.parametrize('invocation', [
    "-i 's/old/new/e'", "-i 's/old/new/w other'",
    "-i 's/old/new/;w other'", "-i -e 's/old/new/' -e 'w other'",
    "-i -f program", "--in-place --file=program",
    "-i.bak 's/old/new/'", "--in-place=.bak 's/old/new/'",
    "-iE 's/old/new/'", "-ni.bak 's/old/new/'",
    "--follow-symlinks -i 's/old/new/'",
    "-i --future-option 's/old/new/'",
    "-i '1s/old/new/'", r"-i 's/old/\n/'",
    '-i "s/$PATTERN/new/"', "-i 's/old/new/' -e 'w other'",
])
def test_unproven_programs_options_and_backups_keep_conservative_exclusion(repo, invocation):
    source = f'sed {invocation} target'
    assert ('tree', str(repo)) in commands.command_write_lease_claims(str(repo), source)
    with _other_file_writer(repo / 'source'):
        result = _run(repo, 'runner', source)
    assert '[workspace-busy]' in result, result
    assert (repo / 'target').read_text() == 'old\n'
    assert (repo / 'other').read_text() == 'other\n'
    assert not (repo / 'target.bak').exists()


@pytest.mark.parametrize('target', [
    'alias', 'hardlink', 'dir-alias/file', '.git/config', 'outdir', 'missing',
    'outdir/../target',
])
def test_aliases_metadata_and_unverified_leaves_do_not_borrow_file_only_scope(repo, target):
    source = f"sed -i 's/old/new/' {target}"
    assert ('tree', str(repo)) in commands.command_write_lease_claims(str(repo), source)


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('source', [
    "sed -i -f program target",  # Incomplete scope covers the parent directory.
    "rm -rf internal-leases",  # Actual namespace removal stays forbidden.
])
def test_namespace_refusal_is_static_and_never_suggests_waiting(repo, monkeypatch, route, source):
    namespace = repo / 'internal-leases'
    namespace.mkdir(mode=0o700)
    monkeypatch.setattr(leases, '_tree_lock_namespace', lambda: namespace)

    def forbidden(*_args, **_kwargs):
        pytest.fail('protected namespace refusal reached execution or snapshots')

    monkeypatch.setattr(commands.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(tools, '_snapshot_targets', forbidden)
    monkeypatch.setattr(tools, '_snapshot_project_files', forbidden)
    result = _run(repo, route, source)
    assert '[workspace-protected-namespace]' in result, result
    assert 'Command was not started' in result
    assert 'waiting will not resolve it' in result
    assert '[workspace-busy]' not in result
    assert 'retry after' not in result
    assert namespace.is_dir()
    assert (repo / 'target').read_text() == 'old\n'
    assert not leases.workspace_lease_fds()
