"""Tofu-managed Git repositories for project folders whose people never run Git.

Project workers run in isolated Git worktrees (docs/PROJECT_COORDINATION.md).
prepare_managed_repository() turns a plain local folder, or a repository with
no commits and nothing staged, into one: a Tofu-authored baseline commit plus a
matching index, so the folder reads as clean. User files are never modified,
moved or deleted; the only additions are ``.git/`` and, when the folder has no
guidance file, a generated ``AGENTS.md``. A new repository is assembled in a
``.tofu_git_init_*`` directory and appears as ``.git`` only when complete, so
no reader observes a half-made repository.

Content policy, recorded in ``.git/tofu-managed.json`` and rendered into the
managed section of ``.git/info/exclude`` (linked worktrees share both):
  * never versioned: runtime, dependency and cache trees, ``.tofu*`` artifacts
    and the root ``.venv`` (bridged per checkout by workspace_runtime);
  * shared: files of at least LARGE_FILE_BYTES, wholly untracked directories
    holding at least BULK_DIRECTORY_FILES files that are mostly not source
    text, virtual environments, nested repositories, paths hidden by the
    folder's own ignore files, root runtime directories and, when the owner
    chose so, nested projects. They are never versioned; link_shared_paths()
    links them into every worker checkout, where workers read them in place;
  * everything else is versioned: worker edits are isolated, reviewed and
    published by the integration owner.
Checkout cost on network volumes grows with the versioned file count, which is
why bulk data is shared (measurement in docs/PROJECT_COORDINATION.md).

A folder without a project manifest at its root (``PROJECT_MANIFESTS``) is
first read by a pruned discovery walk (managed_repository_discovery) that stops
at nested repositories, environments, nested projects and ignored paths, so
the Git walks that follow never enter them. Nested projects in such a folder
are copies or reference data, or part of the project: only the owner can say,
so preparation refuses with a typed question (``ambiguous_project_root``)
until an explicit ``nested_projects`` decision (``shared`` or ``versioned``)
is given; it is recorded in the manifest and never overridden. Discovery
resumes from its journal (outside the folder) after an interruption.

Whole-tree Git (``ls-files``, ``add -A``, ``read-tree``, ``write-tree``,
``check-ignore``) has no total deadline: like integration snapshots it runs
under the shared ``GIT_STALL_SECONDS`` window, so a large folder takes as long
as its volume needs, and a Git that stopped executing for a whole window fails
as ``volume_stalled``. The caller's ``progress`` observes every advance (a Git
tree seen executing, a completed command, a walked path or directory) as its
own liveness and its ``cancel`` (Stop) ends the preparation; neither is a
deadline. Every refusal carries a ``code`` naming its cause.
Preparing an existing managed repository again only extends the policy to new
untracked data: the shared set only grows and no further commit is made.

Entry points: prepare_managed_repository (called through
workspace_delivery.isolated_workspace_refusal), preview_preparation (read-only
report, scripts/prepare_project_folder.py), link_shared_paths (worker binding)
and is_shared_link (checkout reclamation).
Dependencies: integration_control (per-root lock, control root),
git_checkpoint_policy, agent_artifacts, instructions (guidance names),
managed_repository_guidance, managed_repository_discovery,
activity_supervised_process.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

from lib.agent_loop.agent_artifacts import ARTIFACT_PREFIX, GITIGNORE_PATTERN
from lib.git_checkpoint_policy import (
    FORBIDDEN_CHECKPOINT_PARTS, FORBIDDEN_CHECKPOINT_ROOTS, git_policy_environment,
)
from lib.git_runtime import GIT_STALL_SECONDS, git_executable
from lib.log import get_logger
from lib.project_mod.activity_supervised_process import (
    ActivitySupervisedExchange, ProcessCancelled, ProcessStalled, run_activity_supervised,
)
from lib.project_mod.managed_repository_discovery import PROJECT_MANIFESTS, Journal, discover

logger = get_logger(__name__)

LARGE_FILE_BYTES = 10 * 1024 * 1024
BULK_DIRECTORY_FILES = 2000
MANAGED_CONFIG_KEY = 'tofu.managedRepository'
MANIFEST_NAME = 'tofu-managed.json'
BASELINE_BRANCH = 'refs/heads/main'
GUIDANCE_FILE = 'AGENTS.md'
# The owner's answer for nested projects in a folder that declares no project.
NESTED_PROJECT_DECISIONS = ('shared', 'versioned')
# The owner may also name whole folders of read-only run/reference data.
SHARED_FOLDERS_KEY = 'sharedFolders'
# Where the owner answers the preparation questions without running Git.
PREPARE_SCRIPT = 'scripts/prepare_project_folder.py'
_STAGING_PREFIX = ARTIFACT_PREFIX + '_git_init_'
_SECTION_BEGIN = '# BEGIN Tofu managed repository (generated; edits here are replaced)'
_SECTION_END = '# END Tofu managed repository'
_GLOB_SPECIAL = frozenset('\\*?[]')
# Text that people and agents edit. A bulky directory mostly made of these is a
# source tree and stays versioned; any other bulky directory is shared data.
_SOURCE_SUFFIXES = frozenset({
    '.py', '.pyi', '.ipynb', '.r', '.jl', '.m', '.mm', '.c', '.cc', '.cpp', '.cxx', '.h', '.hh',
    '.hpp', '.cs', '.fs', '.java', '.kt', '.kts', '.scala', '.groovy', '.gradle', '.go', '.rs',
    '.swift', '.rb', '.php', '.pl', '.pm', '.lua', '.sh', '.bash', '.zsh', '.ps1', '.sql',
    '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.vue', '.svelte', '.html', '.htm', '.css',
    '.scss', '.less', '.md', '.markdown', '.rst', '.adoc', '.tex', '.bib', '.toml', '.ini',
    '.cfg', '.conf', '.yaml', '.yml', '.proto', '.graphql', '.cmake', '.mk',
})
_SOURCE_NAMES = frozenset({'makefile', 'gnumakefile', 'dockerfile', 'cmakelists.txt',
                           'readme', 'license', 'notice', 'changelog'})
_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Tofu Integration', 'GIT_AUTHOR_EMAIL': 'integration@tofu.local',
    'GIT_COMMITTER_NAME': 'Tofu Integration', 'GIT_COMMITTER_EMAIL': 'integration@tofu.local',
}
_LOCAL_CONFIG = (
    ('core.bare', 'false'), ('core.logAllRefUpdates', 'true'),
    # Personal Git settings must not change which bytes workers receive.
    ('core.autocrlf', 'false'), ('core.excludesFile', os.devnull),
    (MANAGED_CONFIG_KEY, 'true'),
)
_BASELINE_MESSAGE = (
    'Tofu baseline: project folder snapshot for isolated workers\n\n'
    'Created automatically by Tofu. Bulky data, environments and runtime trees '
    'are shared or excluded instead of versioned (.git/tofu-managed.json).\n')


@dataclass(frozen=True)
class ContentPolicy:
    large_file_bytes: int = LARGE_FILE_BYTES
    bulk_directory_files: int = BULK_DIRECTORY_FILES


DEFAULT_CONTENT_POLICY = ContentPolicy()


@dataclass
class ContentPlan:
    """Classification of currently untracked paths; shared values explain why."""
    shared: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    versioned: list[str] = field(default_factory=list)
    versioned_bytes: int = 0


@dataclass(frozen=True)
class Preparation:
    state: str  # created | adopted | refreshed | unchanged | user_repository
    root: str
    baseline: str
    shared: tuple[str, ...]
    added_shared: tuple[str, ...]
    guidance: str


class PreparationCancelled(Exception):
    """The caller's Stop ended the preparation; the folder is left as it was."""


