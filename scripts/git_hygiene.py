#!/usr/bin/env python3
"""Inventory and conservatively reclaim merged Git branches and worktree debris.

Entry point: ``python3 scripts/git_hygiene.py --repo MAIN_CHECKOUT``. Standard
library only; every Git call runs with ``GIT_OPTIONAL_LOCKS=0`` and without
ambient ``GIT_*`` routing variables.

The default run is read-only. It reports registered worktrees, unregistered
integration-pool directories, local branches classified against HEAD and every
``--also-merged-into`` revision (ancestor-merged, patch-equivalent or
unmerged), and per-namespace ``refs/tofu/*`` counts.

``--apply`` first writes a restore manifest to ``data/git-hygiene/<UTC>.json``
and prints the restore command. It then deletes only:

* branches whose tip is an ancestor of HEAD or of an ``--also-merged-into``
  revision. It never deletes the current branch, a branch checked out, rebased
  or bisected in any worktree, or a branch anchoring such a revision. Each
  delete is guarded by the recorded tip (``update-ref`` old value);
* unregistered integration-pool directories holding only the file-history
  bootstrap marker and empty directories;
* with ``--remove-clean-worktrees``: unlocked, idle, clean registered
  worktrees whose HEAD stays reachable from kept refs and whose ignored files
  are only rebuildable caches (``git worktree remove`` without ``--force``).

``refs/tofu/*`` is reported and never modified. ``--restore MANIFEST``
recreates what a manifest recorded and never overwrites existing refs or paths.
"""

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

FORMAT = 'tofu.git-hygiene/v1'
GIT_TIMEOUT_SECONDS = 900
# FileHistory regenerates this marker on demand; it never holds undo history.
FILE_HISTORY_MARKER = '.tofu/file-history/README.txt'
MAX_MARKER_BYTES = 64 * 1024
REBUILDABLE_CACHE_PARTS = frozenset({'__pycache__', '.pytest_cache', '.ruff_cache'})
LINKED_RUNTIME_NAMES = frozenset({'.venv', 'node_modules'})
WORKTREE_IDLE_SECONDS = 24 * 3600
MAX_POOL_DIRECTORY_ENTRIES = 256
PREVIEW = 20
WORKERS = max(1, min(8, len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity')
                     else os.cpu_count() or 1))
CLASSES = ('merged_head', 'merged_also', 'patch_equivalent', 'unmerged')


class HygieneError(RuntimeError):
    """A precondition failed; nothing was changed by the failing step."""


def _environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    return env


def _git_argv(*args: str) -> list[str]:
    return ['git', '-c', 'core.fsmonitor=false', '-c', 'core.quotePath=false', *args]


def _git(cwd, *args: str, stdin: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        _git_argv(*args), cwd=str(cwd), input=stdin, env=_environment(),
        capture_output=True, text=True, encoding='utf-8', errors='surrogateescape',
        timeout=GIT_TIMEOUT_SECONDS, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise HygieneError(f'git {" ".join(args[:2])} failed: {detail}')
    return result


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ── Repository identity ──────────────────────────────────────────────────

def _repository(path: str) -> dict:
    top = Path(path).expanduser().resolve(strict=True)
    lines = _git(top, 'rev-parse', '--show-toplevel', '--git-common-dir',
                 '--verify', 'HEAD^{commit}').stdout.splitlines()
    if len(lines) != 3:
        raise HygieneError('ambiguous repository identity')
    if Path(lines[0]).resolve() != top:
        raise HygieneError(f'--repo must name a checkout root, not {top}')
    common = Path(lines[1])
    common = (common if common.is_absolute() else top / common).resolve()
    current = _git(top, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    return {'top': top, 'common': common, 'head': lines[2], 'currentBranch': current}


def _resolve_revisions(top: Path, revisions: list[str]) -> list[dict]:
    resolved = []
    for revision in revisions:
        sha = _git(top, 'rev-parse', '--verify', '--end-of-options',
                   revision + '^{commit}', check=False).stdout.strip()
        if not sha:
            raise HygieneError(f'--also-merged-into {revision!r} is not a commit')
        resolved.append({'rev': revision, 'sha': sha})
    return resolved


def _revision_containers(top: Path, head: str, also: list[dict]) -> dict[str, set[str] | None]:
    """Refs keeping each revision reachable; ``None`` when HEAD contains it.

    Branches merged into a dangling revision are its only remaining history,
    so an unreachable revision is refused instead of used as merge evidence.
    """
    containers: dict[str, set[str] | None] = {}
    for revision in also:
        if _git(top, 'merge-base', '--is-ancestor', revision['sha'], head,
                check=False).returncode == 0:
            containers[revision['sha']] = None
            continue
        refs = set(_git(top, 'for-each-ref', '--contains', revision['sha'],
                        '--format=%(refname)').stdout.split())
        if not refs:
            raise HygieneError(f'--also-merged-into {revision["rev"]!r} is not reachable from '
                               'any ref; anchor it with a branch first')
        containers[revision['sha']] = refs
    return containers


# ── Worktrees ────────────────────────────────────────────────────────────

def _worktrees(top: Path) -> list[dict]:
    items: list[dict] = []
    for line in _git(top, 'worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            items.append({'path': line[9:], 'head': '', 'branch': '', 'main': not items,
                          'locked': False, 'prunable': False, 'bare': False})
        elif not items:
            continue
        elif line.startswith('HEAD '):
            items[-1]['head'] = line[5:]
        elif line.startswith('branch '):
            items[-1]['branch'] = line[7:]
        elif line == 'bare':
            items[-1]['bare'] = True
        elif line == 'locked' or line.startswith('locked '):
            items[-1]['locked'] = True
        elif line == 'prunable' or line.startswith('prunable '):
            items[-1]['prunable'] = True
    return items


def _admin_directories(common: Path) -> list[Path]:
    linked = common / 'worktrees'
    children = sorted(path for path in linked.iterdir() if path.is_dir()) if linked.is_dir() else []
    return [common, *children]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='surrogateescape').strip()
    except OSError:
        return ''


