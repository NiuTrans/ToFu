"""Deterministic Git integration control plane.

This module deliberately contains no agent or LLM calls.  Writers keep their
own worktrees, checkpoint with an alternate index, and explicitly submit an
immutable commit.  A small background worker serialises ready checkpoints into
``refs/tofu/candidate``.  Conflicts and failed gates are quarantined for a
human (or a separately authorised repair task) instead of consuming tokens.

The canonical checkout is observation-only: integration never stages, resets,
checks out, or merges it.  ``refs/tofu/stable`` is promoted independently, so
hundreds of local edits can be visible without making the known-good ref
ambiguous.

Unlike the retired ``TOFU_WORKTREE_ISOLATION`` experiment, this is not a
transparent per-conversation path redirect. Only explicitly created or
registered writer worktrees enter the queue; their paths and state remain
visible, while the canonical checkout keeps its ordinary semantics.
"""

from __future__ import annotations

import contextlib
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

from lib.log import audit_log, get_logger
from lib.runtime_paths import data_root
from lib import integration_state_repository as _state
from lib.storage import StorageError
from lib.git_checkpoint_policy import (
    PROJECT_GATE_REQUIRED_SUFFIXES as _PROJECT_GATE_REQUIRED_SUFFIXES,
    forbidden_checkpoint_paths as _forbidden_checkpoint_paths,
)

logger = get_logger(__name__)

_APP_ROOT = Path(__file__).resolve().parent.parent
_REF_ROOT = 'refs/tofu'
_CANDIDATE_REF = f'{_REF_ROOT}/candidate'
_STABLE_REF = f'{_REF_ROOT}/stable'
_WORKER_LOCK = threading.Lock()
_WORKER_CONDITION = threading.Condition(_WORKER_LOCK)
_WORKER: threading.Thread | None = None
_WORKER_WAKE_GENERATION = 0
_WORKER_STOP_REQUESTED = False
_WORKER_AUTHORITY_ARMED = False
_STATUS_CACHE: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_TTL_SECONDS = 10.0

IntegrationError = RuntimeError


def _control_root() -> Path:
    root = Path(data_root()) / 'integration'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace_root() -> Path:
    override = os.environ.get('TOFU_INTEGRATION_WORKSPACE_DIR', '').strip()
    root = (Path(override).expanduser().resolve()
            if override else _control_root() / 'worktrees')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _git(cwd: str | Path, args: list[str], *, env: dict[str, str] | None = None,
         timeout: float = 30.0, check: bool = False) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        cp = subprocess.run(
            ['git', '-c', 'core.fsmonitor=false', *args],
            cwd=str(cwd), env=merged_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError(f'git {args[0]} failed: {exc}') from exc
    if check and cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or '').strip()
        raise IntegrationError(f'git {args[0]} failed: {detail[:800]}')
    return cp


def _repo_root(path: str | Path) -> Path:
    raw = str(path or '').strip()
    if not raw:
        raise IntegrationError('No project path is active')
    candidate = Path(raw).expanduser().resolve()
    if not candidate.exists():
        raise IntegrationError(f'Project path does not exist: {candidate}')
    cp = _git(candidate, ['rev-parse', '--show-toplevel'], check=True)
    return Path(cp.stdout.strip()).resolve()


def _common_dir(path: str | Path) -> Path:
    cp = _git(path, ['rev-parse', '--git-common-dir'], check=True)
    value = Path(cp.stdout.strip())
    return (Path(path) / value).resolve() if not value.is_absolute() else value.resolve()


def _rev(path: str | Path, ref: str) -> str:
    cp = _git(path, ['rev-parse', '--verify', ref])
    return cp.stdout.strip() if cp.returncode == 0 else ''


def _short(sha: str) -> str:
    return sha[:12] if sha else ''


def _safe_task(task_id: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', task_id).strip('.-')[:48] or 'task'
    digest = hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:10]
    return f'{slug}-{digest}'


def _require_user_id(user_id: int) -> int:
    if (isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1):
        raise IntegrationError('A positive user_id is required')
    return user_id


def _checkpoint_ref(user_id: int, task_id: str) -> str:
    return f'{_REF_ROOT}/checkpoints/u{user_id}/{_safe_task(task_id)}'


def _quarantine_ref(user_id: int, task_id: str) -> str:
    return f'{_REF_ROOT}/quarantine/u{user_id}/{_safe_task(task_id)}'


def _env_float(name: str, default: float) -> float:
    """Guarded env float: garbage values fall back to the default instead of
    raising ValueError into an unrelated operation (audit 2026-08-20)."""
    try:
        return float(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        logger.warning('[Integration] bad %s value, using default %s', name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        logger.warning('[Integration] bad %s value, using default %s', name, default)
        return default


def _now() -> float:
    return time.time()


def _iso(ts: float | int | None) -> str:
    if not ts:
        return ''
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(ts)))


def _record_event(project_root: str, task_id: str, kind: str,
                  message: str, detail: Any = '', *, user_id: int) -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    _state.record_event(
        user_id=_require_user_id(user_id), project_root=project_root,
        task_id=task_id, kind=kind,
        message=message, detail=detail, now=_now())


def _invalidate(project_root: str, *, user_id: int) -> None:
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.pop((_require_user_id(user_id), project_root), None)


def _push(project_root: str, *, user_id: int) -> None:
    owner_user_id = _require_user_id(user_id)
    _invalidate(project_root, user_id=owner_user_id)
    try:
        from lib.agent_core.push import push_event
        from lib.conversations.project_identity import project_channel_key
        push_event('project', project_channel_key(project_root), {
            'type': 'integration', 'projectPath': project_root,
        }, user_id=owner_user_id)
    except Exception as exc:
        logger.debug('[Integration] push skipped: %s', exc)


@contextlib.contextmanager
def _repo_lock(project_root: str) -> Iterator[None]:
    """Cross-process lock for ref updates and temporary worktree operations."""
    digest = hashlib.sha256(project_root.encode('utf-8')).hexdigest()[:20]
    lock_path = _control_root() / 'locks' / f'{digest}.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+b')
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows path
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
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


def _ensure_refs(root: Path) -> tuple[str, str]:
    head = _rev(root, 'HEAD')
    if not head:
        raise IntegrationError('The project repository has no commits yet')
    candidate = _rev(root, _CANDIDATE_REF)
    stable = _rev(root, _STABLE_REF)
    if not candidate:
        _git(root, ['update-ref', _CANDIDATE_REF, head, ''], check=True)
        candidate = head
    if not stable:
        _git(root, ['update-ref', _STABLE_REF, candidate, ''], check=True)
        stable = candidate
    return candidate, stable


def _clean_origin(origin: Any) -> dict[str, Any]:
    """Validate bounded caller-supplied work/conversation provenance.

    Origin is how a workspace row answers "who started this and why" — the
    automatic work ID, the conversation that owns it, and the creation
    channel. Keys and values are coerced to short strings so the row stays
    small.
    """
    if not origin:
        return {}
    if not isinstance(origin, dict):
        raise IntegrationError('origin must be an object')
    cleaned: dict[str, Any] = {}
    for key, value in origin.items():
        k = str(key or '').strip()[:64]
        if not k:
            continue
        if isinstance(value, (list, tuple)):
            cleaned[k] = [str(v)[:300] for v in value[:64]]
        elif value is None:
            continue
        else:
            cleaned[k] = str(value)[:300]
    return cleaned


