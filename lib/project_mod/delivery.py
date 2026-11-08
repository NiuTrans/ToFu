"""Task-owned delivery of registered worktrees, through integration authority.

The existing workspace row/origin is the only durable owner. Explicit candidate
validation/adoption and automatic task-delta publication use the same intent
and recovery receiver; no detached job ledger or second publication authority. Failed/cancelled tasks preserve, never auto-publish.
External processes not using workspace_write_lease remain an explicit limit.
"""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import threading
from typing import Any

from lib import integration_state_repository as state
from lib.git_runtime import GIT_STALL_SECONDS

from lib.log import get_logger

logger = get_logger(__name__)

_RECOVERY_CURSOR = 0
_DELIVERY_KEY = 'delivery'
_RETRYABLE_CAPTURE_PHASE = 'checkpoint_spawn_retry'
# Pass state, owned by the worker thread: a recovery pass also rechecks
# retained sources; rows seen by the current or last pass keep the next tick due.
_RECOVERY_RETAINED = False
_ACTION_OUTSTANDING = False
# Requests arrive from committing threads and the worker's error paths.
_SCAN_LOCK = threading.Lock()
_SCAN_REQUESTED = False
_RECOVERY_REQUESTED = False

# Commands whose effects the resume pass reads: receipts and acknowledgements
# (set_meta), registration, and the row states pending deliveries wait on.
DELIVERY_STATE_OPERATIONS = (
    'integration.workspace.register',
    'integration.workspace.set_meta',
    'integration.workspace.save_checkpoint',
    'integration.workspace.submit',
    'integration.workspace.retry',
    'integration.workspace.discard',
    'integration.workspace.mark_merged',
    'integration.workspace.mark_failed',
    'integration.workspace.quarantine',
)


def _control():
    from lib import integration_control
    return integration_control


def _canonical(path: str) -> tuple[Path, Path]:
    ic = _control()
    raw = str(path or '').strip()
    if not raw:
        raise ic.IntegrationError('No project path is active')
    candidate = Path(raw).expanduser().resolve()
    if not candidate.exists():
        raise ic.IntegrationError(f'Project path does not exist: {candidate}')
    # One observation binds both values to the same cwd/environment. Do not
    # reuse this identity across calls or publication admission boundaries.
    # Identity reads wait on the volume, not on the work: no wall-clock deadline.
    values = ic._git(candidate, ['rev-parse', '--show-toplevel', '--git-common-dir'],
                     check=True, stall_seconds=GIT_STALL_SECONDS).stdout.strip().splitlines()
    if len(values) != 2 or not all(values):
        raise ic.IntegrationError('Incomplete workspace repository identity')
    workspace = Path(values[0]).resolve()
    common = (candidate / values[1]).resolve()
    listing = ic._git(workspace, ['worktree', 'list', '--porcelain'], check=True,
                      stall_seconds=GIT_STALL_SECONDS).stdout
    first = next((line[9:] for line in listing.splitlines()
                  if line.startswith('worktree ')), '')
    if not first:
        raise ic.IntegrationError('Git did not identify a canonical worktree')
    root = Path(first).resolve()
    # Identical resolved paths have identical repository identity. A linked
    # destination still requires an independent canonical-repository check.
    if root != workspace and ic._common_dir(root) != common:
        raise ic.IntegrationError('Workspace and canonical repository differ')
    return root, workspace


def _receipt(row: dict) -> dict:
    return dict((row.get('origin') or {}).get(_DELIVERY_KEY) or {})


def _save(row: dict, delivery: dict, **updates: Any) -> dict:
    ic = _control()
    value = dict(delivery, **updates)
    previous = _receipt(row)
    if {k: v for k, v in previous.items() if k != 'updatedAt'} == {
            k: v for k, v in value.items() if k != 'updatedAt'}:
        return previous
    value['updatedAt'] = ic._now()
    # Unlike optional narrative metadata, intent is publication authority.
    # A failed durable write must prevent checkpoint/submission/adoption.
    origin = state.set_workspace_meta(
        project_root=row['project_root'], task_id=row['task_id'],
        patch={_DELIVERY_KEY: value}, user_id=int(row['user_id']), now=ic._now())
    value = dict(origin[_DELIVERY_KEY])
    row['origin'] = origin
    ic._push(row['project_root'], user_id=int(row['user_id']))
    retry = value.get('retry') or {}
    if retry and retry != previous.get('retry'):
        # Optional project projection after durable intent. Narrative is
        # diagnostic context for authorized repair, never a new model task.
        try:
            from lib.conversations.project_brain import record_integration_failure
            record_integration_failure(
                row['project_root'], work_id=row['task_id'], user_id=int(row['user_id']),
                reason=f"{value.get('reason', '')}; retry={retry['condition']}; "
                       f"baseline={retry['baselineSha']}; checkpoint={retry['checkpointSha']}; "
                       f"canonical={retry['sourceSha']}; conflicts={retry.get('conflictPathCount', 0)}")
        except Exception:
            from lib.log import get_logger
            get_logger(__name__).warning('Delivery repair projection pending', exc_info=True)
    return value


def _transient_git_spawn_error(error: BaseException) -> bool:
    import errno
    cause = error.__cause__
    return (isinstance(cause, OSError)
            and cause.errno in {errno.EAGAIN, errno.EINTR, errno.ENOMEM})


