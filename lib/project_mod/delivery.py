"""Task-owned delivery of registered worktrees, through integration authority.

The existing workspace row/origin is the only durable owner. Explicit candidate
validation/adoption and automatic task-delta publication use the same intent
and recovery receiver; no detached job ledger or second publication authority. Failed/cancelled tasks preserve, never auto-publish.
External processes not using workspace_write_lease remain an explicit limit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from lib import integration_state_repository as state

_RECOVERY_CURSOR = 0
_DELIVERY_KEY = 'delivery'


def _control():
    from lib import integration_control
    return integration_control


def _canonical(path: str) -> tuple[Path, Path]:
    ic = _control()
    workspace = ic._repo_root(path)
    listing = ic._git(workspace, ['worktree', 'list', '--porcelain'], check=True).stdout
    first = next((line[9:] for line in listing.splitlines()
                  if line.startswith('worktree ')), '')
    if not first:
        raise ic.IntegrationError('Git did not identify a canonical worktree')
    root = Path(first).resolve()
    if ic._common_dir(root) != ic._common_dir(workspace):
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
        base = ic._git(root, ['merge-base', target_head, workspace_head], check=True).stdout.strip()
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
                  else ic._git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip())
        delivery = _save(row, {}, state='working', targetRef=branch,
                         baseHead=ic._rev(root, 'HEAD'), conversationId=conversation_id,
                         reason='', taskOutcome='')
    return {'canonicalRoot': str(root), 'workspacePath': str(actual),
            'workId': str(task_id), 'delivery': delivery}


def settle_task_workspace(project_path: str, task_id: str, *, user_id: int,
                          outcome: str) -> dict | None:
    """Persist intention BEFORE checkpoint/submit, so restart can finish it."""
    ic = _control()
    if outcome not in {'completed', 'failed', 'cancelled', 'stopped'}:
        raise ic.IntegrationError('Unknown task outcome; publication denied')
    # A no-change automatic task may have reclaimed its source clone after
    # durable completion. Terminal receipts remain idempotent without opening
    # that removed checkout again.
    canonical, _selected = _canonical(project_path)
    existing = state.find_workspace(str(canonical), str(task_id), user_id=user_id)
    if existing and _receipt(existing).get('state') in {'delivered', 'preserved'}:
        return _receipt(existing)
    bound = bind_task_workspace(project_path, task_id, user_id=user_id)
    if bound is None:
        return None
    # Serializes revocation with candidate/adoption ref linearization.
    with ic._repo_lock(bound['canonicalRoot']):
        row = state.get_workspace(bound['canonicalRoot'], str(task_id), user_id=user_id)
        receipt = _receipt(row)
        if receipt.get('state') in {'delivered', 'preserved'}:
            return receipt
        previous = receipt.get('taskOutcome')
        original_outcome = previous if previous in {'failed', 'cancelled', 'stopped'} else outcome
        receipt = _save(row, receipt, state='pending', phase='checkpoint',
                        taskOutcome=original_outcome, reason='')
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
    ic = _control()
    root, task_id, user_id = row['project_root'], row['task_id'], int(row['user_id'])
    from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_write_lease
    try:
        with workspace_write_lease(row['workspace_path'], timeout=0.0):
            row = state.get_workspace(root, task_id, user_id=user_id)
            receipt = _receipt(row)
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
            captured_task = (automatic and row['state'] == 'checkpointed'
                             and receipt.get('checkpointSha') == row.get('checkpoint_sha')
                             and bool(row.get('checkpoint_sha')))
            if not captured_task and row['state'] not in {'ready', 'integrating', 'merged', 'quarantined', 'failed'}:
                ic.checkpoint_workspace(root, task_id, user_id=user_id)
                row = state.get_workspace(root, task_id, user_id=user_id)
                if automatic:
                    with ic._repo_lock(root):
                        row = state.get_workspace(root, task_id, user_id=user_id)
                        receipt = _receipt(row)
                        receipt = _save(row, receipt, checkpointSha=row['checkpoint_sha'])
            if receipt.get('taskOutcome') != 'completed':
                return _save(row, receipt, state='preserved', phase='checkpoint',
                             reason='Task did not complete; checkpoint retained without publication')
            if automatic:
                # Gates consume immutable Git objects, not the source checkout.
                # Leave source admission below; publication saves fence the row.
                captured = row
            else:
                return _settle_explicit_checkpoint(row, receipt)
        result = _deliver_task_delta(captured, receipt)
        if result.get('state') == 'delivered':
            try:
                from lib.project_mod.task_delta_delivery import reclaim_no_change_workspace
                with workspace_write_lease(captured['workspace_path'], timeout=0.0):
                    latest = state.get_workspace(root, task_id, user_id=user_id)
                    if _checkpoint_identity(latest) == _checkpoint_identity(captured):
                        reclaim_no_change_workspace(latest)
            except Exception as cleanup_error:
                from lib.log import get_logger
                get_logger(__name__).warning('Completed source clone retained: %s', cleanup_error)
        return result
    except WorkspaceBusyError:
        with ic._repo_lock(root):
            row = state.get_workspace(root, task_id, user_id=user_id)
            receipt = _receipt(row)
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
        if receipt.get('state') in {'delivered', 'preserved'}:
            return receipt
        permanent = any(term in str(exc).lower() for term in ('forbidden', 'prohibited', 'not allowed'))
        return _save(row, receipt, state='failed' if permanent else 'blocked',
                     phase=receipt.get('phase') or 'checkpoint', reason=str(exc)[:900])


def _settle_explicit_checkpoint(row: dict, receipt: dict) -> dict:
    ic = _control()
    root, task_id, user_id = row['project_root'], row['task_id'], int(row['user_id'])
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
    except Exception:
        return True


def _checkpoint_identity(row: dict) -> tuple:
    origin = row.get('origin') or {}
    return (row['state'], row.get('checkpoint_sha'), row['workspace_path'],
            origin.get('sourceSnapshot'), origin.get('inputBaseSha'), origin.get('inputHeadSha'))


def _deliver_task_delta(row: dict, receipt: dict) -> dict:
    from lib.project_mod.task_delta_delivery import deliver_task_delta
    captured = _checkpoint_identity(row)

    def save_exact(latest, current, **updates):
        # The delta receiver calls its save boundary under the repository lock,
        # including the durable intent immediately before git apply. Fence the
        # mutable registration, not source bytes that gates never consume.
        if (_checkpoint_identity(latest) != captured or latest['state'] == 'discarded'
                or (current.get('repair') or {}) != (receipt.get('repair') or {})):
            raise _control().IntegrationError('checkpoint_changed_during_validation')
        return _save(latest, current, **updates)

    return deliver_task_delta(row, receipt, save_exact)


def deliver_registered_workspace(project_path: str, task_id: str, *, user_id: int) -> dict:
    """Adopt exactly this checkpoint onto its pinned target, never old candidate.

    The cooperative checkout lease covers dirty/ref revalidation and git's own
    fast-forward transaction. A crash after Git succeeds is detected by ancestry
    on recovery, without replaying a merge or requiring a magic acknowledgement.
    """
    ic = _control()
    row = state.get_workspace(str(ic._repo_root(project_path)), str(task_id), user_id=user_id)
    receipt = _receipt(row)
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
        import os
        passed, detail = (False, '') if conflict else ic._gate_commands(
            root, head, target, os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip())
        with workspace_write_lease(str(root), timeout=0.0), ic._repo_lock(str(root)):
            row = state.get_workspace(str(root), str(task_id), user_id=user_id)
            receipt = _receipt(row)
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
            if _canonical_in_use(root, conversation_id, user_id):
                return _save(row, receipt, state='blocked', phase='adoption', reason='workspace_busy', headSha=head)
            if conflict:
                return _save(row, receipt, state='blocked', phase='adoption', reason='merge_conflict: ' + conflict[:700], headSha=head)
            if not passed:
                return _save(row, receipt, state='failed', phase='adoption', reason='Gate failed: ' + detail[:700], headSha=head)
            # Only fresh validation may persist publication intent.
            receipt = _save(row, receipt, state='pending', phase='adoption', targetSha=target, headSha=head, reason='')
            cp = ic._git(root, ['merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', target])
            actual = ic._rev(root, 'HEAD')
            if cp.returncode != 0 or actual != target:
                return _save(row, receipt, state='blocked', reason='adoption_failed: ' + (cp.stderr or cp.stdout)[:700])
            return _save(row, receipt, state='delivered', phase='adoption', reason='', headSha=actual, targetSha=target)
    except WorkspaceBusyError:
        with ic._repo_lock(str(root)):
            row = state.get_workspace(str(root), str(task_id), user_id=user_id)
            receipt = _receipt(row)
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
    from lib.tasks_pkg.workspace_delivery import task_outcome

    task_id = str(row['task_id'])
    live = chat_task_runtime.get(task_id)
    # Even a terminal live carrier may still be persisting its verdict. Its
    # normal settlement owns retries; do not race an absent durable row into
    # a permanent publication revocation.
    if live is not None:
        return _receipt(row)
    task = get_storage_client().query('task_results.replay_get', {
        'key': task_id, 'user_id': int(row['user_id']),
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
        outcome=task_outcome(task)) or _receipt(row)


def resume_pending_workspace_deliveries() -> bool:
    """One bounded page per worker tick; cold rows never hit the writer lane."""
    global _RECOVERY_CURSOR
    page = state.delivery_page(_RECOVERY_CURSOR, 64)
    _RECOVERY_CURSOR = int(page.get('nextAfterId') or 0)
    changed = False
    for base_row in page['rows']:
        row = state.get_workspace(base_row['project_root'], base_row['task_id'],
                                  user_id=int(base_row['user_id']))
        before = _receipt(row)
        if before.get('state') == 'working':
            try:
                after = _recover_working_registration(row)
            except Exception as exc:
                from lib.log import get_logger
                get_logger(__name__).warning(
                    'Workspace recovery intent pending task=%s: %s',
                    row['task_id'], exc)
                # Keep the durable working receipt and continue other owners.
                continue
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
        changed = changed or before != after
    return changed or bool(page.get('hasMore'))
