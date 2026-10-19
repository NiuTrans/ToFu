"""Prepare the selected native engine before stopping a serving application.

Standard-library lifecycle boundary shared by CLI, GUI update and startup.
Identity comes from the compiled artifact, not deployment-time Git metadata.
Builds/downloads use private staging; only verified candidates reach the
existing deployment owner. This module never opens an authority or restarts it.
"""
from __future__ import annotations

import argparse
import codecs
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

PREPARE_TIMEOUT_SECONDS = 600
LOCK_WAIT_TIMEOUT_SECONDS = 600
PREPARATION_TOTAL_TIMEOUT_SECONDS = LOCK_WAIT_TIMEOUT_SECONDS + PREPARE_TIMEOUT_SECONDS

SOURCE_PREPARATION_MAX_ATTEMPTS = 3
SOURCE_PREPARATION_SETTLE_SECONDS = 2.0
BUILD_FINALIZATION_RESERVE_SECONDS = 30
MAX_LOG_BYTES = 8 * 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 5.0
PROGRESS_PREFIX = "TOFU_ENGINE_PROGRESS "
PROGRESS_MAX_LINE_CHARACTERS = 16 * 1024
_progress_observer: ContextVar[Callable[[str], None] | None] = ContextVar("engine_progress_observer", default=None)
_progress_phase: ContextVar[str | None] = ContextVar("engine_progress_phase", default=None)
_progress_recovering: ContextVar[bool] = ContextVar("engine_progress_recovering", default=False)
_deadline: ContextVar[float] = ContextVar('native_engine_preparation_deadline', default=float('inf'))

# Sentinel source path marking the synthetic workspace-root manifest entry in
# the snapshot copy list; its bytes are generated, never read from disk.
_SYNTHETIC_WORKSPACE_SENTINEL = '.workspace-root.sentinel'


class EnginePreparationError(RuntimeError):
    pass


class EnginePreparationCancelledError(RuntimeError):
    """Terminal cancellation, deliberately outside repairable artifact failures."""


class EngineCommandFailedError(EnginePreparationError):
    """A completed child refused its candidate; timeout/cancellation stay distinct."""


class EngineSourceChangedError(EnginePreparationError):
    """Observed source drift; only prepublication preparation may retry it."""


# Stdlib TOML extraction used both to resolve path dependency graphs and to
# classify crate manifests. Kept as a child program so one bounded parse
# failure cannot corrupt the serving process.
_TOML_QUERY_PROGRAM = '''
import json, sys, tomllib
manifest = tomllib.load(open(sys.argv[1], "rb"))
found = []
def visit(section):
    if isinstance(section, dict):
        for name, entry in sorted(section.items()):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                found.append(entry["path"])
for key in ("dependencies", "build-dependencies", "dev-dependencies"):
    visit(manifest.get(key))
for _, target in sorted(manifest.get("target", {}).items()):
    if isinstance(target, dict):
        for key in ("dependencies", "build-dependencies", "dev-dependencies"):
            visit(target.get(key))
package = manifest.get("package", {})
print(json.dumps({
    "package": package.get("name", ""),
    "has_workspace": "workspace" in manifest,
    "package_workspace": isinstance(package.get("workspace"), str),
    "package_publish": package.get("publish", True),
    "include": package.get("include"),
    "exclude": package.get("exclude"),
    "external_inputs": package.get("metadata", {}).get("tofu", {}).get("external-inputs", []),
    "dependencies": found,
}))
'''