@dataclass(frozen=True)
class _Observer:
    """The running preparation's caller: ``progress()`` per advance, ``cancel.is_set()`` Stop."""
    progress: object = None
    cancel: object = None


# Bound by prepare_managed_repository for exactly its own synchronous run.
_observer: ContextVar[_Observer] = ContextVar('managed_repository_observer', default=_Observer())  # noqa: B039


def _advanced() -> None:
    """Report one observed advance to the caller, then honour its Stop."""
    observer = _observer.get()
    if observer.progress is not None:
        observer.progress()
    if observer.cancel is not None and observer.cancel.is_set():
        raise PreparationCancelled('Cancelled by its owner')


class ManagedRepositoryUnavailable(RuntimeError):
    """Tofu cannot prepare this folder; carries a human remedy free of Git commands.

    ``code`` names the cause for callers and tools; ``details`` holds its
    structured facts (the owner's question for ``ambiguous_project_root``).
    """

    def __init__(self, reason: str, remedy: str, remedy_zh: str, *,
                 code: str = 'preparation_failed', details: dict | None = None):
        super().__init__(f'{reason} {remedy}')
        self.reason, self.remedy, self.remedy_zh = reason, remedy, remedy_zh
        self.code, self.details = code, details

    def hint(self) -> str:
        return f'解决办法 / How to fix:\n• {self.remedy_zh}\n\n• {self.remedy}'


def _failure(root, detail: str) -> ManagedRepositoryUnavailable:
    return ManagedRepositoryUnavailable(
        f'Tofu could not prepare {root} for isolated workers ({detail}).',
        'Check that the folder is writable and has free disk space and that Git is installed '
        'on the Tofu server, then delegate the work again.',
        '请确认该文件夹可写、磁盘空间充足且 Tofu 服务器已安装 Git，然后重新委派。')


def _stalled(root, step: str, error: Exception) -> ManagedRepositoryUnavailable:
    return ManagedRepositoryUnavailable(
        f'The storage holding {root} stopped responding while Tofu prepared it for isolated '
        f'workers ({step}: {error}).',
        'Check that the project folder is reachable, then delegate the work again; preparation '
        'resumes the folder walk where it stopped.',
        '请确认项目文件夹所在存储可以访问，然后重新委派；准备工作会从中断处继续。',
        code='volume_stalled')


def _command(args, *, cwd, git_dir=None) -> list[str]:
    command = [git_executable(), '-c', 'core.fsmonitor=false',
               '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgSign=false']
    if git_dir is not None:
        command += ['--git-dir', str(git_dir), '--work-tree', str(cwd)]
    return [*command, *args]


