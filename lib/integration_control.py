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

from lib.database import integration_control_repository as _state
from lib.log import get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

_APP_ROOT = Path(__file__).resolve().parent.parent
_REF_ROOT = 'refs/tofu'
_CANDIDATE_REF = f'{_REF_ROOT}/candidate'
_STABLE_REF = f'{_REF_ROOT}/stable'
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_CACHE_LOCK = threading.Lock()


IntegrationError = _state.IntegrationStateError


def _control_root() -> Path:
    root = Path(data_root()) / 'integration'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _db_path() -> Path:
    override = os.environ.get('TOFU_INTEGRATION_DB', '').strip()
    return Path(override).expanduser().resolve() if override else _control_root() / 'control.sqlite3'


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


def _checkpoint_ref(task_id: str) -> str:
    return f'{_REF_ROOT}/checkpoints/{_safe_task(task_id)}'


def _quarantine_ref(task_id: str) -> str:
    return f'{_REF_ROOT}/quarantine/{_safe_task(task_id)}'


def _now() -> float:
    return time.time()


def _iso(ts: float | int | None) -> str:
    if not ts:
        return ''
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(ts)))


def _record_event(project_root: str, task_id: str, kind: str,
                  message: str, detail: Any = '') -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    _state.record_event(
        _db_path(), project_root=project_root, task_id=task_id, kind=kind,
        message=message, detail=detail, now=_now())


def _invalidate(project_root: str) -> None:
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.pop(project_root, None)


def _push(project_root: str) -> None:
    _invalidate(project_root)
    try:
        from lib.agent_core.push import push_event
        from lib.conversations.project_feed import project_channel_key
        push_event('project', project_channel_key(project_root), {
            'type': 'integration', 'projectPath': project_root,
        })
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


def register_workspace(project_path: str, task_id: str, workspace_path: str,
                       title: str = '', *, managed: bool = False) -> dict[str, Any]:
    task_id = str(task_id or '').strip()
    if not task_id:
        raise IntegrationError('taskId is required')
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
    with _repo_lock(str(root)):
        _state.register_workspace(
            _db_path(), project_root=str(root), task_id=task_id,
            title=str(title or '').strip(), workspace_path=str(workspace),
            managed=managed, base_sha=base, now=now)
    _push(str(root))
    return {'ok': True, 'taskId': task_id, 'workspacePath': str(workspace),
            'baseSha': base, 'state': 'running', 'managed': bool(managed)}


def create_workspace(project_path: str, task_id: str,
                     title: str = '') -> dict[str, Any]:
    root = _repo_root(project_path)
    task_id = str(task_id or '').strip()
    if not task_id:
        raise IntegrationError('taskId is required')
    repo_key = hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:12]
    destination = _workspace_root() / repo_key / _safe_task(task_id)
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
                                  managed=True)
    except Exception:
        # The worktree was created solely for this failed operation and has not
        # been handed to a writer, so cleanup is safe and keeps the registry sane.
        with _repo_lock(str(root)):
            _remove_controlled_worktree(root, destination)
        raise


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


def checkpoint_workspace(project_path: str, task_id: str) -> dict[str, Any]:
    root = _repo_root(project_path)
    root_s = str(root)
    task_id = str(task_id or '').strip()
    with _repo_lock(root_s):
        row = _state.get_workspace(_db_path(), root_s, task_id)
        if row['state'] in {'ready', 'integrating'}:
            raise IntegrationError(
                'The submitted checkpoint is immutable while it is in the integration queue')
        workspace = Path(row['workspace_path'])
        if not workspace.exists():
            raise IntegrationError(f'Workspace is missing: {workspace}')
        parent = row['checkpoint_sha'] or row['base_sha'] or _rev(workspace, 'HEAD')
        checkpoint = _alternate_index_checkpoint(workspace, parent, task_id)
        _git(root, ['update-ref', _checkpoint_ref(task_id), checkpoint], check=True)
        _state.save_checkpoint(
            _db_path(), project_root=root_s, task_id=task_id,
            checkpoint_sha=checkpoint, now=_now())
    _push(root_s)
    return {'ok': True, 'taskId': task_id, 'checkpointSha': checkpoint,
            'checkpointRef': _checkpoint_ref(task_id), 'state': 'checkpointed'}


def submit_workspace(project_path: str, task_id: str) -> dict[str, Any]:
    result = checkpoint_workspace(project_path, task_id)
    root = _repo_root(project_path)
    now = _now()
    _state.submit_checkpoint(
        _db_path(), project_root=str(root), task_id=task_id, now=now)
    ensure_worker_started()
    _push(str(root))
    result['state'] = 'ready'
    return result


def retry_workspace(project_path: str, task_id: str) -> dict[str, Any]:
    root = _repo_root(project_path)
    now = _now()
    _state.retry_checkpoint(
        _db_path(), project_root=str(root), task_id=task_id, now=now)
    ensure_worker_started()
    _push(str(root))
    return {'ok': True, 'taskId': task_id, 'state': 'ready'}