def _read_manifest(manifest: Path) -> dict:
    try:
        # This standard-library program must not import checkout modules or
        # execute site hooks. Keep the existing bounded child and joined wait.
        return json.loads(subprocess.run(
            [sys.executable, '-I', '-S', '-c', _TOML_QUERY_PROGRAM, str(manifest)],
            capture_output=True, check=True, text=True, timeout=10).stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise EnginePreparationError(f'Cannot read the storage engine source manifest {manifest}.') from exc


def _manifest_reader() -> Callable[[Path], dict]:
    """One operation's parsed manifests; every new operation reads disk again.

    Dependency discovery, input selection and external declarations use the
    same successful parse. The existing child timeout/failure isolation stays
    at _read_manifest; failed reads are never published into this local memo.
    """
    manifests = {}

    def read(manifest: Path) -> dict:
        key = Path(os.path.realpath(manifest))
        if key not in manifests:
            manifests[key] = _read_manifest(manifest)
        return manifests[key]

    return read


# Names Cargo always includes in a packaged crate regardless of include rules.
_CARGO_ALWAYS_INCLUDE = ('Cargo.toml', 'Cargo.toml.orig', 'Cargo.lock')


def _crate_source_paths(crate: Path, manifest: dict) -> list[Path]:
    """Retained build inputs of one crate, mirroring Cargo's package-include
    semantics: explicit include rules win for workspace crates, otherwise
    Cargo.toml[.orig]/Cargo.lock/build.rs plus every rust source."""
    publish = manifest.get('package_publish', True)
    include = manifest.get('include') if publish is not False else None
    if include:
        selected = [crate / pattern.rstrip('/')
                    for pattern in (*include, *_CARGO_ALWAYS_INCLUDE)]
        paths = [path for path in dict.fromkeys(selected) if path.is_file()]
    else:
        paths = [crate / name for name in ('Cargo.toml', 'Cargo.toml.orig', 'Cargo.lock', 'build.rs')
                 if (crate / name).is_file()]
        for directory, folders, names in os.walk(crate / 'src'):
            folders.sort()
            paths.extend(Path(directory) / name for name in names if name.endswith('.rs'))
    # .cargo/config.toml configures the build but is never a packaged input.
    configuration = crate / '.cargo'
    if configuration.is_dir():
        for directory, folders, names in os.walk(configuration):
            folders.sort()
            paths.extend(Path(directory) / name for name in names)
    return paths


def _normalized(relative: PurePosixPath) -> PurePosixPath:
    """Resolve '.'/'..' segments of a snapshot-relative crate location without
    touching the filesystem, mirroring packages/tofu-db/build.rs::normalized."""
    parts: list[str] = []
    for part in relative.parts:
        if part in ('', '.'):
            continue
        if part == '..':
            # The root crate's own '..' escapes its update-source dir into the
            # snapshot parent (target/); with nothing accumulated it maps to a
            # direct child of the snapshot parent. A nested dependency's '..'
            # pops its accumulated in-snapshot component.
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath('.')


def _path_dependency_crates(crate: Path, discovered: dict[Path, PurePosixPath] | None = None,
                            relative: PurePosixPath | None = None, *,
                            read_manifest: Callable[[Path], dict] | None = None) -> dict[Path, PurePosixPath]:
    """Map every transitive path-dependency crate to its snapshot-relative dir.

    A path dependency contributes real build inputs but lives outside the
    root crate; omitting it leaves the source snapshot uncompilable and lets
    sibling edits escape the compiled source identity. The relative location
    preserves the manifest's path segments so the snapshot reproduces the
    exact Cargo-relative layout the compiler resolves. Locations are
    canonicalized (symlinks resolved) exactly like build.rs::closure so the
    Python and embedded identities agree byte for byte.
    """
    if relative is None:
        relative = PurePosixPath('.')
    if discovered is None:
        discovered = {}
    if read_manifest is None:
        read_manifest = _read_manifest
    anchor = Path(os.path.realpath(crate))
    if anchor in discovered:
        return discovered
    manifest_path = anchor / 'Cargo.toml'
    manifest = read_manifest(manifest_path)
    if not manifest.get('package'):
        raise EnginePreparationError(f'The storage engine source manifest {manifest_path} names no package.')
    discovered[anchor] = relative
    for segments in manifest['dependencies']:
        dependency = Path(os.path.realpath(manifest_path.parent / segments))
        if not dependency.is_relative_to(anchor.parent):
            raise EnginePreparationError(
                'The storage engine path dependency escapes its packages directory.')
        if not (dependency / 'Cargo.toml').is_file():
            raise EnginePreparationError(
                f'The storage engine path dependency has no manifest at {dependency}.')
        child = _normalized(PurePosixPath(*relative.parts, *PurePosixPath(segments).parts))
        _path_dependency_crates(dependency, discovered, child, read_manifest=read_manifest)
    return discovered


def source_layout(root: Path) -> list[tuple[Path, PurePosixPath, list[Path], bool]]:
    """(location, snapshot-relative crate dir, retained source paths,
    uses-workspace-inheritance) for the engine and each path dependency."""
    return _source_layout(root, _manifest_reader())


def _source_layout(root: Path, read_manifest: Callable[[Path], dict]):
    crate = root / 'packages/tofu-db'
    layout = []
    for location, relative in _path_dependency_crates(crate, read_manifest=read_manifest).items():
        manifest = read_manifest(location / 'Cargo.toml')
        inherits = not manifest['has_workspace'] and not manifest['package_workspace']
        layout.append((location, relative, sorted(_crate_source_paths(location, manifest)), inherits))
    return layout


def external_source_inputs(root: Path) -> list[tuple[str, Path]]:
    """Declared project-root inputs shared with the native build script."""
    return _external_source_inputs(root, _read_manifest)


def _external_source_inputs(root: Path, read_manifest: Callable[[Path], dict]):
    values = read_manifest(root / 'packages/tofu-db/Cargo.toml')['external_inputs']
    if not isinstance(values, list):
        raise EnginePreparationError('External native inputs must be a path list.')
    entries = []
    for value in values:
        if not isinstance(value, str):
            raise EnginePreparationError('External native input must be a relative path.')
        relative = PurePosixPath(value)
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                or relative.as_posix() != value or not (value == 'VERSION' or value.startswith('contracts/'))):
            raise EnginePreparationError('External native input escapes its declared source namespace.')
        path = root / value
        if not path.is_file() or path.resolve() != root.resolve() / value:
            raise EnginePreparationError('External native input is missing or uses a symlink.')
        entries.append((value, path))
    if len({key for key, _ in entries}) != len(entries):
        raise EnginePreparationError('External native input paths collide.')
    return entries


def _update_source_digest(digest, key: str, path: Path) -> None:
    """Keep the v2 length-prefixed byte protocol without retaining whole files."""
    try:
        handle = path.open('rb')
    except FileNotFoundError as exc:
        raise EngineSourceChangedError(f'Storage source disappeared while fingerprinting {key}.') from exc
    with handle:
        before = os.fstat(handle.fileno())
        remaining = before.st_size
        digest.update(key.encode() + b'\0')
        digest.update(remaining.to_bytes(8, 'little'))
        while remaining:
            content = handle.read(min(remaining, 1024 * 1024))
            if not content:
                raise EngineSourceChangedError(f'Storage source changed while fingerprinting {key}.')
            digest.update(content)
            remaining -= len(content)
        extra = handle.read(1)
        after = os.fstat(handle.fileno())
        if (extra or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise EngineSourceChangedError(f'Storage source changed while fingerprinting {key}.')


def source_fingerprint(root: Path) -> str | None:
    crate = root / 'packages/tofu-db'
    if not (crate / 'Cargo.toml').is_file():
        return None  # Frozen distributions validate their packaged artifact pin.
    digest = hashlib.sha256(b'tofu-db-source-v2\0')
    entries = []
    read_manifest = _manifest_reader()
    for location, relative, paths, _inherits in _source_layout(root, read_manifest):
        # Logical keys survive relocation into update-source. Never include
        # the synthetic workspace manifest or the staging directory basename.
        prefix = PurePosixPath('tofu-db') if str(relative) == '.' else relative
        for path in paths:
            entries.append(((prefix / path.relative_to(location).as_posix()).as_posix(), path))
    entries.extend(('@external/' + key, path) for key, path in _external_source_inputs(root, read_manifest))
    if len({key for key, _ in entries}) != len(entries):
        raise EnginePreparationError('The storage engine source identity has colliding paths.')
    for key, path in sorted(entries):
        _update_source_digest(digest, key, path)
    return digest.hexdigest()


@contextmanager
def observe_preparation(callback: Callable[[dict], None]) -> Iterator[None]:
    """Scope structured child progress to one restart request; never relay logs."""
    pending = ''
    def receive(chunk: str) -> None:
        nonlocal pending
        pending = (pending + chunk)[-PROGRESS_MAX_LINE_CHARACTERS:]
        lines = pending.split('\n')
        pending = lines.pop()
        for line in lines:
            if line.startswith(PROGRESS_PREFIX):
                try:
                    callback(json.loads(line[len(PROGRESS_PREFIX):]))
                except (ValueError, TypeError, OSError):
                    pass  # Progress cannot prevent cancellation or artifact repair.
    token = _progress_observer.set(receive)
    phase_token = _progress_phase.set(None)
    try:
        yield
    finally:
        _progress_observer.reset(token)
        _progress_phase.reset(phase_token)


def _report_stage(phase: str, message: str) -> None:
    _progress_phase.set(phase)
    _report_progress(message)


def _report_progress(message: str, *, end: str = '\n') -> None:
    try:
        print(message, file=sys.stderr, end=end, flush=True)
        observer = _progress_observer.get()
        if observer is not None:
            observer(message + end)
        phase = _progress_phase.get()
        if phase and end == '\n':
            remaining = max(0, _deadline.get() - time.monotonic())
            frame = {'phase': phase}
            if _progress_recovering.get():
                frame['recovering'] = True
            if remaining != float('inf'):
                frame['remainingSeconds'] = round(remaining)
            encoded = PROGRESS_PREFIX + json.dumps(frame)
            print(encoded, file=sys.stderr, flush=True)
            if observer is not None:
                observer(encoded + '\n')
    except (OSError, ValueError):
        pass  # A closed progress sink cannot break preparation or its cleanup.


def _stop_command(child: subprocess.Popen, grace: float) -> None:
    """Reap the leader and kill remaining group members even if it exits first."""
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                           capture_output=True, timeout=10, check=False)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # The entire group has already exited.
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass  # Escalation below also covers descendants ignoring SIGTERM.
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The entire group exited during graceful termination.
        child.wait(timeout=10)


