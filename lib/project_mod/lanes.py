"""Lanes: a branch plus a warm local checkout for one unit of work.

Responsibility
    Create, recycle, save and restore the lanes of a Git repository. A lane is
    a branch and a linked worktree on fast local storage. Its durable state is
    in Git: the branch's commits and, after every task that wrote in it, a
    work-in-progress commit at ``refs/tofu/lanes/<branch>`` that never moves
    the branch. The checkout is therefore reconstructible (``restore_lane``),
    which is what lets lanes live on fast storage whose persistence is
    unproven. Landing retires a clean lane's checkout as a spare of its
    repository; the next lane takes a spare and rewrites only the files that
    differ instead of checking out the whole tree. When none is left,
    ``ensure_next_spare`` prepares the next one in a detached process, under a
    ``.preparing-`` name that is renamed to a spare only once complete.

Entry points
    ``create_lane``, ``restore_lane``, ``retire_lane``, ``save_wip``,
    ``save_task_lanes``, ``list_lanes``, ``drop_wip``, ``repository``,
    ``is_lane``, ``prepare_spare``, ``ensure_next_spare``. CLI:
    ``scripts/lane.py`` (``new`` and ``restore`` call ``ensure_next_spare``);
    ``scripts/git_land.py`` retires a landed lane.

Dependencies: the git executable; ``serverctl_pkg.local_placement``
(``PURPOSE_LANES``). Standard library otherwise.
Contract: docs/modules/project_integration.md, "Lanes".
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

WIP_REF_PREFIX = 'refs/tofu/lanes/'
_SPARE_PREFIX = '.spare-'
_PREPARING_PREFIX = '.preparing-'
# Lanes stay inside the repository when no faster local root is selected.
_FALLBACK_PARENT = ('.tofu', 'worktrees')
_BRIDGED_DEPENDENCIES = ('node_modules', '.venv')


class LaneError(RuntimeError):
    """A lane operation was refused or failed; the message says what to do."""


@dataclasses.dataclass(frozen=True)
class Repository:
    top: Path
    common: Path


@dataclasses.dataclass(frozen=True)
class Checkout:
    path: Path
    head: str
    branch: str


def _git(cwd, *args: str, env: dict | None = None, check: bool = True,
         input: str | None = None) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('GIT_')
                   or key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_'))}
    environment.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0')
    environment.update(env or {})
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=str(cwd),
                            env=environment, capture_output=True, text=True, input=input)
    if check and result.returncode:
        raise LaneError(f'git {args[0]} failed: {result.stderr.strip()[:600]}')
    return result


def repository(path) -> Repository:
    """The repository containing PATH, named by its main checkout and Git directory."""
    start = Path(path).expanduser().resolve()
    common = _git(start, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
    lines = _git(start, 'worktree', 'list', '--porcelain').stdout.splitlines()
    if not lines or not lines[0].startswith('worktree ') or 'bare' in lines[1:3]:
        raise LaneError(f'{start} belongs to a repository without a main checkout')
    return Repository(Path(lines[0][len('worktree '):]), Path(common).resolve())


def _repository_key(repo: Repository) -> str:
    return hashlib.sha256(str(repo.common).encode('utf-8')).hexdigest()[:12]


def local_root(repo: Repository) -> Path | None:
    """REPO's private directory on fast local storage, when placement selects one."""
    from serverctl_pkg.local_placement import PURPOSE_LANES, choose_local_root
    placement = choose_local_root(repo.top, PURPOSE_LANES)
    return Path(placement.root) / _repository_key(repo) if placement.selected else None