def register_workspace(project_path: str, task_id: str, workspace_path: str,
                       title: str = '', *, managed: bool = False,
                       user_id: int,
                       origin: dict[str, Any] | None = None) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    task_id = str(task_id or '').strip()
    if not task_id:
        raise IntegrationError('workId is required')
    root = _repo_root(project_path)
    workspace = _repo_root(workspace_path)
    if workspace == root:
        raise IntegrationError('The canonical checkout cannot be a writer workspace')
    if _common_dir(root) != _common_dir(workspace):
        raise IntegrationError('Workspace belongs to a different Git repository')
    base = _rev(workspace, 'HEAD')
    if not base:
        raise IntegrationError('Workspace has no HEAD commit')
    now = _now()
    cleaned_origin = _clean_origin(origin)
    with _repo_lock(str(root)):
        _state.register_workspace(
            user_id=owner_user_id, project_root=str(root), task_id=task_id,
            title=str(title or '').strip(), workspace_path=str(workspace),
            managed=managed, base_sha=base, now=now,
            origin=cleaned_origin or None)
    _push(str(root), user_id=owner_user_id)
    return {'ok': True, 'workId': task_id, 'workspacePath': str(workspace),
            'baseSha': base, 'state': 'running', 'managed': bool(managed),
            'origin': cleaned_origin}


def create_workspace(project_path: str, task_id: str,
                     title: str = '', *,
                     user_id: int,
                     origin: dict[str, Any] | None = None) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    task_id = str(task_id or '').strip()
    if not task_id:
        raise IntegrationError('workId is required')
    repo_key = hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:12]
    destination = (
        _workspace_root() / repo_key / f'u{owner_user_id}' / _safe_task(task_id))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise IntegrationError(f'Managed workspace already exists: {destination}')
    with _repo_lock(str(root)):
        candidate, _stable = _ensure_refs(root)
        cp = _git(root, ['worktree', 'add', '--detach', str(destination), candidate],
                  timeout=90.0)
        if cp.returncode != 0:
            raise IntegrationError((cp.stderr or cp.stdout).strip()[:1000])
    try:
        return register_workspace(str(root), task_id, str(destination), title,
                                  managed=True, user_id=owner_user_id,
                                  origin=origin)
    except Exception:
        # The worktree was created solely for this failed operation and has not
        # been handed to a writer, so cleanup is safe and keeps the registry sane.
        with _repo_lock(str(root)):
            _remove_controlled_worktree(root, destination)
        raise


def has_active_workspace_for_work(
    project_path: str,
    work_id: str,
    *,
    user_id: int,
) -> bool:
    """Return whether this owner/work ID has an editable isolated workspace."""
    if not project_path or not work_id:
        return False
    root = _repo_root(project_path)
    row = _state.find_workspace(
        str(root), str(work_id).strip(), user_id=_require_user_id(user_id))
    return bool(row and row.get('state') in {'running', 'checkpointed'})


def _alternate_index_checkpoint(workspace: Path, parent: str,
                                task_id: str) -> str:
    fd, index_path = tempfile.mkstemp(prefix='tofu-index-')
    os.close(fd)
    os.unlink(index_path)  # Git requires a missing or valid index, not empty.
    env = {'GIT_INDEX_FILE': index_path}
    try:
        _git(workspace, ['read-tree', parent], env=env, check=True)
        _git(workspace, ['add', '-A', '--', '.'], env=env, timeout=90.0,
             check=True)
        tree = _git(workspace, ['write-tree'], env=env, check=True).stdout.strip()
        parent_tree = _git(workspace, ['rev-parse', f'{parent}^{{tree}}'],
                           check=True).stdout.strip()
        if tree == parent_tree:
            return parent
        commit_env = {
            **env,
            'GIT_AUTHOR_NAME': 'Tofu Integration',
            'GIT_AUTHOR_EMAIL': 'integration@tofu.local',
            'GIT_COMMITTER_NAME': 'Tofu Integration',
            'GIT_COMMITTER_EMAIL': 'integration@tofu.local',
        }
        cp = _git(workspace, [
            'commit-tree', tree, '-p', parent, '-m',
            f'Tofu checkpoint: {task_id}',
        ], env=commit_env, check=True)
        return cp.stdout.strip()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(index_path)


def checkpoint_workspace(
    project_path: str, task_id: str, *, user_id: int,
) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    root_s = str(root)
    task_id = str(task_id or '').strip()
    with _repo_lock(root_s):
        row = _state.get_workspace(
            root_s, task_id, user_id=owner_user_id)
        if row['state'] in {'ready', 'integrating'}:
            raise IntegrationError(
                'The submitted checkpoint is immutable while it is in the integration queue')
        if row['state'] in {'discarded', 'merged'}:
            raise IntegrationError(
                f"The workspace is {row['state']}; register a new isolated "
                'work item instead of resurrecting a terminal integration record')
        workspace = Path(row['workspace_path'])
        if not workspace.exists():
            raise IntegrationError(f'Workspace is missing: {workspace}')
        workspace_head = _rev(workspace, 'HEAD')
        recorded_base = str(row['base_sha'] or '')
        recorded_checkpoint = str(row['checkpoint_sha'] or '')
        # A repair task may explicitly rebase/reset the writer checkout onto
        # the latest candidate before editing.  The old implementation always
        # preferred checkpoint_sha, so the new checkpoint stayed on the stale
        # parent chain and deterministically reproduced the same conflict.
        # Treat a moved writer HEAD as an explicit re-anchor while retaining
        # chained checkpoints when HEAD has not moved.
        reanchored = bool(
            workspace_head
            and workspace_head not in {recorded_base, recorded_checkpoint})
        if reanchored and row['state'] in {'quarantined', 'failed'}:
            candidate, _stable = _ensure_refs(root)
            if _git(
                    root,
                    ['merge-base', '--is-ancestor', candidate, workspace_head],
            ).returncode != 0:
                raise IntegrationError(
                    'Repair workspace HEAD does not include the latest candidate; '
                    'rebase or reset the isolated worktree onto candidate before '
                    'checkpointing the repair')
        parent = (
            workspace_head if reanchored
            else recorded_checkpoint or recorded_base or workspace_head)
        checkpoint = _alternate_index_checkpoint(workspace, parent, task_id)
        _git(
            root,
            ['update-ref', _checkpoint_ref(owner_user_id, task_id), checkpoint],
            check=True,
        )
        _state.save_checkpoint(
            user_id=owner_user_id, project_root=root_s, task_id=task_id,
            checkpoint_sha=checkpoint,
            base_sha=parent if reanchored else '', now=_now())
        if reanchored:
            _set_meta(
                root_s, task_id,
                {'conflict_files': [], 'repair_base_sha': parent},
                user_id=owner_user_id,
            )
    _push(root_s, user_id=owner_user_id)
    return {'ok': True, 'workId': task_id, 'checkpointSha': checkpoint,
            'checkpointRef': _checkpoint_ref(owner_user_id, task_id),
            'state': 'checkpointed', 'reanchored': reanchored}


def submit_workspace(
    project_path: str, task_id: str, *, user_id: int,
) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    result = checkpoint_workspace(
        project_path, task_id, user_id=owner_user_id)
    root = _repo_root(project_path)
    now = _now()
    _state.submit_checkpoint(
        user_id=owner_user_id, project_root=str(root), task_id=task_id, now=now)
    _start_or_wake_worker()
    _push(str(root), user_id=owner_user_id)
    result['state'] = 'ready'
    return result


def retry_workspace(
    project_path: str, task_id: str, *, user_id: int,
) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    now = _now()
    _state.retry_checkpoint(
        user_id=owner_user_id, project_root=str(root), task_id=task_id, now=now)
    _start_or_wake_worker()
    _push(str(root), user_id=owner_user_id)
    return {'ok': True, 'workId': task_id, 'state': 'ready'}

def discard_workspace(
    project_path: str, task_id: str, *, user_id: int,
) -> dict[str, Any]:
    """Human discard — the terminal park for a row the queue must skip.

    The Git refs (checkpoint / quarantine) and the worktree directory are
    deliberately kept: discard is a queue decision, not data destruction.
    """
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    now = _now()
    _state.discard_workspace(
        user_id=owner_user_id, project_root=str(root), task_id=task_id, now=now)
    _push(str(root), user_id=owner_user_id)
    return {'ok': True, 'workId': task_id, 'state': 'discarded'}


