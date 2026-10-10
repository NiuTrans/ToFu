"""Prepare hardened Git reads before execution; never replay a shell command.

Entry point: prepare_git_inspections. Targets come from git_command_scope;
read eligibility comes from command_analysis. The caller owns the supervised
process lifetime. Legacy Git gets per-checkout temporary index copies whose
lifetime spans that process; writes keep their original index/environment.
No persistent capability cache, subprocess pool, or background worker exists.
"""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile

from .command_analysis import _lease_free_git_segment
from .git_command_scope import git_command_scopes
from .workspace_lease import _checkout_identity


class GitInspectionPreparationError(RuntimeError):
    pass


def _optional_locks_supported(executable, environment):
    try:
        result = subprocess.run(
            [executable, '--no-optional-locks', '--version'],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
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


@contextmanager
def prepare_git_inspections(command, base, *, environment):
    """Yield execution source with legacy read indexes scoped per Git segment.

    Capability probes run once per executable per call, before any command
    body. Temporary files disappear after the supervised process finishes,
    including cancellation/errors. No retry can duplicate a neighbouring write.
    """
    scopes = [scope for scope in git_command_scopes(
        command, base, environment=environment, strict=False)
        if _lease_free_git_segment(command[scope.start:scope.end].strip())
        == command[scope.start:scope.end].strip()]
    supported = {}
    legacy = []
    for scope in scopes:
        executable = shlex.split(command[scope.start:scope.end])[0]
        if executable not in supported:
            supported[executable] = _optional_locks_supported(executable, environment)
        if not supported[executable]:
            legacy.append(scope)
    if not legacy:
        yield command
        return
    private_indexes = {}
    replacements = []
    staging = None
    with ExitStack() as stack:
        for scope in legacy:
            segment = command[scope.start:scope.end].strip()
            if _lease_free_git_segment(segment) != segment:
                continue
            words = shlex.split(segment)
            executable = words[0]
            # These builtins may refresh/query the worktree index. Pure ref,
            # config and object queries must retain their observable paths.
            source_index = None
            if scope.subcommand in {'status', 'diff', 'ls-files', 'blame'}:
                if len(scope.execution_cwds) != 1:
                    raise GitInspectionPreparationError('Legacy Git read has ambiguous execution cwd')
                try:
                    source_index = _index_path(scope.execution_cwds[0], environment)
                except OSError as error:
                    raise GitInspectionPreparationError('Cannot resolve private Git index source') from error
            if source_index is not None and source_index not in private_indexes:
                if staging is None:
                    try:
                        staging = stack.enter_context(tempfile.TemporaryDirectory(prefix='tofu_git_inspection_'))
                    except OSError as error:
                        raise GitInspectionPreparationError('Cannot allocate private Git indexes') from error
                private_index = Path(staging) / ('index-' + str(len(private_indexes)))
                try:
                    shutil.copyfile(source_index, private_index)
                except FileNotFoundError:
                    pass  # Unborn repositories legitimately have no index yet.
                except OSError as error:
                    raise GitInspectionPreparationError('Cannot copy readonly Git index') from error
                private_indexes[source_index] = private_index
            # Remove only the known global hardening flag, never an operand.
            words.remove('--no-optional-locks')
            source = shlex.join(words)
            if source_index is not None:
                source = 'GIT_INDEX_FILE=' + shlex.quote(str(private_indexes[source_index])) + ' ' + source
            replacements.append((scope.start, scope.end, source))
        for start, end, source in reversed(replacements):
            command = command[:start] + ' ' + source + ' ' + command[end:]
        yield command.strip() if replacements else command
