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
    folder's own ignore files, and root runtime directories. They are never
    versioned; link_shared_paths() links them into every worker checkout, where
    workers read them in place;
  * everything else is versioned: worker edits are isolated, reviewed and
    published by the integration owner.
Checkout cost on network volumes grows with the versioned file count, which is
why bulk data is shared (measurement in docs/PROJECT_COORDINATION.md).
Preparing an existing managed repository again only extends the policy to new
untracked data: the shared set only grows and no further commit is made.

Entry points: prepare_managed_repository (called through
workspace_delivery.isolated_workspace_refusal), link_shared_paths (worker
binding) and is_shared_link (checkout reclamation).
Dependencies: integration_control (per-root lock), git_checkpoint_policy,
agent_artifacts, instructions (guidance names), managed_repository_guidance.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from lib.agent_loop.agent_artifacts import ARTIFACT_PREFIX, GITIGNORE_PATTERN
from lib.git_checkpoint_policy import (
    FORBIDDEN_CHECKPOINT_PARTS, FORBIDDEN_CHECKPOINT_ROOTS, git_policy_environment,
)
from lib.git_runtime import git_executable
from lib.log import get_logger

logger = get_logger(__name__)

LARGE_FILE_BYTES = 10 * 1024 * 1024
BULK_DIRECTORY_FILES = 2000
MANAGED_CONFIG_KEY = 'tofu.managedRepository'
MANIFEST_NAME = 'tofu-managed.json'
BASELINE_BRANCH = 'refs/heads/main'
GUIDANCE_FILE = 'AGENTS.md'
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


@dataclass(frozen=True)
class Preparation:
    state: str  # created | adopted | refreshed | unchanged | user_repository
    root: str
    baseline: str
    shared: tuple[str, ...]
    added_shared: tuple[str, ...]
    guidance: str


class ManagedRepositoryUnavailable(RuntimeError):
    """Tofu cannot prepare this folder; carries a human remedy free of Git commands."""

    def __init__(self, reason: str, remedy: str, remedy_zh: str):
        super().__init__(f'{reason} {remedy}')
        self.reason, self.remedy, self.remedy_zh = reason, remedy, remedy_zh

    def hint(self) -> str:
        return f'解决办法 / How to fix:\n• {self.remedy_zh}\n\n• {self.remedy}'


def _failure(root, detail: str) -> ManagedRepositoryUnavailable:
    return ManagedRepositoryUnavailable(
        f'Tofu could not prepare {root} for isolated workers ({detail}).',
        'Check that the folder is writable and has free disk space and that Git is installed '
        'on the Tofu server, then delegate the work again.',
        '请确认该文件夹可写、磁盘空间充足且 Tofu 服务器已安装 Git，然后重新委派。')


def _run(args, *, cwd, git_dir=None, env=None, timeout=120.0,
         stdin: bytes | None = None) -> subprocess.CompletedProcess:
    environment = git_policy_environment()
    environment.update(env or {})
    try:
        command = [git_executable(), '-c', 'core.fsmonitor=false',
                   '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgSign=false']
        if git_dir is not None:
            command += ['--git-dir', str(git_dir), '--work-tree', str(cwd)]
        return subprocess.run([*command, *args], cwd=str(cwd), env=environment, input=stdin,
                              capture_output=True, timeout=timeout, check=False)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise _failure(cwd, f'git {args[0]}: {error}') from error


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
                 policy: ContentPolicy) -> ContentPlan:
    """Classify untracked paths; a directory that holds versioned files is never shared."""
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
        elif not stat.S_ISREG(info.st_mode) or not os.access(target, os.R_OK):
            plan.skipped.append(path)
        elif info.st_size >= policy.large_file_bytes and _anchored_pattern(path) is not None:
            shared[path] = f'{info.st_size / (1024 * 1024):,.1f} MiB'
        else:
            plan.versioned.append(path)
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
                       for key in ('shared', 'skipped'))):
        raise _failure(git_dir.parent, f'invalid {MANIFEST_NAME}')
    return data


def _write_manifest(git_dir: Path, shared, skipped, baseline: str, policy: ContentPolicy) -> None:
    data = {'version': 1, 'baseline': baseline, 'shared': list(shared), 'skipped': list(skipped),
            'policy': {'largeFileBytes': policy.large_file_bytes,
                       'bulkDirectoryFiles': policy.bulk_directory_files}}
    # ensure_ascii keeps undecodable (surrogate-escaped) names representable.
    _atomic_write(git_dir / MANIFEST_NAME, json.dumps(data, indent=1).encode('ascii') + b'\n')