def _busy_branches(common: Path) -> set[str]:
    """Branches a rebase or bisect in any worktree will write back."""
    busy = set()
    for admin in _admin_directories(common):
        for name in ('rebase-merge/head-name', 'rebase-apply/head-name'):
            value = _read_text(admin / name)
            if value.startswith('refs/heads/'):
                busy.add(value)
        value = _read_text(admin / 'BISECT_START')
        if value and not all(char in '0123456789abcdef' for char in value):
            busy.add(value if value.startswith('refs/heads/') else 'refs/heads/' + value)
    return busy


def _admin_directory(path: Path) -> Path | None:
    dotgit = path / '.git'
    if dotgit.is_dir():
        return dotgit
    value = _read_text(dotgit)
    if not value.startswith('gitdir: '):
        return None
    admin = Path(value[8:])
    return admin if admin.is_absolute() else (path / admin).resolve()


def _last_activity(path: Path) -> float:
    # Not the index: status scans with optional locks refresh it on idle clones.
    admin = _admin_directory(path)
    candidates = [path] + ([admin / 'HEAD', admin / 'logs' / 'HEAD'] if admin else [])
    stamps = []
    for candidate in candidates:
        try:
            stamps.append(candidate.stat().st_mtime)
        except OSError:
            continue
    return max(stamps, default=0.0)


def _rebuildable_tree(root: Path, relative: str) -> bool:
    """A collapsed ignored directory on the marker path holds only the marker."""
    entries = 0
    for current, names, files in os.walk(root / relative, followlinks=False):
        entries += len(names) + len(files)
        if entries > MAX_POOL_DIRECTORY_ENTRIES:
            return False
        if any((Path(current) / name).is_symlink() for name in names):
            return False
        for name in files:
            target = Path(current) / name
            if target.is_symlink() or target.relative_to(root).as_posix() != FILE_HISTORY_MARKER:
                return False
    return True


def _rebuildable_ignored(root: Path, entry: str) -> bool:
    relative = entry.rstrip('/')
    if any(part in REBUILDABLE_CACHE_PARTS for part in Path(relative).parts):
        return True
    if relative in LINKED_RUNTIME_NAMES:
        return (root / relative).is_symlink()
    if relative == FILE_HISTORY_MARKER:
        return not (root / relative).is_symlink()
    if entry.endswith('/') and FILE_HISTORY_MARKER.startswith(relative + '/'):
        return _rebuildable_tree(root, relative)
    return False


def _inspect_worktree(item: dict, now: float) -> dict:
    path = Path(item['path'])
    if item['prunable'] or item['bare'] or not path.is_dir():
        return {**item, 'state': 'prunable' if item['prunable'] else 'unavailable'}
    status = _git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=normal',
                  '--ignore-submodules=none', check=False)
    if status.returncode:
        return {**item, 'state': 'unknown', 'detail': status.stderr.strip()[:300]}
    changes = [entry for entry in status.stdout.split('\0') if entry]
    activity = _last_activity(path)
    result = {**item, 'state': 'dirty' if changes else 'clean', 'changes': len(changes),
              'lastActivity': _iso(activity), 'idle': now - activity >= WORKTREE_IDLE_SECONDS}
    if not changes and not item['main']:
        ignored = _git(path, 'ls-files', '--others', '--ignored', '--exclude-standard',
                       '--directory', '-z', check=False)
        if ignored.returncode:
            result['ignoredBlockers'] = ['(ignored files could not be listed)']
        else:
            result['ignoredBlockers'] = [
                entry for entry in ignored.stdout.split('\0')
                if entry and not _rebuildable_ignored(path, entry)][:PREVIEW]
    return result