def _set_meta(
    project_root: str, task_id: str, patch: dict[str, Any], *, user_id: int,
) -> None:
    """Best-effort origin-metadata merge — never raises into a caller."""
    try:
        _state.set_workspace_meta(
            user_id=_require_user_id(user_id), project_root=project_root,
            task_id=task_id,
            patch=patch, now=_now())
    except Exception as exc:
        # A transient storage error here silently drops conflict_files /
        # submitSummary / checkpoint notes — the reviewer and the dispatcher
        # then act on an empty origin document.  Loud, not debug.
        logger.warning('[Integration] set_meta failed for %s: %s', task_id, exc)


_CONFLICT_IN_FILE_RE = re.compile(r'merge conflict in (.+)', re.IGNORECASE)


def _conflict_files_from(text: str) -> list[str]:
    """Extract conflicting file paths from merge output (git merge stderr or
    merge-tree detail). Best-effort; an empty list just means 'unparsed'."""
    files: list[str] = []
    for line in (text or '').splitlines():
        match = _CONFLICT_IN_FILE_RE.search(line)
        if match:
            path = match.group(1).strip().strip('"').rstrip('.')
            if path and path not in files:
                files.append(path)
    return files[:64]


def _gate_argv(configured_command: str, old: str, target: str) -> list[str]:
    """Parse one direct command and expand safe SHA placeholders.

    No shell is involved. ``{base}`` and ``{target}`` let a repository-owned
    test selector inspect the exact candidate delta without command
    substitution or environment-dependent quoting.
    """
    return [
        token.replace('{base}', old).replace('{target}', target)
        for token in shlex.split(configured_command)
    ]


def _gate_commands(root: Path, old: str, target: str,
                   configured_command: str = '') -> tuple[bool, str]:
    diff_check = _git(root, ['diff', '--check', old, target])
    if diff_check.returncode != 0:
        return False, (diff_check.stdout or diff_check.stderr).strip()[:3000]

    status_cp = _git(
        root,
        ['diff', '--name-status', '--find-renames',
         '--diff-filter=ACMRD', '-z', old, target],
        check=True)
    tokens = [token for token in status_cp.stdout.split('\0') if token]
    changed_entries: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        path_count = 2 if status[:1] in {'R', 'C'} else 1
        paths = tokens[index:index + path_count]
        if len(paths) != path_count:
            return False, 'Git returned a malformed name-status gate payload'
        index += path_count
        changed_entries.append((status[:1], paths))
    all_changed = [
        path for _status, paths in changed_entries for path in paths
    ]
    # Existing forbidden artifacts must be removable. Reject every added or
    # modified destination, but allow a pure deletion (and a rename out of a
    # forbidden tree) so historical debt cannot become permanent.
    forbidden_candidates = [
        paths[-1] for status, paths in changed_entries if status != 'D'
    ]
    forbidden = _forbidden_checkpoint_paths(forbidden_candidates)
    if forbidden:
        return False, (
            'Checkpoint contains forbidden dependency/generated/runtime paths: '
            + ', '.join(forbidden))
    project_gate_files = [
        name for name in all_changed
        if Path(name).suffix.lower() in _PROJECT_GATE_REQUIRED_SUFFIXES
    ]
    if project_gate_files and not configured_command:
        return False, (
            'Project integration tests are required for semantic code/config '
            'changes; configure TOFU_INTEGRATION_TEST_CMD. Changed: '
            + ', '.join(project_gate_files[:64]))

    names_cp = _git(root, ['diff', '--name-only', '--diff-filter=ACMR', '-z', old, target],
                    check=True)
    changed = [name for name in names_cp.stdout.split('\0') if name]
    python_files = [name for name in changed if name.endswith('.py')]
    js_files = [name for name in changed if name.endswith(('.js', '.mjs', '.cjs'))]
    json_files = [name for name in changed if name.endswith('.json')]
    if not python_files and not js_files and not json_files and not configured_command:
        return True, ''

    gate_parent = _workspace_root() / '.gates'
    gate_parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(tempfile.mkdtemp(prefix='gate-', dir=str(gate_parent)))
    # `worktree add` requires the destination not to exist.
    temp_path.rmdir()
    try:
        cp = _git(root, ['worktree', 'add', '--detach', str(temp_path), target],
                  timeout=90.0)
        if cp.returncode != 0:
            return False, (cp.stderr or cp.stdout).strip()[:3000]
        if python_files:
            cp = subprocess.run(
            [sys.executable, '-m', 'py_compile', '--', *python_files],
                cwd=str(temp_path), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120.0, check=False,
            )
            if cp.returncode != 0:
                return False, (cp.stderr or cp.stdout).strip()[:3000]
        if js_files and shutil.which('node'):
            for name in js_files:
                input_type = (
                    'commonjs' if name.lower().endswith('.cjs') else 'module'
                )
                cp = subprocess.run(
                    ['node', f'--input-type={input_type}', '--check'],
                    cwd=str(temp_path), text=True,
                    input=(temp_path / name).read_text(encoding='utf-8'),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=30.0, check=False,
                )
                if cp.returncode != 0:
                    detail = (cp.stderr or cp.stdout).strip()[:2800]
                    return False, f'{name}: {detail}'
        for name in json_files:
            cp = subprocess.run(
                [sys.executable, '-m', 'json.tool', name],
                cwd=str(temp_path), text=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=30.0, check=False,
            )
            if cp.returncode != 0:
                return False, (cp.stderr or '').strip()[:3000]
        if configured_command:
            command = _gate_argv(configured_command, old, target)
            if not command:
                return False, 'Configured gate command is empty'
            gate_env = os.environ.copy()
            gate_env.update({
                'TOFU_INTEGRATION_GATE_BASE_SHA': old,
                'TOFU_INTEGRATION_GATE_TARGET_SHA': target,
            })
            cp = subprocess.run(
                command, cwd=str(temp_path), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_env_float('TOFU_INTEGRATION_TEST_TIMEOUT', 600.0),
                env=gate_env, check=False,
            )
            if cp.returncode != 0:
                output = '\n'.join(part for part in [cp.stdout, cp.stderr] if part)
                return False, output.strip()[-3000:]
        return True, ''
    except (OSError, UnicodeError, ValueError,
            subprocess.TimeoutExpired) as exc:
        logger.debug('[Integration] scratch gate execution failed: %s', exc)
        return False, str(exc)
    finally:
        _remove_controlled_worktree(root, temp_path)


def _remove_controlled_worktree(root: Path, path: Path) -> None:
    """Remove only a scratch/managed path owned by this control plane.

    Git 2.11 has no ``worktree remove``.  The fallback used here was previously
    validated on this project's DolphinFS mount: delete the controlled checkout
    and then prune its missing administrative entry.  Arbitrary registered
    external worktrees never flow through this helper.
    """
    try:
        path.resolve().relative_to(_workspace_root().resolve())
    except (OSError, ValueError):
        logger.error('[Integration] refused to remove uncontrolled path %s', path)
        return
    cp = _git(root, ['worktree', 'remove', '--force', str(path)], timeout=60.0)
    if cp.returncode != 0 and path.exists():
        for attempt in range(5):
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError as exc:
                logger.debug('[Integration] scratch worktree already removed: %s', exc)
                break
            except OSError:
                if attempt == 4:
                    logger.warning('[Integration] could not remove scratch worktree %s', path)
                    break
                time.sleep(0.1 * (attempt + 1))
    _git(root, ['worktree', 'prune'], timeout=60.0)