def lane_root(repo: Repository) -> Path:
    """Where new lanes of REPO go: a private fast local root, else inside the repository."""
    root = local_root(repo) or repo.top.joinpath(*_FALLBACK_PARENT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _lane_parents(repo: Repository) -> list[Path]:
    """Every directory this module may have put lanes of REPO in, whatever placement chose."""
    from serverctl_pkg.local_placement import PURPOSE_LANES, candidate_roots
    key = _repository_key(repo)
    parents = [repo.top.joinpath(*_FALLBACK_PARENT),
               *(root / key for root in candidate_roots(PURPOSE_LANES))]
    return list(dict.fromkeys(Path(os.path.realpath(parent)) for parent in parents))


def is_lane(repo: Repository, path) -> bool:
    """Whether PATH is the root of a checkout in one of REPO's lane directories."""
    return Path(os.path.realpath(path)).parent in _lane_parents(repo)


def checkouts(repo: Repository) -> list[Checkout]:
    found, path, head, branch = [], None, '', ''
    for line in _git(repo.top, 'worktree', 'list', '--porcelain').stdout.splitlines() + ['']:
        if line.startswith('worktree '):
            path = Path(line[len('worktree '):])
        elif line.startswith('HEAD '):
            head = line[len('HEAD '):]
        elif line.startswith('branch '):
            branch = line[len('branch refs/heads/'):]
        elif not line and path is not None:
            found.append(Checkout(path, head, branch))
            path, head, branch = None, '', ''
    return found


def _trunk(repo: Repository) -> str:
    ref = _git(repo.top, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
    if not ref.startswith('refs/heads/'):
        raise LaneError(f'the main checkout {repo.top} has no branch checked out')
    return ref


def _move(old: Path, new: Path) -> bool:
    """Rename a linked worktree. The rename is atomic, so it is also the claim.

    Returns False when the worktree's registration is gone; the renamed
    directory is then discarded, since Git no longer knows it.
    """
    marker = (old / '.git').read_text(encoding='utf-8').strip()
    if not marker.startswith('gitdir:'):
        raise LaneError(f'{old} is not a linked worktree')
    admin = Path(marker[len('gitdir:'):].strip())
    if not admin.is_absolute():
        _git(old, 'worktree', 'move', str(old), str(new))
        return True
    os.rename(old, new)
    if not admin.is_dir():
        shutil.rmtree(new, ignore_errors=True)
        return False
    (admin / 'gitdir').write_text(f'{new}/.git\n', encoding='utf-8')
    return True


def _take_spare(root: Path, target: Path) -> bool:
    for spare in sorted(root.glob(f'{_SPARE_PREFIX}*')):
        try:
            moved = _move(spare, target)
        except (FileNotFoundError, NotADirectoryError):
            continue  # another lane claimed it first
        if moved and _git(target, 'rev-parse', '--git-dir', check=False).returncode == 0:
            return True
        shutil.rmtree(target, ignore_errors=True)
    return False


def _free_path(root: Path, branch: str) -> Path:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', branch).strip('-.') or 'lane'
    candidate, number = root / slug, 1
    while os.path.lexists(candidate):
        number += 1
        candidate = root / f'{slug}-{number}'
    return candidate


def _bridge(repo: Repository, checkout: Path) -> None:
    """Link the main checkout's installed dependencies, which are never versioned.

    A link is created only where the repository ignores it as a file: a
    directory-only pattern (``node_modules/``) does not cover a symlink, and an
    unignored link would show as untracked work and could be committed.
    """
    for name in _BRIDGED_DEPENDENCIES:
        source, target = repo.top / name, checkout / name
        if (source.exists() and not os.path.lexists(target)
                and _git(checkout, 'check-ignore', '-q', '--no-index', name,
                         check=False).returncode == 0):
            os.symlink(source, target)


def _branch_tip(repo: Repository, branch: str) -> str:
    return _git(repo.top, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}',
                check=False).stdout.strip()


def create_lane(path, branch: str, base: str | None = None) -> tuple[Path, bool]:
    """Create BRANCH from BASE (default: the main checkout's branch) in a new lane.

    Returns the lane's checkout and whether a spare checkout was recycled.
    """
    repo = repository(path)
    _git(repo.top, 'check-ref-format', '--branch', branch)
    if _branch_tip(repo, branch):
        raise LaneError(f'branch {branch} already exists; continue it with '
                        f'`python3 scripts/lane.py restore {branch}` or choose another name')
    start = _git(repo.top, 'rev-parse', '--verify', f'{base or _trunk(repo)}^{{commit}}').stdout.strip()
    root = lane_root(repo)
    target = _free_path(root, branch)
    recycled = _take_spare(root, target)
    if recycled:
        _git(target, 'checkout', '-q', '-B', branch, start)
    else:
        _git(repo.top, 'worktree', 'add', '-q', '-b', branch, str(target), start)
    _bridge(repo, target)
    return target, recycled


def restore_lane(path, branch: str) -> tuple[Path, str]:
    """Give BRANCH a checkout again, reapplying its saved uncommitted work."""
    repo = repository(path)
    for item in checkouts(repo):
        if item.branch == branch:
            return item.path, 'its checkout already exists'
    tip = _branch_tip(repo, branch)
    if not tip:
        raise LaneError(f'branch {branch} does not exist')
    root = lane_root(repo)
    target = _free_path(root, branch)
    if _take_spare(root, target):
        _git(target, 'checkout', '-q', branch)
    else:
        _git(repo.top, 'worktree', 'add', '-q', str(target), branch)
    _bridge(repo, target)
    wip = _git(repo.top, 'rev-parse', '--verify', '--quiet', WIP_REF_PREFIX + branch,
               check=False).stdout.strip()
    if not wip:
        return target, 'restored; no uncommitted work was saved'
    if _git(repo.top, 'rev-parse', f'{wip}^').stdout.strip() != tip:
        return target, (f'restored; the saved uncommitted work ({wip[:12]}) predates the '
                        f'branch tip, inspect it with `git show {wip[:12]}`')
    _git(target, 'read-tree', '-m', '-u', 'HEAD', wip)
    _git(target, 'reset', '-q')
    return target, 'restored with its saved uncommitted work'


def capture_working_tree(checkout) -> tuple[str, str]:
    """Capture HEAD and current bytes through a private index; move no caller state.

    The same immutable capture serves saved lanes and verification. Staged,
    unstaged and nonignored untracked files are included; the caller's index,
    branch and working files are never rewritten.
    """
    checkout = Path(checkout)
    head = _git(checkout, 'rev-parse', 'HEAD').stdout.strip()
    index = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path',
                      'index').stdout.strip())
    with tempfile.TemporaryDirectory(prefix='lane-wip-') as directory:
        private = Path(directory) / 'index'
        if index.exists():
            # The copy keeps the index's mtime, which Git's racy-entry check reads.
            shutil.copy2(index, private)
        environment = {'GIT_INDEX_FILE': str(private)}
        entries = _git(checkout, 'ls-files', '-v', '-z', env=environment).stdout.split('\0')
        flagged = [entry[2:] for entry in entries if entry and
                   (entry[0].islower() or entry[0] == 'S')]
        if flagged:
            for flag in ('--no-assume-unchanged', '--no-skip-worktree'):
                _git(checkout, 'update-index', flag, '-z', '--stdin',
                     env=environment, input='\0'.join(flagged) + '\0')
        _git(checkout, 'add', '-A', env=environment)
        tree = _git(checkout, 'write-tree', env=environment).stdout.strip()
    return head, tree


