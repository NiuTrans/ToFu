"""Prepare hardened Git reads before execution; never replay a shell command.

Entry points: prepare_git_inspections for shell tools; prepare_git_index_read
for internal structured Git arguments. Shell targets come from git_command_scope;
read eligibility comes from command_analysis. The caller owns the supervised
process lifetime. Index-refreshing reads get per-checkout temporary copies when
optional-lock suppression is insufficient or its capability probe times out;
writes keep their original index.
The snapshot child runs from the source loaded with this module (see
lib.runtime.pinned_helper_source), so checkout updates never reach a running
caller. No capability cache, subprocess pool, or background worker exists.
"""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys
import tempfile

from .command_analysis import _lease_free_git_segment, command_requires_workspace_runtime
from .git_command_scope import UNRESOLVED_CWD, git_command_scopes
from .git_index_snapshot_exec import copy_index_snapshot
from .workspace_lease import _checkout_identity
from lib.runtime.pinned_helper_source import PinnedHelperSource


# The argv built below and the child that parses it come from one import.
_SNAPSHOT_CHILD = PinnedHelperSource(Path(__file__).with_name('git_index_snapshot_exec.py'))


class GitInspectionPreparationError(RuntimeError):
    pass


_INDEX_INSPECTIONS = frozenset({'status', 'diff', 'ls-files', 'blame'})


def _observes_index(subcommand, words):
    return subcommand in _INDEX_INSPECTIONS or (
        subcommand == 'describe' and any(word.split('=', 1)[0] in {'--dirty', '--broken'} for word in words))


def _refreshes_despite_optional_locks(subcommand, words):
    return subcommand == 'diff' or (subcommand == 'describe' and _observes_index(subcommand, words))


def _optional_locks_supported(executable, environment):
    """Whether Git honors --no-optional-locks; False selects private indexes.

    Both strategies observe the same state, so the probe only picks the cheaper
    one. A probe still running at its bound (a loaded host) is unknown
    capability and takes the private index every Git supports; it never fails
    the read it guards.
    """
    try:
        result = subprocess.run(
            [executable, '--no-optional-locks', '--version'],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=2, check=False)
    except subprocess.TimeoutExpired:
        return False
    except OSError as error:
        raise GitInspectionPreparationError('Git read capability probe failed') from error
    if result.returncode == 0:
        return True
    detail = result.stderr.decode('utf-8', 'replace').lower()
    if result.returncode == 129 and 'no-optional-locks' in detail:
        return False
    raise GitInspectionPreparationError('Git read capability probe returned an unknown failure')


def _index_path(cwd, environment):
    if environment.get('GIT_INDEX_FILE'):
        return Path(os.path.abspath(os.path.join(cwd, environment['GIT_INDEX_FILE'])))
    if environment.get('GIT_DIR'):
        return Path(os.path.abspath(os.path.join(cwd, environment['GIT_DIR']))) / 'index'
    identity = _checkout_identity(cwd)
    if identity is None:
        return None  # No repository/index exists; preserve Git's own error.
    marker = Path(identity[0]) / '.git'
    if marker.is_dir():
        return marker / 'index'
    pointer = marker.read_text(encoding='utf-8').strip()
    if not pointer.startswith('gitdir: '):
        raise GitInspectionPreparationError('Invalid readonly Git directory pointer')
    return (marker.parent / pointer[8:]).resolve() / 'index'


def _private_index_directory(stack):
    try:
        return stack.enter_context(tempfile.TemporaryDirectory(prefix='tofu_git_inspection_'))
    except OSError as error:
        raise GitInspectionPreparationError('Cannot allocate private Git indexes') from error


def _copy_read_index(source_index, private_index):
    try:
        copy_index_snapshot(source_index, private_index)
    except OSError as error:
        raise GitInspectionPreparationError('Cannot copy readonly Git index') from error


def _snapshot_child_path():
    try:
        return _SNAPSHOT_CHILD.path()
    except OSError as error:
        raise GitInspectionPreparationError('Cannot stage the Git index snapshot helper') from error