def _inspect_worktrees(items: list[dict]) -> list[dict]:
    now = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda item: _inspect_worktree(item, now), items))


# ── Integration pool ─────────────────────────────────────────────────────

def _classify_pool_directory(path: Path) -> dict:
    files, directories, blockers = [], [], []
    entries = 0
    for current, names, filenames in os.walk(path, followlinks=False):
        entries += len(names) + len(filenames)
        if entries > MAX_POOL_DIRECTORY_ENTRIES:
            blockers.append(f'(more than {MAX_POOL_DIRECTORY_ENTRIES} entries)')
            break
        for name in names:
            target = Path(current) / name
            (blockers if target.is_symlink() else directories).append(
                target.relative_to(path).as_posix())
        for name in filenames:
            target = Path(current) / name
            relative = target.relative_to(path).as_posix()
            if (relative == FILE_HISTORY_MARKER and not target.is_symlink()
                    and target.stat().st_size <= MAX_MARKER_BYTES):
                files.append(relative)
            else:
                blockers.append(relative)
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = None
    return {'path': str(path), 'rebuildable': not blockers, 'files': sorted(files),
            'directories': sorted(directories), 'blockers': blockers[:PREVIEW],
            'modifiedAt': _iso(modified)}


def _pool_directories(top: Path, pool_root: Path, registered: set[str]) -> dict:
    # Same key as integration_control: sha256 of the canonical checkout path.
    base = pool_root / hashlib.sha256(str(top).encode('utf-8')).hexdigest()[:12]
    items = []
    if base.is_dir() and not base.is_symlink():
        for owner in sorted(base.iterdir()):
            if owner.is_symlink() or not owner.is_dir():
                continue
            for candidate in sorted(owner.iterdir()):
                if (candidate.is_symlink() or not candidate.is_dir()
                        or os.path.realpath(candidate) in registered):
                    continue
                items.append(_classify_pool_directory(candidate))
    return {'root': str(base), 'items': items}


# ── Branch classification ────────────────────────────────────────────────

def _branches(top: Path) -> list[dict]:
    fmt = '%(refname)%00%(objectname)%00%(committerdate:unix)%00%(worktreepath)'
    branches = []
    for line in _git(top, 'for-each-ref', '--format=' + fmt, 'refs/heads/').stdout.splitlines():
        ref, sha, stamp, worktree = line.split('\0')
        branches.append({'ref': ref, 'name': ref[len('refs/heads/'):], 'sha': sha,
                         'committedAt': _iso(float(stamp)) if stamp else None,
                         'worktree': worktree})
    return branches


def _merged_into(top: Path, sha: str) -> set[str]:
    return set(_git(top, 'for-each-ref', '--merged', sha, '--format=%(refname)',
                    'refs/heads/').stdout.split())


def _raw_signatures(top: Path, commits: list[str]) -> dict[str, tuple[str, ...]]:
    if not commits:
        return {}
    output = _git(top, 'diff-tree', '--stdin', '-r', '--root', '--no-renames', '--always',
                  stdin=''.join(f'{sha}\n' for sha in commits)).stdout
    signatures: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith(':') and current is not None:
            signatures[current].append(line)
        elif line:
            current = line.split()[0]
            signatures[current] = []
    return {sha: tuple(sorted(entries)) for sha, entries in signatures.items()}


def _paths(signature: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(entry.split('\t', 1)[1] for entry in signature))