def _gate_commands(root: Path, old: str, target: str,
                   configured_command: str = '') -> tuple[bool, str]:
    diff_check = _git(root, ['diff', '--check', old, target])
    if diff_check.returncode != 0:
        return False, (diff_check.stdout or diff_check.stderr).strip()[:3000]

    names_cp = _git(root, ['diff', '--name-only', '--diff-filter=ACMR', '-z', old, target],
                    check=True)
    changed = [name for name in names_cp.stdout.split('\0') if name]
    python_files = [name for name in changed if name.endswith('.py')]
    js_files = [name for name in changed if name.endswith(('.js', '.mjs', '.cjs'))]
    if not python_files and not js_files and not configured_command:
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
                cp = subprocess.run(
                    ['node', '--check', '--', name], cwd=str(temp_path), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=30.0, check=False,
                )
                if cp.returncode != 0:
                    return False, (cp.stderr or cp.stdout).strip()[:3000]
        if configured_command:
            command = shlex.split(configured_command)
            if not command:
                return False, 'Configured gate command is empty'
            cp = subprocess.run(
                command, cwd=str(temp_path), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=float(os.environ.get('TOFU_INTEGRATION_TEST_TIMEOUT', '600')),
                check=False,
            )
            if cp.returncode != 0:
                output = '\n'.join(part for part in [cp.stdout, cp.stderr] if part)
                return False, output.strip()[-3000:]
        return True, ''
    except (OSError, subprocess.TimeoutExpired) as exc:
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
    checkpoint = row['checkpoint_sha']
    if checkpoint:
        _git(root, ['update-ref', _quarantine_ref(row['task_id']), checkpoint],
             check=True)
    _state.quarantine(
        _db_path(), row_id=int(row['id']), project_root=str(root),
        task_id=str(row['task_id']), reason=reason, now=_now())


def _integrate_row(row: Mapping[str, Any]) -> None:
    root = Path(row['project_root'])
    task_id = row['task_id']
    with _repo_lock(str(root)):
        fresh = _state.get_integrating(_db_path(), int(row['id']))
        if fresh is None:
            return
        checkpoint = fresh['checkpoint_sha']
        if not checkpoint or not _rev(root, checkpoint):
            _quarantine(root, fresh, 'Checkpoint commit is missing')
            _push(str(root))
            return
        candidate, _stable = _ensure_refs(root)
        target, conflict = _merge_checkpoint(root, candidate, checkpoint, task_id)
        if conflict:
            _quarantine(root, fresh, conflict)
            _push(str(root))
            return
        command = os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip()
        passed, detail = _gate_commands(root, candidate, target, command)
        if not passed:
            _quarantine(root, fresh, f'Gate failed:\n{detail}')
            _push(str(root))
            return
        cp = _git(root, ['update-ref', _CANDIDATE_REF, target, candidate])
        if cp.returncode != 0:
            # A different process advanced the ref despite our filesystem lock
            # (e.g. a remote administrator). Requeue instead of losing work.
            _state.requeue(
                _db_path(), row_id=int(fresh['id']),
                error='Candidate moved concurrently; retrying', now=_now())
            return
        _state.mark_merged(
            _db_path(), row_id=int(fresh['id']), project_root=str(root),
            task_id=str(task_id), candidate_sha=target, now=_now())
    _push(str(root))


def _claim_next() -> dict | None:
    # One repository transaction recovers abandoned claims, selects an
    # eligible project, and performs the ready→integrating CAS.
    return _state.claim_next(_db_path(), now=_now())


def process_ready_once() -> bool:
    row = _claim_next()
    if row is None:
        return False
    try:
        _integrate_row(row)
    except Exception as exc:
        logger.exception('[Integration] task %s failed', row['task_id'])
        _state.mark_failed(
            _db_path(), row_id=int(row['id']),
            project_root=str(row['project_root']), task_id=str(row['task_id']),
            error=str(exc), now=_now())
        _push(row['project_root'])
    return True


def _autorun_enabled() -> bool:
    return os.environ.get('TOFU_INTEGRATION_AUTORUN', '1').strip().lower() not in {
        '0', 'false', 'no', 'off',
    }


def _worker_loop() -> None:
    while _autorun_enabled():
        if not process_ready_once():
            time.sleep(3.0)


def ensure_worker_started() -> bool:
    global _WORKER
    # Bootstrap/upgrade the separate control authority once at server start.
    # Read-only status calls can then remain genuinely side-effect free while
    # still seeing rows written by the pre-repository schema.
    _state.initialize_store(_db_path())
    if not _autorun_enabled():
        return False
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(
                target=_worker_loop, name='tofu-integration', daemon=True,
            )
            _WORKER.start()
    return True


