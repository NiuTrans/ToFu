"""Verified local replicas of Python environments that live on slow volumes.

Responsibility: keep an immutable, verified copy of the managed worker's
Python environment -- a standalone prefix (conda env, installed CPython) or a
virtualenv, rebased onto a replica of its base when that base is slow too --
on the fast local root chosen by :mod:`serverctl_pkg.local_placement`, so the
server imports from local disk instead of a network filesystem. Replicas are
reconstructible caches: the source environment stays the only install target,
and a missing, stale, suspect or unverifiable replica always falls back to the
source interpreter.

Layout under the placement root::

    <root>/build.lock, build.log                  one builder per root
    <root>/<label>-<key>/live -> g-<id>           newest published generation
    <root>/<label>-<key>/g-<id>/...               immutable, read-only generation
    <root>/<label>-<key>/g-<id>/.tofu-replica.json   written after verification
    <root>/<label>-<key>/g-<id>/.tofu-replica.lease  shared flock while in use
    <root>/<label>-<key>/build-status.json        last outcome, drives backoff

Workers run from an immutable generation path. A generation is in use while a
lease is held on it or a live process maps one of its files; collection never
removes one in use, and a rebuild never changes files under a running
interpreter. Console scripts keep their source shebangs, so ``pip`` and other
installers always act on the source environment.

Entry points: :func:`select_worker_runtime` (lifecycle manager, at spawn),
:func:`request_build` (after the worker is ready), :func:`lease_generation`
and :func:`reject_generation` (worker lifetime),
:func:`runs_marked_environment`, :func:`serves_environment`,
:func:`authoritative_interpreter` and :func:`source_package_directories`
(environment guards, restarts and package installs), and
``python -m serverctl_pkg.runtime_replica {build,status,prune}``.

Dependencies: Python standard library and :mod:`serverctl_pkg.local_placement`.
Boot-time callers use only the identity helpers; the builder machinery and the
placement policy load lazily.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

SCHEMA = 1
MANIFEST_NAME = '.tofu-replica.json'
LEASE_NAME = '.tofu-replica.lease'
REJECTED_NAME = '.tofu-replica.rejected'
LIVE_NAME = 'live'
STATUS_NAME = 'build-status.json'
LOCK_NAME = 'build.lock'
LOG_NAME = 'build.log'
# Written when a build starts; it survives only a builder that died.
BUILDING_STATE = 'building'
# A layout this mechanism cannot reproduce stays unsupported until the
# source environment changes.
UNSUPPORTED_STATE = 'unsupported'
REJECTIONS_BEFORE_UNSUPPORTED = 3
FAILURE_BACKOFF_SECONDS = 1800.0
MAX_FAILURE_BACKOFF_SECONDS = 24 * 3600.0
IDLE_REGISTRY_SECONDS = 30 * 24 * 3600.0
# Fixed copy fan-out for a background cache build: enough to hide network
# round trips, independent of the host's CPU count.
COPY_THREADS = 16
_LOG_ROTATE_BYTES = 1024 * 1024
_SYSTEM_PREFIXES = frozenset({'/', '/usr', '/usr/local', '/opt', '/bin', '/sbin'})
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_PROBE_TIMEOUT_SECONDS = 120.0
_INFO_SUFFIXES = ('.dist-info', '.egg-info')
_VERIFY_MODULES = (
    'encodings', 'json', 'ssl', 'sqlite3', 'ctypes', 'zlib', 'hashlib',
    'decimal', 'lzma', 'bz2', 'uuid', 'site',
)
# Verification failures that repeat for the same source fingerprint.
_DETERMINISTIC_ERRORS = (
    'replica-prefix-mismatch', 'replica-base-mismatch', 'replica-modules-missing',
    'replica-maps-source-files', 'package-listing-mismatch',
)
# No -S: site sets a virtualenv's prefix.
_DESCRIBE_SOURCE = (
    'import json, sys, sysconfig\n'
    'paths = sysconfig.get_paths()\n'
    'print(json.dumps({"prefix": sys.prefix, "base_prefix": sys.base_prefix,'
    ' "executable": sys.executable,'
    ' "base_executable": getattr(sys, "_base_executable", "") or "",'
    ' "purelib": paths["purelib"], "platlib": paths["platlib"],'
    ' "stdlib": paths["stdlib"], "cache_tag": sys.implementation.cache_tag}))\n'
)
_VERIFY_SOURCE = (
    'import importlib, json, os, sys\n'
    'loaded = []\n'
    'for name in json.loads(sys.argv[1]):\n'
    '    try:\n'
    '        importlib.import_module(name)\n'
    '    except Exception:\n'
    '        continue\n'
    '    loaded.append(name)\n'
    'mapped = set()\n'
    'try:\n'
    '    with open("/proc/self/maps", encoding="utf-8") as handle:\n'
    '        for line in handle:\n'
    '            fields = line.split()\n'
    '            if len(fields) >= 6 and fields[5].startswith("/"):\n'
    '                mapped.add(fields[5])\n'
    'except OSError:\n'
    '    pass\n'
    'print(json.dumps({"prefix": os.path.realpath(sys.prefix),'
    ' "base_prefix": os.path.realpath(sys.base_prefix), "loaded": loaded,'
    ' "mapped": sorted(mapped)}))\n'
)


@dataclass(frozen=True, slots=True)
class Environment:
    """One Python environment as its own interpreter reports it.

    ``prefix`` and ``base_prefix`` are real paths; ``python``, ``purelib``,
    ``platlib`` are relative to ``prefix`` and ``stdlib`` to ``base_prefix``.
    ``base_python`` is the base interpreter of a virtualenv, else empty.
    """

    prefix: str
    base_prefix: str
    python: str
    purelib: str
    platlib: str
    stdlib: str
    cache_tag: str
    base_python: str = ''

    @property
    def kind(self) -> str:
        return 'venv' if self.prefix != self.base_prefix else 'prefix'


@dataclass(slots=True)
class WorkerRuntime:
    """The manager's interpreter choice for one worker spawn.

    ``generations`` lists the worker's generation, then the base generation a
    virtualenv replica runs on; ``lease_fds`` holds one lease per entry.
    """

    python: str
    replica: bool
    reason: str
    generations: list[str] = field(default_factory=list)
    lease_fds: list[int] = field(default_factory=list)
    build_root: str = ''
    build_pythons: list[str] = field(default_factory=list)
    status: dict[str, object] = field(default_factory=dict)

    def close_parent_lock(self) -> None:
        for descriptor in self.lease_fds:
            _close(descriptor)
        self.lease_fds = []


# ── identity helpers (boot path: standard library only, no subprocess) ──


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _manifest(generation: Path) -> dict | None:
    manifest = _read_json(generation / MANIFEST_NAME)
    if not manifest or manifest.get('schema') != SCHEMA:
        return None
    return manifest


def replica_origin(prefix: str | None = None) -> str | None:
    """Source prefix when ``prefix`` (default ``sys.prefix``) is a replica."""
    manifest = _manifest(Path(os.path.realpath(prefix or sys.prefix)))
    origin = manifest.get('sourcePrefix') if manifest else None
    return origin if isinstance(origin, str) and origin else None


def serves_environment(prefix: str, environment_prefix: str) -> bool:
    """Whether an interpreter running at ``prefix`` serves ``environment_prefix``.

    True for the environment itself and for any replica published from it,
    so environment re-exec guards accept a manager-selected replica instead
    of re-executing the worker back onto the slow volume.
    """
    if not prefix or not environment_prefix:
        return False
    target = os.path.realpath(environment_prefix)
    return os.path.realpath(prefix) == target or replica_origin(prefix) == target


def authoritative_interpreter(python: str | None = None) -> str:
    """The interpreter that owns the packages of ``python``'s environment.

    ``python`` defaults to this process's interpreter. An interpreter inside
    a replica generation maps to its source interpreter: installing into a
    replica would diverge from its source (and fails: generations are
    read-only), and anything that records or relaunches an interpreter must
    keep re-checking freshness instead of pinning one generation. Every other
    interpreter maps to itself.
    """
    candidate = python or sys.executable
    if not candidate:
        return candidate
    directory = os.path.realpath(os.path.dirname(os.path.abspath(candidate)))
    for generation in (directory, os.path.dirname(directory)):
        manifest = _manifest(Path(generation))
        if manifest is None:
            continue
        source = manifest.get('sourcePython')
        if isinstance(source, str) and os.path.isabs(source) and os.access(source, os.X_OK):
            return source
        break
    return candidate


def runs_marked_environment(target_python: str, environment_prefix: str = '') -> bool:
    """Whether this process already runs the installer-marked environment.

    The prefix comparison is authoritative when the marker records one (a
    virtualenv's interpreter is a symlink to its base, so interpreter paths
    alone cannot tell them apart); a replica of the environment counts as
    the environment.
    """
    if environment_prefix:
        return serves_environment(sys.prefix, environment_prefix)
    return os.path.realpath(authoritative_interpreter()) == os.path.realpath(target_python)


_PROCESS_LEASES: list[int] = []


def hold_process_leases() -> int:
    """Lease the generations this interpreter runs from, for its lifetime.

    The manager's lease ends with the manager (crash, Supervisor reload);
    this one ends with the process. Descriptors are close-on-exec, so
    children do not extend it. Returns the number of generations held.
    """
    if _PROCESS_LEASES:
        return len(_PROCESS_LEASES)
    generation = Path(os.path.realpath(sys.prefix))
    manifest = _manifest(generation)
    if manifest is None:
        return 0
    paths = [generation]
    base = manifest.get('baseGeneration')
    if isinstance(base, str) and base:
        paths.append(Path(base))
    for path in paths:
        descriptor = lease_generation(path)
        if descriptor is not None:
            _PROCESS_LEASES.append(descriptor)
    return len(_PROCESS_LEASES)


def source_package_directories(prefix: str | None = None) -> list[str]:
    """Source package directories of a replica, so fresh installs can load."""
    manifest = _manifest(Path(os.path.realpath(prefix or sys.prefix)))
    if not manifest:
        return []
    source = str(manifest.get('sourcePrefix') or '')
    return list(dict.fromkeys(os.path.join(source, manifest[key])
                              for key in ('purelib', 'platlib')
                              if isinstance(manifest.get(key), str)))


# ── environment description and freshness ──


def _clean_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(('PYTHON', 'VIRTUAL_ENV', 'CONDA'))
        and key not in {'LD_PRELOAD', 'LD_LIBRARY_PATH', '__PYVENV_LAUNCHER__'}
    }
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return environment


def _run_json(argv: list[str]) -> dict | None:
    import subprocess
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT_SECONDS, env=_clean_environment(), cwd=os.sep,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def describe_environment(python: str) -> Environment | None:
    """Ask ``python`` for its layout; ``None`` when it cannot be replicated."""
    if not python or not os.path.isabs(python) or not os.access(python, os.X_OK):
        return None
    report = _run_json([python, '-I', '-c', _DESCRIBE_SOURCE])
    if report is None:
        return None
    try:
        prefix = os.path.realpath(report['prefix'])
        base_prefix = os.path.realpath(report['base_prefix'])
        executable = os.path.abspath(report['executable'] or python)
        base_executable = str(report.get('base_executable') or '')
        paths = {name: os.path.realpath(report[name])
                 for name in ('purelib', 'platlib', 'stdlib')}
        cache_tag = str(report['cache_tag'] or '')
    except (KeyError, TypeError):
        return None
    # The interpreter derives its prefix from its invocation path, so only
    # layouts whose interpreter and packages live inside the copied prefix
    # are reproducible. A system-wide prefix is never copied wholesale; a
    # virtualenv on top of one copies only the virtualenv.
    invoked_parent = os.path.realpath(os.path.dirname(executable))
    if (prefix in _SYSTEM_PREFIXES
            or not _within(invoked_parent, prefix)
            or not _within(paths['purelib'], prefix)
            or not _within(paths['platlib'], prefix)
            or not _within(paths['stdlib'], base_prefix)):
        return None
    return Environment(
        prefix=prefix,
        base_prefix=base_prefix,
        python=os.path.join(os.path.relpath(invoked_parent, prefix),
                            os.path.basename(executable)),
        purelib=os.path.relpath(paths['purelib'], prefix),
        platlib=os.path.relpath(paths['platlib'], prefix),
        stdlib=os.path.relpath(paths['stdlib'], base_prefix),
        cache_tag=cache_tag,
        base_python=(os.path.abspath(base_executable)
                     if prefix != base_prefix and base_executable else ''),
    )


def _excluded_top_level(prefix: str) -> frozenset[str]:
    # A conda *base* installation also hosts every other environment and the
    # package cache; neither belongs to the base interpreter's runtime.
    if os.path.isdir(os.path.join(prefix, 'condabin')):
        return frozenset({'envs', 'pkgs'})
    return frozenset()


def _fingerprint_directories(environment: Environment) -> list[str]:
    prefix = Path(environment.prefix)
    directories = [prefix, prefix / 'bin', prefix / 'lib',
                   prefix / environment.purelib, prefix / environment.platlib]
    if environment.kind == 'prefix':
        stdlib = prefix / environment.stdlib
        directories += [stdlib, stdlib / 'lib-dynload', prefix / 'conda-meta']
    return sorted({str(path) for path in directories if path.is_dir()})


def fingerprint(environment: Environment) -> str | None:
    """Digest of the entries an install, upgrade or removal changes.

    Installers add, remove or rename distributions, scripts, shared libraries
    and conda records, and replace files by unlink-and-create; each changes a
    directory listing, a directory's modification time, or (in package
    directories, where bytecode writes would also touch the directory) a
    distribution's metadata directory. Listing names reads no per-entry
    metadata, so the check costs a few hundred lookups even on a network
    volume. ``.pth`` files are stat-ed because legacy installers edit them in
    place. In-place edits of other files are not detected.
    """
    digest = hashlib.sha256()
    digest.update(json.dumps([
        SCHEMA, environment.kind, environment.python, environment.cache_tag,
        environment.purelib, environment.platlib, environment.stdlib,
    ]).encode('utf-8'))
    package_directories = {os.path.join(environment.prefix, environment.purelib),
                           os.path.join(environment.prefix, environment.platlib)}
    try:
        if environment.kind == 'venv':
            digest.update(Path(environment.prefix, 'pyvenv.cfg').read_bytes())
        excluded = _excluded_top_level(environment.prefix)
        for directory in _fingerprint_directories(environment):
            packages = directory in package_directories
            header = os.path.relpath(directory, environment.prefix)
            if not packages:
                header += f':{os.stat(directory).st_mtime_ns}'
            digest.update(b'\0D' + header.encode('utf-8', 'surrogateescape'))
            rows = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name == '__pycache__' or (
                            directory == environment.prefix and entry.name in excluded):
                        continue
                    if entry.is_symlink():
                        row = f'{entry.name}\0l'
                    elif entry.is_dir(follow_symlinks=False):
                        row = f'{entry.name}\0d'
                    else:
                        row = f'{entry.name}\0f'
                    if packages and entry.name.endswith((*_INFO_SUFFIXES, '.pth')):
                        metadata = entry.stat(follow_symlinks=False)
                        row += f'\0{metadata.st_size}:{metadata.st_mtime_ns}'
                    rows.append(row)
            for row in sorted(rows):
                digest.update(b'\0' + row.encode('utf-8', 'surrogateescape'))
    except OSError:
        return None
    return digest.hexdigest()


def registry_path(root: str | os.PathLike[str], source_prefix: str) -> Path:
    real = os.path.realpath(source_prefix)
    key = hashlib.sha256(real.encode('utf-8')).hexdigest()[:16]
    label = ''.join(ch if ch.isalnum() or ch in '-_.' else '-'
                    for ch in os.path.basename(real))[:32] or 'env'
    return Path(root) / f'{label}-{key}'


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _live_generation(registry: Path) -> Path | None:
    try:
        target = os.readlink(registry / LIVE_NAME)
    except OSError:
        return None
    if os.sep in target or not target.startswith('g-'):
        return None
    return registry / target


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _open_lock(path: Path, *, exclusive: bool, create: bool) -> int | None:
    import fcntl
    descriptor = None
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | (os.O_CREAT if create else 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        _close(descriptor)
        return None


def _close(descriptor: int | None) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _mark_used(generation: Path) -> None:
    """Record use; idle collection keys on the lease file's mtime."""
    try:
        os.utime(generation / LEASE_NAME)
    except OSError:
        pass