def _legacy_transient_capture_receipt(receipt: dict) -> bool:
    reason = str(receipt.get('reason') or '')
    return (receipt.get('state') == 'blocked' and not receipt.get('retry')
            and reason.startswith('git ')
            and any(label in reason for label in (
                '[Errno 11]', '[Errno 4]', '[Errno 12]')))


def _publication_hold(row: dict, receipt: dict) -> dict | None:
    from lib.project_mod.task_delta_delivery import checkpoint_has_no_delta
    if receipt.get('taskOutcome') == 'completed' and checkpoint_has_no_delta(row):
        return None
    from lib.project_dispatch.publication import publication_reason
    reason = publication_reason(row, receipt)
    if reason:
        return _save(row, receipt, state='blocked', phase='workspace_adoption',
                     reason=reason, retry=None)
    return None


def release_publication_hold(project_path: str, task_id: str, *, user_id: int) -> dict:
    """Mark a publication hold pending once its live fence clears; never publish.

    The caller holds repository admission, as the publication pin does. A hold
    written before the pin otherwise stays visible as a refusal until the
    receiver's next pass; the receiver still delivers and rechecks every fence.
    """
    ic = _control()
    row = state.get_workspace(str(ic._repo_root(project_path)), str(task_id), user_id=user_id)
    receipt = _receipt(row)
    if (receipt.get('state') != 'blocked' or receipt.get('taskOutcome') != 'completed'
            or not str(receipt.get('reason') or '').startswith('project_publication_')):
        return receipt
    from lib.project_dispatch.publication import publication_reason
    if publication_reason(row, receipt):
        return receipt
    return _save(row, receipt, state='pending', reason='')


def _wake_delivered_dependents(row: dict, receipt: dict | None) -> None:
    """Notify from the receiver's unlocked return, never from its save callback.

    Ref publication is insufficient. A successful delivered ACK or a durable
    delivered reread after uncertain ACK/crash is required. Readiness still
    checks exact task association, checkpoint, review and publication pins.
    """
    from lib.project_dispatch.admission import KEY
    binding = (row.get('origin') or {}).get(KEY)
    if not isinstance(binding, dict) or not receipt or receipt.get('state') != 'delivered':
        return
    try:
        latest = state.get_workspace(row['project_root'], row['task_id'], user_id=int(row['user_id']))
        if (_receipt(latest).get('state') != 'delivered'
                or (latest.get('origin') or {}).get(KEY) != binding
                or binding['owner'] != latest['user_id']
                or binding['projectPath'] != latest['project_root']):
            return
        from lib.project_dispatch.recovery import wake_project_dependents
        wake_project_dependents(binding['owner'], binding['projectPath'], binding['assignmentId'])
    except Exception:
        from lib.log import get_logger
        get_logger(__name__).warning('Delivered dependency wake retained for recovery task=%s',
                                     row['task_id'], exc_info=True)


def _wake_workspace_dependents(project_path: str, task_id: str, user_id: int,
                               receipt: dict | None) -> None:
    if not receipt or receipt.get('state') != 'delivered':
        return
    try:
        root = str(_control()._repo_root(project_path))
        row = state.get_workspace(root, str(task_id), user_id=user_id)
        _wake_delivered_dependents(row, receipt)
    except Exception:
        from lib.log import get_logger
        get_logger(__name__).warning('Delivered dependency wake read retained for recovery task=%s',
                                     task_id, exc_info=True)


def bind_task_workspace(project_path: str, task_id: str, *, user_id: int,
                        conversation_id: str = '', title: str = '') -> dict | None:
    """Register only the linked checkout actually selected by this task.

    Never silently move a task away from dirty canonical context. Existing
    registration wins; a canonical checkout without registration is unchanged.
    """
    ic = _control()
    user_id = ic._require_user_id(user_id)
    root, workspace = _canonical(project_path)
    row = state.find_workspace(str(root), str(task_id), user_id=user_id)
    if row is None:
        if root == workspace:
            return None
        # Storage claims this normalized physical checkout atomically across
        # users. UI status pagination is neither ownership nor authorization.
        target_head = ic._rev(root, 'HEAD')
        workspace_head = ic._rev(workspace, 'HEAD')
        base = ic._git(root, ['merge-base', target_head, workspace_head], check=True,
                       stall_seconds=GIT_STALL_SECONDS).stdout.strip()
        state.register_workspace(
            project_root=str(root), task_id=str(task_id), title=title or str(task_id),
            workspace_path=str(workspace), base_sha=base,
            managed=False, user_id=user_id, now=ic._now())
        row = state.get_workspace(str(root), str(task_id), user_id=user_id)
    actual = Path(row['workspace_path']).resolve()
    if not actual.is_dir() or ic._common_dir(actual) != ic._common_dir(root):
        raise ic.IntegrationError('Registered workspace is unavailable; refusing wrong-root fallback')
    delivery = _receipt(row)
    if not delivery:
        origin = row.get('origin') or {}
        branch = (origin.get('sourceRef') if origin.get('executionMode') == 'isolated-task'
                  else ic._git(root, ['symbolic-ref', '-q', 'HEAD'],
                               stall_seconds=GIT_STALL_SECONDS).stdout.strip())
        delivery = _save(row, {}, state='working', targetRef=branch,
                         baseHead=ic._rev(root, 'HEAD'), conversationId=conversation_id,
                         reason='', taskOutcome='')
    return {'canonicalRoot': str(root), 'workspacePath': str(actual),
            'workId': str(task_id), 'delivery': delivery}


