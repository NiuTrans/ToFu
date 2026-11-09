#!/usr/bin/env python3
"""Land a finished worktree branch on the shared checkout's current branch.

``git merge`` refuses as soon as a branch touches any file that another writer
is editing, uncommitted, in the shared checkout, so one conversation's
in-flight edit locked every branch that shared a file with it. This command
records the same ``--no-ff`` merge commit (first parent: the pre-integration
tip) without that lock:

1. Git's own merge machinery computes the merge in a private scratch worktree;
   a real branch conflict refuses. Known generated outputs are rebuilt there.
2. Every path the merge changes is compared with the shared checkout's
   current bytes. An unmodified file takes the merged content. A file with
   uncommitted concurrent edits gets a three-way source merge, so those edits
   stay in place, still uncommitted, on top of the new tip. Known generated
   outputs are rebuilt separately for the committed and working source views.
3. Holding Git's own index.lock, the plan is revalidated, files are replaced
   atomically, the index entries of exactly those paths move to the merge,
   and the branch moves by compare-and-swap.

Landing runs no tests (owner decision 2026-11-08): the branch's own
verification before landing is the evidence, and a cleanly merged result is
published without a test ladder. After a landing, conversations whose unlanded
work changes a landed file receive a notice
(lib/project_mod/integration_radar.py), and a landed lane's checkout is
recycled for the next lane (lib/project_mod/lanes.py).

--check previews a landing without changing anything: it merges, rebuilds
generated outputs and validates the publication plan exactly as a landing
does, then reports the paths that would change and the uncommitted edits that
would be carried forward.

Overlapping lines, a staged change, an untracked file in the way, a file-type
clash or an operation in progress refuses before anything is written. Then the
branch's worktree is removed (``git worktree remove``, never forced) and the
branch deleted (``git branch -d``) unless --keep is given.

Usage:  python3 scripts/git_land.py BRANCH [--into CHECKOUT] [--keep] [--check]
Exit:   0 landed or already merged; 1 refused, nothing changed; 2 Git or
        filesystem error (the message states what was written).
Contract: docs/modules/project_integration.md, "Landing worktree branches".
Planning and publication (steps 2 and 3) are the shared
lib.project_mod.checkout_landing owner, which reviewed project deliveries also
use; generation uses the shared lib.project_mod.generated_integration owner.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

# Direct CLI execution must find the same shared owners as imports do.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.project_mod.checkout_landing import (  # noqa: E402 - after the path setup above
    IndexBusy, LandingError, Refused, Replan as _Replan, git as _git, git_paths as _git_paths,
    operations_in_progress as _operations_in_progress, plan as _plan, publish as _publish_plan,
    rev as _rev, revalidate as _revalidate)


def _is_ancestor(top: Path, ancestor: str, descendant: str) -> bool:
    result = _git(top, 'merge-base', '--is-ancestor', ancestor, descendant, check=False)
    if result.returncode not in (0, 1):
        raise LandingError(f'git merge-base failed: {result.stderr.strip()[:800]}')
    return result.returncode == 0


def _refuse_task_input_snapshots(top: Path, branch_ref: str, source: str) -> None:
    """A platform input is borrowed context, never a worker's merge parent."""
    inputs = _git(top, 'for-each-ref', '--format=%(objectname)',
                  'refs/tofu/inputs/').stdout.split()
    if not inputs:
        return
    snapshots = set(inputs).intersection(
        _git(top, 'rev-list', f'{branch_ref}..{source}').stdout.split())
    if not snapshots:
        return
    paths = []
    for snapshot in sorted(snapshots):
        carried = _git(top, 'diff-tree', '--root', '--no-commit-id', '--name-only',
                       '-r', '-z', snapshot).stdout.split('\0')
        paths.extend(f'{snapshot}: {path}' for path in carried if path)
    branch = branch_ref.removeprefix('refs/heads/')
    raise Refused(
        'the landing range contains a task-input snapshot commit: '
        + ', '.join(sorted(snapshots)), paths,
        f'Create a fresh worktree from the shared checkout {top} at its current branch '
        f'{branch} ({branch_ref}), then cherry-pick only your own commits onto a new '
        'branch and land that branch. Do not start from the task workspace\'s HEAD.')


def _main_checkout(start: Path) -> Path:
    lines = _git(start, 'worktree', 'list', '--porcelain').stdout.splitlines()
    if not lines or not lines[0].startswith('worktree ') or 'bare' in lines[1:2]:
        raise Refused('the repository has no main checkout to land into',
                      advice='Pass --into CHECKOUT.')
    return Path(lines[0][len('worktree '):])