def lease_generation(generation: str | os.PathLike[str]) -> int | None:
    """Shared lease that keeps one published generation from collection.

    Collection removes a generation's manifest before its files, so a lease
    that races a collection is released here instead of protecting a
    half-deleted tree.
    """
    path = Path(generation)
    descriptor = _open_lock(path / LEASE_NAME, exclusive=False, create=False)
    if descriptor is None:
        return None
    if _manifest(path) is None:
        _close(descriptor)
        return None
    _mark_used(path)
    return descriptor


def reject_generation(generation: str | os.PathLike[str], reason: str) -> None:
    """Never select this generation again; back off rebuilding its source.

    Called when a worker on this generation failed before readiness and a
    worker on the source interpreter then succeeded, so a defect that
    verification missed cannot become a crash loop.
    """
    path = Path(generation)
    manifest = _manifest(path)
    try:
        _write_json(path / REJECTED_NAME, {'reason': str(reason)[:500], 'rejectedAt': time.time()})
        if manifest:
            digest = str(manifest.get('fingerprint') or '')
            previous = _read_json(path.parent / STATUS_NAME) or {}
            rejections = 1 + _carried(previous, digest).get('rejections', 0)
            _write_json(path.parent / STATUS_NAME, {
                'state': ('rejected' if rejections < REJECTIONS_BEFORE_UNSUPPORTED
                          else UNSUPPORTED_STATE),
                'fingerprint': digest, 'sourcePrefix': manifest.get('sourcePrefix'),
                'error': str(reason)[:500], 'finishedAt': time.time(),
                'rejections': rejections})
    except OSError:
        pass


