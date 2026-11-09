"""Merge completed task changes into the live canonical workspace.

Entries: deliver_task_delta, commit_delivered_task_delta. Baselines, intents,
revocation and receipts retain integration/delivery ownership. A private index
computes the three-way merge. No validation checkout is materialized.

A reviewed project delivery in a repository that opted into automatic commits
lands as exactly its own delta, committed on the current branch through the
shared checkout-landing owner (``lib.project_mod.checkout_landing``, which
``scripts/git_land.py`` also uses): other writers' uncommitted edits are
carried forward by three-way merge, and the receipt says delivered only once
the commit is verified reachable from the branch. A delta that needs other
writers' uncommitted bytes, or overlaps their edits, is held with a visible
reason and lands once its inputs change. Every other task publishes files
only; HEAD, the real index and release gates stay independent of it. A
no-change delivery names HEAD and captures nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from lib.git_runtime import GIT_STALL_SECONDS
from lib.log import get_logger
from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_claims_lease
from lib.project_mod.delivery_retry import input_fingerprint, retry_evidence, conflicted_paths, pin_receipt
from lib.project_mod.workspace_runtime import NODE_TOOL_CACHE_DIRECTORIES

logger = get_logger(__name__)

# Held conflicts whose last full recheck this process found unchanged, by
# (canonical root, owner, task id): the receipt identity plus _hold_observation
# taken before that recheck read any content. While a pass observes the same
# inputs the same hold stands, without the whole-delta claims lease, a capture
# or a merge; any difference runs the full recheck, which replaces or drops the
# entry. One entry per held task delivery in this process.
_UNCHANGED_HOLDS: dict[tuple[str, int, str], tuple] = {}
# DolphinFS records whole-second timestamps: a stat cannot order a write made
# within the same second as the observation (git's racily clean entries).
_TIMESTAMP_RESOLUTION_SECONDS = 1

_INTEGRATION_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Tofu Integration', 'GIT_AUTHOR_EMAIL': 'integration@tofu.local',
    'GIT_COMMITTER_NAME': 'Tofu Integration', 'GIT_COMMITTER_EMAIL': 'integration@tofu.local',
}


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


def _canonical_path_entries(root, head, paths):
    """Observe live checkout bytes at ``paths`` alone, as a capture would list them.

    Returns exactly what ``ls-tree -z <_canonical_tree capture> -- <paths>``
    prints: a private index seeded from ``head`` at those paths stages them by
    the capture's rules (tracked paths always, untracked ones unless ignored).
    No tree is written and no other path is read. None when a path holds a
    directory, which ``ls-tree`` names by a tree this index never writes.
    """
    from lib import integration_control as ic
    with tempfile.TemporaryDirectory(prefix='tofu-index-') as directory:
        environment = {'GIT_INDEX_FILE': str(Path(directory) / 'index')}

        def git(*args, **kwargs):
            # Like the capture, these scale with the delta's volume: stall-supervised.
            kwargs.setdefault('stall_seconds', GIT_STALL_SECONDS)
            return ic._git(root, ['--literal-pathspecs', *args], env=environment,
                           check=True, **kwargs)

        seeded = git('ls-tree', '-r', '-z', '--full-tree', head, '--', *paths).stdout if head else ''
        if seeded:
            # The supervised runner has no stdin; the entries are already in hand.
            git('update-index', '-z', '--index-info', input_text=seeded, stall_seconds=None)
        staged = [entry.split('\t', 1)[1] for entry in seeded.split('\0') if '\t' in entry]
        staged += [path for path in git('ls-files', '--others', '--exclude-standard', '-z',
                                        '--', *paths).stdout.split('\0') if path]
        if staged:
            git('add', '-A', '--', *staged)
        wanted, entries = set(paths), []
        for entry in git('ls-files', '-s', '-z', '--', *paths).stdout.split('\0'):
            if not entry:
                continue
            meta, path = entry.split('\t', 1)
            if path not in wanted:
                return None
            mode, blob, _stage = meta.split(' ')
            entries.append(f"{mode} {'commit' if mode == '160000' else 'blob'} {blob}\t{path}\0")
        return ''.join(entries)


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
    return _commit(root, tree, source, message or f'Tofu workspace delivery: {task_id}'), '', []


def _commit(root, tree, parent, message):
    from lib import integration_control as ic
    return ic._git(root, ['commit-tree', tree, '-p', parent, '-m', message],
                   env=_INTEGRATION_IDENTITY, check=True).stdout.strip()


def _delivered_without_publication(root, row, receipt, head, changed, checkpoint):
    """Receipt updates when HEAD already holds the delta, else None.

    An empty delta publishes nothing: its receipt names HEAD, never a capture
    of the checkout, whose files hold other writers' uncommitted bytes.
    """
    from lib import integration_control as ic

    def reaches(revision):
        return ic._git(root, ['merge-base', '--is-ancestor', revision, head]).returncode == 0

    if not changed or (head and reaches(checkpoint)):
        return dict(state='delivered', phase='no_changes', reason='',
                    checkpointSha=row['checkpoint_sha'], deltaCheckpointSha=checkpoint,
                    headSha=head, targetSha=head, publicationMode='committed')
    target = receipt.get('targetSha')
    if (receipt.get('phase') == 'commit_adoption' and target and head
            and receipt.get('checkpointSha') == row['checkpoint_sha']
            and receipt.get('deltaCheckpointSha') == checkpoint and reaches(target)):
        return dict(state='delivered', reason='', headSha=head, publicationMode='committed')
    return None


def _held_conflict(receipt, *, landing):
    """A conflict hold that only a change of its recorded inputs can lift."""
    if (receipt.get('retry') or {}).get('condition') != 'conflict_inputs_changed':
        return False
    if landing:
        return receipt.get('state') == 'blocked' and receipt.get('phase') == 'commit_adoption'
    return (receipt.get('state') in {'blocked', 'failed'}
            and receipt.get('phase') == 'workspace_adoption')


def _hold_identity(row, receipt):
    return json.dumps([{key: value for key, value in receipt.items() if key != 'updatedAt'},
                       row.get('checkpoint_sha'), (row.get('origin') or {}).get('sourceSnapshot'),
                       row.get('state')], sort_keys=True, default=str)


def _hold_observation(root, paths, *, landing):
    """Every input a held conflict's recheck reads, observed without file content.

    HEAD; the stat identity of each delta path and of every ``.gitignore`` and
    ``info/exclude`` deciding whether an untracked delta path is staged; for a
    commit landing also the real index entries at those paths and Git's
    in-progress markers. ``None`` when an observed file changed within the
    timestamp resolution of now, where a stat cannot order it against a
    concurrent write: the full recheck decides then.
    """
    from lib import integration_control as ic
    from lib.project_mod.checkout_landing import _IN_PROGRESS, git_paths
    head = ic._rev(root, 'HEAD')
    directories = {''}
    for path in paths:
        parts = path.split('/')[:-1]
        directories.update('/'.join(parts[:depth]) for depth in range(1, len(parts) + 1))
    files = [root / path for path in paths]
    files += [root / directory / '.gitignore' for directory in sorted(directories)]
    files += git_paths(root, 'info/exclude', *(_IN_PROGRESS if landing else ()))
    racy_from = (int(time.time()) - _TIMESTAMP_RESOLUTION_SECONDS) * 1_000_000_000
    identities = []
    for path in files:
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            identities.append(None)
            continue
        if max(info.st_mtime_ns, info.st_ctime_ns) >= racy_from:
            return None
        identities.append((info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                           info.st_ino, info.st_dev))
    staged = ic._git(root, ['--literal-pathspecs', 'ls-files', '-s', '-z', '--', *paths], check=True,
                     stall_seconds=GIT_STALL_SECONDS).stdout if landing else ''
    return head, tuple(identities), staged


def _lands_as_commit(row, root) -> bool:
    """A reviewed project delivery into a repository that opted into automatic commits."""
    from lib.linear_git_checkpoint import is_enabled
    from lib.project_dispatch.admission import KEY
    return KEY in (row.get('origin') or {}) and is_enabled(root)


def _landing_fingerprint(root, branch, head, baseline, checkpoint, paths):
    """Everything a held landing read, observed at the delta's paths alone.

    The branch tip, the checkout's bytes and index entries at those paths and
    any Git operation in progress: a hold is rechecked only once one changes.
    """
    from lib import integration_control as ic
    from lib.project_mod.checkout_landing import operations_in_progress
    staged = ic._git(root, ['--literal-pathspecs', 'ls-files', '-s', '-z', '--', *paths],
                     check=True, stall_seconds=GIT_STALL_SECONDS).stdout
    return input_fingerprint(baseline=baseline, checkpoint=checkpoint, branch=f'{branch} {head}',
                             source_tree=[_canonical_path_entries(root, head, paths), staged],
                             command='commit', gate_options={'operations': operations_in_progress(root)})


def _paths_lacking_their_base(root, head, baseline, checkpoint):
    """Delta paths whose HEAD entry differs from the bytes the delta was computed on."""
    pending, _ = _pending_task_paths(root, head, baseline, checkpoint)
    before, current = _tree_entries(root, baseline, pending), _tree_entries(root, head, pending)
    return [path for path in pending if before.get(path) != current.get(path)]


def _borrowed_paths(root, row, paths):
    """PATHS whose input bytes were other writers' uncommitted edits (sourceHead..sourceSnapshot)."""
    from lib import integration_control as ic
    origin = row.get('origin') or {}
    head, snapshot = origin.get('sourceHead'), origin.get('sourceSnapshot')
    if not paths or not head or not snapshot or head == snapshot:
        return []
    listed = ic._git(root, ['--literal-pathspecs', 'diff', '--no-renames', '--name-only', '-z',
                            head, snapshot, '--', *paths], check=True).stdout
    borrowed = set(listed.split('\0'))
    return [path for path in paths if path in borrowed]