def settle_task_workspace(project_path: str, task_id: str, *, user_id: int,
                          outcome: str) -> dict | None:
    """Persist intent and immediately deliver automatic tasks before returning."""
    result = _settle_task_workspace(project_path, task_id, user_id=user_id, outcome=outcome)
    _wake_workspace_dependents(project_path, task_id, user_id, result)
    return result


def record_task_workspace_intent(project_path: str, task_id: str, *, user_id: int,
                                  outcome: str) -> dict | None:
    """Acknowledge restart debt; the lifecycle worker owns capture and delivery."""
    return _settle_task_workspace(
        project_path, task_id, user_id=user_id, outcome=outcome, record_only=True)


def _settle_task_workspace(project_path: str, task_id: str, *, user_id: int,
                           outcome: str, record_only: bool = False) -> dict | None:
    """Persist intention BEFORE checkpoint/submit, so restart can finish it.

    A restart pause (``interrupted``) owes nothing yet: the receipt stays
    ``working`` and nothing is captured, preserved or published until a later
    attempt of the same task settles it. Failed/cancelled/stopped remain sticky;
    both storage authorities refuse to change a recorded denial.
    """
    ic = _control()
    from lib.tasks_pkg.workspace_delivery import RESTART_INTERRUPTED
    if outcome not in {'completed', 'failed', 'cancelled', 'stopped', RESTART_INTERRUPTED}:
        raise ic.IntegrationError('Unknown task outcome; publication denied')
    # A delivered automatic task may have reclaimed its source clone after
    # durable completion. Terminal receipts remain idempotent without opening
    # that removed checkout again.
    canonical, _selected = _canonical(project_path)
    existing = state.find_workspace(str(canonical), str(task_id), user_id=user_id)
    if existing and _receipt(existing).get('state') in {'delivered', 'preserved'}:
        if _receipt(existing).get('state') == 'delivered':
            held = _publication_hold(existing, _receipt(existing))
            if held is not None:
                return held
            if not record_only:
                _reclaim_completed_source(existing)
        return _receipt(existing)
    bound = bind_task_workspace(project_path, task_id, user_id=user_id)
    if bound is None:
        return None
    # Serializes revocation with candidate/adoption ref linearization.
    with ic._repo_lock(bound['canonicalRoot']):
        row = state.get_workspace(bound['canonicalRoot'], str(task_id), user_id=user_id)
        receipt = _receipt(row)
        if receipt.get('state') in {'delivered', 'preserved'}:
            if receipt.get('state') == 'delivered':
                held = _publication_hold(row, receipt)
                if held is not None:
                    return held
            return receipt
        previous = receipt.get('taskOutcome')
        original_outcome = previous if previous in {'failed', 'cancelled', 'stopped'} else outcome
        if original_outcome == RESTART_INTERRUPTED:
            if receipt.get('state') != 'working' or previous != RESTART_INTERRUPTED:
                # A capture taken before the pause predates the resumed work.
                receipt = _save(row, receipt, state='working', taskOutcome=RESTART_INTERRUPTED,
                                checkpointSha='', reason='')
            return receipt
        if receipt.get('taskOutcome') != original_outcome or receipt.get('state') == 'working':
            receipt = _save(row, receipt, state='pending', phase='checkpoint',
                            taskOutcome=original_outcome, reason='')
    if not record_only and (row.get('origin') or {}).get('executionMode') == 'isolated-task':
        result = _settle_registered(row, receipt)
        if result.get('state') not in {'delivered', 'preserved'}:
            ic._start_or_wake_worker()
        return result
    ic._start_or_wake_worker()
    return receipt


def retry_task_delivery(project_path: str, task_id: str, *, user_id: int) -> dict:
    """Explicit retry of an owned frozen automatic checkpoint, never candidate.

    A repair checkpoints its registered source through the existing API first.
    Retry cannot upgrade a cancelled/failed task outcome, recapture a live writer,
    reopen terminal delivery or transfer another task's publication authority.
    """
    ic = _control()
    owner = ic._require_user_id(user_id)
    root = str(ic._repo_root(project_path))
    def admitted(row, receipt):
        return ((row.get('origin') or {}).get('executionMode') == 'isolated-task'
                and receipt.get('taskOutcome') == 'completed'
                and receipt.get('state') not in {'delivered', 'preserved'}
                and row['state'] != 'discarded' and bool(row.get('checkpoint_sha')))

    with ic._repo_lock(root):
        row = state.get_workspace(root, task_id, user_id=owner)
        receipt = _receipt(row)
        if not admitted(row, receipt):
            raise ic.IntegrationError('Only an unpublished completed owned task checkpoint can retry delivery')
        captured = _checkpoint_identity(row)
    from lib.project_mod.delivery_retry import prepare_conflict_repair
    repair = prepare_conflict_repair(row, receipt)
    with ic._repo_lock(root):
        latest = state.get_workspace(root, task_id, user_id=owner)
        current = _receipt(latest)
        if (not admitted(latest, current) or _checkpoint_identity(latest) != captured
                or current != receipt):
            raise ic.IntegrationError('Repair authority changed during preparation; refresh and retry')
        def persist():
            return _save(latest, current, state='pending', phase='checkpoint',
                         checkpointSha=latest['checkpoint_sha'], repair=repair, retry=None, reason='')
        if repair:
            from lib.project_mod.delivery_retry import pin_receipt
            ref = f'refs/tofu/delivery-repairs/u{owner}/{ic._safe_task(task_id)}'
            receipt = pin_receipt(Path(root), ref, repair['checkpointSha'], persist)
        else:
            receipt = persist()
    ic._start_or_wake_worker()
    return {'ok': True, 'workId': task_id, 'state': 'pending', 'delivery': receipt}