def _carried(previous: dict, digest: str) -> dict[str, int]:
    """Counters that survive rebuilds of one unchanged source."""
    if previous.get('fingerprint') != digest:
        return {}
    return {'rejections': _as_int(previous.get('rejections'))}


def _base_of(environment: Environment) -> tuple[Environment, str] | None:
    """A virtualenv's replicable base environment and its fingerprint."""
    if environment.kind != 'venv' or not environment.base_python:
        return None
    base = describe_environment(environment.base_python)
    if base is None or base.kind != 'prefix' or base.prefix != environment.base_prefix:
        return None
    digest = fingerprint(base)
    return (base, digest) if digest else None


def _fresh_generation(root: Path, environment: Environment, digest: str,
                      base: tuple[Environment, str] | None = None) -> Path | None:
    """The published generation of ``environment`` if it is current.

    ``base`` is :func:`_base_of` for a virtualenv, so callers describe each
    interpreter once per decision.
    """
    generation = _live_generation(registry_path(root, environment.prefix))
    if generation is None:
        return None
    manifest = _manifest(generation)
    if (not manifest or manifest.get('fingerprint') != digest
            or manifest.get('sourcePrefix') != environment.prefix
            or (generation / REJECTED_NAME).exists()
            or not os.access(generation / environment.python, os.X_OK)):
        return None
    if environment.kind == 'venv':
        # A virtualenv replica runs on one exact base generation; it is
        # stale once that base is rebuilt, and an unrebased one is stale
        # once a base replica becomes available.
        base_generation = _fresh_generation(root, *base) if base else None
        if (manifest.get('baseGeneration') or '') != str(base_generation or ''):
            return None
    return generation


