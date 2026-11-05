#!/usr/bin/env python3
"""Explicit, supervised Git maintenance for one repository.

Model- and harness-run Git never starts automatic maintenance
(``lib/git_runtime.py``); this command replaces it. Entry point:
``python3 scripts/git_maintenance.py --repo CHECKOUT [--expire-days N] [--apply]``.

The default run only reports: object and pack counts, how many unreachable
loose objects pruning would remove, and whether the commit-graph verifies.
``--apply`` waits for the repository's workspace Git metadata lease
(``lib/project_mod/workspace_lease.py``), then runs with the lease descriptors
inherited by every child:

1. ``pack-refs --all --prune``
2. ``repack -d -l -A --unpack-unreachable=<expiry>``: one pack; unreachable
   packed objects newer than the expiry become loose instead of being dropped
3. ``prune --expire=<expiry>``
4. ``commit-graph write --reachable``, replacing a stale graph. Git neither
   reads nor writes a commit-graph in a shallow repository, so there a graph
   left from before is removed instead: unused, it only fails verification.

and then verifies the commit-graph (``--fsck`` adds a connectivity check).

Other sessions' in-flight work survives: every ref, reflog, worktree HEAD and
index (also of linked worktrees whose directory is missing) is a reachability
root, and an object newer than the expiry keeps everything it references, which
covers staged blobs in private indexes and commits not yet referenced. Reflogs
are never expired and worktree administration is never pruned. The expiry is
at least one day. Decision record: docs/runbooks/GIT_WORKSPACE_GUARD.md
("Automatic Git maintenance never runs inside commands").
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.git_checkpoint_policy import git_policy_environment  # noqa: E402
from lib.git_runtime import git_executable  # noqa: E402
from lib.project_mod.workspace_lease import (  # noqa: E402
    WorkspaceBusyError, workspace_git_metadata_lease, workspace_lease_fds,
)

MINIMUM_EXPIRE_DAYS = 1
LEASE_BUSY_EXIT = 3
_SPLIT_GRAPH_FILE = re.compile(r'commit-graph-chain|graph-[0-9a-f]{40,64}\.graph')


class MaintenanceError(RuntimeError):
    """A Git step failed; later steps did not run."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        [git_executable(), '-C', str(repo), *args], env=git_policy_environment(),
        text=True, capture_output=True, check=False, pass_fds=workspace_lease_fds())
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()[-2000:]
        raise MaintenanceError(f'git {" ".join(args)} exited {process.returncode}: {detail}')
    return process


def _object_counts(repo: Path) -> dict[str, int]:
    counts = {}
    for line in _git(repo, 'count-objects', '-v').stdout.splitlines():
        key, _, value = line.partition(':')
        if value.strip().isdigit():
            counts[key] = int(value)
    return counts


def _report_counts(label: str, counts: dict[str, int]) -> None:
    print(f'{label}: loose={counts.get("count", 0)} '
          f'loose_kib={counts.get("size", 0)} packs={counts.get("packs", 0)} '
          f'pack_kib={counts.get("size-pack", 0)} garbage={counts.get("garbage", 0)}')


def _shallow(repo: Path) -> bool:
    return _git(repo, 'rev-parse', '--is-shallow-repository').stdout.strip() == 'true'


def _remove_unused_commit_graph(common: Path) -> list[str]:
    info = common / 'objects' / 'info'
    split = info / 'commit-graphs'
    files = [info / 'commit-graph']
    if split.is_dir():
        files += [path for path in split.iterdir() if _SPLIT_GRAPH_FILE.fullmatch(path.name)]
    removed = []
    for path in files:
        if path.is_file():
            path.unlink()
            removed.append(str(path.relative_to(common)))
    if split.is_dir() and not any(split.iterdir()):
        split.rmdir()
    return removed


def _commit_graph_status(repo: Path) -> str:
    try:
        _git(repo, 'commit-graph', 'verify')
    except MaintenanceError as error:
        return 'invalid: ' + str(error).splitlines()[0][:300]
    return 'valid'


