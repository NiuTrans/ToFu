"""Publish an isolated task delta while preserving canonical work in progress.

Entry point: deliver_task_delta(row, receipt, save_receipt). Integration owns
Git, immutable checkpoints and gates; delivery.py owns durable intent/receipts.
No candidate ref or canonical index/HEAD is modified by this publication mode.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import tempfile

from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_write_lease
from lib.project_mod.delivery_retry import input_fingerprint, retry_evidence, conflicted_paths, pin_receipt


def deliver_task_delta(row, receipt, save_receipt):
    from lib import integration_control as ic

    root = Path(row['project_root'])
    owner = int(row['user_id'])
    task_id = row['task_id']
    baseline = (row.get('origin') or {}).get('sourceSnapshot')
    checkpoint = row.get('checkpoint_sha')
    if not baseline or not checkpoint:
        raise ic.IntegrationError('Task delta is missing its registered immutable baseline')
    repair = receipt.get('repair') or {}
    if repair:
        if (repair.get('originalBaselineSha') != baseline
                or repair.get('registeredCheckpointSha') != checkpoint):
            raise ic.IntegrationError('Conflict repair checkpoint changed; request a new owned retry')
        baseline, checkpoint = repair['baselineSha'], repair['checkpointSha']

    def save(*, locked=False, **updates):
        def persist():
            latest = ic._state.get_workspace(str(root), task_id, user_id=owner)
            current = dict((latest.get('origin') or {}).get('delivery') or {})
            if current.get('taskOutcome') != 'completed' or current.get('state') in {'delivered', 'preserved'}:
                return current
            if updates.get('state') in {'delivered', 'preserved'}:
                updates.setdefault('retry', None)
            return save_receipt(latest, current, **updates)
        if locked:
            return persist()
        with ic._repo_lock(str(root)):
            return persist()

    def block_on_inputs(condition, fingerprint, source, reason, paths=()):
        # One replaceable owner/task ref pins the current repair input. Roll
        # back a failed durable receipt so the previous dossier never loses its
        # only GC anchor. Neither this ref nor retry metadata grants authority.
        with ic._repo_lock(str(root)):
            input_ref = f'refs/tofu/delivery-inputs/u{owner}/{ic._safe_task(task_id)}'
            def persist():
                return save(locked=True,
                            state='failed' if condition == 'validation_inputs_changed' else 'blocked',
                            phase='delta_adoption',
                            retry=retry_evidence(condition, fingerprint, baseline,
                                                 checkpoint, source, paths), reason=reason)
            return pin_receipt(root, input_ref, source, persist)

    try:
        with workspace_write_lease(str(root), timeout=5.0), ic._repo_lock(str(root)):
            latest = ic._state.get_workspace(str(root), task_id, user_id=owner)
            latest_receipt = dict((latest.get('origin') or {}).get('delivery') or {})
            if (latest_receipt.get('taskOutcome') != 'completed'
                    or latest_receipt.get('state') in {'delivered', 'preserved'}):
                return latest_receipt
            if (latest_receipt.get('repair') or {}) != repair:
                raise ic.IntegrationError('Conflict repair changed during validation')
            receipt = latest_receipt
            branch = ic._git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip()
            if branch != receipt.get('targetRef'):
                return save(locked=True, state='blocked', phase='delta_adoption', reason='target_branch_changed')
            head = ic._rev(root, 'HEAD')
            source = ic._alternate_index_checkpoint(
                root, head, task_id, reuse_commit=(receipt.get('retry') or {}).get('sourceSha') or '')
            changed = ic._git(root, ['diff', '--name-only', '-z', baseline, checkpoint],
                              check=True).stdout.split('\0')
            changed = [path for path in changed if path]
            if not changed:
                return save(locked=True, state='delivered', phase='no_changes', reason='',
                            checkpointSha=row['checkpoint_sha'], deltaCheckpointSha=checkpoint,
                            headSha=head, targetSha=source)
            # Recover a lost receipt after publication without replaying a
            # patch, even if unrelated canonical files changed afterwards.
            previous_target = receipt.get('targetSha')
            if (receipt.get('phase') == 'delta_adoption'
                    and (receipt.get('deltaCheckpointSha') or receipt.get('checkpointSha')) == checkpoint
                    and previous_target
                    and ic._git(root, ['--literal-pathspecs', 'diff', '--quiet', source, previous_target,
                                      '--', *changed]).returncode == 0):
                return save(locked=True, state='delivered', reason='', headSha=head)

        configured = os.environ.get('TOFU_INTEGRATION_TEST_CMD', '').strip()
        verification = ((row.get('origin') or {}).get('_workspaceDelivery') or {}).get('verification') or {}
        gate_options = {}
        if (not configured and verification.get('taskId') == task_id
                and verification.get('ownerUserId') == owner):
            configured = verification.get('command') or ''
            gate_options['verification_directory'] = verification.get('directory') or '.'
            gate_options['verification_source'] = row['workspace_path']
        fingerprint = input_fingerprint(
            baseline=baseline, checkpoint=checkpoint, branch=branch,
            source_tree=ic._rev(root, source + '^{tree}'),
            command=configured, gate_options=gate_options)
        merge_fingerprint = input_fingerprint(
            baseline=baseline, checkpoint=checkpoint, branch=branch,
            source_tree=ic._git(root, ['--literal-pathspecs', 'ls-tree', '-z', source, '--', *changed], check=True).stdout,
            command='', gate_options={})
        previous_retry = receipt.get('retry') or {}
        if (receipt.get('state') in {'blocked', 'failed'}
                and previous_retry.get('condition') in {
                    'conflict_inputs_changed', 'validation_inputs_changed'}
                and previous_retry.get('fingerprint') == (
                    merge_fingerprint if previous_retry.get('condition') == 'conflict_inputs_changed'
                    else fingerprint)):
            # Re-observe durable authority even when no expensive merge/gate is
            # needed. Restart uses the same receipt, not a process-local cache.
            return save(state=receipt['state'])

        patch = ic._git(root, ['diff', '--binary', '--full-index', '--no-ext-diff',
                              '--no-textconv', baseline, checkpoint], check=True).stdout
        with tempfile.TemporaryDirectory(prefix='task-delta-', dir=str(ic._workspace_root())) as directory:
            patch_path = Path(directory) / 'task.patch'
            patch_path.write_text(patch, encoding='utf-8')
            scratch = Path(directory) / 'validation'
            ic._git(root, ['worktree', 'add', '--detach', str(scratch), source],
                    timeout=90.0, check=True)
            try:
                # Three-way application uses the registered blob preimages,
                # preserving compatible concurrent edits even in the same file.
                # Scratch owns its index; canonical HEAD/index stay untouched.
                applied = ic._git(scratch, ['apply', '--3way', '--whitespace=nowarn', str(patch_path)])
                if applied.returncode:
                    paths = conflicted_paths(ic._git(scratch, ['ls-files', '-u', '-z'], check=True).stdout)
                    return block_on_inputs(
                        'conflict_inputs_changed', merge_fingerprint, source,
                        'task_delta_conflict: ' + (applied.stderr or applied.stdout)[:700], paths)
                target = ic._alternate_index_checkpoint(scratch, source, task_id)
                # Publish the resolved source-to-target delta, never replay the
                # original baseline patch over compatible canonical changes.
                resolved_patch = ic._git(root, ['diff', '--binary', '--full-index',
                                                '--no-ext-diff', '--no-textconv',
                                                source, target], check=True).stdout
                patch_path.write_text(resolved_patch, encoding='utf-8')
                passed, detail = ic._gate_commands(root, source, target, configured, **gate_options)
                if not passed:
                    if detail.startswith('integration_gate_timeout: '):
                        return save(state='blocked', phase='delta_adoption', retry=None,
                                    reason='Gate deferred: ' + detail[:700])
                    if detail.startswith('Checkpoint contains forbidden dependency/generated/runtime paths:'):
                        return save(state='failed', phase='delta_adoption', retry=None,
                                    reason='Gate failed: ' + detail[:700])
                    return block_on_inputs('validation_inputs_changed', fingerprint, source,
                                           'Gate failed: ' + detail[:700])
                with workspace_write_lease(str(root), timeout=5.0), ic._repo_lock(str(root)):
                    latest = ic._state.get_workspace(str(root), task_id, user_id=owner)
                    latest_receipt = dict((latest.get('origin') or {}).get('delivery') or {})
                    if (latest_receipt.get('taskOutcome') != 'completed'
                            or latest_receipt.get('state') in {'delivered', 'preserved'}):
                        return latest_receipt
                    if (latest_receipt.get('repair') or {}) != repair:
                        raise ic.IntegrationError('Conflict repair changed during validation')
                    if (ic._rev(root, 'HEAD') != head
                            or ic._git(root, ['symbolic-ref', '-q', 'HEAD']).stdout.strip() != branch):
                        return save(locked=True, state='blocked', reason='canonical_changed_during_validation')
                    current = ic._alternate_index_checkpoint(root, head, task_id)
                    if ic._git(root, ['diff', '--quiet', source, current]).returncode:
                        return save(locked=True, state='blocked', reason='canonical_changed_during_validation')
                    # Intent and immutable recovery target precede mutation,
                    # under the same repository lock as revocation checks.
                    recovery_ref = f'refs/tofu/delivery-checkpoints/u{owner}/{ic._safe_task(task_id)}'
                    ic._git(root, ['update-ref', recovery_ref, target], check=True)
                    receipt = save(locked=True, state='pending', phase='delta_adoption', reason='',
                                   retry=None, sourceSha=source, checkpointSha=row['checkpoint_sha'],
                                   deltaCheckpointSha=checkpoint,
                                   targetSha=target, headSha=head)
                    # Git apply preflights the complete patch and refuses
                    # overlapping edits. It leaves the real index untouched.
                    applied = ic._git(root, ['apply', '--whitespace=nowarn', str(patch_path)])
                    if applied.returncode:
                        return save(locked=True, state='blocked', reason='task_delta_conflict: '
                                    + (applied.stderr or applied.stdout)[:700])
                    return save(locked=True, state='delivered', reason='', headSha=head)
            finally:
                # The validation checkout is solely owned by this call; its
                # target tree is immutable and no user has been handed it.
                with contextlib.suppress(Exception):
                    ic._remove_controlled_worktree(root, scratch)
    except WorkspaceBusyError:
        return save(state='blocked', phase='delta_adoption', reason='workspace_busy')


def reclaim_no_change_workspace(row):
    """Reclaim a reconstructible clone after its terminal receipt is durable.

    Called under the task checkout lease, outside canonical writer admission.
    Any ignored artifact or task delta retains the checkout for recovery/undo.
    """
    from lib import integration_control as ic
    if not row.get('managed'):
        return
    baseline = (row.get('origin') or {}).get('sourceSnapshot')
    checkpoint = row.get('checkpoint_sha')
    if not baseline or not checkpoint:
        return
    root = Path(row['project_root'])
    workspace = Path(row['workspace_path'])
    # Source admission is owned by the caller. Scanning its ignored runtime
    # data does not require canonical ref/publication admission.
    if ic._git(root, ['diff', '--quiet', baseline, checkpoint]).returncode:
        return
    ignored = ic._git(workspace, ['ls-files', '--others', '--ignored',
                                 '--exclude-standard', '-z'], check=True).stdout
    drift = ic._git(workspace, ['status', '--porcelain', '--untracked-files=all'],
                    check=True).stdout
    environment = workspace / '.venv'
    if environment.is_dir():
        known = {'bin', 'Scripts', 'lib', 'lib64', 'Lib', 'include', 'Include', 'pyvenv.cfg'}
        if any(entry.name not in known for entry in environment.iterdir()):
            return
        package_roots = list((environment / 'lib').glob('python*/site-packages'))
        if (environment / 'Lib/site-packages').is_dir():
            package_roots.append(environment / 'Lib/site-packages')
        if any(entry.name not in {'tofu-source-packages.pth', '__pycache__'}
               for packages in package_roots for entry in packages.iterdir()):
            return
    dependencies = workspace / 'node_modules'
    if dependencies.is_dir() and not dependencies.is_symlink():
        if any(not entry.is_symlink() for entry in dependencies.iterdir()):
            return
    reconstructible = {'.venv', 'node_modules', '.pytest_cache', '.ruff_cache', '__pycache__'}
    durable_ignored = [path for path in ignored.split('\0') if path
                       and not any(part in reconstructible for part in Path(path).parts)]
    if durable_ignored or drift:
        return
    # Retain serialization only for identity revalidation and controlled Git
    # administration; discard/checkpoint changes during scans preserve the tree.
    with ic._repo_lock(str(root)):
        latest = ic._state.get_workspace(str(root), row['task_id'], user_id=int(row['user_id']))
        origin = latest.get('origin') or {}
        if (latest['state'] == 'discarded' or not latest.get('managed')
                or latest['workspace_path'] != row['workspace_path']
                or latest.get('checkpoint_sha') != checkpoint
                or origin.get('sourceSnapshot') != baseline
                or (origin.get('delivery') or {}).get('state') != 'delivered'):
            return
        ic._remove_controlled_worktree(root, workspace)