def _build_admissible(root: Path, environment: Environment, digest: str) -> bool:
    registry = registry_path(root, environment.prefix)
    status = _read_json(registry / STATUS_NAME) or {}
    if status.get('fingerprint') != digest:
        return True
    state = status.get('state')
    if state == UNSUPPORTED_STATE:
        return False
    if state == 'insufficient-space':
        from serverctl_pkg import local_placement
        return local_placement.has_free_space(registry, _as_int(status.get('copiedBytes')),
                                              required_inodes=_as_int(status.get('copiedInodes')))
    if state == BUILDING_STATE:
        # A running builder holds the root lock and overwrites this record;
        # one that survived its builder records a crash.
        attempts = _as_int(status.get('failures')) + 1
        since = status.get('startedAt')
    elif state == 'failed':
        attempts = _as_int(status.get('failures'), 1)
        since = status.get('finishedAt')
    elif state == 'rejected':
        attempts = _as_int(status.get('rejections'), 1)
        since = status.get('finishedAt')
    else:
        return True
    backoff = min(MAX_FAILURE_BACKOFF_SECONDS,
                  FAILURE_BACKOFF_SECONDS * 2 ** min(max(attempts, 1) - 1, 16))
    return time.time() - (since if isinstance(since, (int, float)) else 0.0) >= backoff


def _placement(python: str, environ: Mapping[str, str]):
    from serverctl_pkg import local_placement
    # The interpreter's invocation directory lives on its environment's
    # volume; deciding there needs no subprocess.
    return local_placement.choose_local_root(
        os.path.dirname(os.path.abspath(python)), local_placement.PURPOSE_RUNTIME,
        environ=environ)


def _build_plan(root: Path, environment: Environment, python: str, digest: str,
                base: tuple[Environment, str] | None,
                environ: Mapping[str, str]) -> list[str]:
    """Interpreters to (re)build, base first, so a virtualenv can rebase."""
    plan = []
    if base is not None and _placement(environment.base_python, environ).selected:
        generation = _fresh_generation(root, *base)
        if generation is not None:
            _mark_used(generation)
        elif _build_admissible(root, *base):
            plan.append(environment.base_python)
    generation = _fresh_generation(root, environment, digest, base)
    if generation is not None:
        _mark_used(generation)
    elif plan or _build_admissible(root, environment, digest):
        plan.append(python)
    return plan