def _run(args, *, cwd, git_dir=None, env=None, timeout=120.0,
         stdin: bytes | None = None) -> subprocess.CompletedProcess:
    environment = git_policy_environment()
    environment.update(env or {})
    try:
        result = subprocess.run(_command(args, cwd=cwd, git_dir=git_dir), cwd=str(cwd),
                                env=environment, input=stdin, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise _failure(cwd, f'git {args[0]}: {error}') from error
    _advanced()
    return result


def _walk_git(args, *, cwd, git_dir=None, env=None, input: bytes | None = None,
              accepted=(0,)) -> bytes:
    """Run one whole-tree command; its duration follows the folder and its volume.

    No total deadline: only a whole ``GIT_STALL_SECONDS`` window in which no Git
    thread executed, or the caller's Stop, ends it early (docs/modules/
    project_integration.md). Each renewal is reported as progress.
    """
    environment = git_policy_environment()
    environment.update(env or {})
    observer = _observer.get()
    try:
        result = run_activity_supervised(
            _command(args, cwd=cwd, git_dir=git_dir), cwd=cwd, env=environment,
            stall_seconds=GIT_STALL_SECONDS, cancellation=observer.cancel, binary=True,
            on_activity=observer.progress, input=input)
    except ProcessCancelled as error:
        raise PreparationCancelled(str(error)) from error
    except ProcessStalled as error:
        raise _stalled(cwd, f'git {args[0]}', error) from error
    except (OSError, RuntimeError) as error:
        raise _failure(cwd, f'git {args[0]}: {error}') from error
    if result.returncode not in accepted:
        detail = os.fsdecode(result.stderr or result.stdout).strip()[:600]
        raise _failure(cwd, f'git {args[0]}: {detail}')
    _advanced()
    return result.stdout


@contextmanager
def _ignore_rules(root: Path, git_dir: Path):
    """Yield ``ignored(path)``: whether the folder's own ignore files hide it, as Git answers.

    One long-lived ``check-ignore`` answers path after path, so a walk asks
    before it enters a directory instead of listing what the folder ignores.
    """
    environment = git_policy_environment()
    environment['GIT_FLUSH'] = '1'
    observer = _observer.get()
    command = _command(['-c', 'core.excludesFile=' + os.devnull, 'check-ignore', '--stdin',
                        '-z', '--non-matching', '--verbose'], cwd=root, git_dir=git_dir)
    try:
        exchange = ActivitySupervisedExchange(
            command, cwd=root, env=environment, stall_seconds=GIT_STALL_SECONDS,
            cancellation=observer.cancel, on_activity=observer.progress)
    except OSError as error:
        raise _failure(root, f'git check-ignore: {error}') from error

    def ignored(path: str) -> bool:
        try:
            source, _line, pattern, _path = exchange.exchange(os.fsencode(path) + b'\0', 4)
        except ProcessCancelled as error:
            raise PreparationCancelled(str(error)) from error
        except ProcessStalled as error:
            raise _stalled(root, 'git check-ignore', error) from error
        except (OSError, RuntimeError) as error:
            raise _failure(root, f'git check-ignore: {error}') from error
        # A matching negated pattern (``!keep``) re-includes the path.
        return bool(source) and not pattern.startswith(b'!')

    primary = None
    try:
        yield ignored
    except BaseException as error:
        primary = error
        raise
    finally:
        exchange.close(primary)


def _git(args, *, cwd, git_dir=None, env=None, timeout=120.0) -> bytes:
    result = _run(args, cwd=cwd, git_dir=git_dir, env=env, timeout=timeout)
    if result.returncode:
        detail = os.fsdecode(result.stderr or result.stdout).strip()[:600]
        raise _failure(cwd, f'git {args[0]}: {detail}')
    return result.stdout


def _paths(output: bytes) -> list[str]:
    return [os.fsdecode(item) for item in output.split(b'\0') if item]


def _ancestors(path: str) -> list[str]:
    """Shallowest first: 'a/b/c' -> ['a', 'a/b']."""
    parts = path.split('/')
    return ['/'.join(parts[:index]) for index in range(1, len(parts))]


def _outermost(paths) -> list[str]:
    kept: set[str] = set()
    for path in sorted(set(paths), key=lambda item: (item.count('/'), item)):
        if not any(ancestor in kept for ancestor in _ancestors(path)):
            kept.add(path)
    return sorted(kept)


def _anchored_pattern(path: str) -> str | None:
    """Exact repository-relative ignore rule; no trailing slash so a link also matches."""
    if not path or '\n' in path or '\r' in path:
        return None
    escaped = ''.join('\\' + character if character in _GLOB_SPECIAL else character
                      for character in path)
    stripped = escaped.rstrip(' ')
    return '/' + stripped + '\\ ' * (len(escaped) - len(stripped))


def _runtime_patterns() -> list[str]:
    patterns = [GITIGNORE_PATTERN]
    patterns += sorted(part for part in FORBIDDEN_CHECKPOINT_PARTS
                       if part != '.git' and not part.startswith(ARTIFACT_PREFIX))
    patterns += ['*.pyc', '*.pyo', '/.venv']
    patterns += ['/' + name for name in sorted(FORBIDDEN_CHECKPOINT_ROOTS)]
    return patterns


def _runtime_path(path: str) -> bool:
    """Whether _runtime_patterns() exclude this repository-relative path."""
    parts = path.split('/')
    return (parts[0] == '.venv' or parts[0] in FORBIDDEN_CHECKPOINT_ROOTS
            or path.endswith(('.pyc', '.pyo'))
            or any(part.startswith(ARTIFACT_PREFIX) or part in FORBIDDEN_CHECKPOINT_PARTS
                   for part in parts))


def _source_file(path: str) -> bool:
    name = path.rpartition('/')[2].lower()
    return name in _SOURCE_NAMES or os.path.splitext(name)[1] in _SOURCE_SUFFIXES


def _safe_relative(path) -> bool:
    return (isinstance(path, str) and bool(path) and '\0' not in path
            and not path.startswith('/')
            and all(part not in ('', '.', '..') for part in path.split('/')))


def plan_content(root: Path, untracked: list[str], tracked: list[str],
                 policy: ContentPolicy, nested_projects: str | None = None) -> ContentPlan:
    """Classify untracked paths; a directory that holds versioned files is never shared.

    With the owner's ``shared`` decision a directory holding its own project
    manifest is a nested project: shared whole, like a nested repository.
    """
    tracked_files = set(tracked)
    occupied = {ancestor for path in tracked for ancestor in _ancestors(path)}

    def eligible(directory: str) -> bool:
        return (directory not in occupied and directory not in tracked_files
                and _anchored_pattern(directory) is not None)

    plan = ContentPlan()
    shared = plan.shared
    files = []
    for entry in untracked:
        if entry.endswith('/'):
            if eligible(entry.rstrip('/')):
                shared[entry.rstrip('/')] = 'nested repository'
            continue
        files.append(entry)
    for path in files:
        parts = path.split('/')
        if parts[-1] == 'pyvenv.cfg' and len(parts) > 1:
            environment = '/'.join(parts[:-1])
        elif 'conda-meta' in parts[1:-1]:
            environment = '/'.join(parts[:parts.index('conda-meta', 1)])
        else:
            continue
        if eligible(environment):
            shared.setdefault(environment, 'virtual environment')

    def covered(path: str) -> bool:
        return path in shared or any(ancestor in shared for ancestor in _ancestors(path))

    if nested_projects == 'shared':
        projects = {path.rpartition('/')[0] for path in files
                    if '/' in path and path.rpartition('/')[2] in PROJECT_MANIFESTS}
        for directory in _outermost(filter(eligible, projects)):
            if not covered(directory):
                shared[directory] = 'nested project'

    remaining = [path for path in files if not covered(path)]
    totals: Counter = Counter()
    sources: Counter = Counter()
    for path in remaining:
        parent = path.rpartition('/')[0]
        totals[parent] += 1
        sources[parent] += _source_file(path)
    # Deepest first: a shared directory carries nothing into its parent's count.
    directories = {ancestor for path in remaining for ancestor in _ancestors(path)}
    for directory in sorted(directories, key=lambda item: item.count('/'), reverse=True):
        total, source = totals[directory], sources[directory]
        if eligible(directory) and total >= policy.bulk_directory_files and 2 * source < total:
            shared[directory] = f'{total:,} data files'
            continue
        parent = directory.rpartition('/')[0]
        totals[parent] += total
        sources[parent] += source

    for path in remaining:
        _advanced()
        if covered(path):
            continue
        target = root / path
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError:
            plan.skipped.append(path)
            continue
        if stat.S_ISLNK(info.st_mode):
            plan.versioned.append(path)
            plan.versioned_bytes += info.st_size
        elif not stat.S_ISREG(info.st_mode) or not os.access(target, os.R_OK):
            plan.skipped.append(path)
        elif info.st_size >= policy.large_file_bytes and _anchored_pattern(path) is not None:
            shared[path] = f'{info.st_size / (1024 * 1024):,.1f} MiB'
        else:
            plan.versioned.append(path)
            plan.versioned_bytes += info.st_size
    plan.skipped = [path for path in plan.skipped if _anchored_pattern(path) is not None]
    return plan


def _atomic_write(path: Path, payload: bytes) -> None:
    try:
        if path.read_bytes() == payload:
            return
    except FileNotFoundError:
        pass
    handle = tempfile.NamedTemporaryFile(dir=str(path.parent), prefix='.tofu-write-', delete=False)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _write_exclude(git_dir: Path, shared, skipped) -> None:
    path = git_dir / 'info' / 'exclude'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = b''
    kept, inside = [], False
    for line in current.splitlines(keepends=True):
        text = line.rstrip(b'\r\n')
        if text == _SECTION_BEGIN.encode():
            inside = True
        elif text == _SECTION_END.encode():
            inside = False
        elif not inside:
            kept.append(line)
    if kept and not kept[-1].endswith(b'\n'):
        kept[-1] += b'\n'
    lines = [_SECTION_BEGIN, '# Never versioned: runtime, dependency and cache trees.',
             *_runtime_patterns(), '# Shared with workers through links; never versioned.',
             *filter(None, map(_anchored_pattern, shared)),
             '# Unreadable or special files.', *filter(None, map(_anchored_pattern, skipped)),
             _SECTION_END]
    _atomic_write(path, b''.join(kept) + b''.join(os.fsencode(line) + b'\n' for line in lines))


def _read_manifest(git_dir: Path) -> dict | None:
    try:
        data = json.loads((git_dir / MANIFEST_NAME).read_text(encoding='ascii'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise _failure(git_dir.parent, f'unreadable {MANIFEST_NAME}: {error}') from error
    if (not isinstance(data, dict) or data.get('version') != 1
            or not all(isinstance(data.get(key), list) and all(map(_safe_relative, data[key]))
                       for key in ('shared', 'skipped'))
            or data.get('nestedProjects', 'shared') not in NESTED_PROJECT_DECISIONS
            or not isinstance(data.get(SHARED_FOLDERS_KEY, []), list)
            or not all(map(_safe_relative, data.get(SHARED_FOLDERS_KEY, [])))):
        raise _failure(git_dir.parent, f'invalid {MANIFEST_NAME}')
    return data


def _write_manifest(git_dir: Path, shared, skipped, baseline: str, policy: ContentPolicy,
                    nested_projects: str | None, shared_folders=()) -> None:
    data = {'version': 1, 'baseline': baseline, 'shared': list(shared), 'skipped': list(skipped),
            'policy': {'largeFileBytes': policy.large_file_bytes,
                       'bulkDirectoryFiles': policy.bulk_directory_files}}
    if nested_projects is not None:
        data['nestedProjects'] = nested_projects
    if shared_folders:
        data[SHARED_FOLDERS_KEY] = list(shared_folders)
    # ensure_ascii keeps undecodable (surrogate-escaped) names representable.
    _atomic_write(git_dir / MANIFEST_NAME, json.dumps(data, indent=1).encode('ascii') + b'\n')


def _holds_managed_path(root: Path, directory: str, known: set[str]) -> bool:
    """Whether Tofu's own rules (runtime trees, shared paths) exclude anything inside."""
    for current, directories, files in os.walk(root / directory):
        _advanced()
        relative = Path(current).relative_to(root).as_posix()
        for name in (*directories, *files):
            path = f'{relative}/{name}'
            if _runtime_path(path) or path in known:
                return True
    return False


def _hidden_shares(root: Path, git_dir: Path, known: set[str]) -> dict[str, str]:
    """Paths the folder's own ignore files hide; workers still need to read them.

    The ignored listing names matched paths and also untracked directories
    whose whole content is ignored, by any rule, managed ones included (Git
    2.34 lists both such a directory and its content). Only a path an ignore
    rule matches by itself is hidden. An unmatched listed directory is hidden
    whole only when no managed rule excludes anything inside it, so the parent
    of a shared environment is never mistaken for user-ignored data, while a
    directory of thousands of user-ignored files stays one shared path.
    """
    entries = sorted({entry.rstrip('/') for entry in _paths(_walk_git(
        ['ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z'],
        cwd=root, git_dir=git_dir))})
    candidates = [path for path in entries
                  if _safe_relative(path) and not _runtime_path(path)
                  and _anchored_pattern(path) is not None and path not in known
                  and not any(ancestor in known for ancestor in _ancestors(path))]
    if not candidates:
        return {}
    matched = set(_paths(_walk_git(
        ['check-ignore', '--stdin', '-z'], cwd=root, git_dir=git_dir, accepted=(0, 1),
        input=b''.join(os.fsencode(path) + b'\0' for path in candidates))))
    whole = {path for path in candidates if path not in matched
             and (root / path).is_dir() and not (root / path).is_symlink()
             and not _holds_managed_path(root, path, known)}
    return {path: 'ignored by an ignore file in the folder'
            for path in _outermost(matched | whole)}


def _apply_policy(root: Path, git_dir: Path, policy: ContentPolicy, previous: dict, *,
                  discovered: dict[str, str] | None = None, nested_projects: str | None = None,
                  shared_folders=()):
    """Classify what is untracked now; known shared and skipped paths stay so.

    ``discovered`` are paths the discovery walk shared without entering them;
    they are excluded before Git walks the folder, so Git never lists them.
    """
    discovered = {**{folder: 'shared data folder named by the owner' for folder in shared_folders},
                  **(discovered or {})}
    known_shared = _outermost([*(previous.get('shared') or []), *discovered])
    known_skipped = list(previous.get('skipped') or [])
    _write_exclude(git_dir, known_shared, known_skipped)
    untracked = _paths(_walk_git(['ls-files', '--others', '--exclude-standard', '-z'],
                                 cwd=root, git_dir=git_dir))
    tracked = _paths(_walk_git(['ls-files', '-z'], cwd=root, git_dir=git_dir))
    plan = plan_content(root, untracked, tracked, policy, nested_projects)
    for path, reason in discovered.items():
        plan.shared.setdefault(path, reason)
    skipped = sorted(set(known_skipped) | set(plan.skipped))
    shared = _outermost(known_shared + list(plan.shared))
    _write_exclude(git_dir, shared, skipped)
    hidden = _hidden_shares(root, git_dir, set(shared) | set(skipped))
    if hidden:
        # Linked paths must stay excluded even if those ignore files change later.
        plan.shared.update(hidden)
        shared = _outermost(shared + list(hidden))
        _write_exclude(git_dir, shared, skipped)
    return plan, shared, skipped


def _write_default_guidance(root: Path, plan: ContentPlan, shared) -> tuple[Path, bytes] | None:
    """Create-only: never replaces guidance a person or another session wrote."""
    from lib.project_mod.instructions import ROOT_RULE_FILES
    from lib.project_mod.managed_repository_guidance import render_default_guidance
    if any(os.path.lexists(root / name) for name in ROOT_RULE_FILES):
        return None
    environments = []
    if (root / '.venv' / 'pyvenv.cfg').is_file():
        environments.append('.venv')
    if (root / 'node_modules').is_dir():
        environments.append('node_modules')
    content = render_default_guidance(
        root.name, [*plan.versioned, GUIDANCE_FILE],
        {path: plan.shared.get(path, 'shared data') for path in shared},
        environments).encode('utf-8')
    path = root / GUIDANCE_FILE
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
    return path, content


def _discard_generated_guidance(generated) -> None:
    if generated is None:
        return
    path, content = generated
    try:
        if path.read_bytes() == content:
            path.unlink()
    except OSError:
        logger.warning('[ManagedRepository] generated guidance retained at %s', path)


def _record_baseline(root: Path, git_dir: Path, index_path: Path) -> str:
    environment = {'GIT_INDEX_FILE': str(index_path)}
    _walk_git(['add', '-A', '--', '.'], cwd=root, git_dir=git_dir, env=environment)
    tree = _walk_git(['write-tree'], cwd=root, git_dir=git_dir, env=environment).decode().strip()
    commit = _git(['commit-tree', tree, '-m', _BASELINE_MESSAGE], cwd=root, git_dir=git_dir,
                  env={**environment, **_IDENTITY}).decode().strip()
    branch = _git(['symbolic-ref', '-q', 'HEAD'], cwd=root, git_dir=git_dir).decode().strip()
    # Create-only: an empty old value refuses to replace a branch that appeared meanwhile.
    _git(['update-ref', '-m', 'Tofu baseline', branch, commit, ''], cwd=root, git_dir=git_dir)
    return commit


def _local_root(project_path) -> Path:
    if (not isinstance(project_path, str) or not project_path.strip()
            or project_path.startswith('remote:')):
        raise ManagedRepositoryUnavailable(
            f'Project root {project_path} is not a local folder.',
            'Open a local folder as the project.', '请将本地文件夹作为项目打开。',
            code='not_local_folder')
    real = os.path.realpath(os.path.expanduser(project_path))
    if not os.path.isdir(real):
        raise ManagedRepositoryUnavailable(
            f'Project root {project_path} is not an existing local directory.',
            'Open an existing local folder as the project.', '请将一个存在的本地文件夹作为项目打开。',
            code='folder_missing')
    # Project keys retain the person's path; filesystem work and locks use the
    # physical root, including when an ancestor of the project is a symlink.
    if not os.access(real, os.W_OK | os.X_OK):
        raise ManagedRepositoryUnavailable(
            f'Tofu cannot write to the project folder {real}, so it cannot prepare it for isolated workers.',
            'Make the folder writable for Tofu, then delegate the work again.',
            '请让 Tofu 对该文件夹有写权限，然后重新委派。', code='folder_not_writable')
    return Path(real)


def _containing_repository(root: Path) -> tuple[Path, Path] | None:
    if not any(os.path.lexists(parent / '.git') for parent in (root, *root.parents)):
        return None
    result = _run(['rev-parse', '--show-toplevel', '--git-common-dir'], cwd=root)
    values = os.fsdecode(result.stdout).splitlines()
    if result.returncode == 0 and len(values) == 2:
        return (Path(os.path.realpath(values[0])),
                Path(os.path.realpath(os.path.join(root, values[1]))))
    detail = os.fsdecode(result.stderr).strip()
    if 'not a git repository' in detail.lower() and not os.path.lexists(root / '.git'):
        return None  # Discovery stopped at a filesystem boundary above this folder.
    raise ManagedRepositoryUnavailable(
        f'Project root {root} contains Git data that cannot be read ({detail[:300]}).',
        'Open a folder whose Git data is intact as the project.',
        '请将 Git 数据完好的文件夹作为项目打开。', code='unreadable_git_data')


def _discard_staging(root: Path) -> None:
    """Only a crashed preparation leaves these; the root lock excludes live ones."""
    with os.scandir(root) as entries:
        stale = [entry.path for entry in entries
                 if entry.name.startswith(_STAGING_PREFIX) and entry.is_dir(follow_symlinks=False)]
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)


def _init_repository(root: Path, git_dir: Path) -> None:
    """A new repository for ``root`` at ``git_dir``, configured as preparation needs it."""
    _git(['init', '-q', '--bare', '--template=', str(git_dir)], cwd=root)
    for key, value in _LOCAL_CONFIG:
        _git(['config', key, value], cwd=root, git_dir=git_dir)
    _git(['symbolic-ref', 'HEAD', BASELINE_BRANCH], cwd=root, git_dir=git_dir)


def _declares_project(root: Path) -> bool:
    """Whether the folder root holds a project manifest: then the root is the project."""
    with os.scandir(root) as entries:
        return any(entry.name in PROJECT_MANIFESTS and entry.is_file(follow_symlinks=False)
                   for entry in entries)


def _journal(root: Path, mode: str, directory: Path | None = None) -> Journal:
    """One folder's discovery journal, outside it: Tofu's control root unless given."""
    if directory is None:
        from lib.integration_control import _control_root
        directory = _control_root() / 'preparations'
    rules = json.dumps([mode, sorted(PROJECT_MANIFESTS), _runtime_patterns()])
    name = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:20]
    return Journal(directory / f'{name}.jsonl', root=str(root),
                   rules=hashlib.sha256(rules.encode()).hexdigest())


@contextmanager
def _answering_repository(root: Path, git_dir: Path | None):
    """``git_dir``, or a scratch repository outside the folder for Git's ignore answers."""
    if git_dir is not None:
        yield git_dir
        return
    scratch = Path(tempfile.mkdtemp(prefix='tofu-discovery-'))
    try:
        _init_repository(root, scratch)
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@dataclass
class _Boundaries:
    """What discovery decided before Git walks the folder, and what awaits the owner."""
    shares: dict[str, str] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    undecided: list[str] = field(default_factory=list)
    journal: Journal | None = None
    listed: int = 0
    resumed: int = 0


def _discover_folder(root: Path, git_dir: Path | None, nested_projects: str | None, *,
                     journal_directory: Path | None = None, shared_folders=()) -> _Boundaries:
    """Walk a folder that declares no project, or whose owner chose sharing, to its boundaries.

    Nothing is written into the folder: Git answers ignore questions through
    ``git_dir`` (an unborn repository being adopted) or a scratch repository.
    Folders the owner named as shared data are never entered either.
    """
    named = set(shared_folders)
    if nested_projects == 'versioned' or (nested_projects is None and _declares_project(root)):
        return _Boundaries()
    journal = _journal(root, 'create' if git_dir is None else 'adopt', journal_directory)
    with _answering_repository(root, git_dir) as answering, _ignore_rules(root, answering) as ignored:
        def skip(path: str) -> bool:
            return (path in named or any(ancestor in named for ancestor in _ancestors(path))
                    or _runtime_path(path) or _anchored_pattern(path) is None or ignored(path))

        found = discover(root, journal, skip=skip, advanced=_advanced)
        if found.resumed:
            # Recorded listings may predate an ignore-file change; Git rechecks what they found.
            found.units = {path: reason for path, reason in found.units.items() if not ignored(path)}
            found.projects = [path for path in found.projects if not ignored(path)]
            found.project_folders = {path: count for path, count in found.project_folders.items()
                                     if not ignored(path)}
    covered = lambda path: path in named or any(ancestor in named for ancestor in _ancestors(path))
    boundaries = _Boundaries(shares=dict(found.units), projects=found.projects, journal=journal,
                             listed=found.listed, resumed=found.resumed)
    if nested_projects == 'shared':
        boundaries.shares.update({path: reason for path, reason in found.project_shares().items()
                                  if not covered(path)})
    else:
        boundaries.undecided = [path for path in found.projects if not covered(path)]
    return boundaries


def _ambiguous(root: Path, projects: list[str]) -> ManagedRepositoryUnavailable:
    """The owner's question: Tofu never guesses whether other projects belong to this one."""
    groups = Counter(path.split('/', 1)[0] for path in projects)
    listing = '; '.join(f'{name}/ (itself a project)' if name in projects
                        else f'{name}/ ({count:,} inside)' for name, count in sorted(groups.items()))
    question = ('Are these folders copies and reference data that workers only read, or part '
                'of this project that workers change?')
    return ManagedRepositoryUnavailable(
        f'{root} has no project manifest at its root (pyproject.toml, package.json and the like) '
        f'but holds {len(projects):,} other projects, folders with a manifest of their own: '
        f'{listing}. Tofu does not guess whether they belong to this project, so it prepared '
        'nothing and created no worker.',
        f'Ask the owner: {question} Recommended: copies and reference data; Tofu then shares them '
        'in place, never versioned, and versions the rest of the folder. As part of the project '
        'Tofu versions all of them. Tofu applies the answer when it prepares the folder; opening '
        'one of these projects itself as the project also works.',
        '请向负责人确认：这些文件夹是工作者只读的副本和参考数据，还是本项目中工作者要修改的部分？'
        '推荐视为副本和参考数据：Tofu 会原地共享它们、不纳入版本，并把文件夹其余内容纳入版本；'
        '若属于本项目，Tofu 会把它们全部纳入版本。Tofu 在准备该文件夹时采用这个答复；'
        '也可以直接把其中某个项目文件夹作为项目打开。',
        code='ambiguous_project_root',
        details={'candidates': dict(sorted(groups.items())), 'nestedProjects': len(projects),
                 'question': question, 'options': list(NESTED_PROJECT_DECISIONS),
                 'recommended': 'shared', 'prepareScript': PREPARE_SCRIPT,
                 'sharedFoldersHint': 'Whole run/reference folders can also be shared by name '
                                      '(--shared-folder work --shared-folder reference), instead '
                                      'of versioning their non-project artifacts.'})


def _create(root: Path, policy: ContentPolicy, nested_projects: str | None,
            shared_folders=()) -> Preparation:
    boundaries = _discover_folder(root, None, nested_projects, shared_folders=shared_folders)
    if boundaries.undecided:
        raise _ambiguous(root, boundaries.undecided)
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(root)))
    generated = None
    try:
        _init_repository(root, staging)
        plan, shared, skipped = _apply_policy(root, staging, policy, {}, discovered=boundaries.shares,
                                              nested_projects=nested_projects,
                                              shared_folders=shared_folders)
        generated = _write_default_guidance(root, plan, shared)
        baseline = _record_baseline(root, staging, staging / 'index')
        _write_manifest(staging, shared, skipped, baseline, policy, nested_projects, shared_folders)
        if os.path.lexists(root / '.git'):
            raise ManagedRepositoryUnavailable(
                f'A Git repository appeared in {root} while Tofu was preparing it.',
                'Delegate the work again.', '请重新委派。', code='concurrent_repository')
        os.rename(staging, root / '.git')
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        _discard_generated_guidance(generated)
        raise
    if boundaries.journal is not None:
        boundaries.journal.discard()
    logger.info('[ManagedRepository] created root=%s shared=%d versioned=%d listed=%d resumed=%d',
                root, len(shared), len(plan.versioned), boundaries.listed, boundaries.resumed)
    return Preparation('created', str(root), baseline, tuple(shared), tuple(shared),
                       GUIDANCE_FILE if generated else '')