def _report(repo: Path, expiry: str) -> None:
    _report_counts('objects', _object_counts(repo))
    worktrees = _git(repo, 'worktree', 'list', '--porcelain').stdout.split('\n\n')
    worktrees = [entry for entry in worktrees if entry.startswith('worktree ')]
    missing = sum('\nprunable ' in entry for entry in worktrees)
    print(f'worktrees: {len(worktrees)} (directory missing, still kept as roots: {missing})')
    candidates = _git(repo, 'prune', '--dry-run', f'--expire={expiry}').stdout.split()
    kinds = {}
    for kind in candidates[1::2]:
        kinds[kind] = kinds.get(kind, 0) + 1
    print(f'prune --expire={expiry} would remove {len(candidates) // 2} loose objects {kinds}')
    shallow = ' (shallow repository: --apply removes it, Git cannot use it)' if _shallow(repo) else ''
    print(f'commit-graph: {_commit_graph_status(repo)}{shallow}')


def _apply(repo: Path, common: Path, expiry: str, *, fsck: bool) -> None:
    _report_counts('before', _object_counts(repo))
    steps = [
        ('pack-refs', ['pack-refs', '--all', '--prune']),
        ('repack', ['repack', '-d', '-l', '-A', f'--unpack-unreachable={expiry}']),
        ('prune', ['prune', f'--expire={expiry}']),
        ('commit-graph', None if _shallow(repo) else ['commit-graph', 'write', '--reachable']),
        ('verify', ['commit-graph', 'verify']),
    ]
    if fsck:
        steps.append(('fsck', ['fsck', '--connectivity-only', '--no-dangling', '--no-progress']))
    for name, arguments in steps:
        started = time.monotonic()
        if arguments is None:
            removed = _remove_unused_commit_graph(common)
            print(f'{name}: shallow repository, removed unused {removed or "nothing"}')
            continue
        _git(repo, *arguments)
        print(f'{name}: ok in {time.monotonic() - started:.1f}s', flush=True)
    _report_counts('after', _object_counts(repo))


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', required=True, type=Path,
                        help='checkout of the repository to maintain')
    parser.add_argument('--expire-days', type=int, default=MINIMUM_EXPIRE_DAYS,
                        help='prune unreachable objects older than this many days '
                             f'(minimum and default {MINIMUM_EXPIRE_DAYS})')
    parser.add_argument('--apply', action='store_true',
                        help='run the maintenance; without it only report')
    parser.add_argument('--lease-timeout', type=float, default=None,
                        help='seconds to wait for the Git metadata lease (default: wait)')
    parser.add_argument('--fsck', action='store_true',
                        help='with --apply, finish with git fsck --connectivity-only')
    arguments = parser.parse_args(argv)
    if arguments.expire_days < MINIMUM_EXPIRE_DAYS:
        parser.error(f'--expire-days must be at least {MINIMUM_EXPIRE_DAYS}: a shorter '
                     'expiry can delete objects that concurrent Git commands are still writing')
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    expiry = f'{arguments.expire_days}.days.ago'
    try:
        repo = Path(_git(arguments.repo, 'rev-parse', '--show-toplevel').stdout.strip())
        common = Path(repo, _git(repo, 'rev-parse', '--git-common-dir').stdout.strip())
        print(f'repository: {repo} (common directory {common.resolve()})')
        if not arguments.apply:
            _report(repo, expiry)
            print('report only; rerun with --apply to maintain')
            return 0
        if (common / 'gc.pid').exists():
            raise MaintenanceError(f'{common / "gc.pid"} exists: another git gc may be '
                                   'running; nothing changed')
        print(f'waiting for the Git metadata lease of {repo}', flush=True)
        with workspace_git_metadata_lease(str(repo), timeout=arguments.lease_timeout):
            _apply(repo, common, expiry, fsck=arguments.fsck)
    except WorkspaceBusyError as error:
        print(f'error: Git metadata of {arguments.repo} is busy; nothing changed: {error}',
              file=sys.stderr)
        return LEASE_BUSY_EXIT
    except MaintenanceError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