def select_worker_runtime(
    python: str,
    environ: Mapping[str, str] | None = None,
    *,
    avoid: str = '',
) -> WorkerRuntime:
    """Choose the worker interpreter: a fresh leased replica or ``python``.

    Never copies and never starts a build: a missing or stale replica is
    reported through ``build_pythons`` so the manager can build after the
    worker is ready, without competing with its boot for the slow volume.
    ``avoid`` names a generation under suspicion, which is not selected.
    """
    from serverctl_pkg import local_placement
    environment_map = os.environ if environ is None else environ
    mode = local_placement.placement_mode(local_placement.PURPOSE_RUNTIME, environment_map)
    status: dict[str, object] = {'mode': mode, 'sourcePython': python}
    if mode == 'off':
        return WorkerRuntime(python, False, 'disabled', status=status)
    placement = _placement(python, environment_map)
    status['placement'] = placement.as_status()
    if not placement.selected:
        return WorkerRuntime(python, False, placement.reason, status=status)
    environment = describe_environment(python)
    if environment is None:
        return WorkerRuntime(python, False, 'environment-unsupported', status=status)
    status['sourcePrefix'] = environment.prefix
    digest = fingerprint(environment)
    if digest is None:
        return WorkerRuntime(python, False, 'fingerprint-unavailable', status=status)
    root = Path(placement.root)
    status['build'] = _read_json(registry_path(root, environment.prefix) / STATUS_NAME) or {}
    base = _base_of(environment)
    generation = _fresh_generation(root, environment, digest, base)
    builds = _build_plan(root, environment, python, digest, base, environment_map)
    common = {'build_root': str(root), 'build_pythons': builds, 'status': status}
    if generation is None:
        return WorkerRuntime(python, False, 'replica-pending' if builds else 'replica-unavailable',
                             **common)
    if avoid and os.path.realpath(avoid) == os.path.realpath(generation):
        return WorkerRuntime(python, False, 'replica-suspect', **common)
    generations = [generation]
    base = (_manifest(generation) or {}).get('baseGeneration')
    if isinstance(base, str) and base:
        generations.append(Path(base))
    leases = []
    for path in generations:
        descriptor = lease_generation(path)
        if descriptor is None:
            for held in leases:
                _close(held)
            return WorkerRuntime(python, False, 'replica-lease-unavailable', **common)
        leases.append(descriptor)
    status['generation'] = generation.name
    return WorkerRuntime(str(generation / environment.python), True, 'replica',
                         generations=[str(path) for path in generations],
                         lease_fds=leases, **common)


def request_build(pythons: Iterable[str], root: str | os.PathLike[str], *,
                  launcher: str | None = None) -> bool:
    """Start one detached, low-priority builder; never waits for it.

    The builder runs on a source interpreter: collection may delete an old
    generation, which must never be the one the builder itself runs from.
    """
    import subprocess
    from serverctl_pkg import local_placement
    sources = [python for python in dict.fromkeys(pythons) if python]
    if not sources:
        return False
    root_path = Path(root)
    try:
        local_placement.ensure_private_directory(root_path)
    except OSError:
        return False
    probe = _open_lock(root_path / LOCK_NAME, exclusive=True, create=True)
    if probe is None:
        return False
    _close(probe)
    log_path = root_path / LOG_NAME
    try:
        if log_path.stat().st_size > _LOG_ROTATE_BYTES:
            os.replace(log_path, log_path.with_name(LOG_NAME + '.1'))
    except OSError:
        pass
    argv = [authoritative_interpreter(launcher), '-s', '-m', 'serverctl_pkg.runtime_replica',
            'build', '--root', str(root_path)]
    for python in sources:
        argv += ['--python', python]
    try:
        with open(log_path, 'ab') as log:
            subprocess.Popen(
                argv, cwd=str(_PACKAGE_ROOT), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True, env=_clean_environment())
    except OSError:
        return False
    return True


# ── collection ──


def _remove_tree(path: Path) -> None:
    import shutil

    def make_writable(function, failed_path, _error):
        # Generations are read-only below their root.
        try:
            os.chmod(os.path.dirname(failed_path), 0o700)
            function(failed_path)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def mapped_generations(root: str | os.PathLike[str]) -> frozenset[str]:
    """Generations under ``root`` with a file mapped by a live process.

    Covers processes no lease reaches: a worker whose manager restarted, or
    children that outlive their worker. Empty where ``/proc`` is absent.
    """
    prefix = os.path.realpath(root).rstrip(os.sep) + os.sep
    used: set[str] = set()
    try:
        processes = [name for name in os.listdir('/proc') if name.isdigit()]
    except OSError:
        return frozenset()
    for process in processes:
        try:
            with open(f'/proc/{process}/maps', encoding='utf-8', errors='surrogateescape') as maps:
                for line in maps:
                    position = line.find(prefix)
                    if position < 0:
                        continue
                    parts = line[position + len(prefix):].split(os.sep, 2)
                    if len(parts) >= 2 and parts[1].startswith('g-'):
                        used.add(prefix + parts[0] + os.sep + parts[1].split()[0])
        except OSError:
            continue
    return frozenset(used)


def _claim(generation: Path, mapped: frozenset[str]) -> tuple[bool, int | None]:
    """Exclusive claim on an unused generation: ``(claimed, lock fd)``."""
    if os.path.realpath(generation) in mapped:
        return False, None
    if not (generation / LEASE_NAME).exists():
        # Never published: callers hold the build lock, so no build owns it.
        return True, None
    descriptor = _open_lock(generation / LEASE_NAME, exclusive=True, create=False)
    return descriptor is not None, descriptor


def _remove_generation(generation: Path, claim: int | None) -> None:
    try:
        # Invalidate first: a racing lease sees no manifest and lets go.
        try:
            os.unlink(generation / MANIFEST_NAME)
        except FileNotFoundError:
            pass
        _remove_tree(generation)
    finally:
        _close(claim)


def _generations(registry: Path) -> list[Path]:
    try:
        return sorted(path for path in registry.iterdir()
                      if path.name.startswith('g-') and path.is_dir() and not path.is_symlink())
    except OSError:
        return []


def collect_garbage(registry: Path, mapped: frozenset[str] = frozenset()) -> int:
    """Delete generations other than ``live`` that nothing uses.

    Callers hold the root build lock.
    """
    live = _live_generation(registry)
    removed = 0
    for generation in _generations(registry):
        if generation == live:
            continue
        claimed, claim = _claim(generation, mapped)
        if not claimed:
            continue
        try:
            _remove_generation(generation, claim)
            removed += 1
        except OSError:
            pass
    return removed


def _retire_registry(registry: Path, mapped: frozenset[str]) -> bool:
    """Remove a whole registry unless any of its generations is in use."""
    claims = []
    try:
        for generation in _generations(registry):
            claimed, claim = _claim(generation, mapped)
            if not claimed:
                return False
            claims.append((generation, claim))
        try:
            os.unlink(registry / LIVE_NAME)
        except FileNotFoundError:
            pass
        while claims:
            generation, claim = claims.pop()
            _remove_generation(generation, claim)
        _remove_tree(registry)
        return True
    except OSError:
        return False
    finally:
        for _generation, claim in claims:
            _close(claim)


