"""Best-effort Git checkpoints for one model-owned canonical checkout.

Project tools never call this module and are never admitted, delayed, or
rejected by Git state. After a task reaches a terminal state, the existing
commit-round daemon may ask this module to record the checkout's current bytes
as one immutable checkpoint ref. A short cross-process lock serializes only
Git index/ref updates; the shared checkout writer lease additionally excludes
cooperative Tofu writers during capture. Publication writes no checkout file.
The checked-out branch remains at its last published revision until
verification passes.

Concurrent edits are intentionally coalesced workspace state, not task-owned
attribution.  Bytes written after a snapshot remain dirty for a later task.
When such a coalesced capture fails verification, the task's own paths (the
integration radar's record for its conversation, or a delivered delta) are
retried alone so another writer's unfinished change cannot hold them back.
Checkpoint failure is observable in the settlement receipt and logs, while the
task result remains unchanged.  No worktree, merge, stash, reset, checkout,
clean, push, or model-issued Git command is involved.

``refs/tofu/stable`` is a publication pointer, not a synonym for development
``HEAD``.  It advances with the branch only when the checkpoint's own delta,
from the commit it was captured on, passes the gate on a private checkout of
exactly the captured bytes. Writers keep editing the shared checkout
meanwhile: publication moves only the index entries of the paths it changes
and the refs, never a checkout file, and bytes written after capture stay
dirty for the next checkpoint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

from lib.git_checkpoint_policy import (
    forbidden_checkpoint_paths,
    semantic_gate_paths,
    GatePolicyError, git_policy_environment, select_gate_commands,
    require_unchanged_gates,
    PublicationRefError, retire_unrelated_publication_refs, unrelated_publication_refs,
)
from lib.git_runtime import GIT_STALL_SECONDS, git_executable
from lib.log import get_logger

from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_write_lease
logger = get_logger(__name__)

STABLE_REF = 'refs/tofu/stable'
CHECKPOINT_BASELINE_REF = 'refs/tofu/workspace-checkpoint-baseline'
CHECKPOINT_REF_ROOT = 'refs/tofu/checkpoints'

_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on', 'linear'})
_FALSE_VALUES = frozenset({'0', 'false', 'no', 'off', 'disabled'})
_SAFE_REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$')


class LinearCheckpointError(RuntimeError):
    """A workspace checkpoint could not be recorded or promoted safely."""


def _git(
    cwd: str | Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    check: bool = False,
    input_text: str | None = None,
    whole_tree: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run Git; ``whole_tree`` commands have no wall-clock deadline.

    Their duration follows the tree and the volume, so like the integration
    snapshot they run under the shared stall window: only a whole window in
    which no Git thread ran ends them.
    """
    from lib.project_mod.activity_supervised_process import (
        ProcessRetirementUnproven, ProcessStalled, run_activity_supervised)
    from lib.project_mod.git_inspection_execution import (
        GitInspectionPreparationError, prepare_git_index_read)
    merged_env = git_policy_environment()
    if env:
        merged_env.update(env)
    try:
        executable = git_executable()
        with prepare_git_index_read(executable, args, str(cwd), environment=merged_env) as (arguments, environment):
            argv = [executable, '-c', 'core.fsmonitor=false', *arguments]
            if whole_tree:
                process = run_activity_supervised(
                    argv, cwd=cwd, env=environment, stall_seconds=GIT_STALL_SECONDS)
            else:
                process = subprocess.run(
                    argv, cwd=str(cwd), env=environment, text=True,
                    input=input_text,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=timeout, check=False,
                )
    except (OSError, subprocess.TimeoutExpired, GitInspectionPreparationError,
            ProcessStalled, ProcessRetirementUnproven) as error:
        raise LinearCheckpointError(
            f'git {args[0] if args else "command"} failed: {error}') from error
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout or '').strip()[:1000]
        raise LinearCheckpointError(
            f'git {args[0] if args else "command"} failed: {detail}')
    return process


def _repository_root(path: str | Path) -> Path | None:
    raw = str(path or '').strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if not candidate.exists():
        return None
    process = _git(candidate, ['rev-parse', '--show-toplevel'])
    if process.returncode != 0 or not process.stdout.strip():
        return None
    return Path(process.stdout.strip()).resolve()


def _revision(root: Path, ref: str) -> str:
    process = _git(root, ['rev-parse', '--verify', f'{ref}^{{commit}}'])
    return process.stdout.strip() if process.returncode == 0 else ''


def _bounded_float(name: str, default: float, minimum: float,
                   maximum: float) -> float:
    raw = os.environ.get(name, '').strip()
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        logger.warning('[LinearCheckpoint] invalid %s=%r; using %.1f',
                       name, raw, default)
        value = default
    return max(minimum, min(value, maximum))


def _explicit_env_mode() -> bool | None:
    raw = os.environ.get('TOFU_LINEAR_GIT_CHECKPOINT', '').strip().lower()
    if not raw:
        return None
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    logger.warning('[LinearCheckpoint] invalid TOFU_LINEAR_GIT_CHECKPOINT=%r; '
                   'failing closed (disabled)', raw)
    return False


def is_enabled(project_path: str | Path) -> bool:
    """Return the explicit linear-checkpoint mode for one Git repository.

    The environment override is process-wide.  Without it, the repository must
    opt in through ``git config --local tofu.linearCheckpoint true``.  Automatic
    commits are never a surprise default for arbitrary user projects.
    """
    explicit = _explicit_env_mode()
    if explicit is not None:
        return explicit
    root = _repository_root(project_path)
    if root is None:
        return False
    process = _git(root, ['config', '--bool', '--get',
                          'tofu.linearCheckpoint'])
    return process.returncode == 0 and process.stdout.strip().lower() == 'true'