def _holds_managed_path(root: Path, directory: str, known: set[str]) -> bool:
    """Whether Tofu's own rules (runtime trees, shared paths) exclude anything inside."""
    for current, directories, files in os.walk(root / directory):
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
    entries = sorted({entry.rstrip('/') for entry in _paths(_git(
        ['ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z'],
        cwd=root, git_dir=git_dir, timeout=1800.0))})
    candidates = [path for path in entries
                  if _safe_relative(path) and not _runtime_path(path)
                  and _anchored_pattern(path) is not None and path not in known
                  and not any(ancestor in known for ancestor in _ancestors(path))]
    if not candidates:
        return {}
    checked = _run(['check-ignore', '--stdin', '-z'], cwd=root, git_dir=git_dir, timeout=600.0,
                   stdin=b''.join(os.fsencode(path) + b'\0' for path in candidates))
    if checked.returncode not in (0, 1):
        detail = os.fsdecode(checked.stderr).strip()[:600]
        raise _failure(root, f'git check-ignore: {detail}')
    matched = set(_paths(checked.stdout))
    whole = {path for path in candidates if path not in matched
             and (root / path).is_dir() and not (root / path).is_symlink()
             and not _holds_managed_path(root, path, known)}
    return {path: 'ignored by an ignore file in the folder'
            for path in _outermost(matched | whole)}


def _apply_policy(root: Path, git_dir: Path, policy: ContentPolicy, previous: dict):
    """Classify what is untracked now; known shared and skipped paths stay so."""
    known_shared = list(previous.get('shared') or [])
    known_skipped = list(previous.get('skipped') or [])
    _write_exclude(git_dir, known_shared, known_skipped)
    untracked = _paths(_git(['ls-files', '--others', '--exclude-standard', '-z'],
                            cwd=root, git_dir=git_dir, timeout=1800.0))
    tracked = _paths(_git(['ls-files', '-z'], cwd=root, git_dir=git_dir, timeout=600.0))
    plan = plan_content(root, untracked, tracked, policy)
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
    _git(['add', '-A', '--', '.'], cwd=root, git_dir=git_dir, env=environment, timeout=1800.0)
    tree = _git(['write-tree'], cwd=root, git_dir=git_dir, env=environment).decode().strip()
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
            'Open a local folder as the project.', '请将本地文件夹作为项目打开。')
    real = os.path.realpath(os.path.expanduser(project_path))
    if not os.path.isdir(real):
        raise ManagedRepositoryUnavailable(
            f'Project root {project_path} is not an existing local directory.',
            'Open an existing local folder as the project.', '请将一个存在的本地文件夹作为项目打开。')
    # Project keys retain the person's path; filesystem work and locks use the
    # physical root, including when an ancestor of the project is a symlink.
    if not os.access(real, os.W_OK | os.X_OK):
        raise ManagedRepositoryUnavailable(
            f'Tofu cannot write to the project folder {real}, so it cannot prepare it for isolated workers.',
            'Make the folder writable for Tofu, then delegate the work again.',
            '请让 Tofu 对该文件夹有写权限，然后重新委派。')
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
        '请将 Git 数据完好的文件夹作为项目打开。')


def _discard_staging(root: Path) -> None:
    """Only a crashed preparation leaves these; the root lock excludes live ones."""
    with os.scandir(root) as entries:
        stale = [entry.path for entry in entries
                 if entry.name.startswith(_STAGING_PREFIX) and entry.is_dir(follow_symlinks=False)]
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)


def _create(root: Path, policy: ContentPolicy) -> Preparation:
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(root)))
    generated = None
    try:
        _git(['init', '-q', '--bare', '--template=', str(staging)], cwd=root)
        for key, value in _LOCAL_CONFIG:
            _git(['config', key, value], cwd=root, git_dir=staging)
        _git(['symbolic-ref', 'HEAD', BASELINE_BRANCH], cwd=root, git_dir=staging)
        plan, shared, skipped = _apply_policy(root, staging, policy, {})
        generated = _write_default_guidance(root, plan, shared)
        baseline = _record_baseline(root, staging, staging / 'index')
        _write_manifest(staging, shared, skipped, baseline, policy)
        if os.path.lexists(root / '.git'):
            raise ManagedRepositoryUnavailable(
                f'A Git repository appeared in {root} while Tofu was preparing it.',
                'Delegate the work again.', '请重新委派。')
        os.rename(staging, root / '.git')
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        _discard_generated_guidance(generated)
        raise
    logger.info('[ManagedRepository] created root=%s shared=%d versioned=%d',
                root, len(shared), len(plan.versioned))
    return Preparation('created', str(root), baseline, tuple(shared), tuple(shared),
                       GUIDANCE_FILE if generated else '')