def _run(command: list[str], *, cwd: Path, environment: dict, timeout: float,
         progress: str | None = None, termination_grace: float = 5,
         failure_subject: str = 'Storage engine preparation') -> str:
    """Bound output/lifetime; optionally relay output live with idle heartbeats.

    Separate reader/writer offsets prevent progress reads from corrupting the
    child's log. No reader thread, pipe backpressure or persistent log is added.
    Machine-readable build identity remains capture-only.
    """
    started = time.monotonic()
    deadline = min(_deadline.get(), started + timeout)
    if started >= deadline:
        raise EnginePreparationError(f'{failure_subject} timed out (time budget exhausted).')
    with tempfile.TemporaryDirectory(prefix='tofu-engine-command-') as temporary, \
            (Path(temporary) / 'output.log').open('wb') as log, \
            (Path(temporary) / 'output.log').open('rb') as reader:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
        next_progress = started + PROGRESS_INTERVAL_SECONDS
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        failure = ''
        try:
            while True:
                now = time.monotonic()
                size = os.fstat(log.fileno()).st_size
                if size > MAX_LOG_BYTES:
                    failure = f'{failure_subject} exceeded its log budget.'
                    break
                if progress:
                    while reader.tell() < size:
                        data = reader.read(min(65536, size - reader.tell()))
                        _report_progress(decoder.decode(data), end='')
                        next_progress = now + PROGRESS_INTERVAL_SECONDS
                if child.poll() is not None:
                    # One more read below includes output written during poll.
                    break
                if now >= deadline:
                    failure = f'{failure_subject} timed out after {now - started:.1f}s (time budget exhausted).'
                    break
                if progress and now >= next_progress:
                    _report_progress(f'{progress}: still running ({now - started:.0f}s elapsed, '
                                     f'{max(0, deadline - now):.0f}s remaining).')
                    next_progress = now + PROGRESS_INTERVAL_SECONDS
                try:
                    child.wait(timeout=min(0.1, max(0.001, deadline - now)))
                except subprocess.TimeoutExpired:
                    pass  # Poll output and the shared deadline on the next tick.
        except BaseException:
            _stop_command(child, termination_grace)
            raise
        if failure or child.returncode:
            _stop_command(child, termination_grace)
        size = os.fstat(log.fileno()).st_size
        if size > MAX_LOG_BYTES:
            failure = failure or f'{failure_subject} exceeded its log budget.'
        if progress and not failure:
            _report_progress(decoder.decode(reader.read(MAX_LOG_BYTES), final=True), end='')
        output = _failure_output(reader, size)
        if failure:
            raise EnginePreparationError(f'{failure}\n{output}' if output else failure)
        if child.returncode < 0:
            raise EnginePreparationCancelledError(
                f'{failure_subject} was terminated by signal {-child.returncode}; '
                f'preparation was cancelled. {output}')
        if child.returncode:
            raise EngineCommandFailedError(f'{failure_subject} failed (exit {child.returncode}): {output}')
        return output


def _failure_output(reader, size: int, head_bytes: int = 4096, tail_bytes: int = 12288) -> str:
    """Head + tail of the child log for an error report.

    A cargo refusal usually fails EARLY (denied lint, missing feature) while
    the tail alone used to leave the operator guessing; the 8KB tail-only
    capture routinely cut the actual ``error:`` line out of the report.
    """
    if size <= head_bytes + tail_bytes:
        reader.seek(0)
        return reader.read().decode('utf-8', errors='replace').strip()
    reader.seek(0)
    head = reader.read(head_bytes).decode('utf-8', errors='replace')
    reader.seek(size - tail_bytes)
    tail = reader.read(tail_bytes).decode('utf-8', errors='replace')
    return (head.rstrip() +
            f'\n… [log truncated: {size - head_bytes - tail_bytes} bytes omitted] …\n' +
            tail.lstrip()).strip()


def build_identity(binary: Path) -> dict:
    output = _run([str(binary), '--build-info'], cwd=binary.parent,
                  environment=dict(os.environ), timeout=10)
    try:
        info = json.loads(output)
        if (info['format'] != 'tofu-db-build.v1'
                or re.fullmatch('[0-9a-f]{64}', info['source_sha256']) is None):
            raise ValueError('invalid engine identity')
        return info
    except (ValueError, KeyError, TypeError) as exc:
        raise EnginePreparationError('Storage engine has no verifiable source identity; an engine update is required.') from exc


def verify_current(binary: Path, root: Path) -> None:
    expected = source_fingerprint(root)
    if expected is not None and build_identity(binary)['source_sha256'] != expected:
        raise EnginePreparationError('Storage engine does not match this application. Use Update or Restart to prepare the matching engine; the existing server has not been stopped.')


def verify_library(binary: Path) -> None:
    """Require the embedded library to belong to the same deployed pair."""
    try:
        record = json.loads((binary.parent / 'build.json').read_text())
        expected = record['library_sha256']
        digest = hashlib.sha256()
        with (binary.parent / 'libtofu_db.so').open('rb') as handle:
            for data in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(data)
        info = build_identity(binary)
        selected_allocator = 'mimalloc' if sys.platform == 'linux' else 'system'
        if (digest.hexdigest() != expected
                or record['source_sha256'] != info['source_sha256']
                or info.get('embedded_allocator') != selected_allocator):
            raise ValueError('engine pair differs')
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise EnginePreparationError('The embedded storage engine pair is incomplete or mismatched. Use Update to prepare it.') from exc