def _settle_registered(row: dict, receipt: dict) -> dict:
    result = _settle_registered_delivery(row, receipt)
    if result.get('state') == 'delivered':
        _reclaim_completed_source(row)
    _wake_delivered_dependents(row, result)
    return result


def _reclaim_completed_source(row: dict) -> None:
    """Acknowledge source retirement once; transient errors use the existing worker.

    This marker records cleanup only. It neither grants publication authority
    nor changes the terminal delivery receipt. Retained checkouts are not
    repeatedly scanned; retired placeholders, missing ACKs and busy leases
    remain retryable.
    """
    origin = row.get('origin') or {}
    if (not row.get('managed') or origin.get('executionMode') != 'isolated-task'
            or origin.get('sourceRetirement') == 'reclaimed'):
        return
    if origin.get('sourceRetirement') == 'retained':
        marker = Path(row['workspace_path']) / '.git'
        if marker.exists() or marker.is_symlink():
            return
    ic = _control()
    root, task_id, owner = row['project_root'], row['task_id'], int(row['user_id'])
    try:
        from lib.project_mod.task_delta_delivery import checkpoint_has_no_delta, reclaim_delivered_workspace
        from lib.project_mod.workspace_lease import workspace_claims_lease
        row = state.get_workspace(root, task_id, user_id=owner)
        with workspace_claims_lease(
                [('metadata', root), ('tree', row['workspace_path'])], timeout=0.0):
            latest = state.get_workspace(root, task_id, user_id=owner)
            receipt = _receipt(latest)
            if (_checkpoint_identity(latest) != _checkpoint_identity(row)
                    or receipt.get('state') != 'delivered'
                    or receipt.get('taskOutcome') != 'completed'):
                return
            from lib.project_dispatch.publication import publication_reason
            if publication_reason(latest, receipt) and not checkpoint_has_no_delta(latest):
                return
            reclaimed = reclaim_delivered_workspace(latest)
            with ic._repo_lock(root):
                state.set_workspace_meta(
                    project_root=root, task_id=task_id, user_id=owner, now=ic._now(),
                    patch={'sourceRetirement': 'reclaimed' if reclaimed else 'retained'})
    except Exception as cleanup_error:
        logger.warning('Completed source clone retained for retry: %s', cleanup_error)


def _checkpoint_delivered_workspace(row: dict) -> None:
    """Commit a delivered task once the repository opted into automatic commits.

    A Coordinator-reviewed project delivery is committed as exactly its own
    delta (``commit_delivered_task_delta``); the review is its verification.
    Other tasks keep the whole-workspace checkpoint with its coalescing policy
    and verification gates. A terminal receipt proves task outcome, never the
    opt-in. One acknowledgement on the existing origin avoids rescanning cold
    history; a transient refusal records none, so the next pass retries.
    """
    origin = row.get('origin') or {}
    receipt = _receipt(row)
    if (origin.get('executionMode') != 'isolated-task'
            or 'deliveryCheckpoint' in origin
            or receipt.get('state') != 'delivered'
            or receipt.get('taskOutcome') != 'completed'
            or receipt.get('publicationMode') != 'working_tree'):
        return
    try:
        from lib.project_dispatch.publication import publication_reason
        if publication_reason(row, receipt):
            return
        from lib.project_dispatch.admission import KEY
        if KEY in origin:
            result = _commit_reviewed_delivery(row)
            if result is None:
                return
        else:
            from lib.linear_git_checkpoint import settle_task_checkpoint
            task = {'id': row['task_id'], 'convId': origin.get('conversationId', ''),
                    'status': 'done', 'finishReason': 'stop'}
            result = settle_task_checkpoint(task, user_id=int(row['user_id']),
                                            project_path=row['project_root'])
            if result and result.get('status') == 'deferred':
                return
        state.set_workspace_meta(
            project_root=row['project_root'], task_id=row['task_id'],
            user_id=int(row['user_id']), now=_control()._now(),
            patch={'deliveryCheckpoint': result or {'status': 'disabled'}})
    except Exception as error:
        logger.warning('Delivered workspace checkpoint retained for retry: %s', error)


def _commit_reviewed_delivery(row: dict) -> dict | None:
    from lib.linear_git_checkpoint import is_enabled
    if not is_enabled(row['project_root']):
        return {'status': 'disabled'}
    from lib.project_mod.task_delta_delivery import commit_delivered_task_delta
    result = commit_delivered_task_delta(row)
    if result is not None:
        logger.info('[WorkspaceDelivery] reviewed delivery commit task=%s status=%s commit=%s reason=%s',
                    row['task_id'], result.get('status'), result.get('commitSha', ''),
                    result.get('reason', ''))
    return result