def save_wip(checkout) -> str | None:
    """Save a lane's current bytes at its WIP ref without moving its branch."""
    checkout = Path(checkout)
    branch = _git(checkout, 'symbolic-ref', '-q', '--short', 'HEAD', check=False).stdout.strip()
    if not branch:
        return None
    head, tree = capture_working_tree(checkout)
    ref = WIP_REF_PREFIX + branch
    if tree == _git(checkout, 'rev-parse', 'HEAD^{tree}').stdout.strip():
        _git(checkout, 'update-ref', '-d', ref, check=False)
        return None
    commit = _git(checkout, 'commit-tree', tree, '-p', head,
                  '-m', f'Uncommitted work of lane {branch}').stdout.strip()
    _git(checkout, 'update-ref', ref, commit)
    return commit


def drop_wip(repo: Repository, branch: str) -> None:
    _git(repo.top, 'update-ref', '-d', WIP_REF_PREFIX + branch, check=False)


def retire_lane(repo: Repository, checkout: Path, tip: str) -> str:
    """Turn a landed lane's clean checkout into a spare at TIP.

    Returns ``spare``, ``removed`` (no room is left for a spare above the
    placement reserve) or ``kept`` (uncommitted or untracked work stays with its
    owner, as ``git worktree remove`` would keep it).
    """
    from serverctl_pkg.local_placement import has_free_space
    status = _git(checkout, 'status', '--porcelain', '--untracked-files=normal', check=False)
    if status.returncode or status.stdout.strip():
        return 'kept'
    root = Path(os.path.realpath(lane_root(repo)))
    # Only the directory new lanes come from can reuse a spare; a lane placed
    # elsewhere (an earlier placement, the in-repository fallback) is removed.
    if Path(os.path.realpath(checkout)).parent == root and has_free_space(root):
        _git(checkout, 'checkout', '-q', '--detach', tip)
        _move(checkout, root / f'{_SPARE_PREFIX}{uuid.uuid4().hex[:12]}')
        return 'spare'
    _git(repo.top, 'worktree', 'remove', str(checkout))
    return 'removed'


def save_task_lanes(task: dict) -> list[dict]:
    """Save the uncommitted work of every lane this task wrote in (integration radar records)."""
    saved = []
    for path in list(task.get('_radarCheckouts') or ()):
        try:
            repo = repository(path)
            if not is_lane(repo, path):
                continue
            saved.append({'checkout': path, 'wip': save_wip(path) or ''})
        except (LaneError, OSError) as error:
            saved.append({'checkout': path, 'error': str(error)[:300]})
    return saved