def resolve_publication_mode(project_path: str | Path) -> dict[str, Any]:
    """Read-only admission decision; opt-in alone is not publication ownership.

    Known incompatible history selects isolated-task mode without adopting or
    changing any ref. Both publication refs absent permit the existing first
    settlement's CAS baseline activation. Failed/incomplete observations raise
    instead of authorizing shared execution. Settlement still rechecks ownership.
    """
    def decision(requested: bool, eligible: bool, reason: str) -> dict[str, Any]:
        return {'requested': requested, 'eligible': eligible,
                'mode': 'linear-checkpoint' if eligible else 'isolated-task',
                'reason': reason[:500]}

    def probe(root: Path, args: list[str], *, missing=False):
        result = _git(root, args)
        if (missing and result.returncode == 1
                and not result.stdout.strip() and not result.stderr.strip()):
            return result
        if result.returncode != 0 or result.stderr.strip():
            raise LinearCheckpointError(
                f'Cannot resolve publication mode: Git {args[0]} probe failed')
        return result

    raw_mode = os.environ.get('TOFU_LINEAR_GIT_CHECKPOINT', '').strip().lower()
    if raw_mode and raw_mode not in _TRUE_VALUES | _FALSE_VALUES:
        raise LinearCheckpointError('Cannot resolve publication mode: invalid linear opt-in')
    if raw_mode in _FALSE_VALUES:
        return decision(False, False, 'Linear checkpoint was not requested')
    if not str(project_path or '').strip():
        raise LinearCheckpointError('Cannot resolve publication mode: missing repository path')
    root = Path(project_path).expanduser().resolve()
    identity = probe(root, ['rev-parse', '--show-toplevel']).stdout.strip()
    if not identity or len(identity.splitlines()) != 1 or not Path(identity).is_absolute():
        raise LinearCheckpointError('Cannot resolve publication mode: incomplete repository identity')
    root = Path(identity).resolve()
    if not raw_mode:
        configured = probe(root, ['config', '--bool', '--get',
                                  'tofu.linearCheckpoint'], missing=True)
        value = configured.stdout.strip().lower()
        if configured.returncode == 1 or value == 'false':
            return decision(False, False, 'Linear checkpoint was not requested')
        if value != 'true':
            raise LinearCheckpointError('Cannot resolve publication mode: incomplete linear opt-in')

    refs = {}
    expected = {'refs/tofu/candidate', STABLE_REF, CHECKPOINT_BASELINE_REF}
    observed = probe(root, ['for-each-ref', '--count=4',
                           '--format=%(refname) %(objectname) %(objecttype)',
                           *sorted(expected)])
    for line in observed.stdout.splitlines():
        fields = line.split()
        if (len(fields) != 3 or fields[0] not in expected or fields[0] in refs
                or not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', fields[1])
                or fields[2] != 'commit'):
            raise LinearCheckpointError('Cannot resolve publication mode: incomplete publication refs')
        refs[fields[0]] = fields[1]

    branch = probe(root, ['symbolic-ref', '-q', 'HEAD'], missing=True)
    if branch.returncode == 1:
        return decision(True, False, 'Detached HEAD cannot receive linear publication')
    if not branch.stdout.strip().startswith('refs/heads/') or len(branch.stdout.splitlines()) != 1:
        raise LinearCheckpointError('Cannot resolve publication mode: incomplete HEAD identity')
    head = probe(root, ['rev-parse', '--verify', '--quiet', 'HEAD^{commit}'], missing=True)
    if head.returncode == 1:
        return decision(True, False, 'Unborn HEAD cannot receive linear publication')
    head_sha = head.stdout.strip()
    if not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', head_sha):
        raise LinearCheckpointError('Cannot resolve publication mode: incomplete HEAD revision')
    try:
        unrelated = unrelated_publication_refs(
            lambda args, input_text=None: _git(root, args), head_sha)
    except PublicationRefError as error:
        raise LinearCheckpointError(f'Cannot resolve publication mode: {error}') from error
    for name in unrelated:
        refs.pop(name, None)
    retiring = ('Publication refs from an unrelated history will be retired; '
                if unrelated else '')
    if 'refs/tofu/candidate' in refs:
        return decision(True, False, 'Integration candidate exists; linear checkpoint does not own publication')
    marker, stable = refs.get(CHECKPOINT_BASELINE_REF), refs.get(STABLE_REF)
    if stable and not marker:
        return decision(True, False, 'Existing stable history has no linear baseline ownership')
    if marker and not stable:
        return decision(True, False, 'Linear baseline exists but stable ref is missing')
    if stable and stable != head_sha:
        # Ordinary commits and landed merges move HEAD past stable; the next
        # gate verifies the whole stable-to-checkpoint delta. Only a stable
        # outside HEAD's history (a reset or a branch switch) needs reconciling.
        behind = probe(root, ['merge-base', '--is-ancestor', stable, head_sha], missing=True)
        if behind.returncode == 1:
            return decision(True, False, 'Stable is outside HEAD history; linear publication requires reconciliation')
    if marker:
        ancestor = probe(root, ['merge-base', '--is-ancestor', marker, head_sha], missing=True)
        if ancestor.returncode == 1:
            return decision(True, False, 'HEAD is outside the linear baseline history')
        position = ('at HEAD' if stable == head_sha
                    else 'behind HEAD; the next gate verifies the whole delta')
        return decision(True, True, retiring + f'Linear baseline owns stable {position}')
    return decision(True, True, retiring + 'Unowned refs permit initial linear baseline activation')


def _configured_gate_command(root: Path) -> str:
    return select_gate_commands(root).linear


def _valid_source_ref(value: str) -> bool:
    if value == 'HEAD' or re.fullmatch(r'[0-9a-fA-F]{40,64}', value):
        return True
    if not value.startswith('refs/') or not _SAFE_REF_RE.fullmatch(value):
        return False
    return not any(token in value for token in ('..', '@{', '//')) \
        and not value.endswith(('/', '.lock', '.'))


def preferred_export_ref(project_path: str | Path) -> str:
    """Choose the immutable ref an export should resolve before archiving.

    An explicit environment or repository setting wins.  Otherwise an opted-in
    linear repository publishes its stable pointer once that pointer exists;
    repositories outside this mode preserve the historical ``HEAD`` default.
    """
    root = _repository_root(project_path)
    if root is None:
        return 'HEAD'
    configured = os.environ.get('TOFU_EXPORT_SOURCE_REF', '').strip()
    if not configured:
        process = _git(root, ['config', '--get', 'tofu.exportRef'])
        if process.returncode == 0:
            configured = process.stdout.strip()
    if configured:
        if not _valid_source_ref(configured):
            raise LinearCheckpointError(
                f'Invalid configured export source ref: {configured!r}')
        if not _revision(root, configured):
            raise LinearCheckpointError(
                f'Configured export source ref does not resolve: {configured}')
        return configured
    if is_enabled(root):
        # Merely turning on the repository setting must not make an older
        # isolated-mode stable ref replace today's export.  The first task-end
        # checkpoint establishes the explicit trusted linear baseline.
        baseline = _revision(root, CHECKPOINT_BASELINE_REF)
        if not baseline:
            return 'HEAD'
        stable = _revision(root, STABLE_REF)
        if not stable:
            raise LinearCheckpointError(
                'Linear checkpoint baseline exists but refs/tofu/stable is '
                'missing; refusing to export unverified HEAD')
        # A rewritten history strands baseline and stable outside HEAD's
        # lineage. Promotion only fast-forwards, so that stable can never move
        # again and exporting it would ship one frozen tree forever while every
        # newer capability silently disappears (2026-10: 0.17.0 exported from
        # a 0.18.3 checkout). Such a baseline no longer owns publication.
        if not _is_ancestor(root, baseline, 'HEAD'):
            logger.warning(
                '[LinearCheckpoint] baseline %s is outside HEAD history; '
                'exporting HEAD instead of orphaned stable %s',
                baseline[:12], stable[:12])
            return 'HEAD'
        if not _is_ancestor(root, stable, 'HEAD'):
            raise LinearCheckpointError(
                'refs/tofu/stable is not in the history of HEAD; reconcile it '
                'or set TOFU_EXPORT_SOURCE_REF explicitly')
        return STABLE_REF
    return 'HEAD'


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    process = _git(root, ['merge-base', '--is-ancestor', ancestor, descendant])
    if process.returncode not in (0, 1):
        raise LinearCheckpointError(
            'Cannot compare export refs: '
            + (process.stderr or process.stdout).strip()[:300])
    return process.returncode == 0


def _checkpoint_identity(user_id: int, task_id: str) -> tuple[int, str]:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise LinearCheckpointError(
            'Git checkpointing requires an explicit positive user id')
    normalized_task_id = str(task_id or '').strip()
    if not normalized_task_id:
        raise LinearCheckpointError(
            'Git checkpointing requires a task id')
    return user_id, normalized_task_id


def _safe_task_ref_component(task_id: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', task_id).strip('.-')[:48] or 'task'
    digest = hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:10]
    return f'{slug}-{digest}'