def _settle_registered_delivery(row: dict, receipt: dict) -> dict:
    ic = _control()
    root, task_id, user_id = row['project_root'], row['task_id'], int(row['user_id'])
    from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_write_lease
    try:
        failed_capture = (receipt.get('state') == 'blocked'
                          and receipt.get('phase') == 'checkpoint'
                          and receipt.get('taskOutcome') == 'completed'
                          and not receipt.get('retry')
                          # Older publication holds used the capture phase even
                          # though the immutable checkpoint was already ready.
                          and not str(receipt.get('reason') or '').startswith('project_publication_')
                          and receipt.get('reason') not in {
                              'task_background_commands_running',
                              'task_child_agents_running'})
        retry_legacy_capture = _legacy_transient_capture_receipt(receipt)
        recapture_transient = (receipt.get('state') == 'pending'
                               and receipt.get('phase') == _RETRYABLE_CAPTURE_PHASE
                               or retry_legacy_capture)
        frozen = (not recapture_transient and not failed_capture
                  and (row.get('origin') or {}).get('executionMode') == 'isolated-task'
                  and row['state'] == 'checkpointed'
                  and receipt.get('taskOutcome') == 'completed'
                  and receipt.get('checkpointSha') == row.get('checkpoint_sha')
                  and bool(row.get('checkpoint_sha')))
        captured_identity = _checkpoint_identity(row)
        # Once captured, recovery consumes immutable objects. The source may
        # have unrelated ongoing edits; only fresh capture needs its writer
        # lease. Canonical publication and descendants remain independently
        # fenced below and in the delta receiver.
        admission = (nullcontext() if frozen else
                     workspace_write_lease(row['workspace_path'], timeout=0.0))
        with admission:
            row = state.get_workspace(root, task_id, user_id=user_id)
            receipt = _receipt(row)
            if frozen and _checkpoint_identity(row) != captured_identity:
                raise ic.IntegrationError('checkpoint_changed_during_recovery')
            if receipt.get('state') in {'delivered', 'preserved'}:
                held = _publication_hold(row, receipt) if receipt.get('state') == 'delivered' else None
                return held if held is not None else receipt
            if row['state'] == 'discarded':
                return _save(row, receipt, state='preserved', reason='Workspace was explicitly discarded')
            automatic = (row.get('origin') or {}).get('executionMode') == 'isolated-task'
            if automatic:
                from lib.tasks_pkg.background_commands import task_has_running_commands
                if task_has_running_commands(task_id=task_id, user_id=user_id):
                    return _save(row, receipt, state='blocked', phase='checkpoint',
                                 reason='task_background_commands_running')
                from lib.swarm.integration import task_has_running_agents
                if task_has_running_agents(task_id=task_id, user_id=user_id):
                    return _save(row, receipt, state='blocked', phase='checkpoint',
                                 reason='task_child_agents_running')
            captured_task = (automatic and not recapture_transient and not failed_capture
                             and row['state'] == 'checkpointed'
                             and receipt.get('checkpointSha') == row.get('checkpoint_sha')
                             and bool(row.get('checkpoint_sha')))
            if not captured_task and row['state'] not in {'ready', 'integrating', 'merged', 'quarantined', 'failed'}:
                if failed_capture and not retry_legacy_capture:
                    return receipt
                try:
                    ic.checkpoint_workspace(root, task_id, user_id=user_id)
                except Exception as exc:
                    if not _transient_git_spawn_error(exc):
                        raise
                    return _save(row, receipt, state='pending', phase=_RETRYABLE_CAPTURE_PHASE,
                                 reason=f'retryable git spawn failure: {exc}'[:900],
                                 checkpointSha=row.get('checkpoint_sha') or '')
                row = state.get_workspace(root, task_id, user_id=user_id)
                if automatic:
                    with ic._repo_lock(root):
                        row = state.get_workspace(root, task_id, user_id=user_id)
                        receipt = _receipt(row)
                        receipt = _save(row, receipt, checkpointSha=row['checkpoint_sha'])
            if receipt.get('taskOutcome') != 'completed':
                return _save(row, receipt, state='preserved', phase='checkpoint',
                             reason='Task did not complete; checkpoint retained without publication')
            held = _publication_hold(row, receipt)
            if held is not None:
                return held
            if automatic:
                # Merge computation consumes immutable objects, not the source checkout.
                # Leave source admission below; publication saves fence the row.
                captured = row
            else:
                return _settle_explicit_checkpoint(row, receipt)
        result = _deliver_task_delta(captured, receipt)
        return result
    except WorkspaceBusyError:
        with ic._repo_lock(root):
            row = state.get_workspace(root, task_id, user_id=user_id)
            receipt = _receipt(row)
            if receipt.get('state') == 'delivered' and receipt.get('taskOutcome') == 'completed':
                held = _publication_hold(row, receipt)
                if held is not None:
                    return held
            if receipt.get('state') in {'delivered', 'preserved'} or receipt.get('taskOutcome') != 'completed':
                return receipt
            if row['state'] == 'discarded':
                return _save(row, receipt, state='preserved', phase='checkpoint',
                             reason='Workspace was explicitly discarded')
            return _save(row, receipt, state='blocked', phase='checkpoint',
                         reason='workspace_busy')
    except Exception as exc:
        row = state.get_workspace(root, task_id, user_id=user_id)
        receipt = _receipt(row)
        if receipt.get('state') == 'delivered' and receipt.get('taskOutcome') == 'completed':
            held = _publication_hold(row, receipt)
            if held is not None:
                return held
        if receipt.get('state') in {'delivered', 'preserved'}:
            return receipt
        if _transient_git_spawn_error(exc):
            return _save(row, receipt, state='pending', phase=_RETRYABLE_CAPTURE_PHASE,
                         reason=f'retryable git spawn failure: {exc}'[:900],
                         checkpointSha=row.get('checkpoint_sha') or '')
        permanent = any(term in str(exc).lower() for term in ('forbidden', 'prohibited', 'not allowed'))
        return _save(row, receipt, state='failed' if permanent else 'blocked',
                     phase=receipt.get('phase') or 'checkpoint', reason=str(exc)[:900])