def _scratch_merge(root: Path, candidate: str, checkpoint: str,
                   task_id: str) -> tuple[str, str]:
    """Git-2.11-compatible real 3-way merge in a throwaway worktree."""
    merge_parent = _workspace_root() / '.merges'
    merge_parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(tempfile.mkdtemp(prefix='merge-', dir=str(merge_parent)))
    temp_path.rmdir()
    env = {
        'GIT_AUTHOR_NAME': 'Tofu Integration',
        'GIT_AUTHOR_EMAIL': 'integration@tofu.local',
        'GIT_COMMITTER_NAME': 'Tofu Integration',
        'GIT_COMMITTER_EMAIL': 'integration@tofu.local',
    }
    try:
        cp = _git(root, ['worktree', 'add', '--detach', str(temp_path), candidate],
                  timeout=90.0)
        if cp.returncode != 0:
            return '', (cp.stderr or cp.stdout).strip()[:3000]
        cp = _git(temp_path, [
            'merge', '--no-ff', '--no-edit', '--no-verify',
            '-m', f'Tofu integration: {task_id}', checkpoint,
        ], env=env, timeout=120.0)
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or 'Git reported a content conflict').strip()
            _git(temp_path, ['merge', '--abort'])
            return '', detail[:3000]
        return _rev(temp_path, 'HEAD'), ''
    finally:
        _remove_controlled_worktree(root, temp_path)


def _merge_checkpoint(root: Path, candidate: str, checkpoint: str,
                      task_id: str) -> tuple[str, str]:
    if candidate == checkpoint or _git(
            root, ['merge-base', '--is-ancestor', checkpoint, candidate]).returncode == 0:
        return candidate, ''
    if _git(root, ['merge-base', '--is-ancestor', candidate, checkpoint]).returncode == 0:
        return checkpoint, ''
    cp = _git(root, ['merge-tree', '--write-tree', candidate, checkpoint], timeout=90.0)
    lines = (cp.stdout or '').splitlines()
    if cp.returncode in {129} or 'unknown option' in (cp.stderr or '').lower():
        return _scratch_merge(root, candidate, checkpoint, task_id)
    if cp.returncode != 0:
        detail = '\n'.join(lines[1:] if len(lines) > 1 else lines)
        detail = detail or cp.stderr or 'Git reported a content conflict'
        return '', detail.strip()[:3000]
    tree = lines[0].strip() if lines else ''
    if not re.fullmatch(r'[0-9a-fA-F]{40,64}', tree):
        return '', (cp.stderr or cp.stdout or 'merge-tree returned no tree').strip()[:3000]
    env = {
        'GIT_AUTHOR_NAME': 'Tofu Integration',
        'GIT_AUTHOR_EMAIL': 'integration@tofu.local',
        'GIT_COMMITTER_NAME': 'Tofu Integration',
        'GIT_COMMITTER_EMAIL': 'integration@tofu.local',
    }
    merged = _git(root, [
        'commit-tree', tree, '-p', candidate, '-p', checkpoint,
        '-m', f'Tofu integration: {task_id}',
    ], env=env, check=True).stdout.strip()
    return merged, ''


def _quarantine(root: Path, row: Mapping[str, Any], reason: str) -> None:
    owner_user_id = int(row['user_id'])
    checkpoint = row['checkpoint_sha']
    if checkpoint:
        _git(
            root,
            ['update-ref', _quarantine_ref(
                owner_user_id, str(row['task_id'])), checkpoint],
            check=True,
        )
    _state.quarantine(
        row_id=int(row['id']), reason=reason, now=_now())
    try:
        from lib.conversations.project_brain import record_integration_failure
        record_integration_failure(
            str(root), work_id=str(row['task_id']), reason=reason,
            user_id=owner_user_id)
    except Exception as exc:
        logger.debug('[Integration] Project narrative emit failed: %s', exc)
    # Keep bounded conflict paths for human review of the quarantined package.
    conflict_files = _conflict_files_from(reason)
    if conflict_files:
        _set_meta(
            str(root), str(row['task_id']),
            {'conflict_files': conflict_files}, user_id=owner_user_id)


def _integrate_row(row: Mapping[str, Any]) -> None:
    root = Path(row['project_root'])
    task_id = row['task_id']
    owner_user_id = int(row['user_id'])
    merged_origin: Mapping[str, Any] | None = None
    with _repo_lock(str(root)):
        fresh = _state.get_integrating(int(row['id']))
        if fresh is None:
            return
        checkpoint = fresh['checkpoint_sha']
        if not checkpoint or not _rev(root, checkpoint):
            _quarantine(root, fresh, 'Checkpoint commit is missing')
            _push(str(root), user_id=owner_user_id)
            return
        candidate, _stable = _ensure_refs(root)
        target, conflict = _merge_checkpoint(root, candidate, checkpoint, task_id)
        if conflict:
            _quarantine(root, fresh, conflict)
            _push(str(root), user_id=owner_user_id)
            return
        command = os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip()
        # The system-scoped claim/get_integrating rows deliberately stay
        # narrow and do not join owner metadata. Fetch the owner-scoped row
        # before recording the merged package provenance.
        origin = fresh.get('origin') or {}
        if not origin:
            owner_row = _state.get_workspace(
                str(root), str(task_id), user_id=owner_user_id)
            origin = owner_row.get('origin') or {}
        passed, detail = _gate_commands(root, candidate, target, command)
        if not passed:
            _quarantine(root, fresh, f'Gate failed:\n{detail}')
            _push(str(root), user_id=owner_user_id)
            return
        cp = _git(root, ['update-ref', _CANDIDATE_REF, target, candidate])
        if cp.returncode != 0:
            # A different process advanced the ref despite our filesystem lock
            # (e.g. a remote administrator). Requeue instead of losing work.
            _state.requeue(
                row_id=int(fresh['id']),
                error='Candidate moved concurrently; retrying', now=_now())
        else:
            if _state.mark_merged(
                    row_id=int(fresh['id']), candidate_sha=target, now=_now()):
                merged_origin = origin
    _push(str(root), user_id=owner_user_id)
    if merged_origin is not None:
        _record_project_integration_after_merge(
            str(root), str(task_id), owner_user_id)


def _record_project_integration_after_merge(
    project_root: str,
    task_id: str,
    user_id: int,
) -> None:
    """Record the important integration outcome without mutating work state."""
    try:
        from lib.conversations.project_brain import record_integration_success
        record_integration_success(
            project_root, work_id=task_id, user_id=int(user_id))
    except Exception as exc:
        logger.debug(
            '[Integration] success narrative failed work=%s: %s', task_id, exc)


def _claim_next() -> dict | None:
    # One repository transaction recovers abandoned claims, selects an
    # eligible project, and performs the ready→integrating CAS.
    return _state.claim_next(now=_now())


def _peek_ready() -> dict | None:
    """Read-only claimability probe — rides the storage read pool, never the
    single writer lane."""
    return _state.peek_ready(now=_now())


# Idle polls are entirely read-only. The peek reports both claimable ready
# rows and integrating rows past the 660-second recovery horizon, so the full
# claim transaction (recovery sweep + CAS + fsync) runs only when useful.
# The previous periodic write heartbeat could starve user/event writers during
# IO stalls even when no integration work existed.




def process_ready_once() -> bool:
    row = _claim_next()
    if row is None:
        return False
    try:
        _integrate_row(row)
    except Exception as exc:
        logger.exception('[Integration] task %s failed', row['task_id'])
        _state.mark_failed(
            row_id=int(row['id']),
            error=str(exc), now=_now())
        try:
            from lib.conversations.project_brain import record_integration_failure
            record_integration_failure(
                str(row['project_root']), work_id=str(row['task_id']),
                reason=str(exc), user_id=int(row['user_id']))
        except Exception as narrative_exc:
            logger.debug(
                '[Integration] Project narrative emit failed: %s',
                narrative_exc)
        _push(row['project_root'], user_id=int(row['user_id']))
    return True


def _autorun_enabled() -> bool:
    return os.environ.get('TOFU_INTEGRATION_AUTORUN', '1').strip().lower() not in {
        '0', 'false', 'no', 'off',
    }


def _idle_poll_bounds() -> tuple[float, float]:
    base = max(0.5, min(30.0, _env_float(
        'TOFU_INTEGRATION_IDLE_POLL_BASE_SECONDS', 3.0)))
    maximum = max(base, min(600.0, _env_float(
        'TOFU_INTEGRATION_IDLE_POLL_MAX_SECONDS', 60.0)))
    return base, maximum