def _registries(root: Path) -> list[Path]:
    try:
        return sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    except OSError:
        return []


def collect_idle_registries(root: Path, *, everything: bool = False,
                            keep: Iterable[Path] = ()) -> dict[str, int]:
    """Reclaim unused generations; retire idle, orphaned or crowding registries.

    A registry is retired when its source interpreter was removed, nothing
    used it for a month, its volume fell below the free-space reserve, or
    ``everything`` is set. Generations in use and registries in ``keep``
    always survive.
    """
    from serverctl_pkg import local_placement
    kept = {Path(path) for path in keep}
    mapped = mapped_generations(root)
    crowded = not local_placement.has_free_space(root)
    removed = retired = 0
    now = time.time()
    for registry in _registries(root):
        if registry not in kept:
            live = _live_generation(registry)
            manifest = _manifest(live) if live is not None else None
            source_prefix = str((manifest or {}).get('sourcePrefix') or '')
            source_python = str((manifest or {}).get('sourcePython') or '')
            try:
                last_used = os.stat((live or registry) / LEASE_NAME).st_mtime
            except OSError:
                try:
                    last_used = registry.stat().st_mtime
                except OSError:
                    last_used = 0.0
            # An unmounted volume hides the source's parent too; only a
            # visible parent without the interpreter proves removal.
            orphaned = bool(source_prefix and os.path.isdir(os.path.dirname(source_prefix))
                            and not os.path.exists(source_python))
            idle = now - last_used >= IDLE_REGISTRY_SECONDS
            if (everything or crowded or idle or orphaned) and _retire_registry(registry, mapped):
                retired += 1
                continue
        removed += collect_garbage(registry, mapped)
    return {'generationsRemoved': removed, 'registriesRetired': retired}


# ── builder ──