def promote_stable(project_path: str) -> dict[str, Any]:
    root = _repo_root(project_path)
    with _repo_lock(str(root)):
        candidate, stable = _ensure_refs(root)
        if _git(root, ['merge-base', '--is-ancestor', stable, candidate]).returncode != 0:
            raise IntegrationError(
                'Candidate and stable have diverged; stable promotion must be fast-forward')
        command = os.environ.get('TOFU_INTEGRATION_STABLE_TEST_CMD', '').strip()
        passed, detail = _gate_commands(root, stable, candidate, command)
        if not passed:
            _record_event(str(root), '', 'promotion_failed',
                          'Stable promotion gate failed', detail)
            _push(str(root))
            raise IntegrationError(f'Stable promotion gate failed: {detail}')
        cp = _git(root, ['update-ref', _STABLE_REF, candidate, stable])
        if cp.returncode != 0:
            raise IntegrationError('Stable ref moved concurrently; refresh and retry')
        _record_event(str(root), '', 'promoted',
                      f'Stable promoted to {_short(candidate)}')
    _push(str(root))
    return {'ok': True, 'stableSha': candidate, 'candidateSha': candidate}


def prune_worktree_metadata(project_path: str) -> dict[str, Any]:
    """Prune Git records whose worktree directories are already missing.

    This never removes a live worktree directory. The explicit UI confirmation
    authorises ``--expire=now`` so already-missing temporary checkouts do not
    linger for Git's default expiry window; active writer directories remain.
    """
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
        )
    _push(str(root))
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
    return {
        'taskId': row['task_id'], 'title': row['title'],
        'workspacePath': row['workspace_path'], 'managed': bool(row['managed']),
        'exists': workspace.exists(), 'state': row['state'],
        'baseSha': row['base_sha'], 'checkpointSha': row['checkpoint_sha'],
        'candidateSha': row['candidate_sha'], 'error': row['error'],
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


def integration_status(project_path: str, *, use_cache: bool = True) -> dict[str, Any]:
    root = _repo_root(project_path)
    root_s = str(root)
    if use_cache:
        with _STATUS_CACHE_LOCK:
            cached = _STATUS_CACHE.get(root_s)
            if cached and _now() - cached[0] < 3.0:
                return cached[1]
    head = _rev(root, 'HEAD')
    candidate = _rev(root, _CANDIDATE_REF) or head
    stable = _rev(root, _STABLE_REF) or candidate
    dirty = _porcelain(root)
    inventory = _worktree_inventory(root)
    total_worktrees = len(inventory)
    prunable = sum(1 for item in inventory if item['prunable'])
    rows, event_rows = _state.status_rows(_db_path(), root_s)
    scan_limit = max(0, min(32, int(os.environ.get(
        'TOFU_INTEGRATION_STATUS_SCAN_LIMIT', '12'))))
    scanned_rows = rows[:scan_limit]
    if scanned_rows:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(scanned_rows))) as pool:
            workspaces = list(pool.map(_row_payload, scanned_rows))
    else:
        workspaces = []
    workspaces.extend(_row_payload(row, scan=False) for row in rows[scan_limit:])
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
    try:
        ahead = int(ahead_cp.stdout.strip()) if ahead_cp.returncode == 0 else 0
        behind = int(behind_cp.stdout.strip()) if behind_cp.returncode == 0 else 0
    except ValueError as exc:
        logger.debug('[Integration] invalid ahead/behind count; using zero: %s', exc)
        ahead = behind = 0
    warnings: list[str] = []
    if dirty['total']:
        warnings.append(
            'The canonical checkout is dirty. Its files are not part of candidate or stable.')
    if prunable:
        warnings.append(f'{prunable} Git worktree registration(s) are prunable.')
    if behind:
        warnings.append('Candidate and stable have diverged; automatic promotion is unsafe.')
    if len(rows) > scan_limit:
        warnings.append(
            f'{len(rows) - scan_limit} older workspaces were not individually scanned '
            'to keep status latency bounded.')
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
        },
        'counts': counts, 'workspaces': workspaces,
        'events': [{
            'id': row['id'], 'taskId': row['task_id'], 'kind': row['kind'],
            'message': row['message'], 'detail': row['detail'],
            'createdAt': _iso(row['created_at']),
        } for row in event_rows],
        'gates': {
            'builtIn': ['git diff --check', 'Python syntax', 'JavaScript syntax'],
            'testCommandConfigured': bool(os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip()),
            'stableCommandConfigured': bool(os.environ.get('TOFU_INTEGRATION_STABLE_TEST_CMD', '').strip()),
        },
        'server': _server_identity(
            root, candidate, stable, dirty['total'] == 0),
        'warnings': warnings,
    }
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[root_s] = (_now(), payload)
    ensure_worker_started()
    return payload


__all__ = [
    'IntegrationError', 'checkpoint_workspace', 'create_workspace',
    'ensure_worker_started', 'integration_status', 'process_ready_once',
    'promote_stable', 'register_workspace', 'retry_workspace',
    'prune_worktree_metadata', 'submit_workspace',
]