def list_lanes(path) -> list[dict]:
    """Every lane of the repository: branch, checkout, divergence from trunk, uncommitted files."""
    repo = repository(path)
    trunk = _trunk(repo)
    lanes = []
    for item in checkouts(repo):
        if not is_lane(repo, item.path) or not item.branch:
            continue
        counts = _git(repo.top, 'rev-list', '--left-right', '--count',
                      f'{trunk}...refs/heads/{item.branch}', check=False).stdout.split()
        dirty = _git(item.path, 'status', '--porcelain', '--untracked-files=normal',
                     check=False).stdout.splitlines()
        saved = _git(repo.top, 'rev-parse', '--verify', '--quiet', WIP_REF_PREFIX + item.branch,
                     check=False).stdout.strip()
        lanes.append({'branch': item.branch, 'checkout': str(item.path),
                      'behind': int(counts[0]) if len(counts) == 2 else None,
                      'ahead': int(counts[1]) if len(counts) == 2 else None,
                      'uncommitted': len(dirty), 'savedWork': saved[:12]})
    return lanes


def spare_count(repo: Repository) -> int:
    return sum(1 for parent in _lane_parents(repo) if parent.is_dir()
               for _ in parent.glob(f'{_SPARE_PREFIX}*'))


def _lock_is_held(lock_path: Path) -> bool:
    """Whether a live process holds LOCK_PATH; a missing lock is not held."""
    try:
        handle = open(lock_path, 'r+')
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def _clear_abandoned_preparations(repo: Repository, root: Path) -> None:
    """Remove what a preparation that died left behind; a live one keeps its lock."""
    for entry in sorted(root.glob(f'{_PREPARING_PREFIX}*')):
        if entry.name.endswith('.lock'):
            if not (root / entry.name[:-len('.lock')]).exists() and not _lock_is_held(entry):
                entry.unlink(missing_ok=True)
            continue
        lock_path = root / f'{entry.name}.lock'
        if _lock_is_held(lock_path):
            continue
        _git(repo.top, 'worktree', 'remove', '--force', str(entry), check=False)
        shutil.rmtree(entry, ignore_errors=True)
        lock_path.unlink(missing_ok=True)


def _preparation_in_progress(root: Path) -> bool:
    return any(_lock_is_held(lock) for lock in root.glob(f'{_PREPARING_PREFIX}*.lock'))


def prepare_spare(path) -> str:
    """Create the next lane's checkout as a spare at the main checkout's branch.

    Returns ``ready``, ``present`` (a spare already exists) or ``no-space``
    (the placement reserve would be crossed). The checkout is created under a
    ``.preparing-`` name and renamed to a spare only once complete, so
    ``create_lane`` never takes a partial checkout.
    """
    from serverctl_pkg.local_placement import has_free_space
    repo = repository(path)
    root = lane_root(repo)
    _clear_abandoned_preparations(repo, root)
    if any(root.glob(f'{_SPARE_PREFIX}*')):
        return 'present'
    if not has_free_space(root):
        return 'no-space'
    name = uuid.uuid4().hex[:12]
    preparing, lock_path = root / f'{_PREPARING_PREFIX}{name}', root / f'{_PREPARING_PREFIX}{name}.lock'
    with open(lock_path, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _git(repo.top, 'worktree', 'add', '-q', '--detach', str(preparing), _trunk(repo))
            _move(preparing, root / f'{_SPARE_PREFIX}{name}')
        except BaseException:
            _git(repo.top, 'worktree', 'remove', '--force', str(preparing), check=False)
            shutil.rmtree(preparing, ignore_errors=True)
            raise
        finally:
            lock_path.unlink(missing_ok=True)
    return 'ready'


def ensure_next_spare(path) -> bool:
    """Start preparing the next lane's checkout when none is ready or being prepared.

    Runs ``scripts/lane.py prepare-spare`` detached, so the caller returns at
    once and the next lane recycles instead of checking out the whole tree.
    Its output goes to ``.preparation.log`` in the lane root.
    """
    repo = repository(path)
    root = lane_root(repo)
    if any(root.glob(f'{_SPARE_PREFIX}*')) or _preparation_in_progress(root):
        return False
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'lane.py'
    with open(root / '.preparation.log', 'w', encoding='utf-8') as log:
        subprocess.Popen([sys.executable, str(script), 'prepare-spare'], cwd=str(repo.top),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return True