def _adopt(root: Path, git_dir: Path, policy: ContentPolicy,
           nested_projects: str | None, shared_folders=()) -> Preparation:
    """Record the first commit of an unborn repository whose index is empty."""
    boundaries = _discover_folder(root, git_dir, nested_projects, shared_folders=shared_folders)
    if boundaries.undecided:
        raise _ambiguous(root, boundaries.undecided)
    for key, value in _LOCAL_CONFIG:
        _git(['config', key, value], cwd=root, git_dir=git_dir)
    plan, shared, skipped = _apply_policy(root, git_dir, policy, _read_manifest(git_dir) or {},
                                          discovered=boundaries.shares,
                                          nested_projects=nested_projects,
                                          shared_folders=shared_folders)
    generated = _write_default_guidance(root, plan, shared)
    index = git_dir / 'tofu-baseline.index'
    index.unlink(missing_ok=True)
    try:
        baseline = _record_baseline(root, git_dir, index)
    except BaseException:
        index.unlink(missing_ok=True)
        _discard_generated_guidance(generated)
        raise
    os.replace(index, git_dir / 'index')
    _write_manifest(git_dir, shared, skipped, baseline, policy, nested_projects, shared_folders)
    if boundaries.journal is not None:
        boundaries.journal.discard()
    logger.info('[ManagedRepository] adopted root=%s shared=%d', root, len(shared))
    return Preparation('adopted', str(root), baseline, tuple(shared), tuple(shared),
                       GUIDANCE_FILE if generated else '')