def _cached_library_matches_source(binary: Path, expected: str) -> bool:
    """Attest a cached library in a disposable process, without opening data.

    Cargo's --bin builds can leave an older sibling library behind. File
    presence alone is insufficient to reuse an embedded engine pair.
    """
    try:
        _verify_library_source(binary, expected)
        return True
    except (OSError, EnginePreparationError):
        return False


def _verify_library_source(binary: Path, expected: str) -> None:
    """Attest the library without data access and preserve the refusal reason."""
    library = binary.parent / 'libtofu_db.so'
    if not library.is_file():
        raise EnginePreparationError('The candidate embedded library is missing; publication was cancelled.')
    program = (
        'import ctypes,sys; library=ctypes.CDLL(sys.argv[1]); '
        'identity=library.tofudb_embed_source_sha256; '
        'identity.argtypes=[ctypes.c_void_p,ctypes.c_size_t]; '
        'identity.restype=ctypes.c_ssize_t; '
        'output=ctypes.create_string_buffer(65); '
        'size=identity(output,65); '
        'allocator=library.tofudb_embed_allocator_kind; allocator.restype=ctypes.c_uint32; '
        'print(output.value.decode("ascii")+":"+str(allocator()) if size == 64 else "invalid")'
    )
    try:
        reported = _run([sys.executable, '-c', program, str(library)],
                        cwd=library.parent, environment=dict(os.environ), timeout=10)
    except (OSError, EnginePreparationError) as exc:
        raise EnginePreparationError(
            f'The candidate embedded library could not be attested; publication was cancelled. {exc}') from exc
    match = re.fullmatch(r'([0-9a-f]{64}):([0-9]+)', reported)
    if match is None:
        raise EnginePreparationError(
            'The candidate embedded library returned invalid build metadata; publication was cancelled.')
    actual_source, allocator = match.groups()
    if actual_source != expected:
        raise EnginePreparationError(
            f'The candidate embedded library source identity differs from the binary '
            f'(expected {expected}, got {actual_source}); publication was cancelled.')
    selected_allocator = int(sys.platform == 'linux')
    if allocator != str(selected_allocator):
        raise EnginePreparationError(
            f'The candidate embedded library allocator is incompatible '
            f'(expected {selected_allocator}, got {allocator}); publication was cancelled.')


def _verify_pin(binary: Path, environment: dict) -> None:
    pin = environment.get('TOFUDB_EXPECTED_SHA256', '').strip().lower()
    sidecar = binary.with_name(binary.name + '.sha256')
    if not pin and sidecar.exists():
        fields = sidecar.read_text().split()
        pin = fields[0].lower() if fields else 'invalid'
    if pin:
        digest = hashlib.sha256()
        with binary.open('rb') as handle:
            for data in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(data)
        if pin != digest.hexdigest():
            raise EnginePreparationError('The storage engine content pin does not match its artifact.')


def reconcile_project_pin(root: Path, project: dict, environment: dict) -> bool:
    """Carry an inherited, previously managed pin across a verified deployment.

    Old serving processes loaded dotenv before the updater replaced it. The
    deployment owner's previous/current receipt distinguishes that case from
    an unrelated explicit override; the new on-disk content is checked too.
    """
    inherited = environment.get('TOFUDB_EXPECTED_SHA256')
    current = project.get('TOFUDB_EXPECTED_SHA256')
    if not inherited or not current or inherited == current:
        return False
    binary = Path(environment.get('TOFUDB_BIN') or root / 'packages/tofu-db/target/release/tofu-db').expanduser().resolve()
    if not binary.is_relative_to(root.resolve() / 'packages/tofu-db/target'):
        return False
    try:
        receipt = json.loads((binary.parent / 'build.json').read_text())
        if (receipt.get('previous_binary_sha256') != inherited
                or receipt.get('binary_sha256') != current):
            return False
        _verify_pin(binary, {'TOFUDB_EXPECTED_SHA256': current})
    except (OSError, ValueError, EnginePreparationError):
        return False
    environment['TOFUDB_EXPECTED_SHA256'] = current
    return True


@contextmanager
def _preparation_budget(seconds: float) -> Iterator[None]:
    """Scope a stage deadline without extending the caller's total deadline."""
    token = _deadline.set(min(_deadline.get(), time.monotonic() + seconds))
    try:
        yield
    finally:
        _deadline.reset(token)


@contextmanager
def _prepare_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
        handle.flush()
        deadline = min(_deadline.get(), time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS)
        started = time.monotonic()
        next_progress = started
        # Waiting has its own bounded budget and progress countdown. Release
        # that scope before yielding so a late acquisition cannot starve work.
        with _preparation_budget(LOCK_WAIT_TIMEOUT_SECONDS):
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise EnginePreparationError('Another storage engine update is still running. Retry after it finishes.')
                    if time.monotonic() >= next_progress:
                        _report_stage('waiting', f'Waiting for another storage engine preparation '
                                         f'({time.monotonic() - started:.0f}s elapsed).')
                        next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
                    time.sleep(0.2)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _download(url: str, target: Path, maximum: int) -> None:
    if not url.startswith('https://'):
        raise EnginePreparationError('Engine downloads require HTTPS.')
    deadline = min(_deadline.get(), time.monotonic() + 90)
    with urllib.request.urlopen(url, timeout=20) as response, target.open('xb') as output:
        if not response.geturl().startswith('https://'):
            raise EnginePreparationError('Engine download redirected away from HTTPS.')
        total = 0
        while data := response.read(65536):
            total += len(data)
            if total > maximum or time.monotonic() > deadline:
                raise EnginePreparationError('Engine download exceeded its size or time budget.')
            output.write(data)