def _next_idle_poll_delay(current: float) -> float:
    base, maximum = _idle_poll_bounds()
    try:
        delay = float(current)
    except (TypeError, ValueError, OverflowError):
        delay = base
    return min(maximum, max(base, delay * 2.0))


def _worker_generation() -> int:
    with _WORKER_CONDITION:
        return _WORKER_WAKE_GENERATION


def _wait_for_worker(
    delay: float,
    observed_generation: int,
    *,
    wake_on_generation: bool,
) -> tuple[bool, bool]:
    """Wait to one deadline without losing a concurrent durable-work signal."""
    deadline = time.monotonic() + max(0.0, float(delay))
    with _WORKER_CONDITION:
        while True:
            if _WORKER_STOP_REQUESTED:
                return False, False
            if (wake_on_generation
                    and _WORKER_WAKE_GENERATION != observed_generation):
                return True, True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True, False
            _WORKER_CONDITION.wait(remaining)


def _worker_loop() -> None:
    global _WORKER
    current_thread = threading.current_thread()
    backoff = 5.0
    idle_delay = _idle_poll_bounds()[0]
    # Boot with a full claim so a crash-interrupted claim from the previous
    # process is considered immediately rather than one cadence later.
    boot_claim_pending = True
    try:
        while _autorun_enabled():
            observed_generation = _worker_generation()
            try:
                claim_due = boot_claim_pending or _peek_ready() is not None
                boot_claim_pending = False
                progressed = process_ready_once() if claim_due else False
            except StorageError as exc:
                logger.warning(
                    '[Integration] storage error in worker loop '
                    '(retryable=%s code=%s): %s',
                    exc.retryable, exc.code, exc)
                keep_running, _woken = _wait_for_worker(
                    backoff, observed_generation, wake_on_generation=False)
                if not keep_running:
                    break
                backoff = min(30.0, backoff * 2.0)
                continue
            except Exception as exc:
                logger.warning('[Integration] uncaught error in worker loop: %s',
                               exc, exc_info=True)
                keep_running, _woken = _wait_for_worker(
                    backoff, observed_generation, wake_on_generation=False)
                if not keep_running:
                    break
                backoff = min(30.0, backoff * 2.0)
                continue
            backoff = 5.0
            if progressed:
                idle_delay = _idle_poll_bounds()[0]
                continue
            keep_running, work_woke = _wait_for_worker(
                idle_delay, observed_generation, wake_on_generation=True)
            if not keep_running:
                break
            idle_delay = (
                _idle_poll_bounds()[0]
                if work_woke else _next_idle_poll_delay(idle_delay))
    finally:
        with _WORKER_CONDITION:
            if _WORKER is current_thread:
                _WORKER = None
            _WORKER_CONDITION.notify_all()


def _start_or_wake_worker() -> bool:
    global _WORKER, _WORKER_STOP_REQUESTED, _WORKER_WAKE_GENERATION
    if not _autorun_enabled():
        return False
    with _WORKER_CONDITION:
        # Durable submit/retry may run in an API replica. Only the process whose
        # lifecycle owns CAPABILITY_TASK_WORKERS is allowed to execute Git gates.
        if not _WORKER_AUTHORITY_ARMED:
            return False
        worker = _WORKER
        if worker is not None and worker.is_alive():
            if _WORKER_STOP_REQUESTED:
                return False
            _WORKER_WAKE_GENERATION += 1
            _WORKER_CONDITION.notify_all()
            return True
        _WORKER_STOP_REQUESTED = False
        _WORKER_WAKE_GENERATION += 1
        worker = threading.Thread(
            target=_worker_loop, name='tofu-integration', daemon=True)
        _WORKER = worker
        try:
            worker.start()
        except Exception:
            if _WORKER is worker:
                _WORKER = None
            raise
        _WORKER_CONDITION.notify_all()
        return True


def ensure_worker_started() -> bool:
    global _WORKER_AUTHORITY_ARMED
    # Bootstrap/upgrade the separate control authority once at server start.
    # Read-only status calls can then remain genuinely side-effect free while
    # still seeing rows written by the pre-repository schema.
    _state.initialize_store()
    with _WORKER_CONDITION:
        _WORKER_AUTHORITY_ARMED = True
    return _start_or_wake_worker()


def stop_worker(timeout: float = 2.0) -> bool:
    """Stop the exact integration owner without permitting a duplicate."""
    global _WORKER, _WORKER_STOP_REQUESTED, _WORKER_WAKE_GENERATION
    global _WORKER_AUTHORITY_ARMED
    with _WORKER_CONDITION:
        _WORKER_AUTHORITY_ARMED = False
        worker = _WORKER
        if worker is None:
            return True
        _WORKER_STOP_REQUESTED = True
        _WORKER_WAKE_GENERATION += 1
        _WORKER_CONDITION.notify_all()
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError):
        wait_seconds = 2.0
    if worker is not threading.current_thread():
        worker.join(wait_seconds)
    stopped = not worker.is_alive()
    if stopped:
        with _WORKER_CONDITION:
            if _WORKER is worker:
                _WORKER = None
    return stopped


def reconcile_candidate_with_head(
    project_path: str, *, user_id: int,
) -> dict[str, Any]:
    """Merge committed canonical HEAD history into candidate under gates.

    This is the explicit topology-repair verb for a canonical branch that
    advanced independently. It never stages or moves the canonical checkout,
    and it refuses a dirty checkout so visible-but-uncommitted files cannot be
    mistaken for reconciled history.
    """
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    root_s = str(root)
    with _repo_lock(root_s):
        candidate, stable = _ensure_refs(root)
        if _git(
                root,
                ['merge-base', '--is-ancestor', stable, candidate],
        ).returncode != 0:
            raise IntegrationError(
                'Candidate and stable have diverged; repair that topology '
                'before reconciling canonical HEAD')
        dirty = _porcelain(root)
        if dirty['total']:
            raise IntegrationError(
                'Canonical checkout is dirty. Reconcile includes committed '
                'HEAD history only; commit or stash visible changes first')
        head = _rev(root, 'HEAD')
        if not head:
            raise IntegrationError('Canonical HEAD is missing')
        if head == candidate or _git(
                root,
                ['merge-base', '--is-ancestor', head, candidate],
        ).returncode == 0:
            return {
                'ok': True, 'changed': False, 'headSha': head,
                'candidateSha': candidate, 'stableSha': stable,
                'headAlreadyContained': True,
            }

        target, conflict = _merge_checkpoint(
            root, candidate, head, 'canonical-head-reconcile')
        if conflict:
            _record_event(
                root_s, '', 'head_reconcile_failed',
                'Canonical HEAD could not be reconciled into candidate',
                conflict, user_id=owner_user_id)
            _push(root_s, user_id=owner_user_id)
            raise IntegrationError(
                f'Canonical HEAD reconciliation conflicted: {conflict}')
        command = os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip()
        passed, detail = _gate_commands(root, candidate, target, command)
        if not passed:
            _record_event(
                root_s, '', 'head_reconcile_failed',
                'Canonical HEAD reconciliation gate failed', detail,
                user_id=owner_user_id)
            _push(root_s, user_id=owner_user_id)
            raise IntegrationError(
                f'Canonical HEAD reconciliation gate failed: {detail}')
        if _rev(root, 'HEAD') != head:
            raise IntegrationError(
                'Canonical HEAD moved during reconciliation; refresh and retry')
        cp = _git(root, ['update-ref', _CANDIDATE_REF, target, candidate])
        if cp.returncode != 0:
            raise IntegrationError(
                'Candidate moved concurrently; refresh and retry reconciliation')
        merge_commit = target not in {candidate, head}
        _record_event(
            root_s, '', 'head_reconciled',
            f'Canonical HEAD {_short(head)} reconciled into candidate '
            f'{_short(target)}',
            {'previousCandidate': candidate, 'mergeCommit': merge_commit},
            user_id=owner_user_id)
    _push(root_s, user_id=owner_user_id)
    return {
        'ok': True, 'changed': True, 'headSha': head,
        'previousCandidateSha': candidate, 'candidateSha': target,
        'stableSha': stable, 'mergeCommit': merge_commit,
    }


