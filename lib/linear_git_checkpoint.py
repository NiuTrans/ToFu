"""Best-effort Git checkpoints for one model-owned canonical checkout.

Project tools never call this module and are never admitted, delayed, or
rejected by Git state. After a task reaches a terminal state, the existing
commit-round daemon may ask this module to record the checkout's current bytes
as one immutable checkpoint ref. A short cross-process lock serializes only
Git index/ref updates; it does not serialize project-file writers. The checked-
out branch remains at its last published revision until verification passes.

Concurrent edits are intentionally coalesced workspace state, not task-owned
attribution.  Bytes written after a snapshot remain dirty for a later task.
Checkpoint failure is observable in the settlement receipt and logs, while the
task result remains unchanged.  No worktree, merge, stash, reset, checkout,
clean, push, or model-issued Git command is involved.

``refs/tofu/stable`` is a publication pointer, not a synonym for development
``HEAD``.  It advances only when the exact stable-to-checkpoint delta passes a
configured gate and the canonical checkout remains identical to that snapshot
before and after verification.
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
)
from lib.log import get_logger

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
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        process = subprocess.run(
            ['git', '-c', 'core.fsmonitor=false', *args],
            cwd=str(cwd), env=merged_env, text=True,
            input=input_text,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
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


def _configured_gate_command(root: Path) -> str:
    for name in (
        'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD',
        'TOFU_INTEGRATION_TEST_CMD',
    ):
        value = os.environ.get(name, '').strip()
        if value:
            return value
    process = _git(root, ['config', '--get',
                          'tofu.linearCheckpointTestCommand'])
    return process.stdout.strip() if process.returncode == 0 else ''


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
        if not _revision(root, CHECKPOINT_BASELINE_REF):
            return 'HEAD'
        if not _revision(root, STABLE_REF):
            raise LinearCheckpointError(
                'Linear checkpoint baseline exists but refs/tofu/stable is '
                'missing; refusing to export unverified HEAD')
        return STABLE_REF
    return 'HEAD'


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
    """Activate current committed HEAD before the first task-end snapshot."""
    marker = _revision(root, CHECKPOINT_BASELINE_REF)
    stable = _revision(root, STABLE_REF)
    if marker:
        if not stable:
            zero = '0' * len(marker)
            repair = _git(root, [
                'update-ref', '-m', 'tofu: repair checkpoint stable ref',
                STABLE_REF, marker, zero,
            ])
            if repair.returncode != 0:
                raise LinearCheckpointError(
                    'Checkpoint baseline exists but refs/tofu/stable could not '
                    'be repaired: '
                    + (repair.stderr or repair.stdout).strip()[:600])
            stable = marker
        return stable

    zero = '0' * len(head_sha)
    if stable != head_sha:
        process = _git(root, [
            'update-ref', '-m', 'tofu: activate workspace checkpoint baseline',
            STABLE_REF, head_sha, stable or zero,
        ])
        if process.returncode != 0:
            raise LinearCheckpointError(
                'Could not activate refs/tofu/stable at current HEAD: '
                + (process.stderr or process.stdout).strip()[:600])
    marker_update = _git(root, [
        'update-ref', '-m', 'tofu: record linear baseline activation',
        CHECKPOINT_BASELINE_REF, head_sha, zero,
    ])
    if marker_update.returncode != 0:
        raise LinearCheckpointError(
            'Could not record the linear baseline activation: '
            + (marker_update.stderr or marker_update.stdout).strip()[:600])
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
        _git(root, ['read-tree', base_sha], env=env, check=True)
        _git(root, ['add', '-A', '--', '.'], env=env, timeout=120.0,
             check=True)
        tree_sha = _git(root, ['write-tree'], env=env, check=True).stdout.strip()
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
                   env=env)
    if tracked.returncode != 0:
        return False
    untracked = _git(root, [
        'ls-files', '--others', '--exclude-standard', '-z',
    ], env=env, check=True)
    return not bool(untracked.stdout)


def _checkout_matches_snapshot(root: Path, target_sha: str) -> bool:
    """Return whether HEAD, index, tracked bytes, and untracked bytes match."""
    if _revision(root, 'HEAD') != target_sha:
        return False
    return _workspace_matches_snapshot(root, target_sha)


def _workspace_matches_snapshot(root: Path, target_sha: str) -> bool:
    """Return whether current index/worktree bytes equal an immutable tree.

    Unlike ``_checkout_matches_snapshot``, HEAD intentionally remains at the
    last published revision until verification succeeds.
    """
    index_path = ''
    try:
        index_path, workspace_tree, _ = _stage_working_tree(root, target_sha)
        target_tree = _git(
            root, ['rev-parse', f'{target_sha}^{{tree}}'], check=True,
        ).stdout.strip()
        return workspace_tree == target_tree
    except (OSError, LinearCheckpointError):
        return False
    finally:
        if index_path:
            with contextlib.suppress(OSError):
                os.unlink(index_path)


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
    if not _task_succeeded(task):
        return 'task_failed', 'Task did not settle successfully'
    diff_check = _git(root, [
        'diff', '--check', verification_base, probe_sha,
    ])
    if diff_check.returncode != 0:
        return 'failed', (diff_check.stdout or diff_check.stderr).strip()[:3000]
    syntax_ok, syntax_detail = _syntax_check(root, paths)
    if not syntax_ok:
        return 'failed', syntax_detail[:3000]
    ruff_ok, ruff_detail = _ruff_check(root, paths)
    if not ruff_ok:
        return 'failed', ruff_detail[:3000]
    command = _configured_gate_command(root)
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
    environment = os.environ.copy()
    environment.update({
        'TOFU_LINEAR_CHECKPOINT_BASE_SHA': verification_base,
        'TOFU_LINEAR_CHECKPOINT_TARGET_SHA': probe_sha,
    })
    try:
        process = subprocess.run(
            argv, cwd=str(root), env=environment, text=True,
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
    if process.returncode != 0:
        output = '\n'.join(
            part for part in (process.stdout, process.stderr) if part)
        return 'failed', output.strip()[-3000:]
    return 'passed', ''


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
    transaction = '\n'.join((
        'start',
        f'update {branch_ref} {target_sha} {base_sha}',
        f'update {STABLE_REF} {target_sha} {stable_sha}',
        'prepare',
        'commit',
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


def _capture_repository(task: dict[str, Any], user_id: int,
                        root: Path) -> dict[str, Any]:
    """Capture one workspace tree while holding only the Git operation lock."""
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
        for attempt in range(1, 4):
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

                changed_paths = _changed_paths(root, base_sha, tree_sha)
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
                        'task=%s root=%s: %s', task_id[:12], root,
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
        raise LinearCheckpointError('Checkpoint retry limit was exhausted')
    finally:
        _release_checkpoint_lock(handle)


def _verify_captured_checkpoint(task: dict[str, Any],
                                row: dict[str, Any]) -> dict[str, Any]:
    """Verify after releasing the Git lock; never affect task execution."""
    if row.get('status') != 'committed':
        return row
    root = Path(str(row['projectRoot']))
    target_sha = str(row['checkpointSha'])
    if not _task_succeeded(task):
        row['verification'] = 'task_failed'
        row['verificationDetail'] = 'Task did not settle successfully'
        return row
    if not row.get('captureConsistent') or not _workspace_matches_snapshot(
            root, target_sha):
        row['verification'] = 'workspace_changed'
        row['verificationDetail'] = (
            'The canonical workspace changed during or immediately after '
            'capture. The checkpoint is preserved, later bytes remain dirty, '
            'and stable was not moved.')
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

    verification_paths = _changed_paths(root, stable_sha, target_sha)
    verification, detail = _run_verification_gate(
        root, stable_sha, target_sha, verification_paths, task)
    if not _workspace_matches_snapshot(root, target_sha):
        row['verification'] = 'workspace_changed'
        row['verificationDetail'] = (
            'The canonical workspace changed during checkpoint verification. '
            'Those bytes remain available for the next task-end checkpoint; '
            'stable was not moved.')
        return row

    row['verification'] = verification
    row['verificationDetail'] = detail[:3000]
    if verification != 'passed':
        return row
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
    try:
        branch_ref = str(row.get('branchRef') or '')
        base_sha = str(row.get('baseSha') or '')
        current_branch = _git(root, ['symbolic-ref', '-q', 'HEAD'])
        if (
            current_branch.returncode != 0
            or current_branch.stdout.strip() != branch_ref
            or _revision(root, branch_ref) != base_sha
            or _revision(root, STABLE_REF) != stable_sha
            or not _workspace_matches_snapshot(root, target_sha)
        ):
            row['verification'] = 'workspace_changed'
            row['verificationDetail'] = (
                'Branch, stable ref, or workspace changed after verification; '
                'the checkpoint remains recoverable and was not published.')
            return row
        # Prepare the real index before publishing refs. If this fails, both
        # branch and stable still point at the last verified revision.
        index_sync = _git(root, ['read-tree', target_sha], timeout=60.0)
        row['indexSynchronized'] = index_sync.returncode == 0
        if not row['indexSynchronized']:
            raise LinearCheckpointError(
                'Verified checkpoint index could not be prepared before '
                'publication: '
                + (index_sync.stderr or index_sync.stdout).strip()[:600])
        try:
            _publish_verified_refs(
                root, branch_ref=branch_ref, base_sha=base_sha,
                stable_sha=stable_sha, target_sha=target_sha)
        except LinearCheckpointError:
            # Ref publication failed its CAS transaction. Restore the real
            # index to the still-current branch so no staged residue leaks.
            _git(root, ['read-tree', base_sha], timeout=60.0)
            row['indexSynchronized'] = False
            raise
        _git(root, ['update-index', '--refresh'], timeout=60.0)
        promoted_sha = target_sha
        if _revision(root, 'HEAD') != target_sha:
            row['indexSynchronized'] = False
            row['verification'] = 'workspace_changed'
            row['verificationDetail'] = (
                'HEAD moved immediately after atomic verified publication; '
                'the checkpoint and stable ref remain recoverable.')
            return row
    except LinearCheckpointError as error:
        row['verification'] = 'failed'
        row['verificationDetail'] = str(error)[:3000]
        row['stableSha'] = _revision(root, STABLE_REF)
        return row
    finally:
        _release_checkpoint_lock(handle)
    row['stableSha'] = promoted_sha
    row['stableUpdated'] = stable_sha != promoted_sha
    return row


def settle_task_checkpoint(
    task: dict[str, Any], *, user_id: int, project_path: str | None,
    project_paths: list[str] | None = None,
) -> dict[str, Any] | None:
    """Best-effort checkpoint enabled repositories after task settlement.

    This API is intentionally absent from tool dispatch. Every failure is
    represented as receipt data and logs; callers must not rewrite task/tool
    outcomes from it.
    """
    roots = _enabled_repository_roots(project_path, project_paths)
    if not roots:
        return None
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
        except Exception as error:
            logger.warning(
                '[LinearCheckpoint] settlement failed task=%s root=%s: %s',
                task_id[:12], root, error, exc_info=True)
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