def _worktree_of(top: Path, ref: str) -> Path | None:
    path = None
    for line in _git(top, 'worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            path = Path(line[len('worktree '):])
        elif line == f'branch {ref}':
            return path
    return None


def _all_generated(top: Path, paths: list[str]) -> bool:
    """Every path is declared ``linguist-generated`` by the current .gitattributes."""
    fields = _git(top, 'check-attr', '-z', '--cached', 'linguist-generated', '--',
                  *paths).stdout.split('\0')
    values = fields[2::3]
    return len(values) == len(paths) and all(value in ('true', 'set') for value in values)


def _merge_tree(top: Path, head: str, source: str) -> str:
    """The merged tree: the branch's own for a fast-forward, else merged and
    regenerated in a private checkout."""
    if _is_ancestor(top, head, source):
        return _git(top, 'rev-parse', f'{source}^{{tree}}').stdout.strip()
    from lib.project_mod.landing_verification import invalidate_bytecode, merge_checkout

    bases = _git(top, 'merge-base', '--all', head, source, check=False)
    if bases.returncode == 1 and not bases.stdout.strip():
        raise Refused('the branch shares no history with the current branch')
    if bases.returncode:
        raise LandingError(f'git merge-base failed: {bases.stderr.strip()[:800]}')
    # Merge and regeneration read a private working tree; never the canonical
    # checkout's source or index. All Git state stays in that checkout.
    with merge_checkout(top, head) as scratch:
        # Recorded test runs reuse this warm checkout: the sources the merge
        # rewrites must not keep a same-size, same-second stale bytecode.
        invalidate_bytecode(scratch, source)
        return _merge_in(top, scratch, head, source, bases.stdout.split())


def _merge_in(top: Path, scratch: Path, head: str, source: str, bases: list[str]) -> str:
    """Compute the merged tree in SCRATCH; a real conflict refuses."""
    _git(scratch, 'read-tree', head)
    _git(scratch, 'reset', '--hard', head)
    merged = _git(scratch, 'merge-recursive', *bases, '--', head, source, check=False)
    if merged.returncode == 1:
        unmerged = _git(scratch, 'ls-files', '-u', '-z').stdout
        paths = sorted({entry.split('\t', 1)[1]
                        for entry in unmerged.split('\0') if '\t' in entry})
        if paths:
            from lib.project_mod.generated_integration import (
                GeneratedOutputError, resolve_generated_conflicts)
            def scratch_git(_root, args, env=None, **kwargs):
                environment = {'GIT_INDEX_FILE': str(_git_paths(scratch, 'index')[0])}
                environment.update(env or {})
                return _git(scratch, *args, env=environment, **kwargs)
            try:
                if resolve_generated_conflicts(
                        scratch, paths, source, scratch_git, source_root=top) is not None:
                    tree = _git(scratch, 'write-tree').stdout.strip()
                    for state in ('AUTO_MERGE', 'MERGE_HEAD', 'MERGE_MODE', 'MERGE_MSG'):
                        _git_paths(scratch, state)[0].unlink(missing_ok=True)
                    return tree
            except GeneratedOutputError as error:
                raise Refused('generated outputs could not be rebuilt', paths, str(error)) from error
        advice = ('Merge the current branch into the worktree branch, resolve and '
                  'commit there, then rerun.')
        if paths and _all_generated(top, paths):
            advice = ('Only generated files conflict. Merge the current branch into the '
                      'worktree branch, regenerate these files from the merged sources '
                      'with their generators (never hand-merge them), run their checks, '
                      'commit there, then rerun.')
        raise Refused(
            'the branch conflicts with the current branch',
            paths or [line for line in merged.stdout.splitlines()
                      if line.startswith('CONFLICT')],
            advice)
    if merged.returncode:
        raise LandingError('git merge-recursive failed: '
                           + (merged.stderr or merged.stdout).strip()[:800])
    return _git(scratch, 'write-tree').stdout.strip()


def _open_holders(path: Path) -> list[str] | None:
    """PIDs (visible to this user) holding ``path`` open; None without /proc."""
    proc = Path('/proc')
    if not proc.is_dir():
        return None
    target = os.path.realpath(path)
    holders = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if any(os.readlink(fd) == target for fd in (process / 'fd').iterdir()):
                holders.append(process.name)
        except OSError:
            continue  # exited, or not ours to inspect
    return holders


def _busy_lock(lock_path: Path) -> Refused | None:
    """Refusal for an existing index.lock, with the evidence to tell busy from stale.

    None: the lock was released while it was examined, so publication can be
    planned again at once.
    """
    try:
        info = lock_path.stat()
    except FileNotFoundError:
        return None
    age = time.time() - info.st_mtime
    holders = _open_holders(lock_path)
    if holders == []:
        # Git closes its lock descriptor just before renaming the lock over
        # the index; a lock that left during the scan was busy, not stale.
        try:
            again = lock_path.stat()
        except FileNotFoundError:
            return None
        if (again.st_ino, again.st_mtime_ns) != (info.st_ino, info.st_mtime_ns):
            return None
    if holders is None or holders:
        owner = f'process {", ".join(holders)}' if holders else 'another Git process'
        return Refused(f'{lock_path} is held by {owner}: Git is busy in this checkout',
                       advice='Rerun when it finishes.')
    return Refused(
        f'{lock_path} exists ({age:.0f} s old) but no process of this user holds it open; '
        'a Git process probably exited while holding it',
        advice='Confirm no Git command is running in this checkout, remove that file '
               '(Git requires the same manual step), and rerun.')



def _publish(top: Path, branch_ref: str, head: str, commit: str, steps, reflog: str) -> None:
    try:
        _publish_plan(top, branch_ref, head, commit, steps, reflog)
    except IndexBusy as busy:
        refusal = _busy_lock(busy.lock_path)
        if refusal is None:
            raise _Replan() from None
        raise refusal from None


def _cleanup(top: Path, source_ref: str, tip: str) -> list[str]:
    """Retire a source checkout only once its current commits have landed."""
    from lib.project_mod import lanes
    notes = []
    name = source_ref.removeprefix('refs/heads/')
    worktree = _worktree_of(top, source_ref)
    source_tip = _rev(top, source_ref)
    if source_tip and not _is_ancestor(top, source_tip, tip):
        return [f'kept branch {name}: it has unlanded commits; land it again',
                *([f'kept worktree {worktree}'] if worktree is not None else [])]
    if worktree is not None:
        repo = lanes.repository(top)
        if lanes.is_lane(repo, worktree):
            outcome = lanes.retire_lane(repo, worktree, tip)
            if outcome == 'kept':
                return [f'kept lane {worktree}: it has uncommitted or untracked files',
                        f'kept branch {name}: its lane remains']
            notes.append(f'recycled lane {worktree} for the next lane' if outcome == 'spare'
                         else f'removed lane {worktree}')
        else:
            removed = _git(top, 'worktree', 'remove', str(worktree), check=False)
            if removed.returncode:
                return [f'kept worktree {worktree}: {removed.stderr.strip()}',
                        f'kept branch {name}: its worktree remains']
            notes.append(f'removed worktree {worktree}')
    deleted = _git(top, 'branch', '-d', name, check=False)
    notes.append(f'deleted branch {name}' if deleted.returncode == 0
                 else f'kept branch {name}: {deleted.stderr.strip()}')
    if deleted.returncode == 0:
        lanes.drop_wip(lanes.repository(top), name)
    return notes


def land(branch: str, *, into: str | None = None, keep: bool = False,
         check: bool = False, cwd: str | os.PathLike = '.') -> dict:
    """Land BRANCH; raises Refused (nothing changed) or LandingError."""
    top = (Path(_git(into, 'rev-parse', '--show-toplevel').stdout.strip()) if into
           else _main_checkout(Path(cwd)))
    branch_ref = _git(top, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    if not branch_ref.startswith('refs/heads/'):
        raise Refused(f'{top} has no checked-out branch to land on')
    source = _rev(top, branch)
    if not source:
        raise Refused(f'{branch!r} does not name a commit')
    source_ref = _git(top, 'rev-parse', '--symbolic-full-name', branch, check=False).stdout.strip()
    if source_ref == branch_ref:
        raise Refused(f'{branch} is the branch checked out in {top}')
    busy = _operations_in_progress(top)
    if busy:
        raise Refused(f'{top} has an operation in progress ({", ".join(busy)})',
                      advice='Rerun after it finishes.')
    file_mode = _git(top, 'config', '--bool', 'core.fileMode', check=False).stdout.strip() != 'false'
    is_branch = source_ref.startswith('refs/heads/')
    label = source_ref.removeprefix('refs/heads/') if is_branch else source[:12]
    target = branch_ref.removeprefix('refs/heads/')
    message = (f"Merge branch '{label}' into {target}" if is_branch
               else f"Merge commit '{label}' into {target}")
    result: dict = {'branch': label, 'into': target, 'checkout': str(top), 'source': source}
    merged_for = tree = None
    while True:
        head = _rev(top, branch_ref)
        result['preIntegration'] = head
        if _is_ancestor(top, source, head):
            result['status'] = 'already-merged'
            break
        _refuse_task_input_snapshots(top, branch_ref, source)
        if merged_for != head:
            tree = _merge_tree(top, head, source)
        merged_for = head
        try:
            steps = _plan(top, head, tree, file_mode)
            result.update(paths=len(steps), carried=[step.path for step in steps if step.carried])
            if check:
                _revalidate(top, branch_ref, head, steps)
                result['status'] = 'landable'
                return result
            commit = _git(top, 'commit-tree', steps.tree, '-p', head, '-p', source, '-m', message).stdout.strip()
            _publish(top, branch_ref, head, commit, steps,
                     f'merge {label}: landed by scripts/git_land.py')
        except _Replan:
            continue
        result.update(status='landed', commit=commit)
        break
    if check:
        return result
    if result['status'] == 'landed':
        result['notified'] = _notify_concurrent_writers(top, head, result['commit'], label,
                                                        source_ref if is_branch else '')
    if not keep and is_branch:
        result['cleanup'] = _cleanup(top, source_ref, result.get('commit') or head)
    return result


def _notify_concurrent_writers(top: Path, head: str, commit: str, label: str,
                               source_ref: str) -> list[dict]:
    """Leave integration-radar notices; advice only, so a failure never fails the landing."""
    try:
        from lib.project_mod.integration_radar import notify_landing
        paths = _git(top, 'diff', '--name-only', '--no-renames', '-z', head, commit).stdout
        subject = _git(top, 'log', '-1', '--no-merges', '--format=%s',
                       f'{head}..{commit}').stdout.strip()
        return notify_landing(top, [path for path in paths.split('\0') if path],
                              commit=commit, subject=subject, branch=label,
                              landed_root=_worktree_of(top, source_ref) if source_ref else None)
    except Exception as error:  # noqa: BLE001 - advice must never undo a published landing
        print(f'Integration radar notices skipped: {error}', file=sys.stderr)
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Land a finished worktree branch on the shared checkout without '
                    'locking on other writers\' uncommitted edits.')
    parser.add_argument('branch', help='branch (or commit) to land')
    parser.add_argument('--into', help='checkout whose current branch receives the merge '
                                       '(default: the repository\'s main checkout)')
    parser.add_argument('--keep', action='store_true',
                        help='keep the worktree and branch after landing')
    parser.add_argument('--check', action='store_true',
                        help='preview the landing: merge privately and report the paths '
                             'that would change; changes nothing')
    args = parser.parse_args(argv)
    from contextlib import nullcontext
    from lib.runtime.protected_steps import protected_step
    try:
        # A restart must not boot from a half-landed checkout (restart protection).
        with nullcontext() if args.check else protected_step('git_land', detail=args.branch):
            result = land(args.branch, into=args.into, keep=args.keep, check=args.check)
    except Refused as refusal:
        print(f'Refused: {refusal.reason}. Nothing was changed.')
        for path in refusal.paths:
            print(f'  {path}')
        if refusal.advice:
            print(refusal.advice)
        return 1
    except LandingError as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2
    status = result['status']
    if status == 'already-merged':
        print(f"{result['branch']} is already merged into {result['into']} "
              f"({result['preIntegration']}).")
    elif status == 'landable':
        print(f"{result['branch']} merges cleanly into {result['into']}: "
              f"{result['paths']} paths would change.")
    else:
        print(f"Landed {result['branch']} on {result['into']}: merge {result['commit']}; "
              f"pre-integration tip {result['preIntegration']}; {result['paths']} paths changed.")
    if result.get('carried'):
        print('Carried forward uncommitted edits in: ' + ', '.join(result['carried']))
    for item in result.get('notified', []):
        who = f"\"{item['title']}\" ({item['writer']})" if item['title'] else item['writer']
        print(f"Notified {who}, whose unlanded work in {item['checkout']} also changes: "
              + ', '.join(item['paths']))
    for note in result.get('cleanup', []):
        print(note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
