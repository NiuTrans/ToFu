"""Task-to-workspace lifecycle adapter; integration_control owns Git delivery.

Entry points: bind_worker_workspace before the runner, capture_workspace_evidence
before terminal tool state is released, and settle_task_workspaces at settlement.
Ordinary canonical roots execute directly by default; workspace_policy makes
isolation opt-in independent of Git publication. Dispatched file grants still
require isolated candidates. Existing owner/task registrations always retain
their binding and settlement authority. File/cwd
receipts are diagnostic, never proof of workspace ownership; no sibling inventory
infers ownership.
Runtime metadata caches the existing integration origin, not a second schema or
store. File/cwd arguments under the canonical root route to the bound checkout;
literal cross-checkout shell writes are refused, not rewritten. Opaque programs
remain cooperative: this adapter is not an all-tool filesystem sandbox.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
import subprocess
import time

from lib.git_runtime import git_executable
from lib.git_checkpoint_policy import FORBIDDEN_CHECKPOINT_ROOTS, git_policy_environment
from lib.log import get_logger

from lib.observability import observe_task_completion
logger = get_logger(__name__)
_MAX_EVIDENCE = 200
_SCHEMA = 'tofu.workspace-delivery/v1'


class WorkspaceBindingError(RuntimeError):
    """The requested execution root could not be safely bound."""


def _check_workspace_cancelled(task: dict) -> None:
    """Keep cooperative Stop distinct from a failed workspace binding."""
    event = task.get('abort_event')
    if task.get('aborted') or (event is not None and event.is_set()):
        from lib.llm.llm_errors import AbortedError
        raise AbortedError('Task cancelled during workspace admission')


def task_outcome(task: dict) -> str:
    """Only the canonical completed verdict can authorize publication."""
    from lib.turn_verdict import TurnStatus, derive_task_verdict

    verdict = derive_task_verdict(task)
    if verdict.cause == 'internal_fence_abort':
        return 'failed'
    if (task.get('aborted') or task.get('status') in {'aborted', 'cancelled'}
            or task.get('finishReason') in {'aborted', 'cancelled'}):
        return 'cancelled'
    if (task.get('status') in {'done', 'completed', 'success'}
            and verdict.status is TurnStatus.COMPLETED):
        return 'completed'
    return 'failed'


def _local_path(value) -> str:
    if not isinstance(value, str) or not value or value.startswith('remote:'):
        return ''
    return os.path.realpath(os.path.expanduser(value))


def _checkout(path: str) -> tuple[str, str] | None:
    """Read only the nearest repository, including for a deleted owned file."""

    if not path:
        return None
    candidate = Path(path)
    while not candidate.is_dir() and candidate != candidate.parent:
        candidate = candidate.parent
    # Avoid launching git for non-repositories. Do not walk sibling worktrees.
    if not any((parent / '.git').exists()
               for parent in (candidate, *candidate.parents)):
        return None
    # One repository observation and one subprocess instead of two serial
    # launches per identity check. Relative common-dir output remains relative
    # to the same candidate cwd, including linked and nested checkouts.
    result = subprocess.run(
        [git_executable(), '-c', 'core.fsmonitor=false', '-C', str(candidate),
         'rev-parse', '--show-toplevel', '--git-common-dir'],
        capture_output=True, text=True, timeout=10, check=False,
        env=git_policy_environment())
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:800]
        raise WorkspaceBindingError(
            f'Cannot identify workspace repository at {candidate}: {detail}')
    values = result.stdout.strip().splitlines()
    if len(values) != 2 or not all(values):
        raise WorkspaceBindingError('Incomplete workspace repository identity')
    root = _local_path(values[0])
    common = _local_path(os.path.join(str(candidate), values[1]))
    # Matches integration_control's canonical checkout identity. Bare/custom
    # git-dir layouts are not silently guessed into an unrelated project.
    if Path(common).name != '.git':
        raise WorkspaceBindingError('Unsupported canonical Git directory layout')
    return root, str(Path(common).parent)


def _read_only(path: str, task: dict) -> bool:
    path = _local_path(path)
    if not path:
        return False
    from lib.project_mod.config import build_root_policy, readonly_in_roots
    config = task.get('config') or {}
    primary = config.get('projectPath')
    if not primary:
        return False
    # Explicit writable descendants override readonly parents, exactly as at
    # write admission. Extra readonly fences stay conservative before binding.
    extras = list(config.get('projectPaths') or [])
    policy = build_root_policy(primary, extras, config.get('readOnlyPaths'))
    fences = build_root_policy(primary, list(dict.fromkeys([
        *extras, *(config.get('readOnlyPaths') or [])])), config.get('readOnlyPaths'))
    # Unregistered readonly ancestors are fences, not inferred writable roots.
    if readonly_in_roots(path, policy['roots'].values()):
        return True
    if not any(Path(path).is_relative_to(os.path.realpath(state['path']))
               for state in policy['roots'].values()):
        return readonly_in_roots(path, fences['roots'].values())
    return False


def _roots(task: dict, project_path: str = '', project_paths=None) -> list[str]:
    config = task.get('config') or {}
    values = [project_path or config.get('projectPath'),
              *(project_paths if project_paths is not None
                else config.get('projectPaths') or [])]
    return list(dict.fromkeys(p for value in values
                              if (p := _local_path(value)) and not _read_only(p, task)))


def _owner(task: dict) -> int:
    from lib.tasks_pkg.manager import task_user_id
    return int(task_user_id(task))


def _emit_workspace_preparing(task: dict) -> None:
    """Present expensive isolation only when it is actually about to happen."""
    if task.get('_workspacePreparing'):
        return
    task['_workspacePreparing'] = True
    try:
        from lib.agent_core.events import Phase, emit_phase
        emit_phase(
            task, Phase.WORKING,
            detail='Preparing the task workspace; capturing project input and creating an isolated checkout…',
            detailKey='stream.phase.workspacePreparing')
    except Exception as error:
        logger.debug('[WorkspaceDelivery] preparation phase emit failed: %s', error)


def _binding(path: str, task: dict, *, create=False) -> dict | None:
    from lib.integration_state_repository import find_workspace

    source = _checkout(path)
    if source:
        relative = Path(_local_path(path)).relative_to(source[0])
        if relative.parts and relative.parts[0] in FORBIDDEN_CHECKPOINT_ROOTS:
            return None
    if not source or _read_only(path, task):
        return None
    if _read_only(source[1], task):
        raise WorkspaceBindingError('Writable sub-root requires isolation from readonly canonical checkout')
    # create_workspace registers its clean task-created checkout before tools
    # receive it. Otherwise only explicit existing registration is authority;
    # a linked cwd, even with task-stamped writes, cannot claim a dirty peer.
    registered = find_workspace(source[1], str(task['id']), user_id=_owner(task))
    if registered is None:
        if source[0] != source[1]:
            raise WorkspaceBindingError('Linked workspace was not registered')
        if not create:
            return None
        from lib.project_mod.workspace_policy import resolve_workspace_mode
        from lib.project_dispatch.admission import requires_isolated_workspace
        try:
            mode = resolve_workspace_mode(source[1])
            # Dispatch file grants require an owned candidate and explicit
            # publication. This is an additional restriction, never authority;
            # consult roots are readonly and do not reach workspace creation.
            if requires_isolated_workspace(task):
                mode = 'isolated'
        except Exception as exc:
            raise WorkspaceBindingError(
                'Workspace mode admission failed: ' + str(exc)[:500]) from exc
        _check_workspace_cancelled(task)
        task.setdefault('_workspaceExecutionModes', {})[source[1]] = mode
        if mode == 'direct':
            return None
        from lib.integration_control import create_task_workspace
        _emit_workspace_preparing(task)
        create_task_workspace(source[1], str(task['id']), user_id=_owner(task),
            conversation_id=str(task.get('convId') or ''),
            title=str(task.get('lastUserQuery') or '')[:500], task=task)
        registered = find_workspace(source[1], str(task['id']), user_id=_owner(task))
        if registered is None:
            raise WorkspaceBindingError('New workspace registration was not acknowledged')
    workspace = _local_path(registered.get('workspace_path'))
    if _read_only(workspace, task):
        raise WorkspaceBindingError('Registered workspace is readonly; writable execution refused')
    actual = _checkout(workspace)
    if (not actual or actual[0] != workspace or actual[1] != source[1]
            or actual[0] == actual[1]):
        raise WorkspaceBindingError('Registered workspace is missing or mismatched')
    if source[0] != source[1] and source[0] != actual[0]:
        raise WorkspaceBindingError('Task already owns a different linked workspace')
    # Pass the canonical root: even a disappearing registration must not fall
    # through integration's linked-checkout adoption path.
    from lib.integration_control import bind_task_workspace
    result = bind_task_workspace(
        source[1], str(task['id']), user_id=_owner(task),
        conversation_id=str(task.get('convId') or ''),
        title=str(task.get('lastUserQuery') or '')[:500])
    if result is None:
        raise WorkspaceBindingError('Workspace registration disappeared during binding')
    if not isinstance(result, dict) or not all(
            result.get(key) for key in ('canonicalRoot', 'workspacePath', 'workId')):
        raise WorkspaceBindingError('Workspace binding returned incomplete identity')
    row = {key: str(result[key]) for key in (
        'canonicalRoot', 'workspacePath', 'workId')}
    durable_runtime = (registered.get('origin') or {}).get('_workspaceDelivery') or {}
    if isinstance(durable_runtime, dict):
        for key in ('verification', 'runtimePaths', 'runtimePrepared', 'publicationMode'):
            if key in durable_runtime:
                row[key] = durable_runtime[key]
    for cached in (task.get('_workspaceDelivery') or {}).get('repositories') or []:
        if cached.get('workspacePath') == row['workspacePath']:
            for key in ('verification', 'publicationMode'):
                if key not in row and cached.get(key):
                    row[key] = cached[key]
    actual = _checkout(row['workspacePath'])
    if (not actual or actual[0] != _local_path(row['workspacePath'])
            or actual[1] != _local_path(row['canonicalRoot'])
            or actual[0] == actual[1]):
        raise WorkspaceBindingError('Registered workspace is missing or mismatched')
    if _local_path(row['workspacePath']) != workspace:
        raise WorkspaceBindingError('Workspace registration changed during binding')
    if not source or source[1] != actual[1]:
        raise WorkspaceBindingError('Workspace belongs to another project')
    if source[0] != source[1] and source[0] != actual[0]:
        raise WorkspaceBindingError('Task already owns a different linked workspace')
    return row


def _persist(task: dict, row: dict) -> None:
    from lib.integration_state_repository import set_workspace_meta
    set_workspace_meta(
        user_id=_owner(task), project_root=row['canonicalRoot'],
        task_id=str(task['id']), patch={'_workspaceDelivery': row}, now=time.time())


def _remember(task: dict, rows: list[dict], *, errors=None) -> dict:
    payload = {'schema': _SCHEMA, 'repositories': rows}
    if errors:
        payload['errors'] = errors[:20]
    task['_workspaceDelivery'] = payload
    return payload


def bind_worker_workspace(task: dict) -> dict | None:
    """Bind every configured local root before tools can observe task config.

    Canonical writable Git roots execute directly unless isolation is selected.
    Existing registration remains the only ownership proof for linked roots.
    Any binding/persistence failure escapes to the worker's terminal error lane.
    Config changes are applied atomically after all roots and origins validate.
    """
    if task.get('_transientRuntime') or not task.get('id'):
        return None
    _check_workspace_cancelled(task)

    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    if coordinator_scope(task):
        # Planning/dispatch needs no checkout or filesystem execution workspace.
        return None
    config = task.get('config') or {}
    from lib.conversation_sync.workspace import provision_workspace
    from lib.observability import measure_task_startup
    identity = {'task_id': task['id'], 'conversation_id': task.get('convId'),
                'turn_id': task.get('_turnId'), 'attempt_id': task.get('_attemptId'),
                'user_id': task.get('_userId')}
    with measure_task_startup('workspace_provision', identity=identity):
        managed = provision_workspace(task)
    configured_scopes = [_local_path(value) for value in [config.get('projectPath'), *(config.get('projectPaths') or [])] if _local_path(value)]
    mappings = []
    rows = {}

    from lib.project_mod.config import build_root_policy, register_root_policy
    original_policy = build_root_policy(config['projectPath'],
        config.get('projectPaths'), config.get('readOnlyPaths')) if config.get('projectPath') else None
    with measure_task_startup('workspace_roots', identity=identity):
        writable_roots = [] if managed else _roots(task)
    for path in writable_roots:

        _check_workspace_cancelled(task)
        with measure_task_startup('workspace_identity', identity=identity):
            info = _checkout(path)
        if info is None:
            continue
        with measure_task_startup('workspace_root_binding', identity=identity):
            row = _binding(path, task, create=True)
        if row is None:
            continue
        previous = rows.get(row['canonicalRoot'])
        if previous and previous['workspacePath'] != row['workspacePath']:
            raise WorkspaceBindingError('Ambiguous workspaces for one task/project')
        row['status'] = 'bound'
        rows[row['canonicalRoot']] = row
        mappings.append((info[0], row['workspacePath'], row['canonicalRoot']))
    if not rows:
        _check_workspace_cancelled(task)
        if original_policy:
            task['_workspaceRootPolicy'] = original_policy
            register_root_policy(str(task['id']), original_policy)
        return None

    def mapped(value, *, canonical=False):
        path = _local_path(value)
        if not path:
            return value
        for source, workspace, root in sorted(mappings, key=lambda item: -len(item[0])):
            try:
                suffix = Path(path).relative_to(source)
            except ValueError:
                continue
            if suffix.parts and suffix.parts[0] in FORBIDDEN_CHECKPOINT_ROOTS:
                return value
            return str(Path(root if canonical else workspace) / suffix)
        return value

    with measure_task_startup('workspace_persist', identity=identity):
        for row in rows.values():
            _persist(task, row)  # persist before allowing any runner/tool write
    updated = dict(config)

    updated['workspacePublicationModes'] = [
        {'canonicalRoot': row['canonicalRoot'], **row['publicationMode']}
        for row in list(rows.values())[:_MAX_EVIDENCE] if row.get('publicationMode')]
    if config.get('projectPath'):
        updated.setdefault('canonicalProjectPath', mapped(config['projectPath'], canonical=True))
        updated['projectPath'] = mapped(config['projectPath'])
    if 'projectPaths' in config:
        updated.setdefault('canonicalProjectPaths', [mapped(p, canonical=True) for p in config['projectPaths']])
        updated['projectPaths'] = [mapped(p) for p in config['projectPaths']]
    if 'readOnlyPaths' in config:
        updated['readOnlyPaths'] = [mapped(p) for p in config['readOnlyPaths']]
    runtime_roots = []
    for row in rows.values():
        for name in sorted(FORBIDDEN_CHECKPOINT_ROOTS):
            candidate = _local_path(str(Path(row['canonicalRoot']) / name))
            if Path(candidate).is_dir() and any(Path(candidate).is_relative_to(scope) for scope in configured_scopes):
                runtime_roots.append(candidate)
    updated['workspaceRuntimeRoots'] = list(dict.fromkeys(runtime_roots))
    _check_workspace_cancelled(task)
    task['config'] = updated

    if original_policy:
        # Labels/access are root identities, not a private directory basename.
        policy = {'primaryPath': mapped(original_policy['primaryPath']), 'roots': {
            label: {**state, 'path': mapped(state['path'])}
            for label, state in original_policy['roots'].items()}}
        for runtime in updated.get('workspaceRuntimeRoots') or []:
            label = Path(runtime).name
            while label in policy['roots'] and policy['roots'][label]['path'] != runtime:
                label += '_runtime'
            if label in policy['roots']:
                continue
            from lib.project_mod.config import _make_root_state
            policy['roots'][label] = _make_root_state(runtime, access='ro' if _read_only(runtime, task) else 'rw')
        task['_workspaceRootPolicy'] = policy
        register_root_policy(str(task['id']), policy)
    return _remember(task, list(rows.values()))


def execution_repositories(task: dict) -> list:
    """Root delivery owns rows; a delegated child may borrow only their view."""
    return ((task.get('_workspaceDelivery') or {}).get('repositories') or
            (task.get('_workspaceExecution') or {}).get('repositories') or [])


def execution_root_key(task: dict) -> str:
    """Every physical task owns its policy, including deliberate empty roots."""
    return str(task.get('id') or task.get('convId') or '')


def ensure_execution_roots(task: dict) -> dict | None:
    """Rehydrate task-owned labels/access/additions; the LRU is not authority."""
    policy = task.get('_workspaceRootPolicy')
    if (not execution_repositories(task) and policy is None
            and not (task.get('config') or {}).get('projectPath')):
        # Empty selection is positive authority, not a miss in a shared cache.
        # Never clear another conversation's roots or borrow its prior binding.
        policy = {'primaryPath': '', 'roots': {}}
        task['_workspaceRootPolicy'] = policy
    from lib.project_mod.config import build_root_policy, register_root_policy
    if policy is None:
        config = task.get('config') or {}
        if not config.get('projectPath'):
            raise WorkspaceBindingError('Bound execution view lacks a root policy')
        policy = build_root_policy(config['projectPath'], config.get('projectPaths'),
                                   config.get('readOnlyPaths'))
        task['_workspaceRootPolicy'] = policy
    register_root_policy(execution_root_key(task), policy)
    return policy


def execution_root_snapshot(task: dict) -> list[dict]:
    """Project task-owned root identities and mappings without acquiring state.

    Context rendering borrows the admission policy; it never infers that an
    unbound non-Git root moved merely because another root was isolated.
    """
    config = task.get('config') or {}
    policy = task.get('_workspaceRootPolicy')
    if policy is None:
        if not config.get('projectPath'):
            return []
        from lib.project_mod.config import build_root_policy
        policy = build_root_policy(config['projectPath'], config.get('projectPaths'),
                                   config.get('readOnlyPaths'))
    repositories = sorted(execution_repositories(task),
                          key=lambda item: -len(item['workspacePath']))
    result = []
    for label, state in policy['roots'].items():
        path = state['path']
        canonical = path
        for row in repositories:
            try:
                suffix = Path(path).relative_to(row['workspacePath'])
            except ValueError:
                continue
            canonical = str(Path(row['canonicalRoot']) / suffix)
            break
        result.append({'label': label, 'path': path, 'canonicalPath': canonical,
                       'access': state.get('access', 'rw'),
                       'primary': path == policy['primaryPath']})
    return result


def route_project_tool_args(task: dict, name: str, arguments: dict) -> dict:
    """Map explicit canonical paths to the task's acknowledged input checkout.

    Relative paths already use bound config. Shell programs are not rewritten;
    their literal cross-checkout writes are rejected separately at admission.
    """
    rows = execution_repositories(task)
    if not rows:
        return arguments
    def mapped(value):
        if not isinstance(value, str):
            return value
        if value.startswith('file://'):
            value = value[7:]
        if not value.startswith(('/', '~')):
            primary = (task.get('config') or {}).get('canonicalProjectPath')
            if primary and ':' not in value:
                candidate = _local_path(os.path.join(primary, value))
                if any(Path(candidate).is_relative_to(root) for root in (task.get('config') or {}).get('workspaceRuntimeRoots') or []):
                    return candidate
            if ':' in value:
                label, relative = value.split(':', 1)
                policy = ensure_execution_roots(task)
                roots = policy['roots'] if policy is not None else {}
                # Current registered namespaces (including readonly references)
                # always win; never shadow them with a basename replay alias.
                for name, state in roots.items():
                    if name.lower() == label.lower():
                        candidate = Path(state['path']) / relative.lstrip('/')
                        for row in rows:
                            if state['path'] == row['workspacePath'] and Path(relative).parts and Path(relative).parts[0] in FORBIDDEN_CHECKPOINT_ROOTS:
                                return str(Path(row['canonicalRoot']) / relative.lstrip('/'))
                        return str(candidate)
                candidates = [row for row in rows if
                    Path(row['canonicalRoot']).name.lower() == label.lower()]
                if len(candidates) > 1:
                    from lib.project_mod.config import UnknownWorkspaceRootError
                    raise UnknownWorkspaceRootError(f'Ambiguous workspace root alias: {label}')
                if len(candidates) == 1:
                    return str(Path(candidates[0]['workspacePath']) / relative.lstrip('/'))
            return value
        target = Path(_local_path(value))
        if any(target.is_relative_to(row['workspacePath']) for row in rows):
            return value
        for row in sorted(rows, key=lambda item: -len(item['canonicalRoot'])):
            try:
                suffix = target.relative_to(row['canonicalRoot'])
            except ValueError:
                continue
            if suffix.parts and suffix.parts[0] in FORBIDDEN_CHECKPOINT_ROOTS:
                return value
            return str(Path(row['workspacePath']) / suffix)
        return value
    result = dict(arguments)
    for key in ('path', 'working_dir'):
        if key in result:
            result[key] = mapped(result[key])
    for key in ('reads', 'edits', 'searches'):
        if isinstance(result.get(key), list):
            result[key] = [dict(item, path=mapped(item.get('path'))) if
                isinstance(item, dict) and 'path' in item else item for item in result[key]]
    return result


@observe_task_completion('workspace_evidence')
def capture_workspace_evidence(task: dict, project_path: str = '', project_paths=None) -> dict:
    """Capture bounded plain data synchronously; never retain tool transcripts."""
    roots = _roots(task, project_path, project_paths)
    # The presentation list can include timestamp-fallback / continued-turn
    # facts. Exact taskId journal stamps retain diagnostics, not ownership.
    from lib.project_mod.modifications import get_task_modification_paths
    previous = task.get('_workspaceDeliveryEvidence') or {}
    files = [dict(fact) for fact in (previous.get('files') or [])[:_MAX_EVIDENCE]]
    for root in roots:
        if len(files) >= _MAX_EVIDENCE:
            break
        files.extend(get_task_modification_paths(
            root, task.get('id'), conv_id=task.get('convId'),
            limit=_MAX_EVIDENCE - len(files),
            exclude=((fact['path'], fact['rootPath']) for fact in files)))
    cwd_paths = list(previous.get('workingDirs') or [])[:_MAX_EVIDENCE]
    for record in task.get('toolRounds') or ():
        if not isinstance(record, dict) or record.get('toolName') != 'run_command':
            continue
        # Successful cwd receipts are retained for diagnostics only. Even
        # a successful write command cannot establish workspace ownership.
        if record.get('status') != 'done':
            continue
        args = record.get('toolArgs') or {}
        if isinstance(args, str):
            if len(args) > 65536:
                continue
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if not isinstance(args, dict):
            continue
        cwd = args.get('working_dir')
        if isinstance(cwd, str) and os.path.isabs(cwd):
            cwd_paths.append(cwd)
        if len(cwd_paths) >= _MAX_EVIDENCE:
            break
    evidence = {'roots': roots or previous.get('roots') or [], 'files': files,
                'workingDirs': list(dict.fromkeys(cwd_paths))[:_MAX_EVIDENCE]}
    task['_workspaceDeliveryEvidence'] = evidence
    return evidence


def settle_task_workspaces(task: dict, project_path: str = '', project_paths=None,
                           *, evidence: dict | None = None) -> dict | None:
    """Settle selected workspaces only; failed/cancelled never authorize release.

    Integration owns idempotency, terminal intent and restart recovery. Failures
    here become explicit receipt data, not a successful-delivery assertion.
    """
    if (task.get('_transientRuntime') or not task.get('id')
            or task.get('_workspaceDeliveryOwnershipRejected')):
        return None
    outcome = task_outcome(task)
    if task.get('_workspaceDeliveryAcknowledgedOutcome') == outcome:
        return task.get('_workspaceDelivery')
    # Intent admission needs roots and explicit owner/task registrations.
    # Diagnostics are captured by the round commit owner before tool release;
    # restarting must not reload project journals once per orphaned task.
    if evidence:
        candidate_roots = evidence['roots']
    else:
        candidate_roots = (_roots(task, project_path, project_paths)
                           or (task.get('_workspaceDeliveryEvidence') or {}).get('roots')
                           or [])
    roots = [path for path in candidate_roots if not _read_only(path, task)]
    if not roots:
        return None
    previous = task.get('_workspaceDelivery') or {}
    known = {row['canonicalRoot']: dict(row) for row in previous.get('repositories') or []}
    errors = []
    # Registration, not file writes or successful cwd visits, selects the
    # task's checkout. Shell-created unregistered worktrees remain untouched.
    settled = set()
    for path in roots:
        try:
            info = _checkout(path)
            if not info or info[1] in settled:
                continue

            settled.add(info[1])  # never retry a rejected path via canonical fallback
            row = _binding(path, task)
            if row is None:
                continue
            root = row['canonicalRoot']
            settled.add(root)
            # Retain the identity even if the checkpoint/storage operation fails.
            known[root] = row
            row.update(status='pending', outcome=outcome)
            from lib.integration_control import settle_task_workspace
            result = settle_task_workspace(
                row['canonicalRoot'], str(task['id']), user_id=_owner(task), outcome=outcome)
            if not isinstance(result, dict):
                raise WorkspaceBindingError('Registered workspace settlement returned no receipt')
            result = result.get('delivery') or result
            if not isinstance(result, dict) or not (result.get('state') or result.get('status')):
                raise WorkspaceBindingError('Registered workspace settlement returned incomplete receipt')
            # Bounded receipt only: integration origin has a fixed document budget.
            row['status'] = 'intent_recorded'
            row['deliveryStatus'] = str(result.get('state') or result.get('status'))[:500]
            for key in ('status', 'deliveryStatus', 'checkpointSha', 'candidateSha',
                        'stableSha', 'targetSha', 'sourceSha', 'phase', 'reason', 'error', 'detail'):
                if key in result:
                    row[key] = str(result[key])[:500]
            if result.get('publicationMode') == 'working_tree':
                row['deliveryPublicationMode'] = 'working_tree'
            _persist(task, row)
        except Exception as exc:
            errors.append({'path': path, 'status': 'error', 'detail': str(exc)[:500]})
            logger.warning('[WorkspaceDelivery] task=%s path=%s: %s', task['id'], path, exc)
    if not errors:
        task['_workspaceDeliveryAcknowledgedOutcome'] = outcome
    if not known and not errors:
        return None
    return _remember(task, list(known.values()), errors=errors)


def persist_terminal_workspace_intent(task: dict) -> bool:
    """Required completion receiver: false keeps the existing eviction fence.

    Run after executor ownership is acknowledged and before heavy evidence is
    released. Integration synchronously records intent and attempts automatic workspace
    merging before returning; its worker retries delivery debt. Maintenance uses the ordinary
    terminal-persistence lane, rather than creating another queue or store.
    """
    try:
        receipt = settle_task_workspaces(task)
        if receipt and receipt.get('errors'):
            raise WorkspaceBindingError('Workspace terminal intent remains unacknowledged')
    except Exception as exc:
        task['_workspaceDeliveryIntentPending'] = True
        task['_terminalPersistencePending'] = True
        task['_terminalPersistenceRetryReady'] = True
        logger.warning('[WorkspaceDelivery] terminal intent pending task=%s: %s',
                       task.get('id', '?'), exc)
        return False
    task.pop('_workspaceDeliveryIntentPending', None)
    return True


def work_result_summary(task: dict) -> str:
    """Truthful compact addendum; execution completion is not delivery."""
    receipt = task.get('_workspaceDelivery') or {}
    rows = receipt.get('repositories') or []
    if not rows and not receipt.get('errors'):
        return ''
    statuses = ', '.join(sorted({str(row.get('deliveryStatus') or row.get('status') or 'pending') for row in rows}))
    if receipt.get('errors'):
        statuses = (statuses + ', error').strip(', ')
    summary = 'Workspace delivery: ' + statuses + ' (task outcome is not delivery confirmation).'
    if any(row.get('deliveryStatus') == 'delivered' and row.get('deliveryPublicationMode') == 'working_tree' for row in rows):
        summary += ' Changes are merged into the current workspace; Git commit history is independent.'
    reasons = sorted({str(mode['reason'])[:500] for row in rows
                      if isinstance(mode := row.get('publicationMode'), dict)
                      and mode.get('requested') and not mode.get('eligible')
                      and mode.get('reason')})[:3]
    if reasons:
        summary += ' Publication mode: isolated-task (' + '; '.join(reasons)[:500] + ').'
    return summary


def canonical_work_path(task: dict, project_path: str) -> str:
    path = _local_path(project_path)
    for row in (task.get('_workspaceDelivery') or {}).get('repositories') or []:
        for root in (row['workspacePath'], row['canonicalRoot']):
            try:
                Path(path).relative_to(root)
            except ValueError:
                continue
            return row['canonicalRoot']
    return project_path


def checkpoint_roots_without_workspaces(paths: list[str], receipt: dict | None) -> list[str]:
    """Route confirmed working-tree delivery to the existing opt-in checkpoint.

    The checkpoint owner still decides opt-in, gates and coalesced-workspace
    authority. Pending/failed or explicit candidate delivery cannot borrow it.
    A reclaimed source path is replaced using the durable canonical binding.
    """
    if not receipt:
        return paths
    if receipt.get('errors') or receipt.get('status') == 'error':
        return []
    rows = receipt.get('repositories') or []
    isolated = {row['canonicalRoot'] for row in rows}
    delivered = {row['canonicalRoot'] for row in rows
                 if row.get('deliveryStatus') == 'delivered'
                 and row.get('deliveryPublicationMode') == 'working_tree'}
    bindings = {root: row['canonicalRoot'] for row in rows
                for root in (row['canonicalRoot'], row['workspacePath'])}
    result = []
    for path in paths:
        local = _local_path(path)
        canonical = bindings.get(local)
        if canonical is None:
            info = _checkout(local)
            canonical = info[1] if info else None
        if canonical in delivered:
            result.append(canonical)
        elif canonical not in isolated:
            result.append(path)
    return list(dict.fromkeys(result))


def workspace_target_refusal(task: dict, path: str) -> str:
    """A symlink/cwd receipt cannot authorize a canonical or peer write."""
    rows = execution_repositories(task)
    if not rows:
        return ''

    from lib.project_mod.config import readonly_in_roots, _scoped_root_policy
    view = _scoped_root_policy(execution_root_key(task))
    roots = view[2] if view is not None else ensure_execution_roots(task)['roots']
    if readonly_in_roots(_local_path(path), roots.values()):
        return 'workspace-isolation: target is a READ-ONLY root; no write was performed'
    resolved = _local_path(path)
    if any(Path(resolved).is_relative_to(root) for root in (task.get('config') or {}).get('workspaceRuntimeRoots') or []):
        return ''
    info = _checkout(resolved)
    owned = {row['canonicalRoot']: row['workspacePath'] for row in rows}
    if info and info[1] in owned and info[0] != owned[info[1]]:
        return ('workspace-isolation: target is a canonical or peer checkout, '
                'not this task-owned worktree; no write was performed')
    return ''


def command_workspace_refusal(task: dict, command: str, cwd: str) -> str:
    """Refuse observable shell writes outside this task's owned checkout.

    This is a literal-target admission check, not a shell/interpreter sandbox.
    Unknown interpreter internals retain existing approval/trust boundaries.
    """
    rows = execution_repositories(task)
    if not rows:
        return ''
    from lib.project_mod.command_analysis import (
        _command_checkout_mutation, command_segment_literal_targets, _split_pipeline,
        _shell_words, _unwrap_command_parts, _ENV_ASSIGNMENT_GUARD_RE,
    )
    active = _local_path(cwd)
    def forbidden(path):
        return bool(workspace_target_refusal(task, path))
    def refusal():
        return ('Error [workspace-isolation]: command writes a canonical or peer '
                'checkout or READ-ONLY reference instead of a writable task-owned worktree. '
                'Set working_dir to the bound worktree and use relative targets; command was not started.')
    for segment in _split_pipeline(command or ''):
        words = [word.text for word in _shell_words(segment)]
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        words = _unwrap_command_parts(words)
        if not words:
            continue
        if words[0] == 'cd' and len(words) == 2:
            active = _local_path(os.path.join(active, os.path.expanduser(words[1])))
            continue
        target_cwd = active
        if os.path.basename(words[0]) == 'git':
            for index, word in enumerate(words[1:], 1):
                if word in ('-C', '--work-tree') and index + 1 < len(words):
                    target_cwd = _local_path(os.path.join(target_cwd, words[index + 1]))
                elif word.startswith('--work-tree='):
                    target_cwd = _local_path(os.path.join(target_cwd, word.split('=', 1)[1]))
        if os.path.basename(words[0]) == 'git' and _command_checkout_mutation(segment, target_cwd):
            repository = _checkout(target_cwd)
            if repository:
                target_cwd = repository[0]
        if _command_checkout_mutation(segment, target_cwd) and forbidden(target_cwd):
            return refusal()
        # Snapshot completeness is not admission evidence: opaque builds can
        # still redirect into a known foreign checkout. Preserve source-aware
        # literal targets (including quoted dollar signs) and expansion parents.
        for target in command_segment_literal_targets(segment):
            path = os.path.join(target_cwd, os.path.expanduser(target))
            if forbidden(path):
                return refusal()
    return ''


def rebind_project_tool_arguments(task: dict, arguments: dict) -> dict:
    """Use the acknowledged execution view and its frozen namespace authority."""
    ensure_execution_roots(task)
    return route_project_tool_args(task, '', arguments)


def prepare_worker_command_runtime(task: dict) -> None:
    """Load ignored dependencies without excluding unrelated source file edits."""
    from lib.project_mod.workspace_lease import workspace_runtime_setup_lease
    from lib.project_mod.tool_admission import Cancellation
    from lib.project_mod.workspace_runtime import prepare_workspace_runtime
    from lib.project_mod.abs_path_guard import task_is_remote
    owned_rows = (task.get('_workspaceDelivery') or {}).get('repositories') or []
    rows = execution_repositories(task)
    inherited = bool(rows and not owned_rows)
    if not rows:
        from lib.tasks_pkg.invocation import is_child_invocation
        config = task.get('config') or {}
        if is_child_invocation(task) and config.get('canonicalProjectPath'):
            source = _checkout(config['canonicalProjectPath'])
            selected = _checkout(config.get('projectPath') or '')
            if source and selected and source[1] == selected[1] and source[0] != selected[0]:
                inherited = True
                rows = [{'canonicalRoot': source[0], 'workspacePath': selected[0]}]
    for row in rows:
        if row['workspacePath'] in (task.get('_workspaceRuntimePrepared') or []):
            continue
        if row.get('runtimePrepared'):
            continue
        with workspace_runtime_setup_lease(row['workspacePath'], timeout=5.0,
                                           cancel_event=Cancellation(task)):
            if row.get('runtimePrepared'):
                continue
            paths = prepare_workspace_runtime(row['canonicalRoot'], row['workspacePath'],
                                              restricted=task_is_remote(task))
            task.setdefault('_workspaceRuntimePrepared', []).append(row['workspacePath'])
            if paths and not inherited:
                row['runtimePaths'] = paths
                row['runtimePrepared'] = True
                _persist(task, row)



def record_worker_verification(task: dict, arguments: dict, project_path: str, metadata: dict) -> None:
    """Keep one bounded, successful declared verifier per owned worktree.

    This is a replayable gate command, not a claim that subsequent edits were
    tested. Publication reruns it against the exact merged target tree.
    """
    command = arguments.get('command') or ''
    if (type(metadata.get('exitCode')) not in (int, str)
            or metadata.get('exitCode') not in (0, '0') or metadata.get('timedOut')
            or metadata.get('backgrounded') or len(command) > 2000
            or re.search(r'[\r\n$`;&|<>]', command)):
        return
    try:
        argv = shlex.split(command)
    except ValueError:
        return
    if not argv or len(argv) > 128 or any(re.match(
            r'--?(?:api[-_]?key|token|password|secret|authorization)(?:=|$)', arg, re.I) for arg in argv):
        return
    if any(marker in arg for arg in argv for marker in ('{workspace}', '{base}', '{target}')):
        return
    executable = Path(argv[0]).name
    declared = (executable in {'pytest', 'py.test'}
                or (re.fullmatch(r'python(?:3(?:\.[0-9]+)?)?', executable)
                    and argv[1:3] in (['-m', 'pytest'], ['-m', 'unittest']))
                or (executable in {'npm', 'pnpm', 'yarn'} and len(argv) > 1
                    and (argv[1] == 'test' or (argv[1] == 'run' and len(argv) > 2
                         and re.match(r'^(?:test|check|build|typecheck)(?:$|[:_-])', argv[2]))))
                or (executable == 'cargo' and argv[1:2] == ['test'])
                or (executable == 'node' and len(argv) > 1
                    and (argv[1] == '--test' or (argv[1].startswith('tests/')
                         and Path(argv[1]).suffix in {'.js', '.mjs', '.cjs'}))))
    if not declared:
        return
    from lib.project_mod.config import get_conv_cwd
    cwd = arguments.get('working_dir') or get_conv_cwd(execution_root_key(task)) or project_path
    cwd = _local_path(os.path.join(project_path, cwd))
    for row in (task.get('_workspaceDelivery') or {}).get('repositories') or []:
        workspace = row['workspacePath']
        try:
            relative = Path(cwd).relative_to(workspace)
        except ValueError:
            continue
        rebased = [('{workspace}' + arg[len(workspace):])
                   if arg == workspace or arg.startswith(workspace + '/')
                   else arg.replace('=' + workspace + '/', '={workspace}/', 1) for arg in argv]
        row['verification'] = {'command': shlex.join(rebased), 'directory': str(relative),
                               'taskId': str(task['id']), 'ownerUserId': _owner(task)}
        _persist(task, row)
        return