def promote_stable(
    project_path: str, *, user_id: int,
    acknowledge_head_divergence: bool = False,
) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    if not isinstance(acknowledge_head_divergence, bool):
        raise IntegrationError('acknowledge_head_divergence must be a boolean')
    root = _repo_root(project_path)
    with _repo_lock(str(root)):
        candidate, stable = _ensure_refs(root)
        if _git(root, ['merge-base', '--is-ancestor', stable, candidate]).returncode != 0:
            raise IntegrationError(
                'Candidate and stable have diverged; stable promotion must be fast-forward')
        head = _rev(root, 'HEAD')
        head_ahead = _git(
            root, ['rev-list', '--count', f'{candidate}..{head}'])
        candidate_ahead_head = _git(
            root, ['rev-list', '--count', f'{head}..{candidate}'])
        try:
            head_ahead_count = int(head_ahead.stdout.strip())
            candidate_ahead_head_count = int(candidate_ahead_head.stdout.strip())
        except (AttributeError, ValueError):
            head_ahead_count = candidate_ahead_head_count = 0
        head_diverged = bool(head_ahead_count and candidate_ahead_head_count)
        if head_diverged and not acknowledge_head_divergence:
            raise IntegrationError(
                'Canonical HEAD and candidate have diverged; promotion does not '
                'update the canonical branch. Refresh, review both histories, '
                'and explicitly acknowledge the divergence to promote stable')
        stable_command = os.environ.get(
            'TOFU_INTEGRATION_STABLE_TEST_CMD', '').strip()
        project_command = os.environ.get(
            'TOFU_INTEGRATION_TEST_CMD', '').strip()
        # A separate stable command is an optional stronger release gate. If
        # absent, rerun the candidate project gate against stable..candidate;
        # never turn "optional" into an unexplained semantic-change refusal.
        command = stable_command or project_command
        passed, detail = _gate_commands(root, stable, candidate, command)
        if not passed:
            _record_event(str(root), '', 'promotion_failed',
                          'Stable promotion gate failed', detail,
                          user_id=owner_user_id)
            _push(str(root), user_id=owner_user_id)
            raise IntegrationError(f'Stable promotion gate failed: {detail}')
        cp = _git(root, ['update-ref', _STABLE_REF, candidate, stable])
        if cp.returncode != 0:
            raise IntegrationError('Stable ref moved concurrently; refresh and retry')
        _record_event(str(root), '', 'promoted',
                      f'Stable promoted to {_short(candidate)}',
                      ({'headDiverged': head_diverged,
                        'headAheadCandidate': head_ahead_count,
                        'candidateAheadHead': candidate_ahead_head_count}
                       if head_diverged else ''),
                      user_id=owner_user_id)
    _push(str(root), user_id=owner_user_id)
    return {
        'ok': True, 'stableSha': candidate, 'candidateSha': candidate,
        'headDiverged': head_diverged,
    }


def _diffstat(root: Path, old: str, new: str, *, cap: int = 200) -> dict[str, Any]:
    """Files-changed summary between two revs — the answer to "what is in
    this checkpoint?" that a bare SHA can never give. Bounded: the file list
    caps at ``cap`` entries while totals always reflect the full diff."""
    if not old or not new or old == new:
        return {'files': [], 'totalFiles': 0, 'adds': 0, 'dels': 0}
    try:
        cp = _git(root, ['diff', '--numstat', old, new], timeout=30.0)
    except IntegrationError:
        # Same degrade-as-empty contract as the returncode!=0 branch and as
        # _porcelain's timedOut degrade: one slow diff (IO-stall-prone
        # mounts) must never take down the whole status endpoint.
        return {'files': [], 'totalFiles': 0, 'adds': 0, 'dels': 0}
    if cp.returncode != 0:
        return {'files': [], 'totalFiles': 0, 'adds': 0, 'dels': 0}
    files: list[dict[str, Any]] = []
    adds = dels = 0
    total = 0
    for line in cp.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        total += 1
        try:
            a = int(parts[0]) if parts[0] != '-' else 0
            d = int(parts[1]) if parts[1] != '-' else 0
        except ValueError:
            a = d = 0
        adds += a
        dels += d
        if len(files) < cap:
            files.append({'path': parts[-1], 'adds': a, 'dels': d})
    return {'files': files, 'totalFiles': total, 'adds': adds, 'dels': dels}


def prune_worktree_metadata(project_path: str, *, user_id: int) -> dict[str, Any]:
    """Prune Git records whose worktree directories are already missing.

    This never removes a live worktree directory. The explicit UI confirmation
    authorises ``--expire=now`` so already-missing temporary checkouts do not
    linger for Git's default expiry window; active writer directories remain.
    """
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    with _repo_lock(str(root)):
        before_total, before_prunable = _worktree_count(root)
        cp = _git(root, ['worktree', 'prune', '--verbose', '--expire=now'],
                  timeout=90.0)
        if cp.returncode != 0:
            raise IntegrationError((cp.stderr or cp.stdout).strip()[:1000])
        after_total, after_prunable = _worktree_count(root)
        _record_event(
            str(root), '', 'worktrees_pruned',
            f'Pruned {max(0, before_total - after_total)} stale Git worktree records',
            (cp.stderr or cp.stdout).strip(),
            user_id=owner_user_id,
        )
    _push(str(root), user_id=owner_user_id)
    return {
        'ok': True, 'removed': max(0, before_total - after_total),
        'beforePrunable': before_prunable, 'remainingPrunable': after_prunable,
    }


def _porcelain(path: Path, timeout: float = 12.0) -> dict[str, Any]:
    try:
        cp = _git(path, ['status', '--porcelain=v1', '-z', '--untracked-files=normal'],
                  timeout=timeout)
    except IntegrationError as exc:
        logger.debug('[Integration] status scan failed for %s: %s', path, exc)
        return {'modified': 0, 'deleted': 0, 'untracked': 0, 'total': 0,
                'timedOut': True, 'scanned': False, 'error': str(exc)}
    modified = deleted = untracked = 0
    entries = [part for part in cp.stdout.split('\0') if part]
    index = 0
    while index < len(entries):
        entry = entries[index]
        code = entry[:2]
        if code == '??':
            untracked += 1
        elif 'D' in code:
            deleted += 1
        else:
            modified += 1
        # In -z form a rename/copy has a second path entry with no status
        # prefix. It belongs to this record and must not inflate the count.
        index += 2 if ('R' in code or 'C' in code) else 1
    return {'modified': modified, 'deleted': deleted, 'untracked': untracked,
            'total': modified + deleted + untracked, 'timedOut': False,
            'scanned': True}


def _worktree_inventory(root: Path) -> list[dict[str, Any]]:
    cp = _git(root, ['worktree', 'list', '--porcelain'])
    if cp.returncode != 0:
        return []
    blocks = [block for block in cp.stdout.split('\n\n') if block.strip()]
    inventory: list[dict[str, Any]] = []
    for block in blocks:
        item: dict[str, Any] = {
            'path': '', 'head': '', 'branch': '',
            'detached': False, 'prunable': False,
        }
        for line in block.splitlines():
            key, _sep, value = line.partition(' ')
            if key == 'worktree':
                item['path'] = value
            elif key == 'HEAD':
                item['head'] = value
            elif key == 'branch':
                item['branch'] = value.removeprefix('refs/heads/')
            elif key == 'detached':
                item['detached'] = True
            elif key == 'prunable':
                item['prunable'] = True
                item['prunableReason'] = value
        inventory.append(item)
    return inventory