def _refresh(root: Path, git_dir: Path, baseline: str, policy: ContentPolicy, previous: dict,
             nested_projects: str | None) -> Preparation:
    _, shared, skipped = _apply_policy(root, git_dir, policy, previous,
                                       nested_projects=nested_projects,
                                       shared_folders=previous.get(SHARED_FOLDERS_KEY) or ())
    if not (git_dir / 'index').exists():
        _walk_git(['read-tree', baseline], cwd=root, git_dir=git_dir)
    known = set(previous.get('shared') or [])
    added = tuple(path for path in shared if path not in known)
    _write_manifest(git_dir, shared, skipped, previous.get('baseline') or baseline, policy,
                    nested_projects, previous.get(SHARED_FOLDERS_KEY) or ())
    if added:
        logger.info('[ManagedRepository] refreshed root=%s newly_shared=%s', root, list(added[:20]))
    return Preparation('refreshed' if added else 'unchanged', str(root), baseline,
                       tuple(shared), added, '')


def _recorded_decision(root: Path, manifest: dict, nested_projects: str | None) -> str | None:
    """The answer recorded at preparation wins; a different explicit one is refused."""
    recorded = manifest.get('nestedProjects')
    if nested_projects is not None and recorded is not None and nested_projects != recorded:
        raise ManagedRepositoryUnavailable(
            f'Tofu prepared {root} with its nested projects {recorded}; preparing it again never '
            'changes that answer, because worker checkouts were built on it.',
            'Keep the recorded answer. Changing it means preparing the folder again from scratch, '
            'which only the owner can approve.',
            '请保留已记录的答复。若要更改，需要从头重新准备该文件夹，这只能由负责人批准。',
            code='decision_recorded')
    return recorded or nested_projects