def _prebuilt(root: Path, staging: Path, environment: dict) -> Path | None:
    if environment.get('TOFU_TOFUDB_NO_DOWNLOAD') == '1':
        return None
    machine = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(platform.machine().lower(), platform.machine().lower())
    darwin_machine = 'arm64' if machine == 'aarch64' else machine
    triple = {'Linux': f'{machine}-unknown-linux-musl',
              'Darwin': f'{darwin_machine}-apple-darwin'}.get(platform.system())
    if triple is None or machine not in {'x86_64', 'aarch64'}:
        return None
    version = (root / 'VERSION').read_text().strip()
    if re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,79}', version) is None:
        raise EnginePreparationError('Invalid application release version.')
    name = f'tofu-db-{version}-{triple}.tar.gz'
    base = environment.get('TOFU_TOFUDB_PREBUILT_BASE_URL') or f'https://github.com/rangehow/ToFu/releases/download/v{version}'
    archive, checksum = staging / name, staging / (name + '.sha256')
    _report_stage('downloading', f'Checking for a matching prebuilt storage engine ({version}, {triple}).')
    _download(base.rstrip('/') + '/' + name, archive, 512 * 1024 * 1024)
    _download(base.rstrip('/') + '/' + checksum.name, checksum, 4096)
    from scripts.install_tofudb_prebuilt import stage_verified_archive
    return stage_verified_archive(archive, checksum, staging / 'prebuilt')


def _sync_build_snapshot(root: Path, crate: Path, snapshot: Path) -> None:
    """Refresh the one owned source copy without invalidating unchanged inputs.

    The preparation lock serializes this directory. Random source paths and
    rewritten mtimes make Cargo rebuild the engine on every retry even when
    the previously compiled inputs are identical. Path dependencies are
    mirrored as sibling crates below the snapshot so their Cargo-relative
    locations still resolve.
    """
    if snapshot.is_symlink():
        raise EnginePreparationError('The storage build snapshot must not be a symlink.')
    snapshot.mkdir(parents=True, exist_ok=True)
    layout = source_layout(root)
    external = external_source_inputs(root)
    copies: list[tuple[Path, Path]] = []
    member_dirs: list[str] = []
    needs_workspace = False
    for location, relative, paths, inherits in layout:
        target_crate = snapshot if str(relative) == '.' else snapshot.parent / relative
        if str(relative) != '.':
            member_dirs.append(relative.as_posix())
            needs_workspace = needs_workspace or inherits
        copies.extend((path, target_crate / path.relative_to(location)) for path in paths)
    # A mirrored crate that inherits package fields (edition.workspace = true)
    # from a workspace root that is not a build input cannot compile alone.
    # Materialize a minimal synthetic root declaring exactly the mirrored
    # members (by snapshot-relative path) so Cargo resolves the inheritance
    # inside the snapshot.
    workspace_manifest = ''
    if needs_workspace:
        listing = ', '.join(f'"{member}"' for member in sorted(member_dirs))
        workspace_manifest = ('[workspace]\nresolver = "2"\nmembers = [' + listing + ']\n'
                              # The root engine crate snapshot lives below the
                              # synthetic root too; it is not a workspace member.
                              f'exclude = ["{snapshot.name}"]\n'
                              '[workspace.package]\nedition = "2021"\nrust-version = "1.85"\nlicense = "Apache-2.0"\n')
        copies.append((snapshot.parent / _SYNTHETIC_WORKSPACE_SENTINEL,
                       snapshot.parent / 'Cargo.toml'))
    external_root = snapshot.parent.parent
    ledger = snapshot.parent / '.external-inputs.json'
    if ledger.is_symlink():
        raise EnginePreparationError('The external input ledger must not be a symlink.')
    previous = json.loads(ledger.read_text()) if ledger.exists() else []
    def safe_external_key(key):
        return (isinstance(key, str) and bool(key)
                and PurePosixPath(key).as_posix() == key
                and not PurePosixPath(key).is_absolute()
                and '..' not in PurePosixPath(key).parts
                and (key == 'VERSION' or key.startswith('contracts/')))
    if not isinstance(previous, list) or not all(safe_external_key(key) for key in previous):
        raise EnginePreparationError('The external input ledger contains unsafe paths.')
    for key in set(previous) | {key for key, _ in external}:
        target = external_root / key
        if target.resolve() != external_root.resolve() / key:
            raise EnginePreparationError('The external input snapshot contains an unsafe path.')
    for key, path in external:
        target = external_root / key
        if key not in previous and target.exists():
            raise EnginePreparationError('The external input target is not owned by this snapshot.')
    for key, path in external:
        target = external_root / key
        if target.resolve() != external_root.resolve() / key:
            raise EnginePreparationError('The external input snapshot contains an unsafe path.')
        target.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            target.write_bytes(content)
    for key in set(previous) - {key for key, _ in external}:
        target = external_root / key
        if target.resolve() != external_root.resolve() / key:
            raise EnginePreparationError('The external input snapshot contains an unsafe path.')
        target.unlink(missing_ok=True)
    ledger.write_text(json.dumps([key for key, _ in external]))
    retained = {target for _, target in copies}
    for path, target in copies:
        if not target.resolve().is_relative_to(snapshot.parent.resolve()) or target.is_symlink():
            raise EnginePreparationError('The storage build snapshot contains an unsafe path.')
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.name == _SYNTHETIC_WORKSPACE_SENTINEL:
            content = workspace_manifest.encode()
        else:
            content = path.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            target.write_bytes(content)
    # Removed inputs must disappear as well; otherwise the embedded source
    # identity includes obsolete Rust files and can never pass admission. Only
    # the owned snapshot trees are pruned: sibling Cargo target directories
    # (release/, update-build-*/) are caches, never snapshot inputs. A sibling
    # crate directory is owned only when it mirrors a live path dependency or
    # is itself a Cargo crate left from a removed dependency (identified by a
    # manifest), so build caches are never mistaken for stale sources.
    if needs_workspace:
        retained.add(snapshot.parent / 'Cargo.toml')
    owned_roots = [snapshot]
    for entry in snapshot.parent.iterdir():
        if entry == snapshot or not entry.is_dir() or entry.is_symlink():
            continue
        if (entry / 'Cargo.toml').is_file():
            owned_roots.append(entry)
    member_set = {str(member) for member in member_dirs}
    for owned in owned_roots:
        if not owned.is_dir():
            continue
        for directory, folders, names in os.walk(owned, topdown=False):
            for name in names:
                target = Path(directory) / name
                if target not in retained:
                    target.unlink()
            for name in folders:
                folder = Path(directory) / name
                if folder.is_symlink():
                    raise EnginePreparationError('The storage build snapshot contains an unsafe path.')
                if not any(folder.iterdir()):
                    folder.rmdir()
        if owned != snapshot and owned.name not in member_set and not any(owned.iterdir()):
            owned.rmdir()
    # A stale synthetic workspace root must disappear once no mirrored crate
    # needs one, so a later snapshot cannot inherit a phantom workspace.
    stale_root = snapshot.parent / 'Cargo.toml'
    if not needs_workspace and stale_root.is_file() and stale_root not in retained:
        stale_root.unlink()