@dataclass(slots=True)
class _Inventory:
    directories: list[tuple[str, int]] = field(default_factory=list)
    files: list[tuple[str, int, int]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    total_bytes: int = 0


def _scan(item: tuple[str, frozenset[str]]) -> tuple[list, list, list]:
    directory, excluded = item
    directories, files, links = [], [], []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name in excluded:
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                links.append(entry.path)
            elif stat.S_ISDIR(metadata.st_mode):
                directories.append((entry.path, stat.S_IMODE(metadata.st_mode)))
            elif stat.S_ISREG(metadata.st_mode):
                files.append((entry.path, metadata.st_size, metadata.st_mtime_ns))
    return directories, files, links


def _inventory(prefix: str) -> _Inventory:
    from concurrent.futures import ThreadPoolExecutor
    inventory = _Inventory(directories=[(prefix, stat.S_IMODE(os.stat(prefix).st_mode))])
    pending = [(prefix, _excluded_top_level(prefix))]
    with ThreadPoolExecutor(max_workers=COPY_THREADS) as pool:
        while pending:
            results = list(pool.map(_scan, pending))
            pending = []
            for directories, files, links in results:
                inventory.directories += directories
                pending += [(path, frozenset()) for path, _mode in directories]
                inventory.files += files
                inventory.links += links
    inventory.total_bytes = sum(size for _path, size, _mtime in inventory.files)
    return inventory


def _reusable(environment: Environment, inventory: _Inventory,
              previous: Path | None) -> set[str]:
    """Source files identical (size, mtime) to the previous generation's copy."""
    if previous is None:
        return set()
    reusable = set()
    for path, size, mtime_ns in inventory.files:
        try:
            metadata = os.lstat(previous / os.path.relpath(path, environment.prefix))
        except OSError:
            continue
        if (stat.S_ISREG(metadata.st_mode) and metadata.st_size == size
                and metadata.st_mtime_ns == mtime_ns):
            reusable.add(path)
    return reusable


def _replace_text(path: Path, text: str) -> None:
    import shutil
    # Replace the inode: files may be hard links shared with a previous
    # generation, which an in-place write would silently change too.
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temporary.write_text(text, encoding='utf-8')
    shutil.copymode(path, temporary)
    os.replace(temporary, path)


def _rewrite_pth_prefix(path: Path, old: str, new: str) -> None:
    try:
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return
    rewritten = [new + line[len(old):]
                 if line.rstrip('\n') == old or line.startswith(old + os.sep) else line
                 for line in lines]
    if rewritten != lines:
        _replace_text(path, ''.join(rewritten))


def _rebase_venv(generation: Path, environment: Environment, base_generation: Path) -> None:
    config = generation / 'pyvenv.cfg'
    rewritten = []
    for line in config.read_text(encoding='utf-8').splitlines():
        key, separator, value = line.partition('=')
        value = os.path.realpath(value.strip()) if separator else ''
        if (key.strip().lower() in {'home', 'executable', 'base-executable'}
                and _within(value, environment.base_prefix)):
            line = (f'{key.strip()} = '
                    f'{base_generation / os.path.relpath(value, environment.base_prefix)}')
        rewritten.append(line)
    _replace_text(config, '\n'.join(rewritten) + '\n')


def _copy_tree(environment: Environment, generation: Path, inventory: _Inventory,
               base_generation: Path | None, previous: Path | None,
               reusable: set[str]) -> None:
    import shutil
    from concurrent.futures import ThreadPoolExecutor
    prefix = environment.prefix
    base_prefix = environment.base_prefix if base_generation is not None else ''

    def destination(path: str) -> Path:
        return generation / os.path.relpath(path, prefix)

    def inside(path: str) -> str:
        if _within(path, prefix):
            return 'prefix'
        return 'base' if base_prefix and _within(path, base_prefix) else ''

    for directory, _mode in inventory.directories:
        destination(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    for link in inventory.links:
        target = os.readlink(link)
        resolved = os.path.normpath(os.path.join(os.path.dirname(link), target))
        place = inside(resolved)
        if not place:
            real = os.path.realpath(resolved)
            if inside(real):
                resolved, place = real, inside(real)
        if place == 'prefix':
            if os.path.isabs(target) or resolved != os.path.normpath(
                    os.path.join(os.path.dirname(link), target)):
                target = os.path.relpath(destination(resolved), destination(link).parent)
        elif place == 'base':
            target = str(base_generation / os.path.relpath(resolved, base_prefix))
        else:
            # A relative link escaping the prefix would resolve elsewhere
            # from the replica; keep pointing at the original target.
            target = resolved
        os.symlink(target, destination(link))

    def copy(item: tuple[str, int, int]) -> None:
        source = item[0]
        target = destination(source)
        if source in reusable and previous is not None:
            try:
                os.link(previous / os.path.relpath(source, prefix), target)
                return
            except OSError:
                pass
        shutil.copy2(source, target, follow_symlinks=False)

    ordered = sorted(inventory.files, key=lambda item: -item[1])
    with ThreadPoolExecutor(max_workers=COPY_THREADS) as pool:
        for _ in pool.map(copy, ordered, chunksize=32):
            pass
    if base_generation is not None:
        _rebase_venv(generation, environment, base_generation)
    for directory in dict.fromkeys((environment.purelib, environment.platlib)):
        package_root = generation / directory
        if package_root.is_dir():
            for pth in package_root.glob('*.pth'):
                _rewrite_pth_prefix(pth, prefix, str(generation))


def _freeze(environment: Environment, generation: Path, inventory: _Inventory) -> None:
    """Remove write permission below the generation root.

    Installers, uninstallers and editors then fail loudly instead of
    silently diverging a replica from its source. The root stays writable
    for its lease, manifest and rejection marker. Symlinks are skipped
    because chmod follows them.
    """
    prefix = environment.prefix
    for path, _size, _mtime in inventory.files:
        target = generation / os.path.relpath(path, prefix)
        os.chmod(target, stat.S_IMODE(os.lstat(target).st_mode) & ~0o222)
    for directory, mode in reversed(inventory.directories):
        target = generation / os.path.relpath(directory, prefix)
        if target != generation:
            os.chmod(target, (mode | stat.S_IRUSR | stat.S_IXUSR) & ~0o222)


def _verify(environment: Environment, generation: Path, base_generation: Path | None) -> str:
    modules = json.dumps(list(_VERIFY_MODULES))
    source = _run_json([os.path.join(environment.prefix, environment.python), '-I', '-c',
                        _VERIFY_SOURCE, modules])
    replica = _run_json([str(generation / environment.python), '-I', '-c',
                         _VERIFY_SOURCE, modules])
    if source is None or replica is None:
        return 'interpreter-probe-failed'
    if replica.get('prefix') != os.path.realpath(generation):
        return 'replica-prefix-mismatch'
    if environment.kind == 'prefix':
        expected_base = os.path.realpath(generation)
    elif base_generation is not None:
        expected_base = os.path.realpath(base_generation)
    else:
        expected_base = environment.base_prefix
    if replica.get('base_prefix') != expected_base:
        return 'replica-base-mismatch'
    missing = set(source.get('loaded') or ()) - set(replica.get('loaded') or ())
    if missing:
        return 'replica-modules-missing:' + ','.join(sorted(missing))
    copied_roots = [environment.prefix] + ([environment.base_prefix] if base_generation else [])
    if any(_within(path, root) for path in replica.get('mapped') or () for root in copied_roots):
        return 'replica-maps-source-files'
    try:
        expected = set(os.listdir(os.path.join(environment.prefix, environment.purelib)))
        actual = set(os.listdir(generation / environment.purelib))
    except OSError:
        return 'package-listing-unavailable'
    return '' if expected - {'__pycache__'} == actual - {'__pycache__'} else 'package-listing-mismatch'


def build_environment(python: str, root: Path) -> dict:
    """Build, verify and publish one replica; returns the status record.

    The caller holds the root build lock for the whole build.
    """
    from serverctl_pkg import local_placement
    environment = describe_environment(python)
    if environment is None:
        return {'state': 'unsupported-environment', 'sourcePython': python}
    registry = registry_path(root, environment.prefix)
    local_placement.ensure_private_directory(registry)
    previous_status = _read_json(registry / STATUS_NAME) or {}
    status: dict[str, object] = {'sourcePrefix': environment.prefix, 'sourcePython': python,
                                 'startedAt': time.time()}
    digest = fingerprint(environment)
    if digest is None:
        return {**status, 'state': 'failed', 'error': 'fingerprint-unavailable'}
    status['fingerprint'] = digest
    status.update(_carried(previous_status, digest))
    base = _base_of(environment)
    if _fresh_generation(root, environment, digest, base) is not None:
        collect_garbage(registry, mapped_generations(root))
        return {**status, 'state': 'current'}
    if not _build_admissible(root, environment, digest):
        return {**status, 'state': 'deferred'}
    same_source = previous_status.get('fingerprint') == digest
    failures = _as_int(previous_status.get('failures')) if same_source else 0
    if same_source and previous_status.get('state') == BUILDING_STATE:
        failures += 1
    _write_json(registry / STATUS_NAME, {**status, 'state': BUILDING_STATE, 'pid': os.getpid(),
                                         'failures': failures})
    collect_garbage(registry, mapped_generations(root))
    base_generation = _fresh_generation(root, *base) if base else None
    previous = _live_generation(registry)
    if previous is not None and (_manifest(previous) is None
                                 or (previous / REJECTED_NAME).exists()):
        # Hard links share bytes: never inherit them from a suspect copy.
        previous = None
    generation = registry / (f'g-{digest[:12]}-{time.strftime("%Y%m%d%H%M%S")}-'
                             f'{os.urandom(4).hex()}')
    created = False
    try:
        inventory = _inventory(environment.prefix)
        reusable = _reusable(environment, inventory, previous)
        required = sum(size for path, size, _mtime in inventory.files if path not in reusable)
        # Reusable files are hard links; every directory, link and copy needs an inode.
        inodes = (len(inventory.directories) + len(inventory.links)
                  + sum(1 for path, _size, _mtime in inventory.files if path not in reusable))
        status.update(files=len(inventory.files), bytes=inventory.total_bytes,
                      copiedBytes=required, copiedInodes=inodes)
        if not local_placement.has_free_space(registry, required, required_inodes=inodes):
            # Other environments' replicas are caches competing for the
            # same volume; this one is in demand.
            keep = [registry]
            if base_generation is not None:
                keep.append(base_generation.parent)
            collect_idle_registries(root, everything=True, keep=keep)
            if not local_placement.has_free_space(registry, required, required_inodes=inodes):
                return {**status, 'state': 'insufficient-space', 'failures': failures + 1}
        generation.mkdir(mode=0o700)
        created = True
        _copy_tree(environment, generation, inventory, base_generation, previous, reusable)
        _freeze(environment, generation, inventory)
        if fingerprint(environment) != digest:
            _remove_tree(generation)
            return {**status, 'state': 'source-changed'}
        error = _verify(environment, generation, base_generation)
        if error:
            _remove_tree(generation)
            deterministic = error.startswith(_DETERMINISTIC_ERRORS)
            return {**status, 'state': UNSUPPORTED_STATE if deterministic else 'failed',
                    'failures': failures + 1, 'error': error[:500]}
        (generation / LEASE_NAME).touch(mode=0o600)
        _write_json(generation / MANIFEST_NAME, {
            'schema': SCHEMA, 'fingerprint': digest, 'kind': environment.kind,
            'sourcePrefix': environment.prefix,
            'sourcePython': os.path.join(environment.prefix, environment.python),
            'python': environment.python, 'purelib': environment.purelib,
            'platlib': environment.platlib,
            'baseGeneration': str(base_generation) if base_generation else '',
            'files': len(inventory.files), 'bytes': inventory.total_bytes,
            'builtAt': time.time(),
        })
        alias = registry / f'.{LIVE_NAME}.{os.getpid()}'
        try:
            os.unlink(alias)
        except FileNotFoundError:
            pass
        os.symlink(generation.name, alias)
        os.replace(alias, registry / LIVE_NAME)
    except Exception as error:
        if created:
            _remove_tree(generation)
        return {**status, 'state': 'failed', 'failures': failures + 1,
                'error': (str(error) or type(error).__name__)[:500]}
    collect_garbage(registry, mapped_generations(root))
    return {**status, 'state': 'published', 'generation': generation.name}


def _build_main(arguments) -> int:
    from serverctl_pkg import local_placement
    try:
        os.nice(10)
    except OSError:
        pass
    root = Path(arguments.root)
    local_placement.ensure_private_directory(root)
    lock = _open_lock(root / LOCK_NAME, exclusive=True, create=True)
    if lock is None:
        return 0
    try:
        requested = []
        for python in arguments.python:
            started = time.monotonic()
            result = build_environment(python, root)
            result.update(finishedAt=time.time(), seconds=round(time.monotonic() - started, 1))
            prefix = result.get('sourcePrefix')
            if isinstance(prefix, str) and prefix:
                requested.append(registry_path(root, prefix))
                # A build that did not run must not rewrite the outcome its
                # backoff is measured from.
                if result.get('state') != 'deferred':
                    try:
                        _write_json(registry_path(root, prefix) / STATUS_NAME, result)
                    except OSError:
                        pass
            print(json.dumps(result, sort_keys=True), flush=True)
        print(json.dumps(collect_idle_registries(root, keep=requested), sort_keys=True),
              flush=True)
    finally:
        _close(lock)
    return 0


def _status_main(arguments) -> int:
    python = arguments.python or authoritative_interpreter()
    placement = _placement(python, os.environ)
    report: dict[str, object] = {'python': python, 'placement': placement.as_status()}
    environment = describe_environment(python) if placement.selected else None
    if environment is not None:
        root = Path(placement.root)
        digest = fingerprint(environment)
        registry = registry_path(root, environment.prefix)
        generation = (_fresh_generation(root, environment, digest, _base_of(environment))
                      if digest else None)
        report.update(sourcePrefix=environment.prefix, kind=environment.kind,
                      registry=str(registry), fresh=generation is not None,
                      generation=generation.name if generation else None,
                      build=_read_json(registry / STATUS_NAME) or {})
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _prune_main(arguments) -> int:
    from serverctl_pkg import local_placement
    roots = ([Path(arguments.root)] if arguments.root
             else local_placement.candidate_roots(local_placement.PURPOSE_RUNTIME))
    for root in roots:
        if not root.is_dir():
            continue
        lock = _open_lock(root / LOCK_NAME, exclusive=True, create=True)
        if lock is None:
            print(json.dumps({'root': str(root), 'state': 'build-running'}))
            continue
        try:
            result = collect_idle_registries(root, everything=arguments.all)
        finally:
            _close(lock)
        print(json.dumps({'root': str(root), **result}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog='python -m serverctl_pkg.runtime_replica')
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build', help='build missing or stale replicas, then reclaim unused ones')
    build.add_argument('--root', required=True)
    build.add_argument('--python', action='append', default=[])
    status = commands.add_parser('status', help='report placement and freshness for one interpreter')
    status.add_argument('--python', default='')
    prune = commands.add_parser('prune', help='reclaim unused generations and idle registries')
    prune.add_argument('--root', default='')
    prune.add_argument('--all', action='store_true',
                       help='also retire registries that no running process uses')
    arguments = parser.parse_args(argv)
    handler = {'build': _build_main, 'status': _status_main, 'prune': _prune_main}
    return handler[arguments.command](arguments)


if __name__ == '__main__':
    raise SystemExit(main())