def _patch_id_chunk(top: Path, commits: list[str]) -> dict[str, str]:
    if not commits:
        return {}
    diff = subprocess.Popen(
        _git_argv('diff-tree', '--stdin', '-p', '--binary', '--root', '--no-renames',
                  '--no-color', '--no-ext-diff', '--no-textconv', '--full-index'),
        cwd=str(top), env=_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    patch = subprocess.Popen(_git_argv('patch-id', '--stable'), cwd=str(top),
                             env=_environment(), stdin=diff.stdout,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    diff.stdout.close()

    def feed():
        try:
            diff.stdin.write(''.join(f'{sha}\n' for sha in commits).encode())
        except BrokenPipeError:
            pass
        finally:
            diff.stdin.close()

    feeder = threading.Thread(target=feed, name='git-hygiene-patch-id-feed', daemon=True)
    feeder.start()
    try:
        output, _ = patch.communicate(timeout=GIT_TIMEOUT_SECONDS)
    finally:
        for process in (patch, diff):
            if process.poll() is None:
                process.kill()
            process.wait()
        feeder.join()
    if diff.returncode or patch.returncode:
        raise HygieneError('git patch-id pipeline failed')
    return {commit: patch_id for patch_id, commit in
            (line.split() for line in output.decode().splitlines() if line)}


def _patch_ids(top: Path, commits: list[str]) -> dict[str, str]:
    chunks = [commits[index::WORKERS] for index in range(WORKERS)]
    result: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for part in pool.map(lambda chunk: _patch_id_chunk(top, chunk), chunks):
            result.update(part)
    return result


def _twin_commits(top: Path, unique: list[str], kept: list[str]) -> dict[str, set[str]]:
    """Kept non-merge commits carrying the same patch as each unique commit.

    Identical raw diffs are twins without hashing patches; only commits
    sharing an exact changed-path set with a kept commit need ``patch-id``.
    """
    candidate_signatures = _raw_signatures(top, unique)
    kept_signatures = _raw_signatures(
        top, _git(top, 'rev-list', '--no-merges', *kept).stdout.split())
    by_signature: dict[tuple[str, ...], set[str]] = collections.defaultdict(set)
    by_paths: dict[tuple[str, ...], list[str]] = collections.defaultdict(list)
    for sha, signature in kept_signatures.items():
        by_signature[signature].add(sha)
        by_paths[_paths(signature)].append(sha)
    twins = {sha: by_signature[signature] for sha, signature in candidate_signatures.items()
             if signature in by_signature}
    pending = [sha for sha, signature in candidate_signatures.items()
               if sha not in twins and _paths(signature) in by_paths]
    if pending:
        partners = sorted({partner for sha in pending
                           for partner in by_paths[_paths(candidate_signatures[sha])]})
        ids = _patch_ids(top, pending + partners)
        by_id: dict[str, set[str]] = collections.defaultdict(set)
        for sha in partners:
            if sha in ids:
                by_id[ids[sha]].add(sha)
        twins.update({sha: by_id[ids[sha]] for sha in pending if ids.get(sha) in by_id})
    return twins


def _unique_commits(graph: dict[str, list[str]], tip: str) -> set[str]:
    seen: set[str] = set()
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha in graph and sha not in seen:
            seen.add(sha)
            stack.extend(graph[sha])
    return seen


def _patch_equivalent_tips(top: Path, tips: set[str], kept: list[str]) -> set[str]:
    """Tips for which ``git cherry <kept> <tip>`` reports no ``+``.

    Bulk form over the union of kept revisions: every unique non-merge commit
    (merges are ignored, as git cherry does) needs a patch twin in kept history
    outside the tip's own ancestry, so a re-landed reverted change stays unmerged.
    """
    if not tips:
        return set()
    stdin = ''.join(f'{sha}\n' for sha in sorted(tips)) + ''.join(f'^{sha}\n' for sha in kept)
    graph: dict[str, list[str]] = {}
    for line in _git(top, 'rev-list', '--parents', '--stdin', stdin=stdin).stdout.splitlines():
        sha, *parents = line.split()
        graph[sha] = parents
    twins = _twin_commits(top, [sha for sha, parents in graph.items() if len(parents) <= 1], kept)
    unique = {tip: [sha for sha in _unique_commits(graph, tip) if len(graph[sha]) <= 1]
              for tip in tips}
    candidates = sorted(tip for tip in tips if all(sha in twins for sha in unique[tip]))
    if not candidates:
        return set()
    bits = {sha: 1 << index for index, sha in
            enumerate(sorted({twin for found in twins.values() for twin in found}))}
    ancestors: dict[str, int] = {}  # twins reachable from each commit, as a bit mask
    order = _git(top, 'rev-list', '--topo-order', '--parents', '--stdin',
                 stdin=''.join(f'{sha}\n' for sha in candidates)).stdout.splitlines()
    for line in reversed(order):
        sha, *parents = line.split()
        mask = 0
        for parent in parents:
            mask |= ancestors.get(parent, 0) | bits.get(parent, 0)
        ancestors[sha] = mask
    return {tip for tip in candidates if all(
        any(not (ancestors[tip] & bits[twin]) for twin in twins[sha]) for sha in unique[tip])}


def _classify_branches(repository: dict, also: list[dict], branches: list[dict],
                       busy: set[str], containers: dict[str, set[str] | None]) -> None:
    top, head = repository['top'], repository['head']
    merged_head = _merged_into(top, head)
    merged_also = {revision['sha']: _merged_into(top, revision['sha']) for revision in also}
    unmerged_tips = {branch['sha'] for branch in branches if branch['ref'] not in merged_head
                     and not any(branch['ref'] in refs for refs in merged_also.values())}
    equivalent = _patch_equivalent_tips(
        top, unmerged_tips, [head, *(revision['sha'] for revision in also)])
    anchors = {revision['sha'] for revision in also}
    for branch in branches:
        into = [revision['rev'] for revision in also if branch['ref'] in merged_also[revision['sha']]]
        if branch['ref'] in merged_head:
            branch.update({'class': 'merged_head', 'mergedInto': ['HEAD', *into]})
        elif into:
            branch.update({'class': 'merged_also', 'mergedInto': into})
        else:
            branch.update({'class': 'patch_equivalent' if branch['sha'] in equivalent
                           else 'unmerged', 'mergedInto': []})
        if branch['ref'] == repository['currentBranch']:
            branch['protection'] = 'current'
        elif branch['worktree'] or branch['ref'] in busy:
            branch['protection'] = 'checked_out'
        elif branch['sha'] in anchors:
            branch['protection'] = 'anchor'
        else:
            branch['protection'] = ''
        branch['deletable'] = (branch['class'] in ('merged_head', 'merged_also')
                               and not branch['protection'])
    _protect_unanchored_revisions(containers, branches)


def _protect_unanchored_revisions(containers: dict[str, set[str] | None],
                                  branches: list[dict]) -> None:
    """Keep each --also-merged-into revision reachable from a surviving ref."""
    deleting = {branch['ref'] for branch in branches if branch['deletable']}
    for containing in containers.values():
        if containing is None or containing - deleting:
            continue
        for branch in branches:
            if branch['ref'] in containing and branch['deletable']:
                branch.update({'deletable': False, 'protection': 'anchor'})


# ── refs/tofu (report only) ──────────────────────────────────────────────

def _tofu_refs(top: Path) -> dict:
    namespaces: dict[str, dict] = {}
    output = _git(top, 'for-each-ref', '--format=%(refname)%00%(creatordate:unix)',
                  'refs/tofu/').stdout
    for line in output.splitlines():
        ref, stamp = line.split('\0')
        parts = ref.split('/')
        namespace = '/'.join(parts[:3]) if len(parts) > 3 else ref
        entry = namespaces.setdefault(namespace, {'count': 0, 'oldest': None, 'newest': None})
        entry['count'] += 1
        if stamp:
            moment = float(stamp)
            entry['oldest'] = moment if entry['oldest'] is None else min(entry['oldest'], moment)
            entry['newest'] = moment if entry['newest'] is None else max(entry['newest'], moment)
    return {name: {**entry, 'oldest': _iso(entry['oldest']), 'newest': _iso(entry['newest'])}
            for name, entry in sorted(namespaces.items())}


# ── Inventory ────────────────────────────────────────────────────────────

def _kept_tips(top: Path, head: str, deleting: set[str]) -> list[str]:
    output = _git(top, 'for-each-ref', '--format=%(refname) %(objectname)').stdout
    tips = {head}
    for line in output.splitlines():
        ref, sha = line.split(' ', 1)
        if ref not in deleting:
            tips.add(sha)
    return sorted(tips)


def _unreachable_heads(top: Path, heads: set[str], kept: list[str]) -> set[str]:
    if not heads:
        return set()
    stdin = ''.join(f'{sha}\n' for sha in sorted(heads)) + ''.join(f'^{sha}\n' for sha in kept)
    return heads & set(_git(top, 'rev-list', '--stdin', stdin=stdin).stdout.split())


def _removable(worktree: dict) -> bool:
    return (not worktree['main'] and not worktree['locked'] and worktree['state'] == 'clean'
            and worktree.get('idle') and worktree.get('headReachable')
            and not worktree.get('ignoredBlockers'))


def inventory(repo: str, also_merged_into: list[str], pool_dir: str | None,
              include_worktrees: bool) -> dict:
    repository = _repository(repo)
    top = repository['top']
    raw_worktrees = _worktrees(top)
    if not raw_worktrees or os.path.realpath(raw_worktrees[0]['path']) != str(top):
        raise HygieneError(f'--repo must be the main worktree ({raw_worktrees[0]["path"] if raw_worktrees else "?"})')
    also = _resolve_revisions(top, also_merged_into)
    containers = _revision_containers(top, repository['head'], also)
    branches = _branches(top)
    _classify_branches(repository, also, branches, _busy_branches(repository['common']),
                       containers)
    deleting = {branch['ref'] for branch in branches if branch['deletable']}
    worktrees = _inspect_worktrees(raw_worktrees)
    kept = _kept_tips(top, repository['head'], deleting)
    unreachable = _unreachable_heads(
        top, {item['head'] for item in worktrees if item['head']}, kept)
    for item in worktrees:
        item['headReachable'] = bool(item['head']) and item['head'] not in unreachable
        item['removable'] = include_worktrees and _removable(item)
    registered = {os.path.realpath(item['path']) for item in raw_worktrees}
    pool = _pool_directories(top, Path(pool_dir).resolve() if pool_dir
                             else top / 'data' / 'integration' / 'worktrees', registered)
    report = {
        'format': FORMAT,
        'repository': str(top),
        'head': {'sha': repository['head'], 'branch': repository['currentBranch'] or None},
        'alsoMergedInto': also,
        'branches': _branch_summary(branches),
        'worktrees': _worktree_summary(worktrees),
        'poolDirectories': {**pool, 'unregistered': len(pool['items']),
                            'rebuildableOnly': sum(item['rebuildable'] for item in pool['items'])},
        'tofuRefs': _tofu_refs(top),
    }
    report['plan'] = {
        'branches': [{key: branch[key] for key in ('ref', 'sha', 'class', 'mergedInto')}
                     for branch in branches if branch['deletable']],
        'poolDirectories': [item for item in pool['items'] if item['rebuildable']],
        'worktrees': [{key: item[key] for key in ('path', 'head', 'branch')}
                      for item in worktrees if item['removable']],
    }
    return report


def _branch_summary(branches: list[dict]) -> dict:
    counts = collections.Counter(branch['class'] for branch in branches)
    protected = collections.Counter(branch['protection'] for branch in branches if branch['protection'])
    by_prefix: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for branch in branches:
        prefix = branch['name'].split('/', 1)[0] if '/' in branch['name'] else '(none)'
        by_prefix[prefix]['total'] += 1
        by_prefix[prefix][branch['class']] += 1
        by_prefix[prefix]['deletable'] += branch['deletable']
    return {
        'total': len(branches),
        'counts': {name: counts.get(name, 0) for name in CLASSES},
        'protected': dict(sorted(protected.items())),
        'deletable': sum(branch['deletable'] for branch in branches),
        'byPrefix': {prefix: dict(counter) for prefix, counter in
                     sorted(by_prefix.items(), key=lambda item: -item[1]['total'])},
        'items': branches,
    }


def _worktree_summary(worktrees: list[dict]) -> dict:
    states = collections.Counter(item['state'] for item in worktrees)
    return {
        'total': len(worktrees),
        'states': dict(sorted(states.items())),
        'locked': sum(item['locked'] for item in worktrees),
        'headUnreachable': sum(not item['headReachable'] for item in worktrees),
        'cleanIdleReachable': sum(
            not item['main'] and item['state'] == 'clean' and bool(item.get('idle'))
            and item['headReachable'] for item in worktrees),
        'removable': sum(item['removable'] for item in worktrees),
        'items': worktrees,
    }


# ── Apply and restore ────────────────────────────────────────────────────

def _write_manifest(top: Path, report: dict) -> Path:
    directory = top / 'data' / 'git-hygiene'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    pool = []
    for item in report['plan']['poolDirectories']:
        files = [{'path': name, 'base64': base64.b64encode(
            (Path(item['path']) / name).read_bytes()).decode('ascii')} for name in item['files']]
        pool.append({'path': item['path'], 'directories': item['directories'], 'files': files})
    manifest = {
        'format': FORMAT, 'kind': 'restore-manifest', 'createdAt': _iso(time.time()),
        'repository': report['repository'], 'head': report['head'],
        'alsoMergedInto': report['alsoMergedInto'],
        'branches': report['plan']['branches'], 'worktrees': report['plan']['worktrees'],
        'poolDirectories': pool,
    }
    for attempt in range(100):
        target = directory / (f'{stamp}.json' if not attempt else f'{stamp}-{attempt}.json')
        if not target.exists():
            break
    else:
        raise HygieneError('could not choose a unique manifest name')
    temporary = directory / f'.{target.name}.{os.getpid()}.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def _update_refs(top: Path, commands: list[tuple[str, list[str]]]) -> tuple[list[str], list[dict]]:
    """Apply ``update-ref`` commands in one transaction, isolating failures."""
    if not commands:
        return [], []
    stdin = ''.join(command + '\n' for command, _ in commands)
    if _git(top, 'update-ref', '--stdin', stdin=stdin, check=False).returncode == 0:
        return [command.split()[1] for command, _ in commands], []
    done, failed = [], []
    for command, argv in commands:
        result = _git(top, 'update-ref', *argv, check=False)
        if result.returncode:
            failed.append({'ref': command.split()[1], 'error': result.stderr.strip()[:300]})
        else:
            done.append(command.split()[1])
    return done, failed


def _delete_branches(top: Path, repository: dict, also: list[dict],
                     planned: list[dict]) -> dict:
    # Re-validate against the live repository: HEAD may have moved or a branch
    # may have been checked out since the inventory ran.
    head = _git(top, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.strip()
    merged = _merged_into(top, head)
    for revision in also:
        merged |= _merged_into(top, revision['sha'])
    in_use = {item['branch'] for item in _worktrees(top) if item['branch']}
    in_use |= _busy_branches(repository['common'])
    commands, skipped = [], []
    for branch in planned:
        if branch['ref'] in in_use or branch['ref'] not in merged:
            skipped.append(branch['ref'])
            continue
        commands.append((f'delete {branch["ref"]} {branch["sha"]}',
                         ['-d', branch['ref'], branch['sha']]))
    deleted, failed = _update_refs(top, commands)
    return {'deleted': len(deleted), 'skipped': skipped, 'failed': failed}


def _delete_pool_directories(top: Path, planned: list[dict]) -> dict:
    registered = {os.path.realpath(item['path']) for item in _worktrees(top)}
    deleted, failed = [], []
    for item in planned:
        path = Path(item['path'])
        current = _classify_pool_directory(path) if path.is_dir() else None
        if (current is None or os.path.realpath(path) in registered or not current['rebuildable']
                or current['files'] != item['files']
                or current['directories'] != item['directories']):
            failed.append({'path': str(path), 'error': 'changed since inventory'})
            continue
        try:
            for name in item['files']:
                (path / name).unlink()
            # rmdir refuses non-empty directories, so concurrent writes are kept.
            for name in sorted(item['directories'], key=lambda value: -value.count('/')):
                (path / name).rmdir()
            path.rmdir()
            deleted.append(str(path))
        except OSError as error:
            failed.append({'path': str(path), 'error': str(error)[:300]})
    return {'deleted': len(deleted), 'failed': failed}


def _remove_worktrees(top: Path, planned: list[dict]) -> dict:
    current = {item['path']: item for item in _worktrees(top)}
    removed, failed = [], []
    for item in planned:
        live = current.get(item['path'])
        if live is None or live['head'] != item['head'] or live['branch'] != item['branch']:
            failed.append({'path': item['path'], 'error': 'changed since inventory'})
            continue
        checked = _inspect_worktree(live, time.time())
        if checked['state'] != 'clean' or not checked.get('idle') or checked.get('ignoredBlockers'):
            failed.append({'path': item['path'], 'error': 'no longer clean and idle'})
            continue
        result = _git(top, 'worktree', 'remove', item['path'], check=False)
        if result.returncode:
            failed.append({'path': item['path'], 'error': result.stderr.strip()[:300]})
        else:
            removed.append(item['path'])
    return {'removed': len(removed), 'failed': failed}


def apply(report: dict, also: list[dict]) -> dict:
    repository = _repository(report['repository'])
    top = repository['top']
    manifest = _write_manifest(top, report)
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--repo', str(top),
                          '--restore', str(manifest)])
    print(f'Restore manifest: {manifest}\nRestore command: {command}', file=sys.stderr)
    plan = report['plan']
    return {
        'manifest': str(manifest),
        'restoreCommand': command,
        'branches': _delete_branches(top, repository, also, plan['branches']),
        'poolDirectories': _delete_pool_directories(top, plan['poolDirectories']),
        'worktrees': _remove_worktrees(top, plan['worktrees']),
    }


def restore(repo: str, manifest_path: str) -> dict:
    repository = _repository(repo)
    top = repository['top']
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if manifest.get('format') != FORMAT or manifest.get('kind') != 'restore-manifest':
        raise HygieneError('not a git hygiene restore manifest')
    if manifest.get('repository') != str(top):
        raise HygieneError(f'manifest belongs to {manifest.get("repository")}, not {top}')
    zero = '0' * len(repository['head'])
    restored, failed = _update_refs(top, [
        (f'create {branch["ref"]} {branch["sha"]}', [branch['ref'], branch['sha'], zero])
        for branch in manifest['branches']])
    worktrees = {'restored': [], 'failed': []}
    for item in manifest['worktrees']:
        if Path(item['path']).exists():
            worktrees['failed'].append({'path': item['path'], 'error': 'path exists'})
            continue
        name = item['branch'][len('refs/heads/'):] if item['branch'] else ''
        argv = (['worktree', 'add', item['path'], name] if name else
                ['worktree', 'add', '--detach', item['path'], item['head']])
        result = _git(top, *argv, check=False)
        key = 'failed' if result.returncode else 'restored'
        worktrees[key].append(item['path'] if not result.returncode
                              else {'path': item['path'], 'error': result.stderr.strip()[:300]})
    pool = {'restored': [], 'failed': []}
    for item in manifest['poolDirectories']:
        path = Path(item['path'])
        if path.exists():
            pool['failed'].append({'path': str(path), 'error': 'path exists'})
            continue
        path.mkdir(parents=True)
        for name in item['directories']:
            (path / name).mkdir(parents=True, exist_ok=True)
        for entry in item['files']:
            (path / entry['path']).write_bytes(base64.b64decode(entry['base64']))
        pool['restored'].append(str(path))
    return {'format': FORMAT, 'mode': 'restore', 'manifest': str(manifest_path),
            'branches': {'restored': len(restored), 'failed': failed},
            'worktrees': worktrees, 'poolDirectories': pool}


# ── Presentation ─────────────────────────────────────────────────────────

def _render(report: dict) -> str:
    branches, worktrees, pool = report['branches'], report['worktrees'], report['poolDirectories']
    head = report['head']
    lines = [f'Git hygiene {report["mode"]}: {report["repository"]} '
             f'(HEAD {head["sha"][:9]}{" on " + head["branch"] if head["branch"] else ", detached"})',
             f'Branches: {branches["total"]} local']
    labels = {'merged_head': 'merged into HEAD',
              'merged_also': 'merged into ' + (', '.join(rev['rev'] for rev in report['alsoMergedInto']) or '-'),
              'patch_equivalent': 'patch-equivalent (report only)',
              'unmerged': 'unmerged'}
    for name in CLASSES:
        lines.append(f'  {labels[name]:<40} {branches["counts"][name]:>6}')
    protected = ', '.join(f'{name} {count}' for name, count in branches['protected'].items()) or 'none'
    lines.append(f'  protected: {protected}')
    lines.append(f'  deletable with --apply: {branches["deletable"]}')
    prefixes = list(branches['byPrefix'].items())[:12]
    lines.append('  by prefix: ' + '; '.join(
        f'{prefix} {stats["total"]} (merged {stats.get("merged_head", 0) + stats.get("merged_also", 0)}, '
        f'equivalent {stats.get("patch_equivalent", 0)}, unmerged {stats.get("unmerged", 0)})'
        for prefix, stats in prefixes))
    states = ', '.join(f'{name} {count}' for name, count in worktrees['states'].items())
    lines.append(f'Worktrees: {worktrees["total"]} registered ({states}); locked {worktrees["locked"]}; '
                 f'HEAD unreachable from kept refs {worktrees["headUnreachable"]}; '
                 f'clean+idle+reachable {worktrees["cleanIdleReachable"]}; '
                 f'removable with --remove-clean-worktrees {worktrees["removable"]}')
    lines.append(f'Integration pool {pool["root"]}: unregistered {pool["unregistered"]}, '
                 f'rebuildable-only {pool["rebuildableOnly"]}')
    lines.append('refs/tofu (report only): ' + ('; '.join(
        f'{name[len("refs/tofu/"):]} {entry["count"]} [{(entry["oldest"] or "?")[:10]} .. '
        f'{(entry["newest"] or "?")[:10]}]' for name, entry in report['tofuRefs'].items()) or 'none'))
    plan = report['plan']
    lines.append(f'Plan: {len(plan["branches"])} branches, {len(plan["poolDirectories"])} pool '
                 f'directories, {len(plan["worktrees"])} worktrees.')
    if 'applied' in report:
        applied = report['applied']
        lines.append(f'Applied: branches deleted {applied["branches"]["deleted"]}, skipped '
                     f'{len(applied["branches"]["skipped"])}, failed {len(applied["branches"]["failed"])}; '
                     f'pool directories deleted {applied["poolDirectories"]["deleted"]}, failed '
                     f'{len(applied["poolDirectories"]["failed"])}; worktrees removed '
                     f'{applied["worktrees"]["removed"]}, failed {len(applied["worktrees"]["failed"])}.')
        lines.append(f'Restore with: {applied["restoreCommand"]}')
    else:
        lines.append('Nothing was changed (dry run). Use --json for item lists and --apply to delete the plan.')
    return '\n'.join(lines)


def _failures(applied: dict) -> int:
    return (len(applied['branches']['failed']) + len(applied['poolDirectories']['failed'])
            + len(applied['worktrees']['failed']))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', required=True, help='main worktree of the repository')
    parser.add_argument('--also-merged-into', action='append', default=[], metavar='REV',
                        help='also treat branches merged into REV as merged (repeatable)')
    parser.add_argument('--pool-dir', help='integration worktree pool '
                        '(default: <repo>/data/integration/worktrees)')
    parser.add_argument('--apply', action='store_true',
                        help='write a restore manifest, then delete the plan (default: dry run)')
    parser.add_argument('--remove-clean-worktrees', action='store_true',
                        help='also plan removal of clean, idle, reachable registered worktrees')
    parser.add_argument('--restore', metavar='MANIFEST', help='recreate what MANIFEST recorded')
    parser.add_argument('--json', action='store_true', help='print the full JSON report')
    args = parser.parse_args(argv)
    try:
        if args.restore:
            result = restore(args.repo, args.restore)
            print(json.dumps(result, indent=2))
            failed = (len(result['branches']['failed']) + len(result['worktrees']['failed'])
                      + len(result['poolDirectories']['failed']))
            return 1 if failed else 0
        report = inventory(args.repo, args.also_merged_into, args.pool_dir,
                           args.remove_clean_worktrees)
        report['mode'] = 'apply' if args.apply else 'dry-run'
        if args.apply:
            report['applied'] = apply(report, report['alsoMergedInto'])
    except (HygieneError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}))
        return 2
    print(json.dumps(report, indent=2) if args.json else _render(report))
    return 1 if args.apply and _failures(report['applied']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
