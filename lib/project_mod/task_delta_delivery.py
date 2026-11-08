"""Merge completed task changes into the live canonical workspace.

Entries: deliver_task_delta, commit_delivered_task_delta. Baselines, intents,
revocation and receipts retain integration/delivery ownership. A private index
computes the three-way merge; publication changes files only. HEAD, the real
index and release gates stay independent of workspace delivery. No validation
checkout is materialized. A reviewed project delivery in a repository that opted
into automatic commits is then recorded on the current branch as exactly its
own delta, without writing any checkout file.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import tempfile

from lib.git_runtime import GIT_STALL_SECONDS
from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_claims_lease
from lib.project_mod.delivery_retry import input_fingerprint, retry_evidence, conflicted_paths, pin_receipt
from lib.project_mod.workspace_runtime import NODE_TOOL_CACHE_DIRECTORIES


def checkpoint_has_no_delta(row) -> bool:
    """Prove an ordinary captured task has no filesystem effect to authorize."""
    from lib import integration_control as ic

    origin = row.get('origin') or {}
    if (origin.get('delivery') or {}).get('repair'):
        return False  # Repair proposals retain their independent publication fence.
    baseline = origin.get('sourceSnapshot') or origin.get('inputHeadSha')
    checkpoint = row.get('checkpoint_sha')
    return bool(baseline and checkpoint and ic._git(
        Path(row['project_root']), ['diff', '--quiet', baseline, checkpoint, '--'],
        stall_seconds=GIT_STALL_SECONDS,
    ).returncode == 0)


def retired_workspace_placeholder(root: Path, workspace: Path) -> bool:
    """Recognize a removed checkout's empty/file-history-bootstrap directory.

    An existing Git identity, registered worktree, symlink, or durable file
    prevents reclamation. Git discovery must never walk into the parent repo
    and mistake its HEAD/index for this former checkout's identity.
    """
    from lib import integration_control as ic
    from lib.file_history.store import readme_path

    marker = workspace / '.git'
    if workspace.is_symlink() or marker.exists() or marker.is_symlink():
        return False
    listed = ic._git(root, ['worktree', 'list', '--porcelain'], check=True,
                     stall_seconds=GIT_STALL_SECONDS).stdout
    if any(Path(line[9:]).resolve() == workspace.resolve()
           for line in listed.splitlines() if line.startswith('worktree ')):
        return False
    history_marker = Path(readme_path(str(workspace)))
    # The store also bootstraps a nested .gitignore (self-ignore rule): it is
    # reconstructible policy, not durable user content, so an empty store
    # holding only the marker and that rule is still a removable placeholder.
    from lib.file_history.store import store_dir
    history_gitignore = Path(store_dir(str(workspace))) / '.gitignore'
    allowed = {history_marker, history_gitignore}
    pending = [workspace]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink():
                    return False
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif not entry.is_file(follow_symlinks=False) or Path(entry.path) not in allowed:
                    return False
    return True


def _canonical_tree(root, head):
    """Capture live checkout bytes as a tree; comparisons never mint a commit.

    Retry polls of a blocked delivery used to commit every capture of a shared
    checkout that other sessions keep editing: one unreachable commit per poll
    and task (63k of 89k unreachable loose objects in Tofu's repository on
    2026-10-27). Only a merge, a pin or a receipt needs the commit.
    """
    from lib import integration_control as ic
    with tempfile.TemporaryDirectory(prefix='tofu-index-') as directory:
        return ic._alternate_index_tree(root, head, str(Path(directory) / 'index'))


def _tree_entries(root, rev, paths):
    from lib import integration_control as ic
    listed = ic._git(root, ['--literal-pathspecs', 'ls-tree', '-r', '-z', '--full-tree', rev,
                            '--', *paths], check=True).stdout
    entries = {}
    for entry in listed.split('\0'):
        if '\t' in entry:
            meta, path = entry.split('\t', 1)
            mode, _, blob = meta.split(' ')
            entries[path] = (mode, blob)
    return entries


def _pending_task_paths(root, source, baseline, checkpoint):
    """Split the task delta against the captured canonical source.

    Overlapping chains re-carry commits already delivered under other SHAs.
    A path whose source entry already equals the checkpoint entry is applied;
    re-applying it would only fail. `git apply --3way` cannot merge a creation
    patch onto an existing path (it reports "does not match index"), so a path
    the task creates that the source already holds with other bytes is an
    add/add conflict named explicitly.
    """
    from lib import integration_control as ic
    paths = [path for path in ic._git(root, ['diff', '--no-renames', '--name-only', '-z',
                                             baseline, checkpoint], check=True).stdout.split('\0') if path]
    if not paths:
        return [], []
    before, current, wanted = (_tree_entries(root, rev, paths) for rev in (baseline, source, checkpoint))
    pending = [path for path in paths if current.get(path) != wanted.get(path)]
    conflicts = [path for path in pending
                 if path not in before and path in current and path in wanted]
    return pending, conflicts


def _merge_workspace_delta(root, source, patch_path, task_id, index_path, baseline, checkpoint,
                           message=''):
    """Resolve against captured workspace bytes without touching its index."""
    from lib import integration_control as ic

    environment = {'GIT_INDEX_FILE': str(index_path)}
    ic._git(root, ['read-tree', source], env=environment, check=True)
    pending, created_conflicts = _pending_task_paths(root, source, baseline, checkpoint)
    if created_conflicts:
        return '', ('task and canonical both created different content at: '
                    + ', '.join(created_conflicts[:20])), sorted(created_conflicts)
    if pending:
        Path(patch_path).write_bytes(ic._git(root, [
            '--literal-pathspecs', 'diff', '--no-renames', '--binary', '--full-index', '--no-ext-diff',
            '--no-textconv', baseline, checkpoint, '--', *pending], check=True, binary=True).stdout)
        applied = ic._git(root, ['apply', '--cached', '--3way', '--whitespace=nowarn',
                                 str(patch_path)], env=environment)
    else:
        applied = ic._git(root, ['write-tree'], env=environment)
    if applied.returncode:
        conflicts = ic._git(root, ['ls-files', '-u', '-z'], env=environment, check=True).stdout
        paths = conflicted_paths(conflicts)
        from lib.project_mod.generated_integration import (
            GeneratedOutputError, resolve_generated_conflicts)
        try:
            rebuilt = resolve_generated_conflicts(root, paths, checkpoint,
                lambda _root, args, env=None, **kwargs: ic._git(
                    root, args, env={**environment, **(env or {})},
                    input_text=kwargs.pop('stdin', None), **kwargs))
        except GeneratedOutputError as error:
            return '', str(error), paths
        if rebuilt is None:
            return '', applied.stderr or applied.stdout or f'Git merge failed with exit {applied.returncode}', paths
    tree = ic._git(root, ['write-tree'], env=environment, check=True).stdout.strip()
    target = ic._git(root, ['commit-tree', tree, '-p', source, '-m',
                            message or f'Tofu workspace delivery: {task_id}'], env={
        **environment, 'GIT_AUTHOR_NAME': 'Tofu Integration',
        'GIT_AUTHOR_EMAIL': 'integration@tofu.local',
        'GIT_COMMITTER_NAME': 'Tofu Integration',
        'GIT_COMMITTER_EMAIL': 'integration@tofu.local',
    }, check=True).stdout.strip()
    return target, '', []


def deliver_task_delta(row, receipt, save_receipt):
    from lib import integration_control as ic

    root = Path(row['project_root'])
    owner, task_id = int(row['user_id']), row['task_id']
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
    changed = [path for path in ic._git(root, ['diff', '--name-only', '-z', baseline,
                                             checkpoint], check=True).stdout.split('\0') if path]
    # Checkout-wide admission made unrelated editors wait for both full-tree
    # captures. Only these paths can be published. The lease owner widens
    # aliases/directory transitions conservatively and orders physical locks.
    claims = [('file', str(root / path)) for path in changed]

    def hold(latest, current):
        if not changed and not repair:
            return None  # Terminal bookkeeping has no filesystem publication.
        from lib.project_dispatch.publication import publication_reason
        reason = publication_reason(latest, current)
        if reason:
            return save_receipt(latest, current, state='blocked', phase='workspace_adoption',
                                reason=reason, retry=None)
        return None

    def save(*, locked=False, **updates):
        def persist():
            latest = ic._state.get_workspace(str(root), task_id, user_id=owner)
            current = dict((latest.get('origin') or {}).get('delivery') or {})
            if current.get('taskOutcome') == 'completed':
                held = hold(latest, current)
                if held is not None:
                    return held
            if current.get('taskOutcome') != 'completed' or current.get('state') in {'delivered', 'preserved'}:
                return current
            if (updates.get('reason') == 'workspace_busy'
                    and (current.get('retry') or {}).get('condition') == 'conflict_inputs_changed'):
                return current
            if updates.get('state') in {'delivered', 'preserved'}:
                updates.setdefault('retry', None)
            return save_receipt(latest, current, **updates)
        if locked:
            return persist()
        with ic._repo_lock(str(root)):
            return persist()

    def admitted():
        latest = ic._state.get_workspace(str(root), task_id, user_id=owner)
        current = dict((latest.get('origin') or {}).get('delivery') or {})
        if current.get('taskOutcome') == 'completed':
            held = hold(latest, current)
            if held is not None:
                return latest, held, False
        if (current.get('taskOutcome') != 'completed'
                or current.get('state') in {'delivered', 'preserved'}):
            return latest, current, False
        if ((current.get('repair') or {}) != repair
                or latest.get('checkpoint_sha') != row['checkpoint_sha']
                or (latest.get('origin') or {}).get('sourceSnapshot') != (row.get('origin') or {}).get('sourceSnapshot')
                or latest.get('state') == 'discarded'):
            raise ic.IntegrationError('checkpoint_changed_during_publication')
        return latest, current, True

    def same_paths(left, right):
        return ic._git(root, ['--literal-pathspecs', 'diff', '--quiet', left, right,
                             '--', *changed]).returncode == 0

    try:
        with workspace_claims_lease(claims, timeout=5.0), ic._repo_lock(str(root)):
            _, receipt, allowed = admitted()
            if not allowed:
                return receipt
            head = ic._rev(root, 'HEAD')
            if (head and ic._git(root, [
                    'merge-base', '--is-ancestor', checkpoint, head]).returncode == 0):
                return save(locked=True, state='delivered', phase='no_changes', reason='',
                            checkpointSha=row['checkpoint_sha'], deltaCheckpointSha=checkpoint,
                            headSha=head, targetSha=head, publicationMode='committed')
            source = _canonical_tree(root, head)
            reuse = (receipt.get('retry') or {}).get('sourceSha') or ''
            if not changed:
                return save(locked=True, state='delivered', phase='no_changes', reason='',
                            checkpointSha=row['checkpoint_sha'], deltaCheckpointSha=checkpoint, headSha=head,
                            targetSha=ic._checkpoint_tree(root, head, source, task_id, reuse_commit=reuse),
                            publicationMode='working_tree')
            previous_target = receipt.get('targetSha')
            if (receipt.get('phase') == 'commit_adoption' and previous_target and head
                    and receipt.get('checkpointSha') == row['checkpoint_sha']
                    and receipt.get('deltaCheckpointSha') == checkpoint
                    and ic._git(root, ['merge-base', '--is-ancestor', previous_target, head]).returncode == 0):
                return save(locked=True, state='delivered', reason='', headSha=head,
                            publicationMode='committed')
            if (receipt.get('phase') == 'workspace_adoption'
                    and receipt.get('checkpointSha') == row['checkpoint_sha']
                    and receipt.get('deltaCheckpointSha') == checkpoint
                    and previous_target and same_paths(source, previous_target)):
                return save(locked=True, state='delivered', reason='', headSha=head,
                            publicationMode='working_tree')
            # History/index movement cannot block delivery of unchanged files.
            fingerprint = input_fingerprint(
                baseline=baseline, checkpoint=checkpoint, branch='',
                source_tree=ic._git(root, ['--literal-pathspecs', 'ls-tree', '-z', source,
                                         '--', *changed], check=True).stdout,
                command='', gate_options={})
            previous_retry = receipt.get('retry') or {}
            if (receipt.get('state') in {'blocked', 'failed'}
                    and previous_retry.get('condition') == 'conflict_inputs_changed'
                    and previous_retry.get('fingerprint') == fingerprint):
                return save(locked=True, state='blocked', phase='workspace_adoption',
                            reason=(receipt.get('reason') if receipt.get('reason') != 'workspace_busy' else
                                    'task_delta_conflict: unchanged conflict inputs require an owned repair checkpoint'))
            source = ic._checkpoint_tree(root, head, source, task_id, reuse_commit=reuse)

        with tempfile.TemporaryDirectory(prefix='task-delta-', dir=str(ic._workspace_root())) as directory:
            patch_path = Path(directory) / 'task.patch'
            target, conflict, paths = _merge_workspace_delta(
                root, source, patch_path, task_id, Path(directory) / 'index', baseline, checkpoint)
            if conflict:
                with ic._repo_lock(str(root)):
                    ref = f'refs/tofu/delivery-inputs/u{owner}/{ic._safe_task(task_id)}'
                    return pin_receipt(root, ref, source, lambda: save(
                        locked=True, state='blocked', phase='workspace_adoption',
                        retry=retry_evidence('conflict_inputs_changed', fingerprint, baseline,
                                             checkpoint, source, paths),
                        reason='task_delta_conflict: ' + conflict[:700]))
            resolved = ic._git(root, ['diff', '--binary', '--full-index', '--no-ext-diff',
                                     '--no-textconv', source, target], check=True, binary=True).stdout
            patch_path.write_bytes(resolved)
            with workspace_claims_lease(claims, timeout=5.0), ic._repo_lock(str(root)):
                _, receipt, allowed = admitted()
                if not allowed:
                    return receipt
                current = _canonical_tree(root, ic._rev(root, 'HEAD'))
                if not same_paths(source, current):
                    return save(locked=True, state='blocked', phase='workspace_adoption',
                                reason='canonical_changed_during_merge', retry=None)
                recovery_ref = f'refs/tofu/delivery-checkpoints/u{owner}/{ic._safe_task(task_id)}'
                ic._git(root, ['update-ref', recovery_ref, target], check=True)
                receipt = save(locked=True, state='pending', phase='workspace_adoption', reason='',
                               retry=None, sourceSha=source, checkpointSha=row['checkpoint_sha'],
                               deltaCheckpointSha=checkpoint, targetSha=target,
                               headSha=ic._rev(root, 'HEAD'), publicationMode='working_tree')
                if receipt.get('state') != 'pending':
                    return receipt
                _, receipt, allowed = admitted()
                if not allowed:
                    return receipt
                # Git preflights the whole resolved patch; no --reject, no
                # real-index writes and no conflict markers in user files.
                if resolved:
                    applied = ic._git(root, ['apply', '--binary', '--whitespace=nowarn', str(patch_path)])
                    if applied.returncode:
                        return save(locked=True, state='blocked', reason='task_delta_conflict: '
                                    + (applied.stderr or applied.stdout)[:700], retry=None)
                return save(locked=True, state='delivered', reason='',
                            headSha=ic._rev(root, 'HEAD'), publicationMode='working_tree')
    except WorkspaceBusyError:
        return save(state='blocked', phase='workspace_adoption', reason='workspace_busy')

def reclaim_delivered_workspace(row) -> bool:
    """Reclaim a reconstructible clone after its terminal receipt is durable.

    Called under the task tree-retirement lease, outside canonical writer admission.
    Immutable checkpoint refs retain delivered bytes. Uncaptured source/index
    edits and durable ignored artifacts, including FileHistory undo history,
    retain the checkout for recovery/undo. Managed-repository shared-data links
    are reconstructible; removal unlinks them without touching their targets.
    """
    from lib import integration_control as ic
    from lib.file_history.store import readme_path
    if not row.get('managed'):
        return False
    baseline = (row.get('origin') or {}).get('sourceSnapshot')
    checkpoint = row.get('checkpoint_sha')
    if not baseline or not checkpoint:
        return False
    root = Path(row['project_root'])
    workspace = Path(row['workspace_path'])
    if not workspace.exists():
        return True
    # Checkpointing deliberately leaves the writer's HEAD/index untouched, so
    # ordinary delivered edits still look dirty. Compare against the captured
    # checkpoint, not HEAD, and require durable reachability before retirement.
    if ic._rev(root, ic._checkpoint_ref(int(row['user_id']), row['task_id'])) != checkpoint:
        return False
    if retired_workspace_placeholder(root, workspace):
        return _retire_captured_workspace(row, root, workspace, baseline, checkpoint)
    discovered = ic._git(workspace, ['rev-parse', '--show-toplevel', '--git-common-dir'])
    identity = discovered.stdout.splitlines()
    canonical = ic._git(root, ['rev-parse', '--git-common-dir'], check=True).stdout.strip()
    if (discovered.returncode or len(identity) != 2
            or Path(identity[0]).resolve() != workspace.resolve()
            or (workspace / identity[1]).resolve() != (root / canonical).resolve()):
        return False
    source_head = ic._rev(workspace, 'HEAD')
    if not source_head or ic._git(root, ['merge-base', '--is-ancestor', source_head, checkpoint]).returncode:
        return False
    staged = [ic._git(workspace, ['diff', '--cached', '--quiet', revision, '--']).returncode
              for revision in (source_head, checkpoint)]
    if not all(code in (0, 1) for code in staged) or 0 not in staged:
        return False  # A third staged version is not recoverable from either tree.
    with tempfile.TemporaryDirectory(prefix='tofu-reclaim-index-') as directory:
        current = ic._alternate_index_tree(workspace, checkpoint, str(Path(directory) / 'index'))
    if current != ic._rev(root, checkpoint + '^{tree}'):
        return False
    ignored = ic._git(workspace, ['ls-files', '--others', '--ignored',
                                 '--exclude-standard', '-z'], check=True).stdout
    environment = workspace / '.venv'
    if environment.is_dir():
        known = {'bin', 'Scripts', 'lib', 'lib64', 'Lib', 'include', 'Include', 'pyvenv.cfg'}
        if any(entry.name not in known for entry in environment.iterdir()):
            return False
        package_roots = list((environment / 'lib').glob('python*/site-packages'))
        if (environment / 'Lib/site-packages').is_dir():
            package_roots.append(environment / 'Lib/site-packages')
        if any(entry.name not in {'tofu-source-packages.pth', '__pycache__'}
               for packages in package_roots for entry in packages.iterdir()):
            return False
    dependencies = workspace / 'node_modules'
    if dependencies.is_dir() and not dependencies.is_symlink():
        if any(not entry.is_symlink() and entry.name not in NODE_TOOL_CACHE_DIRECTORIES
               for entry in dependencies.iterdir()):
            return False
    reconstructible = {'.venv', 'node_modules', '.pytest_cache', '.ruff_cache', '__pycache__'}
    # An empty FileHistory store is only its regenerated bootstrap marker and
    # its nested self-ignore rule; snapshots, the tracked index and backups
    # remain durable undo history.
    from lib.file_history.store import store_dir
    bootstrap = {Path(os.path.relpath(readme_path(str(workspace)), str(workspace))).as_posix(),
                 Path(os.path.relpath(os.path.join(store_dir(str(workspace)), '.gitignore'),
                                      str(workspace))).as_posix()}
    from lib.project_mod.managed_repository import is_shared_link
    durable_ignored = [path for path in ignored.split('\0') if path and path not in bootstrap
                       and not any(part in reconstructible for part in Path(path).parts)
                       and not is_shared_link(root, workspace, path)]
    if durable_ignored:
        return False
    return _retire_captured_workspace(row, root, workspace, baseline, checkpoint)


def _retire_captured_workspace(row, root, workspace, baseline, checkpoint):
    from lib import integration_control as ic

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
            return False
        ic._remove_controlled_worktree(root, workspace)
        if workspace.exists():
            raise ic.IntegrationError('Source retirement did not remove the owned checkout')
        return True


_OPERATIONS_IN_PROGRESS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge',
                           'rebase-apply', 'BISECT_LOG')


def commit_delivered_task_delta(row) -> dict | None:
    """Record a reviewed working-tree delivery as one commit on the current branch.

    Working-tree delivery leaves the task's bytes uncommitted in a checkout that
    other writers keep editing, where a whole-workspace checkpoint can neither
    attribute nor verify them. This commits exactly the delivered
    baseline-to-checkpoint delta on top of committed HEAD: the delta merges in a
    private index; then, holding Git's index.lock, only the index entries of the
    delta's paths move to the new commit and the branch moves by compare-and-swap.
    No checkout file is written, so other writers' uncommitted edits, including
    borrowed input the task built on, stay uncommitted.

    Returns the acknowledgement to record, or None while a transient condition
    (index busy, branch moved, staged edits on a delta path, an operation in
    progress) keeps the next delivery pass due.
    """
    from lib import integration_control as ic
    from lib.project_mod.delivery_retry import retry_evidence

    root = Path(row['project_root'])
    baseline, checkpoint = _delivered_delta(row)
    if not baseline or not checkpoint:
        return {'status': 'skipped', 'reason': 'The delivered task has no registered baseline'}
    branch = ic._git(root, ['symbolic-ref', '-q', 'HEAD'],
                     stall_seconds=GIT_STALL_SECONDS).stdout.strip()
    head = ic._rev(root, branch) if branch.startswith('refs/heads/') else ''
    if not head:
        return {'status': 'skipped',
                'reason': 'The checkout has no committed branch to receive the delivery'}
    if _contains(root, checkpoint, head):
        return {'status': 'committed', 'branchRef': branch, 'commitSha': head, 'contained': True}
    with tempfile.TemporaryDirectory(prefix='task-delta-commit-',
                                     dir=str(ic._workspace_root())) as directory:
        commit, conflict, paths = _merge_workspace_delta(
            root, head, Path(directory) / 'task.patch', row['task_id'],
            Path(directory) / 'index', baseline, checkpoint,
            message=_delivery_commit_message(root, row, baseline, checkpoint))
    if conflict:
        # The delta needs bytes HEAD lacks (borrowed uncommitted input) or
        # conflicts with committed history; the delivered bytes stay as they are.
        evidence = retry_evidence('conflict_inputs_changed', '', baseline, checkpoint, head, paths)
        return {'status': 'entangled', 'branchRef': branch, 'headSha': head,
                'reason': conflict[:700],
                **{key: evidence[key] for key in (
                    'conflictPaths', 'conflictPathCount', 'conflictPathsTruncated')}}
    if ic._rev(root, commit + '^{tree}') == ic._rev(root, head + '^{tree}'):
        return {'status': 'no_changes', 'branchRef': branch, 'headSha': head}
    return _publish_task_commit(root, branch, head, commit, row['task_id'])


def _delivered_delta(row):
    """The baseline/checkpoint pair whose bytes working-tree delivery published."""
    origin = row.get('origin') or {}
    repair = (origin.get('delivery') or {}).get('repair') or {}
    if repair:
        return repair.get('baselineSha'), repair.get('checkpointSha')
    return origin.get('sourceSnapshot'), row.get('checkpoint_sha')


def _contains(root, ancestor, descendant) -> bool:
    from lib import integration_control as ic
    result = ic._git(root, ['merge-base', '--is-ancestor', ancestor, descendant],
                     stall_seconds=GIT_STALL_SECONDS)
    if result.returncode not in (0, 1):
        raise ic.IntegrationError(f'git merge-base failed: {result.stderr.strip()[:800]}')
    return result.returncode == 0


def _delivery_commit_message(root, row, baseline, checkpoint) -> str:
    """Name the commit after the Worker's own commits; trailers keep provenance."""
    from lib import integration_control as ic
    from lib.project_dispatch.admission import KEY
    subjects = [subject for subject in reversed(ic._git(
        root, ['log', '--no-merges', '--format=%s', f'{baseline}..{checkpoint}'],
        check=True, stall_seconds=GIT_STALL_SECONDS).stdout.splitlines())
        if subject and not subject.startswith('Tofu checkpoint: ')]
    if len(subjects) == 1:
        lines = [subjects[0]]
    else:
        lines = [f'Deliver reviewed project work {row["task_id"]}']
        if subjects:
            lines += [''] + [f'- {subject}' for subject in subjects]
    assignment = ((row.get('origin') or {}).get(KEY) or {}).get('assignmentId')
    lines += ['', f'Tofu-Task: {row["task_id"]}', f'Tofu-Checkpoint: {checkpoint}']
    if assignment:
        lines.append(f'Tofu-Assignment: {assignment}')
    return '\n'.join(lines) + '\n'