def _lock_path(root: Path) -> Path:
    process = _git(root, ['rev-parse', '--git-common-dir'], check=True)
    git_common_dir = Path(process.stdout.strip())
    if not git_common_dir.is_absolute():
        git_common_dir = root / git_common_dir
    # One tiny persistent lock belongs to the repository lifecycle. Keeping it
    # in the common Git directory avoids an unbounded process registry, and
    # linked checkouts still serialize the same physical Git authority.
    return git_common_dir.resolve() / 'tofu-linear-checkpoint.lock'


def _try_os_lock(handle: BinaryIO) -> bool:
    try:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    except ImportError:  # pragma: no cover - Windows path
        import msvcrt
        try:
            handle.seek(0)
            if handle.read(1) == b'':
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False


def _unlock_os(handle: BinaryIO) -> None:
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover - Windows path
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _write_lock_metadata(handle: BinaryIO, *, user_id: int, task_id: str,
                         conv_id: str) -> None:
    payload = json.dumps({
        'schema': 'tofu.linear-git-checkpoint-lock/v1',
        'userId': user_id,
        'taskId': task_id,
        'convId': conv_id,
        'pid': os.getpid(),
        'acquiredAt': time.time(),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    handle.seek(0)
    handle.truncate(0)
    handle.write(payload)
    handle.flush()


def _acquire_checkpoint_lock(
    root: Path, *, user_id: int, task_id: str, conv_id: str,
) -> BinaryIO | None:
    """Return the short Git-operation lock, or ``None`` after bounded wait."""
    lock_path = _lock_path(root)
    wait_seconds = _bounded_float(
        'TOFU_LINEAR_GIT_CHECKPOINT_LOCK_WAIT_SECONDS',
        30.0, 0.0, 600.0)
    deadline = time.monotonic() + wait_seconds
    handle = open(lock_path, 'a+b')
    while True:
        if _try_os_lock(handle):
            _write_lock_metadata(
                handle, user_id=user_id, task_id=task_id, conv_id=conv_id)
            return handle
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            handle.close()
            return None
        time.sleep(min(0.2, remaining))


def _release_checkpoint_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        handle.truncate(0)
        handle.flush()
    except OSError as error:
        logger.debug('[LinearCheckpoint] lock metadata clear failed: %s',
                     error)
    with contextlib.suppress(Exception):
        _unlock_os(handle)


def _working_tree_status(root: Path) -> list[str]:
    process = _git(root, [
        'status', '--porcelain=v1', '-z', '--untracked-files=all',
    ], timeout=60.0, check=True)
    entries: list[str] = []
    tokens = [token for token in process.stdout.split('\0') if token]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        status = token[:2]
        path = token[3:] if len(token) > 3 else token
        entries.append(path)
        index += 2 if status[:1] in {'R', 'C'} else 1
    return entries


def _ensure_checkpoint_baseline(root: Path, head_sha: str) -> str:
    """CAS-activate only unowned refs; never adopt another controller's history.

    Publication refs from an unrelated (rewritten) history are archived first:
    they cannot own this history, and left in place they refused every
    checkpoint after the rewrite. An owned stable may trail HEAD (ordinary
    commits, landed merges): verification covers the whole stable-to-checkpoint
    delta and publication moves branch and stable together. Only a stable
    outside HEAD's history fails closed.
    """
    retired = retire_unrelated_publication_refs(
        lambda args, input_text=None: _git(root, args, input_text=input_text), head_sha)
    if retired:
        logger.warning(
            '[LinearCheckpoint] retired publication refs from an unrelated history root=%s: %s',
            root, ', '.join(f'{name} -> {archive}' for name, archive in retired.items()))
    marker = _revision(root, CHECKPOINT_BASELINE_REF)
    stable = _revision(root, STABLE_REF)
    if _revision(root, 'refs/tofu/candidate'):
        raise LinearCheckpointError('Integration candidate exists; linear checkpoint does not own publication')
    if _revision(root, 'HEAD') != head_sha:
        raise LinearCheckpointError('HEAD moved during baseline activation')
    if marker:
        if not stable:
            raise LinearCheckpointError('Checkpoint stable ref is missing; explicit recovery is required')
        if stable != head_sha and _git(
                root, ['merge-base', '--is-ancestor', stable, head_sha]).returncode:
            raise LinearCheckpointError(
                'Owned stable is outside HEAD history; explicit reconciliation is required')
        if _git(root, ['merge-base', '--is-ancestor', marker, head_sha]).returncode:
            raise LinearCheckpointError('HEAD is outside the checkpoint baseline history')
        return stable
    if stable:
        raise LinearCheckpointError('Existing stable history is not owned by linear checkpoint')
    zero = '0' * len(head_sha)
    # --stdin commits the entire batch atomically on EOF, including Git 2.11.
    # Explicit start/prepare/commit controls require newer Git and add no
    # isolation here because we send the whole transaction in one input.
    transaction = '\n'.join((
        f'verify HEAD {head_sha}',
        f'verify refs/tofu/candidate {zero}',
        f'create {STABLE_REF} {head_sha}',
        f'create {CHECKPOINT_BASELINE_REF} {head_sha}',
        '',
    ))
    _git(root, ['update-ref', '-m', 'tofu: activate workspace checkpoint baseline',
                '--stdin'], input_text=transaction, check=True)
    return head_sha


def _enabled_repository_roots(project_path: str | None,
                              project_paths: list[str] | None) -> list[Path]:
    candidates = [candidate for candidate in [
        project_path, *(project_paths or []),
    ] if candidate]
    roots: dict[str, Path] = {}
    for candidate in candidates:
        root = _repository_root(candidate)
        if root is None or not is_enabled(root):
            continue
        roots[str(root)] = root
    return [roots[key] for key in sorted(roots)]


def _commit_environment(index_path: str) -> dict[str, str]:
    return {
        'GIT_INDEX_FILE': index_path,
        'GIT_AUTHOR_NAME': 'Tofu Agent',
        'GIT_AUTHOR_EMAIL': 'agent@tofu.local',
        'GIT_COMMITTER_NAME': 'Tofu Agent',
        'GIT_COMMITTER_EMAIL': 'agent@tofu.local',
    }


def _stage_working_tree(root: Path, base_sha: str) -> tuple[str, str, dict[str, str]]:
    descriptor, index_path = tempfile.mkstemp(prefix='tofu-linear-index-')
    os.close(descriptor)
    os.unlink(index_path)
    env = _commit_environment(index_path)
    try:
        _git(root, ['read-tree', base_sha], env=env, check=True, whole_tree=True)
        _git(root, ['add', '-A', '--', '.'], env=env, check=True, whole_tree=True)
        tree_sha = _git(root, ['write-tree'], env=env, check=True,
                        whole_tree=True).stdout.strip()
        return index_path, tree_sha, env
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(index_path)
        raise


def _commit_tree(root: Path, tree_sha: str, base_sha: str, *,
                 task: dict[str, Any], verification: str) -> str:
    task_id = str(task.get('id') or '')
    outcome = ('failed' if task.get('error') or task.get('aborted')
               else str(task.get('status') or 'unknown'))
    subject = ('Tofu WIP checkpoint' if outcome != 'done'
               else 'Tofu checkpoint')
    message = f'{subject}: {task_id[:24]}'
    body = '\n'.join((
        f'Task: {task_id}',
        f'Conversation: {str(task.get("convId") or "")}',
        f'Outcome: {outcome}',
        f'Verification: {verification}',
    ))
    env = _commit_environment('')
    env.pop('GIT_INDEX_FILE', None)
    process = _git(root, [
        'commit-tree', tree_sha, '-p', base_sha,
        '-m', message, '-m', body,
    ], env=env, check=True)
    return process.stdout.strip()


def _changed_paths(root: Path, old_sha: str, target_sha: str) -> list[str]:
    process = _git(root, [
        'diff', '--name-status', '--find-renames',
        '--diff-filter=ACMRD', '-z', old_sha, target_sha,
    ], check=True)
    tokens = [token for token in process.stdout.split('\0') if token]
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        count = 2 if status[:1] in {'R', 'C'} else 1
        entry_paths = tokens[index:index + count]
        if len(entry_paths) != count:
            raise LinearCheckpointError(
                'Git returned malformed name-status data')
        paths.extend(entry_paths)
        index += count
    return paths


def _worktree_matches_index(root: Path, env: dict[str, str]) -> bool:
    tracked = _git(root, ['diff-files', '--quiet', '--ignore-submodules'],
                   env=env, whole_tree=True)
    if tracked.returncode != 0:
        return False
    untracked = _git(root, [
        'ls-files', '--others', '--exclude-standard', '-z',
    ], env=env, check=True, whole_tree=True)
    return not bool(untracked.stdout)


def _task_succeeded(task: dict[str, Any]) -> bool:
    return (
        str(task.get('status') or '') == 'done'
        and not task.get('error')
        and not task.get('aborted')
        and str(task.get('finishReason') or '') != 'error'
    )


def _syntax_check(root: Path, paths: list[str]) -> tuple[bool, str]:
    current_files = [path for path in paths if (root / path).is_file()]
    marker_pattern = re.compile(r'^(?:<<<<<<<|=======|>>>>>>>)(?:\s|$)', re.M)
    for relative in current_files:
        try:
            source = (root / relative).read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        except OSError as error:
            return False, f'{relative}: {error}'
        if marker_pattern.search(source):
            return False, f'{relative}: unresolved merge-conflict marker'
    for relative in [path for path in current_files if path.endswith('.py')]:
        try:
            source = (root / relative).read_text(encoding='utf-8')
            compile(source, relative, 'exec')
        except (OSError, UnicodeError, SyntaxError) as error:
            return False, f'{relative}: {error}'
    for relative in [path for path in current_files if path.endswith('.json')]:
        try:
            json.loads((root / relative).read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError) as error:
            return False, f'{relative}: {error}'
    node = shutil.which('node')
    if node:
        for relative in [
            path for path in current_files
            if path.endswith(('.js', '.mjs', '.cjs'))
        ]:
            try:
                source = (root / relative).read_text(encoding='utf-8')
            except (OSError, UnicodeError) as error:
                return False, f'{relative}: {error}'
            input_type = (
                'commonjs' if relative.lower().endswith('.cjs') else 'module'
            )
            process = subprocess.run(
                [node, f'--input-type={input_type}', '--check'],
                cwd=str(root), text=True, input=source,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30.0, check=False,
            )
            if process.returncode != 0:
                detail = (process.stderr or process.stdout).strip()[:1200]
                return False, f'{relative}: {detail}'
    return True, ''


def _ruff_check(root: Path, paths: list[str]) -> tuple[bool, str]:
    """Run the repository's Python static gate on changed source files."""
    ruff_enabled = (root / 'ruff.toml').is_file() or \
        (root / '.ruff.toml').is_file()
    pyproject = root / 'pyproject.toml'
    if not ruff_enabled and pyproject.is_file():
        try:
            ruff_enabled = '[tool.ruff' in pyproject.read_text(
                encoding='utf-8')
        except (OSError, UnicodeError):
            ruff_enabled = False
    if not ruff_enabled:
        return True, ''
    python_paths = [
        path for path in paths
        if path.endswith('.py') and (root / path).is_file()
    ]
    if not python_paths:
        return True, ''
    process = subprocess.run(
        [os.sys.executable, '-m', 'ruff', 'check', '--no-cache',
         '--output-format=concise',
         *python_paths],
        cwd=str(root), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=120.0, check=False,
    )
    if process.returncode == 0:
        return True, ''
    detail = '\n'.join(
        part for part in (process.stdout, process.stderr) if part
    ).strip()[-3000:]
    return False, detail or 'ruff check failed'


def _run_verification_gate(
    root: Path,
    verification_base: str,
    probe_sha: str,
    paths: list[str],
    task: dict[str, Any],
) -> tuple[str, str]:
    """Verify the checkpoint's own bytes on a private checkout of them.

    ``verification_base`` is the commit the checkpoint was captured on, so the
    gate covers exactly the delta publication adds. Writers that keep editing
    the shared checkout can neither invalidate the result nor leak into it.
    """
    if not _task_succeeded(task):
        return 'task_failed', 'Task did not settle successfully'
    diff_check = _git(root, [
        'diff', '--check', verification_base, probe_sha,
    ])
    if diff_check.returncode != 0:
        return 'failed', (diff_check.stdout or diff_check.stderr).strip()[:3000]
    from lib.project_mod.landing_verification import verification_checkout
    try:
        with verification_checkout(root, probe_sha) as tree:
            return _gate_checkout(root, tree, verification_base, probe_sha, paths)
    except OSError as error:
        return 'failed', f'Verification checkout failed: {error}'[:3000]


def _gate_checkout(root: Path, tree: Path, verification_base: str,
                   probe_sha: str, paths: list[str]) -> tuple[str, str]:
    syntax_ok, syntax_detail = _syntax_check(tree, paths)
    if not syntax_ok:
        return 'failed', syntax_detail[:3000]
    ruff_ok, ruff_detail = _ruff_check(tree, paths)
    if not ruff_ok:
        return 'failed', ruff_detail[:3000]
    try:
        selected_policy = select_gate_commands(root)
        command = selected_policy.linear
    except GatePolicyError as error:
        return 'failed', str(error)
    semantic_paths = semantic_gate_paths(paths)
    if semantic_paths and not command:
        return (
            'required',
            'Semantic code/config changes require '
            'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD (or '
            'tofu.linearCheckpointTestCommand): '
            + ', '.join(semantic_paths[:64]),
        )
    if not command:
        return 'passed', ''
    try:
        argv = [
            token.replace('{base}', verification_base).replace(
                '{target}', probe_sha)
            for token in shlex.split(command)
        ]
    except ValueError as error:
        return 'failed', f'Invalid checkpoint test command: {error}'
    if not argv:
        return 'failed', 'Configured checkpoint test command is empty'
    environment = git_policy_environment()
    environment.update({
        'TOFU_LINEAR_CHECKPOINT_BASE_SHA': verification_base,
        'TOFU_LINEAR_CHECKPOINT_TARGET_SHA': probe_sha,
        # Imports resolve to the verified checkout, never an inherited path.
        'PYTHONPATH': str(tree),
    })
    try:
        process = subprocess.run(
            argv, cwd=str(tree), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_bounded_float(
                'TOFU_LINEAR_GIT_CHECKPOINT_TEST_TIMEOUT',
                _bounded_float('TOFU_INTEGRATION_TEST_TIMEOUT',
                               600.0, 1.0, 3600.0),
                1.0, 3600.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 'failed', str(error)[:3000]
    rewritten = _git(tree, ['status', '--porcelain=v1', '--untracked-files=no'],
                     whole_tree=True)
    if rewritten.returncode != 0 or rewritten.stdout.strip():
        return 'workspace_changed', (
            'The verification command rewrote tracked files in its checkout, so the '
            'verified bytes are not the checkpoint; nothing was published: '
            + (rewritten.stdout or rewritten.stderr).strip())[:3000]
    attribution = ''
    if process.returncode != 0:
        output = '\n'.join(
            part for part in (process.stdout, process.stderr) if part).strip()
        verdict, attribution = _attribute_gate_failure(
            root, tree, verification_base, probe_sha, output)
        if verdict != 'passed':
            return verdict, attribution

    try:
        if select_gate_commands(root) != selected_policy:
            return 'failed', 'Git gate policy changed during verification'
    except GatePolicyError as error:
        return 'failed', str(error)
    return 'passed', attribution


def _attribute_gate_failure(root: Path, tree: Path, base_sha: str, probe_sha: str,
                            output: str) -> tuple[str, str]:
    """Only a reproduced regression of the checkpoint's own delta blocks it.

    The configured gate reports pass or fail. The repository's test selector,
    through landing verification's merged-result attribution, decides whether
    its failures are new: tests already failing on the base, and failures that do not
    reproduce, never block publication. Without that evidence the failure stands.
    """
    from lib.project_mod.landing_verification import verify_merge
    try:
        verdict = verify_merge(root, base_sha, probe_sha, tree)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return 'failed', f'{output}\n(failure attribution unavailable: {error})'[-3000:]
    if verdict.status == 'passed':
        notes = [verdict.reason]
        if verdict.preexisting:
            notes.append('already failing on the base: ' + ', '.join(verdict.preexisting))
        if verdict.flaky:
            notes.append('not reproducible: ' + ', '.join(verdict.flaky))
        return 'passed', ('The configured gate failed, but landing verification '
                          'attributes none of its failures to this checkpoint: '
                          + '; '.join(notes))[:3000]
    if verdict.status == 'regressed':
        return 'failed', ('Tests that pass on the base fail on the checkpoint: '
                          + ', '.join(verdict.regressions) + '\n' + verdict.evidence)[:3000]
    return 'failed', (f'{output}\n(failure attribution {verdict.status}: '
                      f'{verdict.reason})')[-3000:]


def _publish_verified_refs(
    root: Path,
    *,
    branch_ref: str,
    base_sha: str,
    stable_sha: str,
    target_sha: str,
) -> None:
    """Atomically publish the development branch and verified stable ref."""
    ancestor = _git(root, [
        'merge-base', '--is-ancestor', stable_sha, target_sha,
    ])
    if ancestor.returncode != 0:
        raise LinearCheckpointError(
            'refs/tofu/stable diverged from the linear development history; '
            'automatic promotion refuses to merge it')
    # --stdin commits the entire batch atomically on EOF, including Git 2.11.
    # Explicit start/prepare/commit controls require newer Git and add no
    # isolation here because we send the whole transaction in one input.
    transaction = '\n'.join((
        f'update {branch_ref} {target_sha} {base_sha}',
        f'update {STABLE_REF} {target_sha} {stable_sha}',
        f'verify refs/tofu/candidate {"0" * len(base_sha)}',
        '',
    ))
    process = _git(
        root,
        ['update-ref', '-m', 'tofu: verified linear checkpoint', '--stdin'],
        input_text=transaction,
    )
    if process.returncode != 0:
        raise LinearCheckpointError(
            'Development branch or stable ref moved concurrently: '
            + (process.stderr or process.stdout).strip()[:600])


def _carry_captured_tree_over_head(
    root: Path, base_sha: str, current_head: str, tree_sha: str,
) -> str:
    """Replay the workspace delta onto an unrelated landed HEAD privately."""
    if not _is_ancestor(root, base_sha, current_head):
        raise LinearCheckpointError(
            'HEAD was rewritten during capture; workspace left untouched')
    from lib.integration_control import _task_delta_tree

    merged, detail = _task_delta_tree(
        root, base_sha, current_head, tree_sha, 'linear-checkpoint')
    if not merged:
        raise LinearCheckpointError(
            'Workspace changes conflict with a concurrent HEAD commit: ' + detail)
    return merged


def _capture_repository(task: dict[str, Any], user_id: int,
                        root: Path) -> dict[str, Any]:
    """Capture while holding the same lease as tools, before the Git lock."""
    try:
        with workspace_write_lease(root):
            return _capture_repository_leased(task, user_id, root)
    except WorkspaceBusyError as error:
        return {'projectRoot': str(root), 'status': 'deferred',
                'stableUpdated': False, 'verification': 'not_run',
                'verificationDetail': str(error)}


def _capture_repository_leased(task: dict[str, Any], user_id: int,
                               root: Path) -> dict[str, Any]:
    task_id = str(task.get('id') or '')
    handle = _acquire_checkpoint_lock(
        root, user_id=user_id, task_id=task_id,
        conv_id=str(task.get('convId') or ''))
    if handle is None:
        return {
            'projectRoot': str(root), 'status': 'deferred',
            'stableSha': _revision(root, STABLE_REF),
            'stableUpdated': False, 'verification': 'not_run',
            'verificationDetail': (
                'Another task-end checkpoint still owns the Git operation '
                'lock. Project tools were not blocked; current bytes remain '
                'in the workspace for the next checkpoint.'),
        }

    try:
        branch_process = _git(root, ['symbolic-ref', '-q', 'HEAD'])
        branch_ref = branch_process.stdout.strip()
        if (branch_process.returncode != 0
                or not branch_ref.startswith('refs/heads/')):
            raise LinearCheckpointError(
                f'{root}: detached or unborn HEAD cannot receive an '
                'automatic workspace checkpoint')
        base_sha = _revision(root, 'HEAD')
        if not base_sha:
            raise LinearCheckpointError(
                f'{root}: HEAD does not resolve to a commit')
        stable_sha = _ensure_checkpoint_baseline(root, base_sha)

        index_path = ''
        try:
            index_path, tree_sha, index_env = _stage_working_tree(
                root, base_sha)
            capture_consistent = _worktree_matches_index(root, index_env)
            base_tree = _git(root, [
                'rev-parse', f'{base_sha}^{{tree}}',
            ], check=True).stdout.strip()
            if tree_sha == base_tree:
                return {
                    'projectRoot': str(root), 'status': 'no_changes',
                    'baseSha': base_sha, 'checkpointSha': base_sha,
                    'stableSha': stable_sha, 'stableUpdated': False,
                    'verification': (
                        'not_needed' if capture_consistent
                        else 'workspace_changed'),
                    'verificationDetail': (
                        '' if capture_consistent else
                        'Workspace changed while the no-op snapshot was '
                        'being observed; later bytes remain uncommitted.'),
                }

            if _git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip() != branch_ref:
                raise LinearCheckpointError(
                    'Branch changed during capture; workspace left untouched')
            changed_paths = _changed_paths(root, base_sha, tree_sha)
            current_head = _revision(root, 'HEAD')
            if current_head != base_sha:
                tree_sha = _carry_captured_tree_over_head(
                    root, base_sha, current_head, tree_sha)
                base_sha = current_head
                stable_sha = _ensure_checkpoint_baseline(root, base_sha)

            forbidden = forbidden_checkpoint_paths(changed_paths)
            if forbidden:
                raise LinearCheckpointError(
                    'Checkpoint contains forbidden runtime/dependency '
                    'paths; bytes were left untouched: '
                    + ', '.join(forbidden))

            final_sha = _commit_tree(
                root, tree_sha, base_sha, task=task,
                verification='pending_settlement_gate')
            checkpoint_ref = (
                f'{CHECKPOINT_REF_ROOT}/u{user_id}/'
                f'{_safe_task_ref_component(task_id)}')
            ref_update = _git(root, [
                'update-ref', '-m', 'tofu: task-end workspace checkpoint',
                checkpoint_ref, final_sha,
            ])
            if ref_update.returncode != 0:
                logger.warning(
                    '[LinearCheckpoint] checkpoint evidence ref failed '
                    'task=%s root=%s: %s', task_id, root,
                    (ref_update.stderr or ref_update.stdout).strip()[:600])
                checkpoint_ref = ''

            return {
                'projectRoot': str(root), 'status': 'committed',
                'userId': user_id,
                'baseSha': base_sha, 'checkpointSha': final_sha,
                'branchRef': branch_ref,
                'checkpointRef': checkpoint_ref,
                'changedPaths': changed_paths[:128],
                'stableSha': stable_sha, 'stableUpdated': False,
                'verification': 'pending', 'verificationDetail': '',
                'captureConsistent': capture_consistent,
                'indexSynchronized': False,
            }
        finally:
            if index_path:
                with contextlib.suppress(OSError):
                    os.unlink(index_path)
    finally:
        _release_checkpoint_lock(handle)


def _verify_captured_checkpoint(task: dict[str, Any],
                                row: dict[str, Any]) -> dict[str, Any]:
    """Verify the captured bytes, then publish them; never affect task execution.

    The gate covers the delta from the commit the checkpoint was captured on:
    commits already on that base reached the branch through their own
    verification (a landing, an earlier checkpoint, a reviewed delivery).
    Concurrent writers do not invalidate the result; bytes they write after
    capture stay in the workspace for the next checkpoint.
    """
    if row.get('status') != 'committed':
        return row
    root = Path(str(row['projectRoot']))
    target_sha = str(row['checkpointSha'])
    base_sha = str(row.get('baseSha') or '')
    if not _task_succeeded(task):
        row['verification'] = 'task_failed'
        row['verificationDetail'] = 'Task did not settle successfully'
        return row

    stable_sha = _revision(root, STABLE_REF)
    if not stable_sha:
        row['verification'] = 'failed'
        row['verificationDetail'] = 'refs/tofu/stable is missing'
        return row
    if _git(root, [
            'merge-base', '--is-ancestor', stable_sha, target_sha,
    ]).returncode != 0:
        row['verification'] = 'diverged'
        row['verificationDetail'] = (
            'refs/tofu/stable does not precede the checkpoint; automatic '
            'promotion refuses to merge divergent history')
        return row

    try:
        selected_policy = select_gate_commands(root)
    except GatePolicyError as error:
        row['verification'] = 'failed'
        row['verificationDetail'] = str(error)
        return row
    verification_paths = _changed_paths(root, base_sha, target_sha)
    verification, detail = _run_verification_gate(
        root, base_sha, target_sha, verification_paths, task)
    row['verification'] = verification
    row['verificationDetail'] = detail[:3000]
    if verification != 'passed':
        return row
    return _promote_captured_checkpoint(task, row, root, target_sha, stable_sha, selected_policy)


class _PublicationDeferred(LinearCheckpointError):
    """A transient condition (busy index, moved branch, operation in progress)."""


_OPERATIONS_IN_PROGRESS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                           'rebase-merge', 'rebase-apply', 'BISECT_LOG')


def _index_info(records, oid_length: int) -> str:
    """``update-index --index-info`` input; a None entry removes the path."""
    zero = '0' * oid_length
    return ''.join(f'{entry[0]} {entry[1]}\t{path}\0' if entry else f'0 {zero}\t{path}\0'
                   for path, entry in records)


def _git_bytes(root: Path, args: list[str], data: bytes) -> bytes:
    process = subprocess.run(
        [git_executable(), '-c', 'core.fsmonitor=false', *args], cwd=str(root),
        env=git_policy_environment(), input=data, capture_output=True, check=False)
    if process.returncode != 0:
        raise LinearCheckpointError(
            f'git {args[0]} failed: {process.stderr.decode(errors="replace").strip()[:600]}')
    return process.stdout


def _tree_blobs(root: Path, tree_sha: str, scopes: list[str]) -> tuple[dict, dict]:
    """(path -> (mode, oid), path -> bytes) for the blobs of TREE_SHA under SCOPES."""
    listed = _git(root, ['--literal-pathspecs', 'ls-tree', '-r', '-z', tree_sha, '--', *scopes],
                  check=True).stdout
    entries = {}
    for record in listed.split('\0'):
        meta, _, path = record.partition('\t')
        if path:
            mode, kind, oid = meta.split()
            if kind == 'blob':
                entries[path] = (mode, oid)
    data = _git_bytes(root, ['cat-file', '--batch'],
                      ''.join(oid + '\n' for _mode, oid in entries.values()).encode())
    contents, offset = {}, 0
    for path in entries:
        header_end = data.index(b'\n', offset)
        _oid, kind, size = data[offset:header_end].split()
        if kind != b'blob':
            raise LinearCheckpointError(f'Unexpected object for {path}')
        start = header_end + 1
        contents[path] = data[start:start + int(size)]
        offset = start + int(size) + 1
    return entries, contents


def _own_checkpoint_tree(root: Path, base_sha: str, captured_sha: str,
                         selected: set[str]) -> str:
    """BASE plus the captured changes of SELECTED paths only.

    Generated outputs never come from the shared checkout, whose copies follow
    every writer's sources: the affected families are rebuilt from this tree's
    own sources, as git_land does.
    """
    from lib.project_mod.generated_integration import (
        affected_families, build_outputs, family_roots, output_family)
    changes = [(path, new) for path, _old, new in _tree_changes(root, base_sha, captured_sha)
               if path in selected and not output_family(path)]
    if not changes:
        return ''
    families = affected_families(path for path, _new in changes)
    with tempfile.TemporaryDirectory(prefix='tofu-linear-own-') as directory:
        env = {'GIT_INDEX_FILE': str(Path(directory) / 'index')}
        _git(root, ['read-tree', base_sha], env=env, check=True)
        _git(root, ['update-index', '-z', '--index-info'], env=env, check=True,
             input_text=_index_info(changes, len(base_sha)))
        tree = _git(root, ['write-tree'], env=env, check=True).stdout.strip()
        if not families:
            return tree
        entries, files = _tree_blobs(root, tree, family_roots(families))
        outputs = build_outputs(root, families, files, committed=True)
        records = []
        for path in sorted({path for path in entries if output_family(path) in families}
                           | outputs.keys()):
            if path in outputs:
                oid = _git_bytes(root, ['hash-object', '-w', '--stdin'],
                                 outputs[path]).decode().strip()
                records.append((path, (entries.get(path, ('100644',))[0], oid)))
            else:
                records.append((path, None))
        _git(root, ['update-index', '-z', '--index-info'], env=env, check=True,
             input_text=_index_info(records, len(base_sha)))
        return _git(root, ['write-tree'], env=env, check=True).stdout.strip()


def _own_paths_retry(task: dict[str, Any], root: Path, whole: dict[str, Any],
                     own_paths: set[str]) -> dict[str, Any] | None:
    """Retry a failed whole-workspace checkpoint with only the task's own changes.

    The whole capture coalesces every writer's uncommitted bytes, so one writer's
    unfinished change would keep every other task's work out of history. The
    retry commits, on the same base, only the captured changes of paths the
    task's conversation wrote (integration radar) or its caller delivered, and
    verifies and publishes them like any checkpoint. Everything else stays in
    the workspace for later checkpoints. None when no narrower set exists.
    """
    base_sha, captured_sha = str(whole['baseSha']), str(whole['checkpointSha'])
    changed = set(_changed_paths(root, base_sha, captured_sha))
    selected = changed & own_paths
    if not selected or selected == changed:
        return None
    tree = _own_checkpoint_tree(root, base_sha, captured_sha, selected)
    if not tree or tree == _git(root, ['rev-parse', f'{base_sha}^{{tree}}'],
                                check=True).stdout.strip():
        return None
    commit = _commit_tree(root, tree, base_sha, task=task, verification='pending_settlement_gate')
    checkpoint_ref = str(whole.get('checkpointRef') or '')
    if checkpoint_ref:
        checkpoint_ref += '-own'
        if _git(root, ['update-ref', '-m', 'tofu: own-paths checkpoint',
                       checkpoint_ref, commit]).returncode != 0:
            checkpoint_ref = ''
    row = dict(whole, checkpointSha=commit, checkpointRef=checkpoint_ref,
               changedPaths=sorted(selected)[:128], verification='pending',
               verificationDetail='', wholeWorkspace={
                   'checkpointSha': captured_sha, 'changedPathCount': len(changed),
                   'verification': whole.get('verification'),
                   'verificationDetail': str(whole.get('verificationDetail') or '')[:1500]})
    return _verify_captured_checkpoint(task, row)


def _tree_changes(root: Path, old_sha: str, new_sha: str):
    tokens = _git(root, ['diff-tree', '-r', '-z', '--no-renames', '--no-abbrev',
                         old_sha, new_sha], check=True).stdout.split('\0')
    changes = []
    for meta, path in zip(tokens[0::2], tokens[1::2]):
        old_mode, new_mode, old_oid, new_oid, _status = meta.lstrip(':').split(' ')
        changes.append((path,
                        None if not old_mode.strip('0') else (old_mode, old_oid),
                        None if not new_mode.strip('0') else (new_mode, new_oid)))
    return changes


def _index_entries(root: Path, paths: list[str]) -> dict[str, list[list[str]]]:
    if not paths:
        return {}
    listed = _git(root, ['--literal-pathspecs', 'ls-files', '-s', '-v', '-z', '--', *paths],
                  check=True).stdout
    entries: dict[str, list[list[str]]] = {}
    for record in listed.split('\0'):
        meta, _, path = record.partition('\t')
        if path:
            entries.setdefault(path, []).append(meta.split(' '))
    return entries


def _operation_in_progress(root: Path) -> bool:
    arguments = [part for name in _OPERATIONS_IN_PROGRESS for part in ('--git-path', name)]
    paths = _git(root, ['rev-parse', *arguments], check=True).stdout.splitlines()
    return any((root / path).exists() for path in paths)


@contextlib.contextmanager
def _prepared_index_publication(root, base_sha, target_sha):
    """Hold Git's index.lock and prepare only the entries this publication moves.

    For each path base->target changes, an entry still holding the base version
    moves to the target and one already at the target stays. An entry another
    writer staged differently, flagged assume-unchanged/skip-worktree or left
    unmerged is preserved, so their staged work survives and never blocks
    publication. The prepared index is installed only after the caller moved
    the refs and HEAD is the target; a refused CAS removes only our lock. A
    busy index or an operation in progress raises ``_PublicationDeferred``.
    Yields whether every changed path's entry now matches the target.
    """
    raw = _git(root, ['rev-parse', '--git-path', 'index'], check=True).stdout.strip()
    index_path = (root / raw).resolve()
    lock_path = Path(str(index_path) + '.lock')
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise _PublicationDeferred(f'The Git index is busy ({lock_path} exists)') from error
    except OSError as error:
        raise LinearCheckpointError(f'Real index is unavailable: {error}') from error
    temporary = ''
    lock_owned = True
    install = False
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            if _operation_in_progress(root):
                raise _PublicationDeferred('A Git operation is in progress in the checkout')
            changes = _tree_changes(root, base_sha, target_sha)
            entries = (_index_entries(root, [path for path, _old, _new in changes])
                       if index_path.is_file() else None)
            moves, preserved = [], []
            for path, old, new in changes:
                records = [] if entries is None else entries.get(path, [])
                actual = None
                if records:
                    tag, mode, oid, stage = records[0]
                    if len(records) > 1 or tag != 'H' or stage != '0':
                        preserved.append(path)
                        continue
                    actual = (mode, oid)
                if entries is not None and actual == new:
                    continue
                if entries is not None and actual == old:
                    moves.append((path, new))
                else:
                    preserved.append(path)
            if moves:
                fd, temporary = tempfile.mkstemp(prefix='tofu-linear-index-')
                os.close(fd)
                shutil.copyfile(index_path, temporary)
                _git(root, ['update-index', '-z', '--index-info'],
                     input_text=_index_info(moves, len(base_sha)),
                     env={'GIT_INDEX_FILE': temporary}, check=True)
                with open(temporary, 'rb') as prepared:
                    shutil.copyfileobj(prepared, stream)
                stream.flush()
                os.fsync(stream.fileno())
                install = True
        yield not preserved
        if install and _revision(root, 'HEAD') == target_sha:
            os.replace(lock_path, index_path)
            lock_owned = False
    finally:
        # Never unlink a subsequent writer's index.lock after our rename.
        if lock_owned:
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()
        if temporary:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def _carry_verified_checkpoint(root: Path, base_sha: str, head_sha: str,
                               target_sha: str) -> tuple[str, str]:
    """Publish a verified checkpoint over a branch that moved since capture.

    The moved commits arrived through their own verification. When they touch
    none of the checkpoint's paths, its delta replays onto them; only their
    interaction across different files goes unverified, the same exposure as a
    landing whose verdict still covers a newer tip. When the branch already
    holds the same bytes, nothing is left to publish. Changes to the same files
    were never verified together: the bytes stay in the workspace, and the next
    checkpoint captures and verifies them on the new branch tip.
    """
    try:
        merged = _carry_captured_tree_over_head(root, base_sha, head_sha, target_sha)
    except LinearCheckpointError as error:
        return '', f'{error}; its bytes remain in the workspace for the next checkpoint.'
    if merged == _git(root, ['rev-parse', f'{head_sha}^{{tree}}'], check=True).stdout.strip():
        return head_sha, ''
    landed = _git(root, ['diff', '--name-only', '--no-renames', '-z', base_sha, head_sha],
                  check=True).stdout.split('\0')
    moved = sorted(set(filter(None, landed)) & set(_changed_paths(root, base_sha, target_sha)))
    if moved:
        return '', ('The branch moved over paths this checkpoint changes since capture ('
                    + ', '.join(moved) + '). The checkpoint stays recoverable and its bytes '
                    'remain in the workspace; the next checkpoint verifies them on the new '
                    'branch tip.')
    environment = _commit_environment('')
    environment.pop('GIT_INDEX_FILE', None)
    message = _git(root, ['log', '-1', '--format=%B', target_sha], check=True).stdout
    return _git(root, ['commit-tree', merged, '-p', head_sha, '-F', '-'], env=environment,
                input_text=message, check=True).stdout.strip(), ''


def _promote_captured_checkpoint(task, row, root, target_sha, stable_sha, selected_policy):
    handle = _acquire_checkpoint_lock(
        root,
        user_id=int(row.get('userId') or 1),
        task_id=str(task.get('id') or ''),
        conv_id=str(task.get('convId') or ''),
    )
    if handle is None:
        row['verification'] = 'promotion_deferred'
        row['verificationDetail'] = (
            'Verification passed, but another checkpoint owns the Git lock; '
            'the immutable checkpoint ref is preserved for a later promotion.')
        return row
    promoted_sha = target_sha
    try:
        branch_ref = str(row.get('branchRef') or '')
        try:
            require_unchanged_gates(root, selected_policy)
        except GatePolicyError as error:
            row['verification'] = 'failed'
            row['verificationDetail'] = str(error)
            return row
        base_sha = str(row.get('baseSha') or '')
        current_branch = _git(root, ['symbolic-ref', '-q', 'HEAD'])
        if (
            current_branch.returncode != 0
            or current_branch.stdout.strip() != branch_ref
            or _revision(root, STABLE_REF) != stable_sha
            or stable_sha != row.get('stableSha')
            or _revision(root, 'refs/tofu/candidate')
        ):
            row['verification'] = 'workspace_changed'
            row['verificationDetail'] = (
                'The branch was switched, or the stable or candidate ref moved, after '
                'verification; the checkpoint remains recoverable and was not published.')
            return row
        head_sha = _revision(root, branch_ref)
        if head_sha != base_sha:
            promoted_sha, detail = _carry_verified_checkpoint(root, base_sha, head_sha, target_sha)
            if not promoted_sha:
                row['verification'] = 'workspace_changed'
                row['verificationDetail'] = detail[:3000]
                return row
            if promoted_sha == head_sha:
                row['verificationDetail'] = (
                    'The branch already contains these bytes; nothing was left to publish.')
                row['headAfter'] = head_sha
                return row
        with _prepared_index_publication(root, head_sha, promoted_sha) as index_prepared:
            # Recheck HEAD after preparing under index.lock as well as CAS refs.
            if (_revision(root, 'HEAD') != head_sha
                    or _git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip() != branch_ref):
                raise _PublicationDeferred('HEAD moved before publication')
            _publish_verified_refs(
                root, branch_ref=branch_ref, base_sha=head_sha,
                stable_sha=stable_sha, target_sha=promoted_sha)

            # Ref publication is irreversible here. If index installation
            # subsequently fails, report the published state, never rollback.
            row.update({'stableSha': promoted_sha, 'stableUpdated': True,
                        'headAfter': promoted_sha})
            if promoted_sha != target_sha:
                row['publishedSha'] = promoted_sha
        row['indexSynchronized'] = index_prepared
        if _revision(root, 'HEAD') != promoted_sha:
            row['indexSynchronized'] = False
            row['verification'] = 'workspace_changed'
            row['verificationDetail'] = (
                'HEAD moved immediately after atomic verified publication; '
                'the checkpoint and stable ref remain recoverable.')
            return row
    except _PublicationDeferred as error:
        row['verification'] = 'promotion_deferred'
        row['verificationDetail'] = (
            f'{error}; the verified checkpoint stays recoverable and its bytes remain '
            'in the workspace for the next checkpoint.')
        return row
    except (LinearCheckpointError, OSError) as error:
        row['indexSynchronized'] = False
        row['verification'] = 'failed'
        row['verificationDetail'] = str(error)[:3000]
        row['stableSha'] = _revision(root, STABLE_REF)
        return row
    finally:
        _release_checkpoint_lock(handle)
    row['stableSha'] = promoted_sha
    row['stableUpdated'] = stable_sha != promoted_sha
    return row


def publish_commit(root: str | Path, *, branch_ref: str, base_sha: str, target_sha: str,
                   reflog: str) -> dict[str, Any] | None:
    """Move BRANCH_REF from BASE_SHA to TARGET_SHA without writing a checkout file.

    The checkpoint's own publication: only the index entries of the paths the
    commit changes move, and entries other writers staged are preserved.
    Returns None while the index is busy, the branch moved or an operation is
    in progress, so the caller retries; raises LinearCheckpointError when Git
    refuses the reference update.
    """
    root = Path(root)
    try:
        with _prepared_index_publication(root, base_sha, target_sha) as synchronized:
            current = _git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip()
            if current != branch_ref or _revision(root, branch_ref) != base_sha:
                raise _PublicationDeferred('The branch moved')
            moved = _git(root, ['update-ref', '-m', reflog, branch_ref, target_sha, base_sha])
            if moved.returncode != 0:
                raise LinearCheckpointError(
                    f'{branch_ref} did not move from {base_sha}: '
                    + (moved.stderr or moved.stdout).strip()[:600])
    except _PublicationDeferred:
        return None
    return {'indexSynchronized': synchronized}


def settle_task_checkpoint(
    task: dict[str, Any], *, user_id: int, project_path: str | None,
    project_paths: list[str] | None = None, own_paths: list[str] | None = None,
) -> dict[str, Any] | None:
    """Best-effort checkpoint enabled repositories after task settlement.

    This API is intentionally absent from tool dispatch. Every failure is
    represented as receipt data and logs; callers must not rewrite task/tool
    outcomes from it. OWN_PATHS (repository-relative, in PROJECT_PATH's
    repository) add to the paths the integration radar recorded for the task's
    conversation when a failed whole-workspace capture is narrowed to them.
    """
    roots = _enabled_repository_roots(project_path, project_paths)
    if not roots:
        return None
    primary = _repository_root(project_path) if project_path else None
    try:
        validated_user_id, task_id = _checkpoint_identity(
            user_id, str(task.get('id') or ''))
    except LinearCheckpointError as error:
        logger.warning('[LinearCheckpoint] settlement identity invalid: %s', error)
        payload = {
            'schema': 'tofu.linear-git-checkpoint/v2', 'status': 'error',
            'repositories': [], 'detail': str(error)[:3000],
        }
        task['_linearGitCheckpoint'] = payload
        return payload

    results: list[dict[str, Any]] = []
    for root in roots:
        try:
            row = _capture_repository(task, validated_user_id, root)
            row = _verify_captured_checkpoint(task, row)
            if row.get('status') == 'committed' and row.get('verification') == 'failed':
                from lib.project_mod.integration_radar import recorded_paths
                own = recorded_paths(task, root) | set(
                    own_paths if root == primary and own_paths else ())
                row = _own_paths_retry(task, root, row, own) or row
        except Exception as error:
            logger.warning(
                '[LinearCheckpoint] settlement failed task=%s root=%s: %s',
                task_id, root, error, exc_info=True)
            row = {
                'projectRoot': str(root), 'status': 'error',
                'baseSha': _revision(root, 'HEAD'),
                'stableSha': _revision(root, STABLE_REF),
                'stableUpdated': False, 'verification': 'failed',
                'verificationDetail': str(error)[:3000],
            }
        results.append(row)

    statuses = {str(row.get('status') or '') for row in results}
    if statuses == {'no_changes'}:
        status = 'no_changes'
    elif statuses == {'deferred'}:
        status = 'deferred'
    elif statuses == {'error'}:
        status = 'error'
    elif 'error' in statuses or 'deferred' in statuses:
        status = 'partial'
    else:
        status = 'committed'
    payload = {
        'schema': 'tofu.linear-git-checkpoint/v2',
        'status': status, 'repositories': results,
    }
    task['_linearGitCheckpoint'] = payload
    return payload


__all__ = [
    'CHECKPOINT_REF_ROOT',
    'CHECKPOINT_BASELINE_REF',
    'LinearCheckpointError',
    'STABLE_REF',
    'is_enabled',
    'preferred_export_ref',
    'settle_task_checkpoint',
]