def _settle_explicit_checkpoint(row: dict, receipt: dict) -> dict:
    ic = _control()
    root, task_id, user_id = row['project_root'], row['task_id'], int(row['user_id'])
    held = _publication_hold(row, receipt)
    if held is not None:
        return held
    publication = ic.task_publication_checkpoint(row)
    if publication == (row.get('origin') or {}).get('inputHeadSha'):
        return _save(row, receipt, state='delivered', phase='no_changes', reason='',
                     headSha=publication, targetSha=publication)
    receipt = _save(row, receipt, state='pending', phase='integration', reason='')
    if row['state'] in {'running', 'checkpointed'}:
        ic.submit_workspace(root, task_id, user_id=user_id)
    return receipt


def _canonical_in_use(root: Path, conversation_id: str, user_id: int) -> bool:
    # Background publication is stricter than the advisory model-facing guard:
    # presence failure must not authorize a checkout mutation.
    from lib import presence
    try:
        peers = presence.snapshot(str(root), user_id=user_id).get('peers') or []
        return any(p.get('convId') != conversation_id for p in peers)
    except Exception as diagnostic_error:
        logger.debug('_canonical_in_use failure handled: %s', type(diagnostic_error).__name__)
        return True


def _checkpoint_identity(row: dict) -> tuple:
    from lib.project_dispatch.admission import KEY
    origin = row.get('origin') or {}
    return (row['state'], row.get('checkpoint_sha'), row['workspace_path'],
            origin.get('sourceSnapshot'), origin.get('inputBaseSha'), origin.get('inputHeadSha'),
            KEY in origin, origin.get(KEY))


def _deliver_task_delta(row: dict, receipt: dict) -> dict:
    from lib.project_mod.task_delta_delivery import deliver_task_delta
    captured = _checkpoint_identity(row)

    def save_exact(latest, current, **updates):
        # The delta receiver calls its save boundary under the repository lock,
        # including the durable intent immediately before git apply. Fence the
        # mutable registration, independently of uncheckpointed source bytes.
        if (_checkpoint_identity(latest) != captured or latest['state'] == 'discarded'
                or (current.get('repair') or {}) != (receipt.get('repair') or {})):
            raise _control().IntegrationError('checkpoint_changed_during_validation')
        return _save(latest, current, **updates)

    return deliver_task_delta(row, receipt, save_exact)


def deliver_registered_workspace(project_path: str, task_id: str, *, user_id: int) -> dict:
    """Adopt the checkpoint, then notify after ACK and all publication release."""
    result = _deliver_registered_workspace(project_path, task_id, user_id=user_id)
    _wake_workspace_dependents(project_path, task_id, user_id, result)
    return result