def _worktree_count(root: Path) -> tuple[int, int]:
    inventory = _worktree_inventory(root)
    return len(inventory), sum(1 for item in inventory if item['prunable'])


def _row_payload(row: Mapping[str, Any], *, scan: bool = True) -> dict[str, Any]:
    workspace = Path(row['workspace_path'])
    dirty = (_porcelain(workspace, timeout=4.0) if scan and workspace.exists()
             else {'modified': 0, 'deleted': 0, 'untracked': 0, 'total': 0,
                   'timedOut': False, 'scanned': False})
    origin = row.get('origin') or {}
    checkpoint = row['checkpoint_sha'] or ''
    base = row['base_sha'] or ''
    # The checkpoint's CONTENT summary — "what is in this package?" — computed
    # off the shared object store (never the writer's checkout), so it works
    # even for a deleted worktree. Bounded by the same scan budget as the
    # per-row porcelain scan.
    diffstat = (_diffstat(Path(row['project_root']), base, checkpoint)
                if scan and checkpoint and base else
                {'files': [], 'totalFiles': 0, 'adds': 0, 'dels': 0})
    return {
        'workId': row['task_id'], 'title': row['title'],
        'workspacePath': row['workspace_path'], 'managed': bool(row['managed']),
        'exists': workspace.exists(), 'state': row['state'],
        'baseSha': row['base_sha'], 'checkpointSha': row['checkpoint_sha'],
        'candidateSha': row['candidate_sha'], 'error': row['error'],
        'origin': origin,
        'conflictFiles': [str(f) for f in (origin.get('conflict_files') or [])],
        'diffstat': diffstat,
        'dirty': dirty, 'createdAt': _iso(row['created_at']),
        'updatedAt': _iso(row['updated_at']),
    }


def _server_identity(root: Path, candidate: str, stable: str,
                     canonical_clean: bool) -> dict[str, Any]:
    try:
        from lib.boot_identity import BOOT_ID, BOOT_TS, PID, code_fingerprint
        fingerprint = code_fingerprint()
    except Exception as exc:
        logger.debug('[Integration] server identity unavailable: %s', exc)
        return {}
    try:
        same_repo = _common_dir(root) == _common_dir(_APP_ROOT)
    except IntegrationError as exc:
        logger.debug('[Integration] common-dir comparison failed; using path identity: %s',
                     exc)
        same_repo = root == _APP_ROOT
    loaded_head = str(fingerprint.get('head') or '')
    # boot_identity intentionally fingerprints tracked source only. An
    # untracked Python/JS module may still be imported, so claiming that the
    # process serves an immutable ref additionally requires the complete
    # canonical status (including untracked files) to be clean.
    clean = fingerprint.get('dirty') is False and canonical_clean
    return {
        'pid': PID, 'bootId': BOOT_ID, 'bootedAt': _iso(BOOT_TS),
        'codeFingerprint': fingerprint, 'sameRepository': same_repo,
        'sourceTreeDirty': not clean,
        'servesCandidate': bool(same_repo and clean and loaded_head
                                and candidate.startswith(loaded_head)),
        'servesStable': bool(same_repo and clean and loaded_head
                             and stable.startswith(loaded_head)),
    }


def integration_status(
    project_path: str, *, user_id: int, use_cache: bool = True,
) -> dict[str, Any]:
    owner_user_id = _require_user_id(user_id)
    root = _repo_root(project_path)
    root_s = str(root)
    if use_cache:
        with _STATUS_CACHE_LOCK:
            cached = _STATUS_CACHE.get((owner_user_id, root_s))
            if cached and _now() - cached[0] < _STATUS_CACHE_TTL_SECONDS:
                return cached[1]
    head = _rev(root, 'HEAD')
    candidate = _rev(root, _CANDIDATE_REF) or head
    stable = _rev(root, _STABLE_REF) or candidate
    dirty = _porcelain(root)
    inventory = _worktree_inventory(root)
    total_worktrees = len(inventory)
    prunable = sum(1 for item in inventory if item['prunable'])
    rows, event_rows = _state.status_rows(
        root_s, user_id=owner_user_id)
    scan_limit = max(0, min(32, _env_int(
        'TOFU_INTEGRATION_STATUS_SCAN_LIMIT', 8)))
    scan_states = {
        'running', 'checkpointed', 'ready', 'integrating',
        'quarantined', 'failed',
    }
    scannable_rows = [row for row in rows if row['state'] in scan_states]
    scanned_rows = scannable_rows[:scan_limit]
    if scanned_rows:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(scanned_rows))) as pool:
            scanned_payloads = list(pool.map(_row_payload, scanned_rows))
    else:
        scanned_payloads = []
    scanned_by_id = {
        int(row['id']): payload
        for row, payload in zip(scanned_rows, scanned_payloads, strict=True)
    }
    # Preserve durable newest-first ordering while avoiding porcelain/diffstat
    # subprocesses for terminal merged/discarded history.
    workspaces = [
        scanned_by_id.get(int(row['id'])) or _row_payload(row, scan=False)
        for row in rows
    ]
    registered_paths = {
        str(Path(item['workspacePath']).resolve()) for item in workspaces
        if item.get('workspacePath')
    }
    unregistered = [
        item for item in inventory
        if item['path'] and Path(item['path']).resolve() != root
        and str(Path(item['path']).resolve()) not in registered_paths
    ]
    counts: dict[str, int] = {}
    for item in workspaces:
        counts[item['state']] = counts.get(item['state'], 0) + 1
    ahead_cp = _git(root, ['rev-list', '--count', f'{stable}..{candidate}'])
    behind_cp = _git(root, ['rev-list', '--count', f'{candidate}..{stable}'])
    candidate_ahead_head_cp = (
        _git(root, ['rev-list', '--count', f'{head}..{candidate}'])
        if head else None)
    head_ahead_candidate_cp = (
        _git(root, ['rev-list', '--count', f'{candidate}..{head}'])
        if head else None)
    try:
        ahead = int(ahead_cp.stdout.strip()) if ahead_cp.returncode == 0 else 0
        behind = int(behind_cp.stdout.strip()) if behind_cp.returncode == 0 else 0
        candidate_ahead_head = (
            int(candidate_ahead_head_cp.stdout.strip())
            if candidate_ahead_head_cp is not None
            and candidate_ahead_head_cp.returncode == 0 else 0)
        head_ahead_candidate = (
            int(head_ahead_candidate_cp.stdout.strip())
            if head_ahead_candidate_cp is not None
            and head_ahead_candidate_cp.returncode == 0 else 0)
    except ValueError as exc:
        logger.debug('[Integration] invalid ahead/behind count; using zero: %s', exc)
        ahead = behind = candidate_ahead_head = head_ahead_candidate = 0
    head_candidate_diverged = bool(
        candidate_ahead_head and head_ahead_candidate)
    warnings: list[str] = []
    if dirty['total']:
        warnings.append(
            'The canonical checkout is dirty. Its files are not part of candidate or stable.')
    integration_gate_configured = bool(
        os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip())
    stable_gate_configured = bool(
        os.environ.get('TOFU_INTEGRATION_STABLE_TEST_CMD', '').strip())
    if not integration_gate_configured:
        warnings.append(
            'TOFU_INTEGRATION_TEST_CMD is not configured. Semantic code/config '
            'changes will quarantine instead of receiving syntax-only acceptance.')
    if not stable_gate_configured:
        warnings.append(
            'TOFU_INTEGRATION_STABLE_TEST_CMD is not configured. Stable promotion '
            'still requires the candidate gate for semantic code/config changes.')
    if prunable:
        warnings.append(f'{prunable} Git worktree registration(s) are prunable.')
    if behind:
        warnings.append('Candidate and stable have diverged; automatic promotion is unsafe.')
    if head_candidate_diverged:
        warnings.append(
            'Canonical HEAD and candidate have diverged '
            f'(HEAD +{head_ahead_candidate}, candidate +{candidate_ahead_head}). '
            'Promoting stable does not update the canonical branch.')
    elif head_ahead_candidate:
        warnings.append(
            f'Candidate is behind canonical HEAD by {head_ahead_candidate} commit(s).')
    unscanned_active = max(0, len(scannable_rows) - len(scanned_rows))
    if unscanned_active:
        warnings.append(
            f'{unscanned_active} older active/problem workspaces were not individually scanned '
            'to keep status latency bounded.')
    events: list[dict[str, Any]] = []
    for row in event_rows:
        full_detail = str(row['detail'] or '')
        detail = full_detail[:1200]
        events.append({
            'id': row['id'], 'workId': row['task_id'], 'kind': row['kind'],
            'message': row['message'], 'detail': detail,
            'detailTruncated': len(full_detail) > len(detail),
            'createdAt': _iso(row['created_at']),
        })
    payload = {
        'ok': True, 'enabled': True, 'autorun': _autorun_enabled(),
        'repo': {
            'root': root_s, 'head': head,
            'branch': _git(root, ['branch', '--show-current']).stdout.strip(),
            'dirty': dirty, 'canonicalClean': dirty['total'] == 0,
            'worktreesTotal': total_worktrees, 'prunableWorktrees': prunable,
            'unregisteredWorktreesCount': len(unregistered),
            'unregisteredWorktrees': unregistered[:20],
        },
        'refs': {
            'candidate': candidate, 'stable': stable,
            'candidateInitialized': bool(_rev(root, _CANDIDATE_REF)),
            'stableInitialized': bool(_rev(root, _STABLE_REF)),
            'candidateAheadStable': ahead, 'stableAheadCandidate': behind,
            # Keep the historical field for existing clients; its name was
            # ambiguous, so new clients use the two explicit directions.
            'headBehindCandidate': candidate_ahead_head,
            'candidateAheadHead': candidate_ahead_head,
            'headAheadCandidate': head_ahead_candidate,
            'headCandidateDiverged': head_candidate_diverged,
        },
        'counts': counts, 'workspaces': workspaces, 'events': events,
        'gates': {
            'builtIn': [
                'git diff --check', 'forbidden-path policy',
                'Python syntax', 'JavaScript syntax', 'JSON syntax',
            ],
            'testCommandConfigured': integration_gate_configured,
            'stableCommandConfigured': stable_gate_configured,
            'projectGateRequiredSuffixes': sorted(_PROJECT_GATE_REQUIRED_SUFFIXES),
        },
        'server': _server_identity(
            root, candidate, stable, dirty['total'] == 0),
        'warnings': warnings,
    }
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[(owner_user_id, root_s)] = (_now(), payload)
    return payload