@contextmanager
def prepare_git_index_read(executable, arguments, cwd, *, environment):
    """Protect index-observing builtins without changing writes or ref queries.

    Arguments start with the builtin; this port performs no shell parsing or
    authorization. Its caller retains command policy and process supervision.
    A private legacy index lives until the child has completed, even on error.
    """
    if not arguments or not _observes_index(arguments[0], arguments[1:]):
        yield arguments, environment
        return
    # Porcelain diff and dirty describe refresh even with --no-optional-locks
    # on supported Git versions (including 2.34.1); isolate those refreshes.
    if (not _refreshes_despite_optional_locks(arguments[0], arguments[1:])
            and _optional_locks_supported(executable, environment)):
        yield ['--no-optional-locks', *arguments], environment
        return
    try:
        source_index = _index_path(cwd, environment)
    except OSError as error:
        raise GitInspectionPreparationError('Cannot resolve private Git index source') from error
    if source_index is None:
        yield arguments, environment  # Preserve Git's non-repository diagnostic.
        return
    with ExitStack() as stack:
        private_index = Path(_private_index_directory(stack)) / 'index'
        _copy_read_index(source_index, private_index)
        yield arguments, {**environment, 'GIT_INDEX_FILE': str(private_index)}


@contextmanager
def prepare_git_inspections(command, base, *, environment):
    """Yield execution source with read indexes scoped per Git segment.

    Capability probes run once per executable per call, before any command
    body. Temporary files disappear after the supervised process finishes,
    including cancellation/errors. No retry can duplicate a neighbouring write.
    """
    scopes = [scope for scope in git_command_scopes(
        command, base, environment=environment, strict=False)
        if _lease_free_git_segment(command[scope.start:scope.end].strip())
        == command[scope.start:scope.end].strip()]
    supported = {}
    protected = []
    for scope in scopes:
        words = shlex.split(command[scope.start:scope.end])
        executable = words[0]
        if executable not in supported:
            supported[executable] = _optional_locks_supported(executable, environment)
        if not supported[executable] or _refreshes_despite_optional_locks(scope.subcommand, words):
            protected.append(scope)
    if not protected:
        yield command
        return
    # Lease-free interpreters can still write an index. Reuse the stronger
    # complete readonly proof that also owns dependency-preparation skipping.
    readonly_batch = not command_requires_workspace_runtime(command)
    replacements = []
    staging = None
    with ExitStack() as stack:
        for scope in scopes:
            segment = command[scope.start:scope.end].strip()
            if _lease_free_git_segment(segment) != segment:
                continue
            if scope not in protected:
                continue
            words = shlex.split(segment)
            executable = words[0]
            # These builtins may refresh/query the worktree index. Pure ref,
            # config and object queries must retain their observable paths.
            source_index = None
            private_index = None
            observes_index = _observes_index(scope.subcommand, words)
            # After `cd "$X"` only the shell knows the checkout whose index it reads.
            known_cwd = (len(scope.execution_cwds) == 1
                         and scope.execution_cwds[0] != UNRESOLVED_CWD)
            snapshot_at_execution = not readonly_batch or not known_cwd
            if observes_index and known_cwd:
                try:
                    source_index = _index_path(scope.execution_cwds[0], environment)
                except OSError as error:
                    raise GitInspectionPreparationError('Cannot resolve private Git index source') from error
            if source_index is not None or (snapshot_at_execution and observes_index):
                if staging is None:
                    staging = _private_index_directory(stack)
                private_index = Path(staging) / ('index-' + str(len(replacements)))
                if source_index is not None:
                    _copy_read_index(source_index, private_index)
            # Remove only the known global hardening flag, never an operand.
            if not supported[executable]:
                words.remove('--no-optional-locks')
            source = shlex.join(words)
            if private_index is not None:
                if not snapshot_at_execution:
                    # A proven read-only batch can pin its observation at entry
                    # without launching an additional interpreter for each read.
                    source = 'GIT_INDEX_FILE=' + shlex.quote(str(private_index)) + ' ' + source
                else:
                    # Mixed bodies and conditional cwd paths must see the
                    # shell's actual directory, preceding writes and Git env.
                    # Git itself resolves the index at execution.
                    # Distinct destinations keep background reads independent.
                    source = shlex.join([
                        sys.executable, '-I', '-S', _snapshot_child_path(),
                        json.dumps(scope.directory_steps), str(private_index), '--', *words])
            replacements.append((scope.start, scope.end, source))
        for start, end, source in reversed(replacements):
            command = command[:start] + ' ' + source + ' ' + command[end:]
        yield command.strip() if replacements else command