def _deliver_registered_workspace(project_path: str, task_id: str, *, user_id: int) -> dict:
    """Adopt exactly this checkpoint onto its pinned target, never old candidate.

    The cooperative checkout lease covers dirty/ref revalidation and git's own
    fast-forward transaction. A crash after Git succeeds is detected by ancestry
    on recovery, without replaying a merge or requiring a magic acknowledgement.
    """
    ic = _control()
    row = state.get_workspace(str(ic._repo_root(project_path)), str(task_id), user_id=user_id)
    receipt = _receipt(row)
    if receipt.get('taskOutcome') == 'completed':
        held = _publication_hold(row, receipt)
        if held is not None:
            return held
    if receipt.get('taskOutcome') != 'completed' or receipt.get('state') in {'preserved', 'delivered', 'failed'}:
        return receipt
    if row['state'] != 'merged':
        if row['state'] in {'quarantined', 'failed', 'discarded'}:
            return _save(row, receipt, state='blocked', phase='integration', reason=row.get('error') or row['state'])
        return receipt
    root = Path(row['project_root'])
    captured = _checkpoint_identity(row)
    checkpoint = ic.task_publication_checkpoint(row)
    from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_write_lease
    try:
        with workspace_write_lease(str(root), timeout=0.0), ic._repo_lock(str(root)):
            row = state.get_workspace(str(root), str(task_id), user_id=user_id)
            receipt = _receipt(row)
            if receipt.get('taskOutcome') == 'completed':
                held = _publication_hold(row, receipt)
                if held is not None:
                    return held
            if receipt.get('taskOutcome') != 'completed' or receipt.get('state') in {'preserved', 'delivered', 'failed'}:
                return receipt
            if _checkpoint_identity(row) != captured:
                return _save(row, receipt, state='blocked', phase='adoption', reason='checkpoint_changed_during_validation')
            branch = ic._git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip()
            if not receipt.get('targetRef') or branch != receipt['targetRef']:
                return _save(row, receipt, state='blocked', phase='adoption', reason='target_branch_changed')
            head = ic._rev(root, 'HEAD')
            dirty = ic._porcelain(root)
            if dirty['total'] or dirty.get('timedOut'):
                return _save(row, receipt, state='blocked', phase='adoption', reason='canonical_dirty', headSha=head)
            if ic._git(root, ['merge-base', '--is-ancestor', checkpoint, head]).returncode == 0:
                return _save(row, receipt, state='delivered', phase='adoption', reason='', headSha=head, targetSha=head)
            if _canonical_in_use(root, receipt.get('conversationId', ''), user_id):
                return _save(row, receipt, state='blocked', phase='adoption', reason='workspace_busy', headSha=head)
            if receipt.get('reason', '').startswith('merge_conflict:') and receipt.get('headSha') == head:
                return receipt
            conversation_id = receipt.get('conversationId', '')
        # Validation consumes only immutable commits. Neither cooperative
        # canonical writers nor terminal task revocation wait for these gates.
        target, conflict = ic._merge_checkpoint(root, head, checkpoint, str(task_id))
        from lib.git_checkpoint_policy import GatePolicyError, select_gate_commands, require_unchanged_gates
        try:
            selected_policy = select_gate_commands(root)
            acceptance = {}
            passed, detail = (False, '') if conflict else ic._gate_commands(
                root, head, target, selected_policy.integration, evidence_out=acceptance)
        except GatePolicyError as error:
            acceptance = {}
            passed, detail = False, str(error)
        with workspace_write_lease(str(root), timeout=0.0), ic._repo_lock(str(root)):
            row = state.get_workspace(str(root), str(task_id), user_id=user_id)
            receipt = _receipt(row)
            if receipt.get('taskOutcome') == 'completed':
                held = _publication_hold(row, receipt)
                if held is not None:
                    return held
            if receipt.get('taskOutcome') != 'completed' or receipt.get('state') in {'preserved', 'delivered', 'failed'}:
                return receipt
            if _checkpoint_identity(row) != captured:
                return _save(row, receipt, state='blocked', phase='adoption', reason='checkpoint_changed_during_validation')
            if receipt.get('targetRef') != branch or receipt.get('conversationId', '') != conversation_id:
                return _save(row, receipt, state='blocked', phase='adoption', reason='target_branch_changed')
            latest_dirty = ic._porcelain(root)
            if (ic._rev(root, 'HEAD') != head or latest_dirty['total']
                    or latest_dirty.get('timedOut')
                    or ic._git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip() != branch):
                return _save(row, receipt, state='blocked', reason='canonical_changed_during_validation')
            from lib.project_mod.delivery_acceptance import persist_gate_record
            persist_gate_record(row, acceptance, checkpoint=row['checkpoint_sha'])
            if _canonical_in_use(root, conversation_id, user_id):
                return _save(row, receipt, state='blocked', phase='adoption', reason='workspace_busy', headSha=head)
            if conflict:
                return _save(row, receipt, state='blocked', phase='adoption', reason='merge_conflict: ' + conflict[:700], headSha=head)
            if not passed:
                return _save(row, receipt, state='failed', phase='adoption', reason='Gate failed: ' + detail[:700], headSha=head)
            # Only fresh validation may persist publication intent.
            receipt = _save(row, receipt, state='pending', phase='adoption', targetSha=target, headSha=head, reason='')
            # A durable intent ACK is not fresh publication authority.
            row = state.get_workspace(str(root), str(task_id), user_id=user_id)
            held = _publication_hold(row, _receipt(row))
            if held is not None:
                return held
            try:
                require_unchanged_gates(root, selected_policy)
            except GatePolicyError as error:
                return _save(row, receipt, state='blocked', phase='adoption', reason=str(error))
            cp = ic._git(root, ['merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', target])
            actual = ic._rev(root, 'HEAD')
            if cp.returncode != 0 or actual != target:
                return _save(row, receipt, state='blocked', reason='adoption_failed: ' + (cp.stderr or cp.stdout)[:700])
            return _save(row, receipt, state='delivered', phase='adoption', reason='', headSha=actual, targetSha=target)
    except WorkspaceBusyError:
        with ic._repo_lock(str(root)):
            row = state.get_workspace(str(root), str(task_id), user_id=user_id)
            receipt = _receipt(row)
            if receipt.get('state') == 'delivered' and receipt.get('taskOutcome') == 'completed':
                held = _publication_hold(row, receipt)
                if held is not None:
                    return held
            if receipt.get('taskOutcome') != 'completed' or receipt.get('state') in {'preserved', 'delivered', 'failed'}:
                return receipt
            return _save(row, receipt, state='blocked', phase='adoption', reason='workspace_busy')