def _commit_reviewed_delta(row, root, baseline, checkpoint, changed, claims, admitted, save,
                           unchanged_hold):
    """Land a reviewed project delta as its own commit on the current branch.

    The delta merges onto committed HEAD in a private index. The checkout-landing
    owner then plans every changed path against the checkout's current bytes
    (uncommitted edits are carried forward by three-way merge) and, under Git's
    own ``index.lock``, rechecks the plan, writes the files, moves exactly those
    index entries and the branch by compare-and-swap. The receipt says delivered
    only after the commit is verified reachable from the branch. A delta that
    needs bytes HEAD lacks, conflicts with history, or overlaps uncommitted
    edits is held ``blocked`` with retry evidence; unchanged inputs keep the
    hold without merging again, and nothing is written meanwhile.
    """
    from lib import integration_control as ic
    from lib.project_mod import checkout_landing

    owner, task_id = int(row['user_id']), row['task_id']
    with workspace_claims_lease(claims, timeout=5.0), ic._repo_lock(str(root)):
        _, receipt, allowed = admitted()
        if not allowed:
            return receipt
        head = ic._rev(root, 'HEAD')
        settled = _delivered_without_publication(root, row, receipt, head, changed, checkpoint)
        if settled is not None:
            return save(locked=True, **settled)
        branch = ic._git(root, ['symbolic-ref', '-q', 'HEAD'],
                         stall_seconds=GIT_STALL_SECONDS).stdout.strip()
        if not head or not branch.startswith('refs/heads/'):
            return save(locked=True, state='blocked', phase='commit_adoption', retry=None,
                        reason='task_delta_uncommitted: the checkout has no committed branch '
                               'to receive the delivery')
        fingerprint = _landing_fingerprint(root, branch, head, baseline, checkpoint, changed)
        retry = receipt.get('retry') or {}
        if (receipt.get('state') == 'blocked' and receipt.get('phase') == 'commit_adoption'
                and retry.get('condition') == 'conflict_inputs_changed'
                and retry.get('fingerprint') == fingerprint):
            return unchanged_hold(receipt)
    file_mode = ic._git(root, ['config', '--bool', 'core.fileMode']).stdout.strip() != 'false'
    message = _delivery_commit_message(root, row, baseline, checkpoint)

    def hold(reason, paths):
        with ic._repo_lock(str(root)):
            ref = f'refs/tofu/delivery-inputs/u{owner}/{ic._safe_task(task_id)}'
            return pin_receipt(root, ref, head, lambda: save(
                locked=True, state='blocked', phase='commit_adoption', reason=reason[:900],
                retry=retry_evidence('conflict_inputs_changed', fingerprint, baseline,
                                     checkpoint, head, paths)))

    while True:
        with tempfile.TemporaryDirectory(prefix='task-delta-commit-',
                                         dir=str(ic._workspace_root())) as directory:
            commit, conflict, paths = _merge_workspace_delta(
                root, head, Path(directory) / 'task.patch', task_id,
                Path(directory) / 'index', baseline, checkpoint, message=message)
        if conflict:
            paths = paths or _paths_lacking_their_base(root, head, baseline, checkpoint)
            entangled = _borrowed_paths(root, row, paths)
            if entangled:
                return hold('task_delta_entangled: ' + ', '.join(entangled[:20])
                            + ' carry other writers\' uncommitted edits this delivery built on; '
                            'it lands as its own commit once they are committed', paths)
            return hold('task_delta_conflict: ' + conflict[:700], paths)
        tree = ic._rev(root, commit + '^{tree}')
        if tree == ic._rev(root, head + '^{tree}'):
            return save(state='delivered', phase='no_changes', reason='', retry=None,
                        checkpointSha=row['checkpoint_sha'], deltaCheckpointSha=checkpoint,
                        headSha=head, targetSha=head, publicationMode='committed')
        try:
            steps = checkout_landing.plan(root, head, tree, file_mode)
            if steps.tree != tree:
                commit = _commit(root, steps.tree, head, message)
            step_claims = [('file', str(root / step.path)) for step in steps]
            with workspace_claims_lease(step_claims, timeout=5.0), ic._repo_lock(str(root)):
                _, receipt, allowed = admitted()
                if not allowed:
                    return receipt
                checkout_landing.revalidate(root, branch, head, steps)
                recovery_ref = f'refs/tofu/delivery-checkpoints/u{owner}/{ic._safe_task(task_id)}'
                ic._git(root, ['update-ref', recovery_ref, commit], check=True)
                receipt = save(locked=True, state='pending', phase='commit_adoption', reason='',
                               retry=None, sourceSha=head, checkpointSha=row['checkpoint_sha'],
                               deltaCheckpointSha=checkpoint, targetSha=commit, headSha=head,
                               publicationMode='committed')
                if receipt.get('state') != 'pending':
                    return receipt
                _, receipt, allowed = admitted()
                if not allowed:
                    return receipt
                checkout_landing.publish(root, branch, head, commit, steps,
                                         f'tofu delivery: {task_id}')
                tip = ic._rev(root, branch)
                if not _contains(root, commit, tip):
                    return save(locked=True, state='pending', phase='commit_adoption',
                                reason=f'task_delta_commit_unreachable: {branch} is at {tip}, '
                                       f'which does not contain {commit}; the next pass lands it again')
                return save(locked=True, state='delivered', reason='', headSha=tip,
                            targetSha=commit, publicationMode='committed')
        except checkout_landing.Replan:
            head = ic._rev(root, branch)
            fingerprint = _landing_fingerprint(root, branch, head, baseline, checkpoint, changed)
        except checkout_landing.Refused as refusal:
            held = [path for path in changed if any(
                item == path or item.startswith(path + ': ') for item in refusal.paths)]
            return hold('task_delta_entangled: ' + refusal.reason + ''.join(
                f'; {item}' for item in refusal.paths[:10]), held)
        except checkout_landing.IndexBusy:
            return save(state='blocked', phase='commit_adoption', reason='workspace_busy')


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
    landing = bool(changed) and _lands_as_commit(row, root)
    # A held conflict is rechecked when an input changes, not on every pass:
    # one observed exactly as before its last unchanged verdict keeps the hold
    # without the whole-delta lease (docs/CONTINUOUS_DELIVERY.md).
    hold_key, observation = (str(root), owner, task_id), None
    if changed and _held_conflict(receipt, landing=landing):
        observation = _hold_observation(root, changed, landing=landing)
        if (observation is not None
                and _UNCHANGED_HOLDS.get(hold_key) == (_hold_identity(row, receipt), *observation)):
            return receipt
    _UNCHANGED_HOLDS.pop(hold_key, None)

    def unchanged_hold(result):
        if observation is not None and _held_conflict(result, landing=landing):
            _UNCHANGED_HOLDS[hold_key] = (_hold_identity(row, result), *observation)
        return result

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

    def fingerprint_of(source_entries):
        return input_fingerprint(baseline=baseline, checkpoint=checkpoint, branch='',
                                 source_tree=source_entries, command='', gate_options={})

    def unchanged_conflict_reason(current):
        return (current.get('reason') if current.get('reason') != 'workspace_busy' else
                'task_delta_conflict: unchanged conflict inputs require an owned repair checkpoint')

    def settled_conflict_unchanged(current, head):
        """Keep a settled conflict whose exact inputs are unchanged, capturing nothing.

        Its fingerprint reads the canonical checkout only at task-delta paths,
        so observing those paths decides. Anything else, including a receipt
        that adoption recovery could still settle, takes the whole capture.
        """
        retry = current.get('retry') or {}
        if (not changed or current.get('state') not in {'blocked', 'failed'}
                or current.get('phase') != 'workspace_adoption'
                or retry.get('condition') != 'conflict_inputs_changed'):
            return False
        try:
            entries = _canonical_path_entries(root, head, changed)
            previous_target = current.get('targetSha')
            if entries is None or (
                    previous_target and current.get('checkpointSha') == row['checkpoint_sha']
                    and current.get('deltaCheckpointSha') == checkpoint
                    and ic._git(root, ['--literal-pathspecs', 'ls-tree', '-z', previous_target,
                                       '--', *changed], check=True).stdout == entries):
                return False
        except Exception as error:
            # The whole capture below decides instead and reports its own failure.
            logger.debug('Settled-conflict path recheck skipped task=%s: %s', task_id, error)
            return False
        return fingerprint_of(entries) == retry.get('fingerprint')

    try:
        if landing:
            return _commit_reviewed_delta(row, root, baseline, checkpoint, changed, claims,
                                          admitted, save, unchanged_hold)
        with workspace_claims_lease(claims, timeout=5.0), ic._repo_lock(str(root)):
            _, receipt, allowed = admitted()
            if not allowed:
                return receipt
            head = ic._rev(root, 'HEAD')
            settled = _delivered_without_publication(root, row, receipt, head, changed, checkpoint)
            if settled is not None:
                return save(locked=True, **settled)
            if settled_conflict_unchanged(receipt, head):
                return unchanged_hold(save(locked=True, state='blocked', phase='workspace_adoption',
                                           reason=unchanged_conflict_reason(receipt)))
            source = _canonical_tree(root, head)
            reuse = (receipt.get('retry') or {}).get('sourceSha') or ''
            previous_target = receipt.get('targetSha')
            if (receipt.get('phase') == 'workspace_adoption'
                    and receipt.get('checkpointSha') == row['checkpoint_sha']
                    and receipt.get('deltaCheckpointSha') == checkpoint
                    and previous_target and same_paths(source, previous_target)):
                return save(locked=True, state='delivered', reason='', headSha=head,
                            publicationMode='working_tree')
            # History/index movement cannot block delivery of unchanged files.
            fingerprint = fingerprint_of(ic._git(
                root, ['--literal-pathspecs', 'ls-tree', '-z', source, '--', *changed],
                check=True).stdout)
            previous_retry = receipt.get('retry') or {}
            if (receipt.get('state') in {'blocked', 'failed'}
                    and previous_retry.get('condition') == 'conflict_inputs_changed'
                    and previous_retry.get('fingerprint') == fingerprint):
                return unchanged_hold(save(locked=True, state='blocked', phase='workspace_adoption',
                                           reason=unchanged_conflict_reason(receipt)))
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
    if not checkout_reconstructible(root, workspace, checkpoint):
        return False
    return _retire_captured_workspace(row, root, workspace, baseline, checkpoint)