def _tree_changes(root, old, new):
    from lib import integration_control as ic
    tokens = ic._git(root, ['diff-tree', '-r', '-z', '--no-renames', '--no-abbrev', old, new],
                     check=True, stall_seconds=GIT_STALL_SECONDS).stdout.split('\0')
    changes = []
    for meta, path in zip(tokens[0::2], tokens[1::2]):
        old_mode, new_mode, old_oid, new_oid, _status = meta.lstrip(':').split(' ')
        changes.append((path,
                        None if not old_mode.strip('0') else (old_mode, old_oid),
                        None if not new_mode.strip('0') else (new_mode, new_oid)))
    return changes


def _operation_in_progress(root) -> bool:
    from lib import integration_control as ic
    arguments = [part for name in _OPERATIONS_IN_PROGRESS for part in ('--git-path', name)]
    paths = ic._git(root, ['rev-parse', *arguments], check=True,
                    stall_seconds=GIT_STALL_SECONDS).stdout.splitlines()
    return any((root / path).exists() for path in paths)


def _index_admits(root, changes) -> bool:
    """Only paths the index holds at HEAD, or already at the delivery, may move."""
    from lib import integration_control as ic
    listed = ic._git(root, ['--literal-pathspecs', 'ls-files', '-s', '-v', '-z', '--',
                            *(path for path, _old, _new in changes)],
                     check=True, stall_seconds=GIT_STALL_SECONDS).stdout
    index: dict[str, list[list[str]]] = {}
    for record in listed.split('\0'):
        meta, _, path = record.partition('\t')
        if path:
            index.setdefault(path, []).append(meta.split(' '))
    for path, old, new in changes:
        records = index.get(path, [])
        if len(records) > 1:
            return False  # an unresolved merge
        actual = None
        if records:
            tag, mode, oid, stage = records[0]
            if tag != 'H' or stage != '0':
                return False  # assume-unchanged/skip-worktree bits hide working bytes
            actual = (mode, oid)
        if actual not in (old, new):
            return False  # another writer staged this path
    return True