def _build(root: Path, staging: Path, environment: dict, *, build_root: Path | None = None) -> Path:
    # A GUI/service launch need not inherit an interactive shell's PATH.
    # Resolve only from this deployment environment, including custom rustup
    # installations declared in .env; never borrow the caller's ambient PATH.
    build_environment = dict(environment)
    search_path = environment.get('PATH', os.defpath)
    cargo = shutil.which('cargo', path=search_path)
    if not cargo:
        user_home = Path(environment.get('HOME') or environment.get('USERPROFILE') or Path.home())
        cargo_home = Path(environment.get('CARGO_HOME') or user_home / '.cargo').expanduser()
        cargo = shutil.which('cargo', path=str(cargo_home / 'bin'))
    if not cargo:
        raise EnginePreparationError(
            'No matching engine download was available and Cargo was not found '
            'on the service PATH or in CARGO_HOME/bin (default: HOME/.cargo/bin). '
            'Configure CARGO_HOME and, for a relocated rustup installation, '
            'RUSTUP_HOME in the project .env, or install a matching release. '
            'The existing server remains running.')
    # Cargo invokes sibling rustc/rustup proxies by name. Finding cargo by an
    # absolute fallback path alone is not sufficient with a minimal service
    # PATH. Keep the proxy path (do not resolve its symlink to rustup), prepend
    # its directory only for this build, and preserve explicit toolchain roots.
    cargo = os.path.abspath(cargo)
    build_environment['PATH'] = os.pathsep.join((str(Path(cargo).parent), search_path))
    # Containers can inherit compiler paths from a vanished host. Repair only
    # stale absolute overrides in the build child; retain usable user choices.
    for key in ('CC', 'CXX', 'HOST_CC', 'HOST_CXX'):
        value = build_environment.get(key, '')
        if os.path.isabs(value) and not (os.path.isfile(value) and os.access(value, os.X_OK)):
            _report_progress(f'Ignoring unavailable compiler override {key}={value}')
            build_environment.pop(key, None)
    if sys.platform == 'linux' and not build_environment.get('CC'):
        compiler = next((shutil.which(name, path=build_environment['PATH'])
                         for name in ('cc', 'gcc', 'clang')
                         if shutil.which(name, path=build_environment['PATH'])), None)
        if compiler is None:
            raise EnginePreparationError(
                'Source storage build requires a C compiler (cc, gcc or clang) '
                'on the service PATH; no usable compiler was found. '
                'Install a compiler or provide a verified matching engine artifact.')
        build_environment['CC'] = compiler
    crate = root / 'packages/tofu-db'
    # Mirror the repository layout entirely inside the owned build tree.
    # External root inputs resolve two levels above the crate; putting the
    # crate directly in target would spill those inputs into retained source.
    target_root = build_root if build_root is not None else crate / 'target'
    snapshot_root = target_root / 'update-source-root'
    if snapshot_root.is_symlink() or (snapshot_root / 'packages').is_symlink():
        raise EnginePreparationError('The storage build snapshot must not use a symlink.')
    snapshot = snapshot_root / 'packages/tofu-db'
    _sync_build_snapshot(root, crate, snapshot)
    from runtime_guards import probe_system_resources
    capacity = probe_system_resources(environment)
    jobs = max(1, min(capacity.effective_cpu_count,
                      (capacity.effective_memory_available_mb or 1024) // 1024))
    build_environment.setdefault('CARGO_BUILD_JOBS', str(jobs))
    # Release optimization stays at opt-level=3. Parallel LLVM code generation
    # and Thin LTO avoid serial whole-crate optimization during a source restart.
    # Explicit operator profile overrides remain authoritative; Cargo tracks
    # these settings in the stable host cache's own fingerprints.
    build_environment.setdefault('CARGO_PROFILE_RELEASE_CODEGEN_UNITS', str(jobs))
    build_environment.setdefault('CARGO_PROFILE_RELEASE_LTO', 'thin')
    build_environment.setdefault('CARGO_PROFILE_RELEASE_INCREMENTAL', 'true')
    # Shared storage survives container replacement, compiled host programs do
    # not. Namespace Cargo cache by the current ABI and build environment.
    host_identity = json.dumps({
        'platform': platform.platform(), 'libc': platform.libc_ver(),
        'cargo': cargo, 'compiler': build_environment.get('CC'),
        'linker_environment': {key: build_environment.get(key, '') for key in
                               ('LD_LIBRARY_PATH', 'LIBRARY_PATH', 'RUSTFLAGS', 'CFLAGS', 'CXXFLAGS')},
    }, sort_keys=True).encode()
    host_key = hashlib.sha256(host_identity).hexdigest()[:16]
    target_dir = target_root / f'update-build-{host_key}'
    build_environment['CARGO_TARGET_DIR'] = str(target_dir)
    _report_stage('compiling', f'Compiling matching storage engine (build workers: {build_environment["CARGO_BUILD_JOBS"]}); '
                     'this may take several minutes. The running service has not been stopped.')
    command = [cargo, 'build', '--release', '--locked']
    if sys.platform == 'linux':
        command.extend(['--features', 'embedded-mimalloc'])
    # Spend the remaining preparation budget, retaining time for identity,
    # library verification and publication. Unused lock-wait time remains
    # available to compilation; neither acquisition nor work renews the clock.
    remaining = min(PREPARATION_TOTAL_TIMEOUT_SECONDS, _deadline.get() - time.monotonic())
    compile_timeout = max(0.0, remaining - BUILD_FINALIZATION_RESERVE_SECONDS)
    _run(command, cwd=snapshot,
         environment=build_environment, timeout=compile_timeout,
         progress='Compiling storage engine')
    return target_dir / 'release' / ('tofu-db.exe' if os.name == 'nt' else 'tofu-db')


def prepare_isolated(root: Path, output: Path, *, candidate: Path | None = None,
                     environment: dict | None = None) -> Path:
    """Certify into a new private directory without reading or syncing dotenv.

    No managed artifact/cache or user authority is touched. An explicit candidate
    fails closed; it is never silently replaced by a download or compilation.
    """
    root = root.resolve()
    output = output.absolute()
    if output.resolve().is_relative_to(root):
        raise EnginePreparationError('Isolated preparation output must be outside the source checkout.')
    expected = source_fingerprint(root)
    if expected is None:
        raise EnginePreparationError('Isolated preparation requires source certification.')
    # Exclusive creation refuses existing directories and symlinks, including
    # managed destinations. All persistent writes below belong to this call.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    environment = dict(os.environ if environment is None else environment)
    for key in ('TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256'):
        environment.pop(key, None)
    with _preparation_budget(PREPARATION_TOTAL_TIMEOUT_SECONDS):
        with tempfile.TemporaryDirectory(prefix='build-', dir=output) as temporary:
            staging = Path(temporary)
            selected = (candidate.expanduser().resolve() if candidate is not None
                        else _build(root, staging, environment, build_root=staging))
            verify_current(selected, root)
            if environment.get('TOFUDB_TRANSPORT') == 'embedded' and not (selected.parent / 'libtofu_db.so').is_file():
                raise EnginePreparationError('The selected embedded transport requires a matching engine library.')
            _validate_candidate(selected, root, environment)
            if source_fingerprint(root) != expected:
                raise EnginePreparationError('Engine source changed during preparation.')
            from scripts import deploy_tofudb_engine
            dest = output / ('tofu-db.exe' if os.name == 'nt' else 'tofu-db')
            deploy_tofudb_engine.deploy(argparse.Namespace(
                binary=str(selected), dest=str(dest), evidence=None,
                note='isolated source-identity and disposable semantic certification',
                env_file=None, no_env_sync=True, no_smoke=False, dry_run=False))
            verify_current(dest, root)
            return dest


def prepare(root: Path, environment: dict | None = None) -> bool:
    """Return whether an artifact changed; reject failures before shutdown."""
    token = _deadline.set(min(_deadline.get(), time.monotonic() + PREPARATION_TOTAL_TIMEOUT_SECONDS))
    phase_token = _progress_phase.set(None)
    recovery_token = _progress_recovering.set(False)
    try:
        return _prepare(root, environment)
    finally:
        _deadline.reset(token)
        _progress_phase.reset(phase_token)
        _progress_recovering.reset(recovery_token)


def _prepare(root: Path, environment: dict | None) -> bool:
    from tofu_dotenv import read_dotenv_values
    root = root.resolve()
    project_environment = read_dotenv_values(root / '.env')
    environment = {**project_environment, **(os.environ if environment is None else environment)}
    from runtime_guards import load_deployment_configuration
    if load_deployment_configuration(environment).storage_backend != 'tofudb':
        return False
    reconcile_project_pin(root, project_environment, environment)
    if not (root / 'packages/tofu-db/Cargo.toml').is_file():
        return False  # A packaged install's resolver verifies the bundled pin.
    name = 'tofu-db.exe' if os.name == 'nt' else 'tofu-db'
    dest = Path(environment.get('TOFUDB_BIN') or root / f'packages/tofu-db/target/release/{name}').expanduser().resolve()
    _report_stage('checking', 'Checking storage engine source identity and artifact pins.')
    # Lock wait consumes the total clock; unused queue time belongs to work.
    # Do not start a fresh work clock after acquisition.
    with _prepare_lock(root / 'packages/tofu-db/target/.engine-update.lock'):
        for attempt in range(1, SOURCE_PREPARATION_MAX_ATTEMPTS + 1):
            _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
            _require_storage_configuration(root, project_environment)
            try:
                # Queue time and previous attempts never authorize an older
                # generation. Keep the original configuration and total clock.
                expected = source_fingerprint(root)
                if expected is None:
                    raise EngineSourceChangedError('Storage source disappeared during preparation.')
                return _prepare_generation(root, environment, project_environment, dest, name, expected)
            except EngineSourceChangedError as exc:
                _require_storage_configuration(root, project_environment)
                if attempt == SOURCE_PREPARATION_MAX_ATTEMPTS:
                    raise EnginePreparationError(
                        'Application files kept changing while restart was being prepared. '
                        'Automatic preparation retries were exhausted; restart was not started '
                        'and the current server is still running. Finish the application update '
                        'or source edits, then choose Restart again. No manual build or cache cleanup is needed.'
                    ) from exc
                _progress_recovering.set(True)
                _report_stage('retrying',
                    f'Application files changed during preparation. Automatically refreshing '
                    f'the engine (attempt {attempt + 1}/{SOURCE_PREPARATION_MAX_ATTEMPTS}); '
                    'the current server is still running. No action is needed.')
                time.sleep(_remaining_preparation_seconds(SOURCE_PREPARATION_SETTLE_SECONDS))


def _remaining_preparation_seconds(limit: float) -> float:
    remaining = _deadline.get() - time.monotonic()
    if remaining <= 0:
        raise EnginePreparationError(
            'Storage engine preparation timed out. Restart was not started and '
            'the current server is still running. Finish any application update '
            'or source edits, then choose Restart again; verified build caches are retained.')
    return min(limit, remaining)


def _require_storage_configuration(root: Path, project_environment: dict) -> None:
    from tofu_dotenv import read_dotenv_values
    current = read_dotenv_values(root / '.env')
    if any(current.get(key) != project_environment.get(key) for key in (
            'TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256', 'TOFU_PERSONAL_STORAGE_BACKEND',
            'TOFU_DEPLOYMENT_MODE', 'TOFUDB_TRANSPORT')):
        raise EnginePreparationError(
            'Storage configuration changed during preparation. Restart was not started '
            'and the current server is still running. Review the new configuration, then choose Restart again.')


def _require_source_generation(root: Path, expected: str) -> None:
    if source_fingerprint(root) != expected:
        raise EngineSourceChangedError('Application source changed during engine preparation.')


def _verify_generation(binary: Path, root: Path, expected: str) -> None:
    # Compare with the captured generation, not an uncoordinated second hash.
    # Live-source drift is checked separately before any publication.
    if build_identity(binary)['source_sha256'] != expected:
        _require_source_generation(root, expected)
        raise EnginePreparationError(
            'Storage engine does not match the prepared application source. '
            'The current server is still running; candidate publication was cancelled.')


def _prepare_generation(root: Path, environment: dict, project_environment: dict,
                        dest: Path, name: str, expected: str) -> bool:
    try:
        _verify_generation(dest, root, expected)
        _verify_pin(dest, environment)
        if (environment.get('TOFUDB_TRANSPORT') == 'embedded'
                or (environment.get('TOFUDB_TRANSPORT', 'auto') == 'auto'
                    and (dest.parent / 'libtofu_db.so').exists())):
            verify_library(dest)
    except EngineSourceChangedError:
        raise
    except (EnginePreparationError, OSError):
        pass  # Missing/old engines are the repair trigger, not success.
    else:
        _require_source_generation(root, expected)
        _require_storage_configuration(root, project_environment)
        _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
        _report_stage('cached', 'Storage engine already matches; no download or build needed.')
        return False
    if not dest.is_relative_to(root / 'packages/tofu-db/target'):
        raise EnginePreparationError('The explicitly configured external storage engine is incompatible. Select the managed engine in the application configuration before updating.')
    if (environment.get('TOFUDB_EXPECTED_SHA256')
            and environment['TOFUDB_EXPECTED_SHA256'] != project_environment.get('TOFUDB_EXPECTED_SHA256')):
        raise EnginePreparationError('An external storage engine pin overrides the application configuration. Update that override before preparing a different engine.')
    with tempfile.TemporaryDirectory(prefix='.engine-update-', dir=dest.parent if dest.parent.exists() else root / 'packages/tofu-db/target') as temporary:
        staging = Path(temporary)
        candidate = None
        requires_library = (environment.get('TOFUDB_TRANSPORT') == 'embedded'
                            or (environment.get('TOFUDB_TRANSPORT', 'auto') == 'auto'
                                and (dest.parent / 'libtofu_db.so').is_file()))
        # A developer build or a timed-out publication may already have
        # produced the exact engine. Reuse it before any network/compiler
        # work, but retain source, semantic and deployment verification.
        host_cached = sorted((root / 'packages/tofu-db/target').glob(f'update-build-*/release/{name}'))
        for cached in (*host_cached,
                       root / f'packages/tofu-db/target/update-build/release/{name}',
                       root / f'packages/tofu-db/target/release/{name}'):
            if cached.resolve() == dest:
                continue
            try:
                _verify_generation(cached, root, expected)
                if requires_library and not _cached_library_matches_source(cached, expected):
                    continue
                candidate = cached
                _report_stage('cached', 'Reusing a matching locally compiled storage engine.')
                break
            except EngineSourceChangedError:
                raise
            except (OSError, ValueError, EnginePreparationError):
                continue
        if candidate is None:
            try:
                candidate = _prebuilt(root, staging, environment)
                if candidate is not None:
                    _verify_generation(candidate, root, expected)
                    if requires_library and not (candidate.parent / 'libtofu_db.so').is_file():
                        candidate = None
            except EngineSourceChangedError:
                raise
            except (OSError, ValueError, EnginePreparationError) as exc:
                _report_progress(f'Matching engine download unavailable: {exc}. Trying local build.')
                candidate = None
        _require_source_generation(root, expected)
        if candidate is None:
            candidate = _build(root, staging, environment)
        _require_storage_configuration(root, project_environment)
        _require_source_generation(root, expected)
        _verify_generation(candidate, root, expected)
        if (requires_library
                and not (candidate.parent / 'libtofu_db.so').is_file()):
            raise EnginePreparationError('The selected embedded transport requires a matching engine library; candidate publication was cancelled.')
        _report_stage('validating', 'Validating storage engine against a disposable database.')
        try:
            _validate_candidate(candidate, root, environment)
        except EngineCommandFailedError:
            # The probe's fresh supervisor also checks live source identity.
            # Recover an observed source change, not unchanged-source semantic
            # refusals, timeouts or cancellation (which have different types).
            _require_storage_configuration(root, project_environment)
            _require_source_generation(root, expected)
            raise
        _require_source_generation(root, expected)
        _require_storage_configuration(root, project_environment)
        _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
        # Existing owner retains pin publication, rollback binary and dotenv.
        _report_stage('publishing', 'Publishing verified storage engine and artifact pins.')
        from scripts import deploy_tofudb_engine
        deployment = deploy_tofudb_engine.deploy(argparse.Namespace(
            binary=str(candidate), dest=str(dest), evidence=None,
            note='automatic source-identity preflight; no user authority opened',
            env_file=str(root / '.env'), no_env_sync=False, no_smoke=False, dry_run=False))
        try:
            _verify_generation(dest, root, expected)
            _require_source_generation(root, expected)
        except EngineSourceChangedError as exc:
            # Publication has already written managed pins. Never restart
            # the preparation loop with the original configuration now.
            raise EnginePreparationError(
                'Application files changed during final engine publication. '
                'Restart was not started and the current server is still running. '
                'The engine was published, but activation was cancelled. Finish the '
                'application update or source edits, then choose Restart again.'
            ) from exc
        published_environment = dict(project_environment)
        if 'TOFUDB_EXPECTED_SHA256' in published_environment and deployment['env_file']:
            published_environment['TOFUDB_EXPECTED_SHA256'] = deployment['binary_sha256']
        _require_storage_configuration(root, published_environment)
        _remaining_preparation_seconds(PREPARATION_TOTAL_TIMEOUT_SECONDS)
        _report_stage('prepared', 'Matching storage engine prepared; restart can now activate it.')
        return True


def _validate_candidate(candidate: Path, root: Path, environment: dict) -> None:
    if (candidate.parent / 'libtofu_db.so').is_file():
        expected = build_identity(candidate)['source_sha256']
        _verify_library_source(candidate, expected)
    # Probe only a disposable authority. The probe explicitly clears inherited
    # connection locators and pins the exact candidate before launching it.
    _run([sys.executable, str(root / 'scripts/check_tofudb_engine_drift.py'),
          '--binary', str(candidate), '--json'], cwd=root,
         environment=environment, timeout=90, progress='Validating storage engine')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--prepare-only', type=Path, metavar='NEW_DIRECTORY',
                        help='prepare isolated test artifacts; never read or sync .env')
    parser.add_argument('--candidate', type=Path, help='certify this exact candidate without building')
    args = parser.parse_args()
    if args.candidate and not args.prepare_only:
        parser.error('--candidate requires --prepare-only')
    if os.name != 'nt':
        def interrupted(_signal, _frame):
            raise EnginePreparationCancelledError('Storage engine preparation was cancelled.')
        signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.prepare_only:
            print(prepare_isolated(args.project, args.prepare_only, candidate=args.candidate))
        else:
            prepare(args.project)
        return 0
    except KeyboardInterrupt:
        _report_progress('Storage engine preparation cancelled; the running service has not been stopped.')
        return 130
    except (OSError, ValueError, EnginePreparationError, EnginePreparationCancelledError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