def checkout_reconstructible(root: Path, workspace: Path, captured: str) -> bool:
    """Whether removing WORKSPACE loses nothing beyond CAPTURED and rebuildable files.

    CAPTURED is a commit of the checkout's bytes: a delivered task's checkpoint,
    or the work in progress a discarded local checkout has just saved. HEAD must
    be in its history, the staged state one of the two trees and the current
    bytes exactly its tree. Ignored files may only be dependency bridges,
    caches, shared-data links and the FileHistory bootstrap: undo history and
    other durable ignored artifacts keep the checkout.
    """
    from lib import integration_control as ic
    from lib.file_history.store import readme_path
    discovered = ic._git(workspace, ['rev-parse', '--show-toplevel', '--git-common-dir'])
    identity = discovered.stdout.splitlines()
    canonical = ic._git(root, ['rev-parse', '--git-common-dir'], check=True).stdout.strip()
    if (discovered.returncode or len(identity) != 2
            or Path(identity[0]).resolve() != workspace.resolve()
            or (workspace / identity[1]).resolve() != (root / canonical).resolve()):
        return False
    source_head = ic._rev(workspace, 'HEAD')
    if not source_head or ic._git(root, ['merge-base', '--is-ancestor', source_head, captured]).returncode:
        return False
    staged = [ic._git(workspace, ['diff', '--cached', '--quiet', revision, '--']).returncode
              for revision in (source_head, captured)]
    if not all(code in (0, 1) for code in staged) or 0 not in staged:
        return False  # A third staged version is not recoverable from either tree.
    with tempfile.TemporaryDirectory(prefix='tofu-reclaim-index-') as directory:
        current = ic._alternate_index_tree(workspace, captured, str(Path(directory) / 'index'))
    if current != ic._rev(root, captured + '^{tree}'):
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
    return not durable_ignored


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