# ── Agent-facing tool executor (integration_checkpoint / _submit / _status) ──
# The writer of an isolated work item drives its worktree through these. Resolution
# rule for an omitted task_id: exactly ONE active (running/checkpointed)
# workspace whose origin.convId is the calling conversation — anything else is
# a clear error naming the fix, never a guess.

_ACTIVE_WRITER_STATES = {'running', 'checkpointed'}


def _resolve_writer_task_id(
    project_root: str, conv_id: str, *, user_id: int,
) -> tuple[str, str]:
    """Return (task_id, error). Exactly one active workspace owned by conv_id
    resolves; zero or many returns ('', guidance)."""
    try:
        rows, _events = _state.status_rows(
            project_root, user_id=_require_user_id(user_id))
    except Exception as exc:
        logger.warning('[Integration] status_rows failed for tool executor: %s', exc)
        return '', f'Error: could not read integration state: {exc}'
    owned = [r for r in rows
             if r.get('state') in _ACTIVE_WRITER_STATES
             and str((r.get('origin') or {}).get('convId') or '') == conv_id]
    if len(owned) == 1:
        return str(owned[0]['task_id']), ''
    if not owned:
        return '', ('Error: this conversation owns no active isolated workspace. '
                    'These tools are only for an execution started in an isolated '
                    'workspace; pass its work ID (pw_…) explicitly if needed.')
    ids = ', '.join(str(r['task_id']) for r in owned)
    return '', (f'Error: ambiguous — this conversation owns {len(owned)} active '
                f'isolated workspaces ({ids}). Pass task_id explicitly.')


def execute_integration_tool(fn_name: str, fn_args: dict, *,
                             project_path: str, user_id: int,
                             conv_id: str = '') -> str:
    """Agent entry for checkpoint/submit only; returns text and never raises."""
    owner_user_id = _require_user_id(user_id)
    if not project_path:
        return 'Error: integration tools require project mode (no active project).'
    try:
        root_s = str(_repo_root(project_path))
    except IntegrationError as exc:
        # Never-raises contract: a non-git / empty project must surface as a
        # curated tool result, not a raw rev-parse traceback in error.log.
        return f'Error: {exc}'

    if fn_name in ('integration_checkpoint', 'integration_submit'):
        task_id = str((fn_args or {}).get('task_id') or '').strip()
        if not task_id:
            task_id, err = _resolve_writer_task_id(
                root_s, conv_id, user_id=owner_user_id)
            if err:
                return err
        try:
            row = _state.get_workspace(
                root_s, task_id, user_id=owner_user_id)
        except Exception as e:
            logger.debug('[Integration] get_workspace failed task=%s: %s',
                         task_id, e)
            return (f'Error: no integration workspace named {task_id}. '
                    'The runtime-bound work ID has no isolated workspace.')
        state = str(row.get('state') or '')
        if state in ('discarded', 'merged'):
            return (f'Error: workspace {task_id} is {state} — it is no longer '
                    'writable. A later request must create a new work item.')
        # Quarantined/failed workspaces remain writable so their assigned
        # conversation can repair the isolated checkout and resubmit it.
        try:
            if fn_name == 'integration_checkpoint':
                note = str((fn_args or {}).get('note') or '').strip()
                if note:
                    _set_meta(
                        root_s, task_id,
                        {'lastCheckpointNote': note[:500]},
                        user_id=owner_user_id,
                    )
                res = checkpoint_workspace(
                    root_s, task_id, user_id=owner_user_id)
                audit_log('integration_checkpoint', project_path=root_s,
                          task_id=task_id, conv_id=conv_id)
                return (f'Checkpoint saved for {task_id}: '
                        f"{str(res.get('checkpointSha') or '')[:12]} "
                        f'(ref {res.get("checkpointRef", "")}). The shared '
                        'checkout is untouched; keep working, and run '
                        'integration_submit when this work is done.')
            summary = str((fn_args or {}).get('summary') or '').strip()
            if not summary:
                return ('Error: integration_submit requires `summary` — tell '
                        'the human reviewer what changed, why, and how you '
                        'verified it.')
            _set_meta(
                root_s, task_id, {'submitSummary': summary[:2000]},
                user_id=owner_user_id,
            )
            res = submit_workspace(
                root_s, task_id, user_id=owner_user_id)
            audit_log('integration_submit', project_path=root_s,
                      task_id=task_id, conv_id=conv_id)
            return (f'Submitted {task_id} for HUMAN review '
                    f"(final checkpoint {str(res.get('checkpointSha') or '')[:12]}). "
                    'The workspace is now IMMUTABLE — do not edit it further. '
                    'Human review and project checker results determine '
                    'whether it may move to the candidate branch.')
        except IntegrationError as exc:
            return f'Error: {exc}'
        except Exception as exc:
            logger.warning('[Integration] %s failed for %s: %s',
                           fn_name, task_id, exc, exc_info=True)
            return f'Error: {fn_name} failed: {exc}'

    return f'Unknown integration tool: {fn_name}'


__all__ = [
    'IntegrationError', 'checkpoint_workspace', 'create_workspace',
    'discard_workspace', 'ensure_worker_started', 'execute_integration_tool',
    'has_active_workspace_for_work', 'integration_status',
    'process_ready_once',
    'promote_stable', 'reconcile_candidate_with_head', 'register_workspace',
    'retry_workspace',
    'prune_worktree_metadata', 'submit_workspace',
]