def _adopt(root: Path, git_dir: Path, policy: ContentPolicy) -> Preparation:
    """Record the first commit of an unborn repository whose index is empty."""
    for key, value in _LOCAL_CONFIG:
        _git(['config', key, value], cwd=root, git_dir=git_dir)
    plan, shared, skipped = _apply_policy(root, git_dir, policy, _read_manifest(git_dir) or {})
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
    _write_manifest(git_dir, shared, skipped, baseline, policy)
    logger.info('[ManagedRepository] adopted root=%s shared=%d', root, len(shared))
    return Preparation('adopted', str(root), baseline, tuple(shared), tuple(shared),
                       GUIDANCE_FILE if generated else '')


def _refresh(root: Path, git_dir: Path, baseline: str, policy: ContentPolicy) -> Preparation:
    previous = _read_manifest(git_dir) or {}
    _, shared, skipped = _apply_policy(root, git_dir, policy, previous)
    if not (git_dir / 'index').exists():
        _git(['read-tree', baseline], cwd=root, git_dir=git_dir)
    known = set(previous.get('shared') or [])
    added = tuple(path for path in shared if path not in known)
    _write_manifest(git_dir, shared, skipped, previous.get('baseline') or baseline, policy)
    if added:
        logger.info('[ManagedRepository] refreshed root=%s newly_shared=%s', root, list(added[:20]))
    return Preparation('refreshed' if added else 'unchanged', str(root), baseline,
                       tuple(shared), added, '')


def prepare_managed_repository(project_path: str, *, refresh: bool = True,
                               policy: ContentPolicy = DEFAULT_CONTENT_POLICY) -> Preparation:
    """Make the folder a Tofu-managed repository; refresh extends an existing one's policy.

    Idempotent and serialized per folder across processes. A repository its
    people already committed to is never modified (state ``user_repository``).
    """
    root = _local_root(project_path)
    from lib.integration_control import _repo_lock
    with _repo_lock(str(root)):
        _discard_staging(root)
        repository = _containing_repository(root)
        if repository is None:
            return _create(root, policy)
        toplevel, common = repository
        git_dir = root / '.git'
        if toplevel != root:
            raise ManagedRepositoryUnavailable(
                f'Project root {root} is a subdirectory of the Git repository {toplevel}; Tofu '
                'prepares only whole folders and never changes an enclosing repository.',
                f'Open {toplevel} as the project, or move this folder outside {toplevel}.',
                f'请将 {toplevel} 作为项目打开，或把该文件夹移到 {toplevel} 之外。')
        if common != git_dir:
            if common.name == '.git':
                raise ManagedRepositoryUnavailable(
                    f'Project root {root} is a linked Git worktree of {common.parent}.',
                    f'Open {common.parent} as the project.', f'请将 {common.parent} 作为项目打开。')
            raise ManagedRepositoryUnavailable(
                f'Project root {root} keeps its Git data outside the folder ({common}).',
                'Open a folder that keeps its own Git data as the project.',
                '请将自带 Git 数据的文件夹作为项目打开。')
        head = _run(['rev-parse', '--verify', '--quiet', 'HEAD^{commit}'], cwd=root, git_dir=git_dir)
        baseline = os.fsdecode(head.stdout).strip() if head.returncode == 0 else ''
        managed = os.fsdecode(_run(['config', '--bool', '--get', MANAGED_CONFIG_KEY],
                                   cwd=root, git_dir=git_dir).stdout).strip() == 'true'
        if baseline and not managed:
            return Preparation('user_repository', str(root), baseline, (), (), '')
        if baseline and refresh:
            return _refresh(root, git_dir, baseline, policy)
        if baseline:
            shared = tuple((_read_manifest(git_dir) or {}).get('shared') or ())
            return Preparation('unchanged', str(root), baseline, shared, (), '')
        if _git(['ls-files', '-z'], cwd=root, git_dir=git_dir).strip(b'\0'):
            raise ManagedRepositoryUnavailable(
                f'The Git repository {root} has staged files but no commits; Tofu does not '
                'commit what someone else staged.',
                'Commit the staged files with your Git tool, or ask the person who staged them '
                'to, then delegate the work again.',
                '该仓库已有暂存的文件但还没有提交；Tofu 不会替他人提交暂存内容。请先提交这些'
                '暂存文件，然后重新委派。')
        return _adopt(root, git_dir, policy)


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