def _decision(nested_projects) -> str | None:
    if nested_projects is not None and nested_projects not in NESTED_PROJECT_DECISIONS:
        raise ValueError(f'nested_projects must be one of {NESTED_PROJECT_DECISIONS} or None')
    return nested_projects


def _shared_folders(root: Path, folders) -> tuple[str, ...]:
    """Owner-named read-only data folders: existing directories inside the project root."""
    named = []
    for value in folders or ():
        relative = os.fspath(value).strip('/').replace(os.sep, '/')
        if not _safe_relative(relative):
            raise ValueError(f'shared folder must be a relative path inside the project: {value!r}')
        if not (root / relative).is_dir():
            raise ManagedRepositoryUnavailable(
                f'The owner named {relative} as shared data, but it is not a directory in {root}.',
                'Name only folders that exist in the project.',
                '请只填写项目中存在的文件夹。', code='shared_folder_missing')
        named.append(relative)
    return _outermost(named)


def prepare_managed_repository(project_path: str, *, refresh: bool = True,
                               policy: ContentPolicy = DEFAULT_CONTENT_POLICY,
                               progress=None, cancel=None,
                               nested_projects: str | None = None,
                               shared_folders=()) -> Preparation:
    """Make the folder a Tofu-managed repository; refresh extends an existing one's policy.

    Idempotent and serialized per folder across processes. A repository its
    people already committed to is never modified (state ``user_repository``).
    ``progress()`` runs on this thread at every observed advance; a set
    ``cancel`` (Stop) raises ``PreparationCancelled``. A failed or stopped
    preparation leaves the folder as it was; its discovery journal, kept
    outside the folder, lets the next preparation resume. ``nested_projects``
    is the owner's answer to ``ambiguous_project_root`` (``shared`` or
    ``versioned``); ``shared_folders`` names whole folders of read-only data.
    Both are recorded when the repository is created.
    """
    nested_projects = _decision(nested_projects)
    root = _local_root(project_path)
    named = _shared_folders(root, shared_folders)
    from lib.integration_control import IntegrationError, _repo_lock
    token = _observer.set(_Observer(progress, cancel))
    try:
        with _repo_lock(str(root), cancel=cancel):
            return _prepare_locked(root, refresh, policy, nested_projects, named)
    except IntegrationError as error:
        if cancel is None or not cancel.is_set():
            raise
        raise PreparationCancelled(str(error)) from error
    finally:
        _observer.reset(token)