def commit_delivered_task_delta(row) -> dict | None:
    """Record a reviewed working-tree delivery as one commit on the current branch.

    Working-tree delivery leaves the task's bytes uncommitted in a checkout that
    other writers keep editing, where a whole-workspace checkpoint can neither
    attribute it. This commits exactly the delivered baseline-to-checkpoint
    delta on top of committed HEAD: the delta merges in a private index, then
    the linear checkpoint's publication moves only the index entries of the
    delta's paths and the branch by compare-and-swap. No checkout file is
    written, so other writers' uncommitted edits, including borrowed input the
    task built on, stay uncommitted, and entries they staged are preserved.

    Returns the acknowledgement to record, or None while a transient condition
    (index busy, branch moved, an operation in progress) keeps the next
    delivery pass due.
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
    from lib.linear_git_checkpoint import LinearCheckpointError, publish_commit
    try:
        published = publish_commit(root, branch_ref=branch, base_sha=head, target_sha=commit,
                                   reflog=f'tofu delivery: {row["task_id"]}')
    except LinearCheckpointError as error:
        raise ic.IntegrationError(str(error)) from error
    if published is None:
        return None
    return {'status': 'committed', 'branchRef': branch, 'parentSha': head,
            'commitSha': commit, 'indexSynchronized': published['indexSynchronized']}


def delivered_paths(row) -> list[str]:
    """Repository paths whose bytes the working-tree delivery of ROW published."""
    from lib import integration_control as ic
    baseline, checkpoint = _delivered_delta(row)
    if not baseline or not checkpoint:
        return []
    listed = ic._git(Path(row['project_root']),
                     ['diff', '--name-only', '--no-renames', '-z', baseline, checkpoint],
                     check=True, stall_seconds=GIT_STALL_SECONDS).stdout
    return [path for path in listed.split('\0') if path]


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