def _prepared_index(root, index_path: Path, changes, oid_length: int) -> Path:
    from lib import integration_control as ic
    descriptor, name = tempfile.mkstemp(prefix='index.tofu-delivery-', dir=str(index_path.parent))
    os.close(descriptor)
    prepared = Path(name)
    try:
        shutil.copyfile(index_path, prepared)
        zero = '0' * oid_length
        records = ''.join(f'{new[0]} {new[1]}\t{path}\0' if new else f'0 {zero}\t{path}\0'
                          for path, _old, new in changes)
        ic._git(root, ['update-index', '-z', '--index-info'], input_text=records,
                env={'GIT_INDEX_FILE': str(prepared)}, check=True)
    except BaseException:
        prepared.unlink(missing_ok=True)
        raise
    return prepared


def _publish_task_commit(root, branch, head, commit, task_id):
    """Index entries of the delta's paths first, then the branch: Git's own order.

    An interruption between the two leaves the delivery staged, never a HEAD
    whose index reverts it; the next pass recomputes and finishes it.
    """
    from lib import integration_control as ic
    changes = _tree_changes(root, head, commit)
    index_path = (root / ic._git(root, ['rev-parse', '--git-path', 'index'], check=True,
                                 stall_seconds=GIT_STALL_SECONDS).stdout.strip()).resolve()
    lock_path = index_path.with_name(index_path.name + '.lock')
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        return None  # Another Git process owns the index; a later pass retries.
    installed, prepared = False, None
    try:
        with os.fdopen(descriptor, 'wb') as lock:
            current = ic._git(root, ['symbolic-ref', '-q', 'HEAD'],
                              stall_seconds=GIT_STALL_SECONDS).stdout.strip()
            if (current != branch or ic._rev(root, branch) != head
                    or _operation_in_progress(root) or not _index_admits(root, changes)):
                return None
            prepared = _prepared_index(root, index_path, changes, len(head))
            lock.write(prepared.read_bytes())
            lock.flush()
            os.fsync(lock.fileno())
        os.replace(lock_path, index_path)
        installed = True
    finally:
        if not installed:
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()
        if prepared is not None:
            with contextlib.suppress(FileNotFoundError):
                prepared.unlink()
    moved = ic._git(root, ['update-ref', '-m', f'tofu delivery: {task_id}', branch, commit, head],
                    stall_seconds=GIT_STALL_SECONDS)
    if moved.returncode:
        raise ic.IntegrationError(
            f'The index carries delivery commit {commit}, but {branch} did not move from '
            f'{head}: {moved.stderr.strip()[:400]}')
    return {'status': 'committed', 'branchRef': branch, 'parentSha': head,
            'commitSha': commit, 'paths': len(changes)}