def _prepare_locked(root: Path, refresh: bool, policy: ContentPolicy,
                    nested_projects: str | None, shared_folders=()) -> Preparation:
    _discard_staging(root)
    repository = _containing_repository(root)
    if repository is None:
        return _create(root, policy, nested_projects, shared_folders)
    toplevel, common = repository
    git_dir = root / '.git'
    if toplevel != root:
        raise ManagedRepositoryUnavailable(
            f'Project root {root} is a subdirectory of the Git repository {toplevel}; Tofu '
            'prepares only whole folders and never changes an enclosing repository.',
            f'Open {toplevel} as the project, or move this folder outside {toplevel}.',
            f'请将 {toplevel} 作为项目打开，或把该文件夹移到 {toplevel} 之外。',
            code='inside_other_repository')
    if common != git_dir:
        if common.name == '.git':
            raise ManagedRepositoryUnavailable(
                f'Project root {root} is a linked Git worktree of {common.parent}.',
                f'Open {common.parent} as the project.', f'请将 {common.parent} 作为项目打开。',
                code='linked_worktree')
        raise ManagedRepositoryUnavailable(
            f'Project root {root} keeps its Git data outside the folder ({common}).',
            'Open a folder that keeps its own Git data as the project.',
            '请将自带 Git 数据的文件夹作为项目打开。', code='external_git_data')
    head = _run(['rev-parse', '--verify', '--quiet', 'HEAD^{commit}'], cwd=root, git_dir=git_dir)
    baseline = os.fsdecode(head.stdout).strip() if head.returncode == 0 else ''
    managed = os.fsdecode(_run(['config', '--bool', '--get', MANAGED_CONFIG_KEY],
                               cwd=root, git_dir=git_dir).stdout).strip() == 'true'
    if baseline and not managed:
        return Preparation('user_repository', str(root), baseline, (), (), '')
    if baseline:
        previous = _read_manifest(git_dir) or {}
        decision = _recorded_decision(root, previous, nested_projects)
        if refresh:
            return _refresh(root, git_dir, baseline, policy, previous, decision)
        return Preparation('unchanged', str(root), baseline,
                           tuple(previous.get('shared') or ()), (), '')
    if _walk_git(['ls-files', '-z'], cwd=root, git_dir=git_dir).strip(b'\0'):
        raise ManagedRepositoryUnavailable(
            f'The Git repository {root} has staged files but no commits; Tofu does not '
            'commit what someone else staged.',
            'Commit the staged files with your Git tool, or ask the person who staged them '
            'to, then delegate the work again.',
            '该仓库已有暂存的文件但还没有提交；Tofu 不会替他人提交暂存内容。请先提交这些'
            '暂存文件，然后重新委派。', code='staged_without_commits')
    return _adopt(root, git_dir, policy, nested_projects, shared_folders)