def _recover_working_registration(row: dict) -> dict:
    """Recover intent even when Project Brain enrollment never succeeded.

    Worker registration happens after runtime admission. A live task remains
    untouched; an absent task is reconciled against owner-scoped durable verdict
    evidence. Unknown execution is preservation debt, never publication consent.
    Storage failures escape so the working receipt remains retryable.
    """
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.workspace_delivery import RESTART_INTERRUPTED, workspace_outcome

    task_id = str(row['task_id'])
    if _receipt(row).get('taskOutcome') == RESTART_INTERRUPTED:
        # Restart-paused: the resumed attempt of this task settles it.
        return _receipt(row)
    # A project Worker's checkout is keyed by its assignment's work id; the
    # physical attempt executing in it records itself as executorTaskId.
    executor_id = str((row.get('origin') or {}).get('executorTaskId') or task_id)
    live = chat_task_runtime.get(executor_id)
    # Even a terminal live carrier may still be persisting its verdict. Its
    # normal settlement owns retries; do not race an absent durable row into
    # a permanent publication revocation.
    if live is not None:
        return _receipt(row)
    task = get_storage_client().query('task_results.replay_get', {
        'key': executor_id, 'user_id': int(row['user_id']),
        'include_terminal_payload': False, 'include_metadata': True,
    })
    task = dict(task or {})
    metadata = task.get('metadata')
    if isinstance(metadata, dict):
        for key in ('finishReason', 'streamState', 'aborted', '_abort_reason'):
            if key not in task and key in metadata:
                task[key] = metadata[key]
    return settle_task_workspace(
        row['workspace_path'], task_id, user_id=int(row['user_id']),
        outcome=workspace_outcome(task)) or _receipt(row)


def request_workspace_delivery_scan(*, recovery: bool = False) -> None:
    """Make the worker's next pass due; ``recovery`` also rechecks retained sources.

    A hint only: the pass rereads durable receipts and grants no authority.
    """
    global _SCAN_REQUESTED, _RECOVERY_REQUESTED
    with _SCAN_LOCK:
        _SCAN_REQUESTED = True
        _RECOVERY_REQUESTED = _RECOVERY_REQUESTED or recovery


def resume_due_workspace_deliveries() -> bool:
    """Worker tick: continue an open pass, or start one only when it is due.

    A pass is due on request (startup or worker-error recovery, a committed
    delivery-state command) and while the last pass returned rows: a held
    receipt whose cause clears without a commit keeps its per-tick retry.
    Otherwise nothing is read, however long the delivery history.
    """
    global _SCAN_REQUESTED, _RECOVERY_REQUESTED
    if _RECOVERY_CURSOR:
        return resume_pending_workspace_deliveries()
    with _SCAN_LOCK:
        requested, recovery = _SCAN_REQUESTED, _RECOVERY_REQUESTED
        _SCAN_REQUESTED = _RECOVERY_REQUESTED = False
    if not (requested or _ACTION_OUTSTANDING):
        return False
    return resume_pending_workspace_deliveries(retained=recovery)


def resume_pending_workspace_deliveries(*, retained: bool = False) -> bool:
    """Act on one page of rows whose receipt asks the worker to act.

    Page rows are complete workspace rows, so acknowledged history costs no
    client read or filesystem probe. ``retained`` applies to a pass starting
    here: a retained checkout can vanish without any commit.
    """
    global _RECOVERY_CURSOR, _RECOVERY_RETAINED, _ACTION_OUTSTANDING
    if not _RECOVERY_CURSOR:
        _RECOVERY_RETAINED, _ACTION_OUTSTANDING = retained, False
    page = state.delivery_action_page(_RECOVERY_CURSOR, 64, retained=_RECOVERY_RETAINED)
    _RECOVERY_CURSOR = int(page.get('nextAfterId') or 0)
    _ACTION_OUTSTANDING = _ACTION_OUTSTANDING or bool(page['rows'])
    changed = False
    for row in page['rows']:
        try:
            before = _receipt(row)
            if before.get('state') == 'delivered':
                _checkpoint_delivered_workspace(row)
                _reclaim_completed_source(row)
                continue
            if before.get('state') == 'working':
                after = _recover_working_registration(row)
                changed = changed or before != after
                continue
            if before.get('state') not in {'pending', 'blocked'}:
                retry = before.get('retry') or {}
                if (before.get('state') != 'failed'
                        or retry.get('condition') != 'validation_inputs_changed'):
                    continue
            if before.get('phase') == 'checkpoint' or row['state'] in {'running', 'checkpointed'}:
                after = _settle_registered(row, before)
            else:
                after = deliver_registered_workspace(row['project_root'], row['task_id'], user_id=int(row['user_id']))
            if after.get('state') == 'delivered':
                latest = state.get_workspace(row['project_root'], row['task_id'], user_id=int(row['user_id']))
                _checkpoint_delivered_workspace(latest)
            changed = changed or before != after
        except Exception as exc:
            # A page contains independent owners. Keep this row's durable debt
            # and let the same paged worker retry it without starving siblings.
            logger.warning('Workspace recovery pending task=%s: %s',
                           row.get('task_id', '?'), exc)
    return changed or bool(page.get('hasMore'))