@dataclass(frozen=True)
class PreparationPreview:
    """What preparing a plain folder would do, computed without writing into it."""
    root: str
    state: str  # repository | ambiguous | would_create
    refusal: ManagedRepositoryUnavailable | None = None
    projects: tuple[str, ...] = ()
    versioned: tuple[str, ...] = ()
    versioned_bytes: int = 0
    shared: dict = field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    listed: int = 0
    resumed: int = 0
    seconds: dict = field(default_factory=dict)


def preview_preparation(project_path: str, *, scratch: Path, nested_projects: str | None = None,
                        policy: ContentPolicy = DEFAULT_CONTENT_POLICY, hash_content: bool = False,
                        progress=None, cancel=None, shared_folders=()) -> PreparationPreview:
    """Classify a plain folder exactly as creating its repository would; write only in ``scratch``.

    Git reads the folder through a scratch repository in ``scratch``, which
    also keeps the discovery journal, so a later preview resumes it while a
    real preparation keeps its own. ``hash_content`` also stages the versioned
    files into the scratch repository, measuring the baseline's cost. A folder
    that already holds Git data is reported as ``repository`` only.
    """
    nested_projects = _decision(nested_projects)
    root = _local_root(project_path)
    named = _shared_folders(root, shared_folders)
    scratch = Path(scratch)
    token = _observer.set(_Observer(progress, cancel))
    try:
        if _containing_repository(root) is not None:
            return PreparationPreview(str(root), 'repository')
        scratch.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        boundaries = _discover_folder(root, None, nested_projects, journal_directory=scratch,
                                      shared_folders=named)
        seconds = {'discovery': time.monotonic() - started}
        found = {'projects': tuple(boundaries.projects), 'listed': boundaries.listed,
                 'resumed': boundaries.resumed, 'seconds': seconds}
        if boundaries.undecided:
            return PreparationPreview(str(root), 'ambiguous', _ambiguous(root, boundaries.undecided),
                                      **found)
        git_dir = Path(tempfile.mkdtemp(prefix='repository-', dir=str(scratch)))
        _init_repository(root, git_dir)
        started = time.monotonic()
        plan, shared, skipped = _apply_policy(root, git_dir, policy, {}, discovered=boundaries.shares,
                                              nested_projects=nested_projects, shared_folders=named)
        seconds['classification'] = time.monotonic() - started
        if hash_content:
            started = time.monotonic()
            _walk_git(['add', '-A', '--', '.'], cwd=root, git_dir=git_dir,
                      env={'GIT_INDEX_FILE': str(git_dir / 'index')})
            seconds['baseline'] = time.monotonic() - started
        return PreparationPreview(
            str(root), 'would_create', versioned=tuple(sorted(plan.versioned)),
            versioned_bytes=plan.versioned_bytes,
            shared={path: plan.shared.get(path, 'shared data') for path in shared},
            skipped=tuple(skipped), **found)
    finally:
        _observer.reset(token)


def link_shared_paths(canonical_root, workspace) -> list[str]:
    """Link a managed repository's shared paths into one of its worker checkouts.

    Idempotent; returns the repository-relative paths linked by this call.
    Repositories without a Tofu manifest receive nothing. A path already present
    in the checkout, or one whose parent resolves outside the checkout, is left
    alone. Managed ignore rules keep every link out of snapshots and checkpoints.
    A link that cannot be created is logged and skipped: the data stays readable
    at its canonical path, so the worker is not refused over it.
    """
    root, checkout = Path(canonical_root), Path(workspace)
    manifest = _read_manifest(root / '.git')
    if manifest is None:
        return []
    boundary = os.path.realpath(checkout)
    linked = []
    for relative in [*manifest['shared'], *sorted(FORBIDDEN_CHECKPOINT_ROOTS)]:
        source, destination = root / relative, checkout / relative
        if not os.path.lexists(source) or os.path.lexists(destination):
            continue
        anchor = destination.parent
        while not os.path.lexists(anchor):
            anchor = anchor.parent
        resolved = os.path.realpath(anchor)
        if resolved != boundary and not resolved.startswith(boundary + os.sep):
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(source, destination, target_is_directory=source.is_dir())
        except OSError as error:
            logger.warning('[ManagedRepository] shared path %s not linked into %s: %s',
                           relative, checkout, error)
            continue
        linked.append(relative)
    return linked


def is_shared_link(canonical_root, workspace, relative: str) -> bool:
    """Whether a checkout path is exactly a link link_shared_paths() would create.

    Such a link is reconstructible: retiring the checkout loses nothing.
    """
    try:
        return os.readlink(Path(workspace) / relative) == str(Path(canonical_root) / relative)
    except OSError:
        return False
