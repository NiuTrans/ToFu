"""Tofu-DB vs SQLite/PostgreSQL performance comparison harness.

Drives identical operation mixes against three authorities through their
production client contracts and reports per-workload latency distributions
and throughput:

* ``tofudb``   — a real ``tofu-db serve`` process over storage.v2
  using the production ``StorageV2Client`` by default. ``--native-client wire``
  explicitly measures a persistent-session harness instead;
* ``sqlite``   — the legacy Python storage authority, SQLite backend
  (``lib.storage`` supervisor + client, as production personal mode uses);
* ``rawsqlite`` — bare in-process SQLite (WAL, ``synchronous=FULL``), the
  same logical operation shapes executed as direct SQL with no supervisor,
  socket, or serialization layer: the engine-floor baseline that bounds
  how much of each production stack's latency is its client and process
  boundary rather than its storage engine;
* ``postgres`` — the same legacy authority on its PostgreSQL backend,
  enabled only when a disposable database DSN is supplied through
  ``--postgres-dsn`` (never use a live authority).

The workloads model the agent harness's common storage traffic: sidebar
conversation paging, opening a conversation, transcript writes
(``turn.append_settled``), generic record put/get, conversation creation,
full-text ``conversation.search``, an 80/10/10 read/write mix, an
``open_windowed_long_history`` phase (windowed snapshot over a conversation
carrying real turn history) and a ``sidebar_with_history`` phase (sidebar
paging over history-bearing conversations) that expose history-scaled read
paths the header-only corpora cannot see, a
``concurrent_read`` phase (one writer plus four concurrent readers), a
``swarm_write`` phase (W per-conversation writers plus R readers, the real
multi-agent concurrency shape), a ``turn_append_large`` phase (tool-result
class 256 KiB turn payloads) matching production traffic, and a
``maintenance_reopen`` phase (graceful stop + reopen cycles). After all
phases the report records each engine's post-run local file footprint and
process write volume. Native authority and search projection roots are counted
separately; logical lengths are not physical allocation or steady-state size.

Rust/SQLite create disposable authorities. PostgreSQL is a persistent server,
so the caller must provision a disposable database: every run-scoped identity
(conversation, command, turn) carries a per-run unique prefix, so a rerun
against a dirty database cannot conflict, but accumulated catalogs skew scan
workloads — pass ``--postgres-reset`` to drop/recreate/re-migrate the named
database (must be named ``tofu_perf*``/``tofu_bench*``, never a live
authority). Engines see equivalent payloads through their respective
client stacks, not equivalent raw database primitives.  Results are written as JSON (``--output``) and printed as a
table with tofu-db speedup ratios.  Search samples include every page round trip needed to return the requested hit
limit (or exhaust the projection), including empty partial pages. This is a
measurement tool, not a timing gate: CI must never assert wall-clock thresholds from it.

Usage::

    python3 scripts/tofudb_perf_compare.py [--scale small|medium]
        [--engines tofudb,sqlite,postgres] [--postgres-dsn DSN]
        [--postgres-reset] [--output report.json]
"""

from __future__ import annotations

import argparse
import base64
import random
from contextlib import contextmanager

import hashlib
import json
import os

import select
import signal
import stat
import sqlite3
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from unittest.mock import patch
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from tofu_db.wire import (  # noqa: E402
    StorageV2Error,
    StorageV2Session,
)

_TEST_SECRET = b'tofudb-perf-compare-secret-000001'  # 34 ASCII bytes
_OWNER_ID = 11
_TENANT_ID = 7
_TS_BASE = 1_800_000_000_000
_SEARCH_TS = 1_700_000_000_000
_MINIMUM_TAIL_SAMPLES = json.loads((_PROJECT_ROOT /
    'contracts/storage_harness_certification_v1.json').read_text())['minimum_tail_samples']


def _cid() -> str:
    return f'perf-{uuid.uuid4().hex[:24]}'


# ── Engine drivers ───────────────────────────────────────────────────────
class BenchError(Exception):
    pass


def _v2_execute(session: 'StorageV2Session', operation: str, payload: dict,
                command: bool, engine=None):
    """One wire operation with production-faithful bounded retry.

    Classified retryable rejections (rotation backpressure, busy lanes) are
    retried with the SAME command id: a rejected command carries no mutation,
    and a committed one answers from its receipt, so retrying is idempotent
    in both cases. Retries are never hidden: they are counted per engine and
    printed in the report.
    """
    retry = bool(getattr(engine, '_retry_backpressure', False))
    command_id = _cid() if command else None
    for _attempt in range(6000 if retry else 1):
        try:
            response = session.request(operation, payload,
                command_id=command_id,
                deadline_unix_ms=int(time.time() * 1000) + 30_000)
        except StorageV2Error as exc:
            raise BenchError(f'{operation}: {exc.code} {exc.message}') from exc
        if response.status == 0:
            return response.value()
        if not response.retryable:
            break
        if engine is not None:
            engine.client_retries += 1
        time.sleep(0.01)
    raise BenchError(f'{operation}: {response.error_code} {response.error_message}')


class _V2ThreadSession:
    """Per-thread v2 connection with the engine's execute/close surface."""

    def __init__(self, session: 'StorageV2Session', engine):
        self._session = session
        self._engine = engine

    def execute(self, operation: str, payload: dict, command: bool):
        return _v2_execute(self._session, operation, payload, command,
                           self._engine)

    def close(self) -> None:
        self._session.close()


def _application_execute(client, operation: str, payload: dict, command: bool,
                         engine=None):
    """Legacy-client variant of the same bounded-retry discipline."""
    from lib.storage.errors import StorageError
    retry = bool(getattr(engine, '_retry_backpressure', False))
    command_id = _cid() if command else None
    for _attempt in range(6000 if retry else 1):
        try:
            if command:
                return client.command(operation, payload, command_id,
                                      deadline=30.0)
            return client.query(operation, payload, deadline=30.0)
        except StorageError as exc:
            if not exc.retryable:
                raise BenchError(f'{operation}: {exc.code} {exc}') from exc
            last_code, last_message = exc.code, str(exc)
            if engine is not None:
                engine.client_retries += 1
            time.sleep(0.01)
    raise BenchError(f'{operation}: {last_code} {last_message}')


class _LegacyThreadSession:
    """Per-thread view of a legacy engine.

    ``StorageClient`` opens one socket per RPC and is thread-safe by
    construction, so concurrent threads share the production client and still
    reach the sidecar over parallel connections.
    """

    def __init__(self, engine: 'LegacyEngine'):
        self._engine = engine

    def execute(self, operation: str, payload: dict, command: bool):
        return self._engine.execute(operation, payload, command)

    def close(self) -> None:
        pass


def _read_readiness(fd: int, timeout: float = 120.0) -> str:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise BenchError('tofu-db readiness timed out')
        chunk = os.read(fd, 4096 - len(data))
        if not chunk:
            raise BenchError('tofu-db exited before complete readiness')
        data.extend(chunk)
        if b'\n' in data:
            return bytes(data).split(b'\n', 1)[0].decode('utf-8').strip()
    raise BenchError('tofu-db readiness exceeds 4096 bytes')


def _large_turn_content(index: int, size: int, pattern: str) -> str:
    """Deterministic fresh input, constructed outside measured RPC latency."""
    if pattern == 'unique':
        # Every request changes every block, defeating accidental CAS dedupe.
        return base64.b64encode(random.Random(str(index)).randbytes((size * 3 + 3) // 4)).decode()[:size]
    if pattern == 'escaped':
        unit = '\"\\\n\t中🙂'.encode()
        content = (f'tool result batch {index} '.encode() + unit * (size // len(unit) + 1))[:size]
        value = content.decode('utf-8', errors='ignore')
        return value + ' ' * (size - len(value.encode('utf-8')))
    if pattern != 'repetitive':
        raise BenchError('unknown large-turn payload pattern')
    return (f'tool result batch {index} ' + '0123456789abcdef' * ((size + 15) // 16))[:size]


@contextmanager
def _observe_resources(engine, enabled: bool):
    """Sample process RSS at 20 ms; this is observed RSS, not heap attribution."""
    result = {}
    if not enabled:
        yield result
        return
    import psutil
    stopped = threading.Event()
    parent = psutil.Process()
    result.update(engine_peak_rss_observed_bytes=0, client_peak_rss_observed_bytes=0,
                  resource_sample_count=0, resource_sample_interval_ms=20)

    def sample():
        while not stopped.is_set():
            process = getattr(engine, '_process', None)
            runtime = getattr(engine, '_runtime', None)
            pid = process.pid if process is not None else runtime.status().get('pid') if runtime else None
            try:
                if pid:
                    result['engine_peak_rss_observed_bytes'] = max(
                        result['engine_peak_rss_observed_bytes'], psutil.Process(pid).memory_info().rss)
                result['client_peak_rss_observed_bytes'] = max(
                    result['client_peak_rss_observed_bytes'], parent.memory_info().rss)
                result['resource_sample_count'] += 1
            except psutil.Error:
                pass  # A restart interval has no live authority to sample.
            stopped.wait(0.02)

    worker = threading.Thread(target=sample, name='benchmark-rss', daemon=True)
    worker.start()
    try:
        yield result
    finally:
        stopped.set()
        worker.join(timeout=2)
        if worker.is_alive():
            raise BenchError('resource observer did not stop within its budget')


def _pid_write_bytes(pid: int | None) -> int | None:
    """Linux /proc write_bytes for a live pid; None when unavailable."""
    if pid is None:
        return None
    try:
        with open(f'/proc/{pid}/io', encoding='ascii') as stream:
            for line in stream:
                if line.startswith('write_bytes:'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


_FOOTPRINT_MAX_ENTRIES = 100_000
_FOOTPRINT_MAX_SECONDS = 5.0
_FOOTPRINT_MAX_DEPTH = 64


def _directory_footprints(roots: dict[str, Path | None], *,
                          max_entries: int = _FOOTPRINT_MAX_ENTRIES,
                          max_seconds: float = _FOOTPRINT_MAX_SECONDS,
                          max_depth: int = _FOOTPRINT_MAX_DEPTH) -> dict:
    """Read only declared disposable roots; never infer reachability from names.

    Logical/allocated totals count regular-file inodes once across all roots,
    in declaration order. Nested declared roots belong to the more specific
    component. Equal roots cannot establish separate ownership and fail closed.
    Legacy apparent bytes retain path multiplicity (including file symlinks),
    exactly as the former os.walk/lstat sum, but are explicitly incomplete on
    errors or budget exhaustion. Directory metadata and symlink targets are not
    part of the new totals. This is a bounded observation, not an atomic snapshot
    or a hard I/O deadline: a single filesystem call may outlast the time budget.
    """
    if max_entries < 1 or max_seconds <= 0 or max_depth < 1:
        raise ValueError('footprint collection budgets must be positive')
    started = time.monotonic()
    paths = {name: Path(os.path.abspath(path)) if path is not None else None
             for name, path in roots.items()}
    components = {}
    legacy = {}
    seen_files: set[tuple[int, int]] = set()
    seen_directories: set[tuple[int, int]] = set()
    entries_visited = 0
    exhausted = None

    for name, path in paths.items():
        component = {
            'path': str(path) if path is not None else None,
            'status': 'not_configured', 'complete': False,
            'logical_bytes': None, 'allocated_bytes': None, 'file_count': None,
            'observed': {'logical_bytes': 0, 'allocated_bytes': 0, 'file_count': 0},
            'allocated_bytes_supported': None,
            'symlink_count': 0, 'special_file_count': 0, 'duplicate_file_count': 0,
            'excluded_nested_roots': [], 'error_count': 0, 'error_examples': [],
        }
        components[name] = component
        legacy[name] = {'bytes': 0, 'complete': False}
        if path is None:
            continue

        def problem(reason, error=None):
            component['complete'] = False
            component['error_count'] += 1
            if len(component['error_examples']) < 8:
                component['error_examples'].append({
                    'reason': reason, 'errno': getattr(error, 'errno', None)})

        prior = list(paths)[:list(paths).index(name)]
        if any(paths[other] == path for other in prior):
            component['status'] = 'overlapping_root'
            problem('same_root_has_multiple_owners')
            continue
        nested = {other_path for other_path in paths.values()
                  if other_path is not None and other_path != path
                  and path in other_path.parents}
        component['excluded_nested_roots'] = sorted(str(item) for item in nested)
        try:
            # Refuse a symlink root or ancestor, not just symlinks found inside
            # it. No resolve() that could redirect a walk outside declared scope.
            if any(stat.S_ISLNK(os.lstat(parent).st_mode)
                   for parent in reversed((path, *path.parents))):
                component['status'] = 'symlink'
                continue
            root_stat = os.lstat(path)
        except FileNotFoundError:
            component['status'] = 'absent'
            continue
        except OSError as exc:
            component['status'] = 'inaccessible'
            problem('root_stat_failed', exc)
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            component['status'] = 'not_directory'
            continue
        component.update(status='complete', complete=True,
                         allocated_bytes_supported=hasattr(root_stat, 'st_blocks'))

        def visit(directory: Path, depth: int):
            nonlocal entries_visited, exhausted
            if exhausted is not None:
                problem(exhausted)
                return
            if time.monotonic() - started >= max_seconds:
                exhausted = 'time_limit'
                problem(exhausted)
                return
            if depth > max_depth:
                problem('depth_limit')
                return
            try:
                current = os.lstat(directory)
                if not stat.S_ISDIR(current.st_mode):
                    problem('directory_changed_during_collection')
                    return
                identity = (current.st_dev, current.st_ino)
                if identity in seen_directories:
                    problem('directory_alias')
                    return
                seen_directories.add(identity)
                with os.scandir(directory) as children:
                    for child in children:
                        if entries_visited >= max_entries:
                            exhausted = 'entry_limit'
                        elif time.monotonic() - started >= max_seconds:
                            exhausted = 'time_limit'
                        if exhausted is not None:
                            problem(exhausted)
                            return
                        entries_visited += 1
                        child_path = directory / child.name
                        try:
                            info = child.stat(follow_symlinks=False)
                            if child_path in nested:
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                visit(child_path, depth + 1)
                                continue
                            if stat.S_ISLNK(info.st_mode):
                                component['symlink_count'] += 1
                                # os.walk formerly excluded directory symlinks
                                # from its files list. Only this legacy scalar
                                # consults target type; targets are never walked.
                                if not child.is_dir(follow_symlinks=True):
                                    legacy[name]['bytes'] += info.st_size
                                continue
                            legacy[name]['bytes'] += info.st_size
                            if not stat.S_ISREG(info.st_mode):
                                component['special_file_count'] += 1
                                continue
                            identity = (info.st_dev, info.st_ino)
                            if not info.st_ino:
                                problem('file_identity_unavailable')
                            elif identity in seen_files:
                                component['duplicate_file_count'] += 1
                                continue
                            seen_files.add(identity)
                            observed = component['observed']
                            observed['logical_bytes'] += info.st_size
                            observed['file_count'] += 1
                            blocks = getattr(info, 'st_blocks', None)
                            if type(blocks) is int and blocks >= 0:
                                observed['allocated_bytes'] += blocks * 512
                            else:
                                component['allocated_bytes_supported'] = False
                        except OSError as exc:
                            problem('entry_stat_failed', exc)
            except OSError as exc:
                problem('directory_read_failed', exc)

        visit(path, 0)
        if not component['allocated_bytes_supported']:
            component['observed']['allocated_bytes'] = None
        legacy[name]['complete'] = component['complete']
        if component['complete']:
            component.update(component['observed'])
        else:
            component['status'] = 'partial'

    # Reconstruct the old root scope even when a separately declared projection
    # is nested inside it. New component totals remain exclusive and additive.
    own_legacy = {name: dict(value) for name, value in legacy.items()}
    for name, path in paths.items():
        if path is not None:
            for other, other_path in paths.items():
                if other_path is not None and path in other_path.parents:
                    legacy[name]['bytes'] += own_legacy[other]['bytes']
                    legacy[name]['complete'] &= own_legacy[other]['complete']
    complete = all(item['complete'] for item in components.values())
    observed = {field: sum(item['observed'][field] for item in components.values())
                for field in ('logical_bytes', 'file_count')}
    allocated = [item['observed']['allocated_bytes'] for item in components.values()]
    observed['allocated_bytes'] = (sum(allocated)
        if all(item is not None for item in allocated)
        and all(item['allocated_bytes_supported'] is True for item in components.values())
        else None)
    return {
        'components': components,
        'total': {'complete': complete, 'observed': observed,
                  **{key: value if complete else None for key, value in observed.items()}},
        'collection': {
            'max_entries': max_entries, 'max_seconds': max_seconds,
            'max_depth': max_depth, 'entries_visited': entries_visited,
            'elapsed_seconds': round(time.monotonic() - started, 6),
            'budget_exhausted': exhausted,
            'time_budget_scope': 'cooperative between filesystem calls, not an I/O timeout',
            'symlink_policy': 'exclude links and never walk targets',
            'deduplication': 'regular files by (st_dev, st_ino); first declared component owns shared inodes',
        },
        'legacy': legacy,
    }


def _directory_bytes(path: Path) -> int:
    """Legacy apparent path bytes observed under one bounded disposable root."""
    return _directory_footprints({'directory': path})['legacy']['directory']['bytes']


class TofuDbEngine:
    """One fresh-authority ``tofu-db serve`` process."""

    name = 'tofudb'

    def __init__(self, binary: Path, durability: str = 'strict',
                 wal_flush_ms: int = 10, client_mode: str = 'application',
                 expected_sha256: str = '',
                 authority_dir_parent: Path | None = None):
        if durability not in ('strict', 'normal'):
            raise BenchError('tofudb durability must be strict or normal')
        if not 1 <= wal_flush_ms <= 1000:
            raise BenchError('tofudb wal flush interval must be 1..1000 ms')
        self._binary = binary
        self._binary_source = binary.resolve()
        self._expected_sha256 = expected_sha256
        if client_mode not in ('application', 'wire', 'embedded'):
            raise BenchError('native client must be application, wire or embedded')
        self._client_mode = client_mode
        self._client = None
        self._durability = durability
        self._wal_flush_ms = wal_flush_ms
        # Parent for the disposable authority/projection dirs; pointing it at
        # a network/FUSE mount reproduces the degraded-filesystem case.
        self._authority_dir_parent = authority_dir_parent
        self._retry_backpressure = os.environ.get(
            'TOFUDB_PERF_RETRY_BACKPRESSURE', '') == '1'
        self.client_retries = 0
        self._dirs: list[Path] = []
        self._process: subprocess.Popen | None = None
        self._session: StorageV2Session | None = None
        self._embedded = None
        self._port: int | None = None
        self._data_dir: Path | None = None
        self._projection_dir: Path | None = None
        self._write_bytes_baseline: int | None = None
        self._write_bytes_accumulated: int = 0
        self._write_bytes_observed = False
        self._write_bytes_incomplete = False
        self._cache_environment_requested: dict[str, str] | None = None

    def _write_bytes_pid(self) -> int | None:
        return os.getpid() if self._client_mode == 'embedded' else getattr(self._process, 'pid', None)

    def _begin_write_bytes_window(self) -> None:
        self._write_bytes_baseline = _pid_write_bytes(self._write_bytes_pid())
        if self._write_bytes_baseline is None:
            self._write_bytes_incomplete = True

    def _accumulate_write_bytes(self) -> None:
        baseline = self._write_bytes_baseline
        if baseline is None:
            return  # No live window, including an already folded stop.
        current = _pid_write_bytes(self._write_bytes_pid())
        self._write_bytes_baseline = None
        if current is None or current < baseline:
            self._write_bytes_incomplete = True
            return
        self._write_bytes_accumulated += current - baseline
        self._write_bytes_observed = True

    def start(self) -> None:
        self._log = None
        self.log_path: str | None = None
        try:
            self._start_isolated()
        except BaseException:
            self.stop()
            raise

    def _pin_binary(self) -> None:
        """Keep init, serve and reopen on one verified executable snapshot."""
        with self._binary_source.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if self._expected_sha256 and digest != self._expected_sha256:
            raise BenchError('native binary SHA256 does not match the requested build')
        directory = Path(tempfile.mkdtemp(prefix='tofudb-perf-binary-'))
        self._dirs.append(directory)
        pinned = directory / 'tofu-db'
        shutil.copy2(self._binary_source, pinned)
        with pinned.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise BenchError('native binary changed while being pinned')
        pinned.chmod(0o500)
        self._binary = pinned
        if self._client_mode == 'embedded':
            # The shared library IS the engine in embedded mode; pin the
            # same build snapshot next to the pinned executable.
            library = self._binary_source.parent / 'libtofu_db.so'
            if not library.is_file():
                raise BenchError(
                    'embedded client requires libtofu_db.so next to the binary; '
                    'build with cargo (cdylib target)')
            shutil.copy2(library, directory / 'libtofu_db.so')

    def _start_isolated(self) -> None:
        self._pin_binary()
        from serverctl_pkg.native_engine import build_identity, source_fingerprint
        self._build_identity = build_identity(self._binary)
        self._checkout_source_sha256 = source_fingerprint(_PROJECT_ROOT)
        parent = self._authority_dir_parent
        data_dir = Path(tempfile.mkdtemp(
            prefix='tofudb-perf-authority-', dir=parent))
        self._dirs.append(data_dir)
        projection_parent = Path(tempfile.mkdtemp(
            prefix='tofudb-perf-proj-', dir=parent))
        self._dirs.append(projection_parent)
        subprocess.run(
            [str(self._binary), 'init', '--data-dir', str(data_dir)],
            check=True, capture_output=True, text=True, timeout=120,
        )
        self._data_dir = data_dir
        self._projection_dir = projection_parent / 'projection'
        self._launch_serve()

    def _launch_serve(self) -> None:
        self._cache_environment_requested = {
            name: os.environ[name]
            for name in ('TOFU_DB_BLOCK_CACHE_MIB', 'TOFU_DB_PAGE_CACHE_MIB')
            if name in os.environ
        }
        if self._client_mode == 'embedded':
            self._launch_embedded()
            return
        env = dict(os.environ)
        env['TOFU_STORAGE_TOKEN'] = _TEST_SECRET.decode('ascii')
        serve_args = [
            str(self._binary), 'serve',
            '--data-dir', str(self._data_dir),
            '--owner-id', str(_OWNER_ID),
            '--tenant-id', str(_TENANT_ID),
            '--search-projection-dir', str(self._projection_dir),
        ]
        # Always pass the tier explicitly: an absent flag lets the daemon's
        # launch probe auto-tier (network volumes select 'normal'), which
        # then mismatches the harness expectation below. An explicit
        # `--durability` always wins over the probe (main.rs).
        serve_args += ['--durability', self._durability]
        if self._durability != 'strict':
            serve_args += ['--wal-flush-interval-ms', str(self._wal_flush_ms)]
        self._process = subprocess.Popen(
            serve_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._serve_log(),
            env=env,
            text=True,
        )
        readiness_line = _read_readiness(self._process.stdout.fileno())
        if not readiness_line:
            raise BenchError('tofu-db serve exited before readiness')
        readiness = json.loads(readiness_line)
        port = int(readiness['port'])
        from tofu_db.generated_contract import STORAGE_SCHEMA_VERSION
        advertised_schema = readiness.get('schemaId', STORAGE_SCHEMA_VERSION)
        if advertised_schema != STORAGE_SCHEMA_VERSION:
            raise BenchError(
                f"daemon schemaId {advertised_schema!r} does not match SDK "
                f"schema {STORAGE_SCHEMA_VERSION}; pin a matching generated contract")
        if readiness.get('walDurability', 'strict') != self._durability:
            raise BenchError(
                f"daemon reports walDurability "
                f"{readiness.get('walDurability')!r}, expected {self._durability!r}")
        self._port = port
        self._begin_write_bytes_window()
        if self._client_mode == 'application':
            from lib.storage.v2_client import StorageV2Client
            self._client = StorageV2Client('127.0.0.1', port, _TEST_SECRET,
                owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=30.0)
            # Production supervisors forward the daemon's readiness budget
            # (lib/storage/supervisor.py); the pool sizes itself from the
            # advertised connection cap exactly like the deployed client.
            self._client.set_readiness_budget(readiness.get('resourceBudget'))
            return
        self._session = StorageV2Session(
            '127.0.0.1', port, _TEST_SECRET,
            owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=30.0,
        )

    def _launch_embedded(self) -> None:
        """In-process authority: libtofu_db runtime, no daemon process.

        The embedded runtime mirrors serve's lifecycle (drivers, strict WAL
        barrier, search projection, graceful shutdown tail) inside the
        harness process; write volume is the harness's /proc/self/io delta
        (an upper bound, like the rawsqlite engine's).
        """
        if self._durability != 'strict':
            raise BenchError('embedded client serves strict durability only')
        from tofu_db.embedded import EmbeddedRuntime
        library = self._binary.parent / 'libtofu_db.so'
        self._begin_write_bytes_window()
        self._embedded = EmbeddedRuntime(
            library, self._data_dir,
            owner_id=_OWNER_ID, tenant_id=_TENANT_ID,
            projection_dir=self._projection_dir,
            secret=_TEST_SECRET,
        )
        self._embedded_build_identity = self._embedded.build_identity()

    def _embedded_execute(self, operation: str, payload: dict, command: bool):
        """Application-adapter-equivalent retry discipline over the embedded
        transport (same classified-retryable, same command-id replay)."""
        from lib.storage.v2_semantics import request_payload
        retry = self._retry_backpressure
        command_id = _cid() if command else None
        body = request_payload(operation, payload, owner_id=_OWNER_ID)
        for _attempt in range(6000 if retry else 1):
            response = self._embedded.execute_json(
                operation, body,
                command_id=command_id if command else None,
                deadline_unix_ms=int(time.time() * 1000) + 30_000,
            )
            if response.get('status') == 0:
                return response.get('payload')
            if not response.get('retryable'):
                raise BenchError(
                    f"{operation}: {response.get('error_code')} "
                    f"{response.get('error_message')}")
            self.client_retries += 1
            time.sleep(0.01)
        raise BenchError(
            f"{operation}: {response.get('error_code')} "
            f"{response.get('error_message')}")

    def _close_connections(self) -> None:
        """Retire application pools as well as direct wire sessions."""
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._session is not None:
            self._session.close()
            self._session = None

    def restart_cycle(self) -> tuple[float, float]:
        """Graceful stop then reopen of the SAME authority, both timed.

        The data directory survives: shutdown pays the clean catch-up and
        reopen pays bounded recovery of whatever the workload left behind —
        the maintenance pair the charter gates. Directories are only removed
        by the final ``stop``.
        """
        if self._client_mode == 'embedded':
            if self._embedded is None:
                raise BenchError('tofu-db benchmark engine is not started')
            began = time.perf_counter_ns()
            try:
                self._embedded.close()
            except BaseException:
                self._write_bytes_incomplete = True
                raise
            finally:
                self._accumulate_write_bytes()
            shutdown_ms = (time.perf_counter_ns() - began) / 1e6
            began = time.perf_counter_ns()
            self._launch_embedded()
            reopen_ms = (time.perf_counter_ns() - began) / 1e6
            return shutdown_ms, reopen_ms
        if self._process is None or self._data_dir is None:
            raise BenchError('tofu-db benchmark engine is not started')
        began = time.perf_counter_ns()
        self._close_connections()
        if self._log is not None:
            self._log.close()
        self._accumulate_write_bytes()
        if self._process.stdin:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=120)
        except subprocess.TimeoutExpired as exc:
            self._process.kill()
            self._process.wait(timeout=10)
            raise BenchError('tofu-db graceful shutdown exceeded 120 s') from exc
        shutdown_ms = (time.perf_counter_ns() - began) / 1e6
        self._process = None
        began = time.perf_counter_ns()
        self._launch_serve()
        reopen_ms = (time.perf_counter_ns() - began) / 1e6
        return shutdown_ms, reopen_ms

    def crash_cycle(self) -> float:
        """SIGKILL the daemon then reopen the SAME authority, timed end to end.

        The ungraceful half of restart_cycle: no stdin EOF, no clean
        catch-up; recovery must replay the WAL tail exactly like a power
        loss. Callers verify acknowledged writes afterwards — strict
        durability makes every pre-kill acknowledgement durable. Embedded
        mode abandons the runtime (drivers joined, WAL-window retirement
        skipped) — the in-process power-loss approximation.
        """
        if self._client_mode == 'embedded':
            if self._embedded is None:
                raise BenchError('tofu-db benchmark engine is not started')
            began = time.perf_counter_ns()
            try:
                self._embedded.abandon()
            except BaseException:
                self._write_bytes_incomplete = True
                raise
            finally:
                self._accumulate_write_bytes()
            self._embedded = None
            self._launch_embedded()
            return (time.perf_counter_ns() - began) / 1e6
        if self._process is None or self._data_dir is None:
            raise BenchError('tofu-db benchmark engine is not started')
        self._close_connections()
        if self._log is not None:
            self._log.close()
            self._log = None
        began = time.perf_counter_ns()
        self._process.kill()
        self._accumulate_write_bytes()
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise BenchError('tofu-db SIGKILL did not reap within 30 s') from exc
        self._process = None
        self._launch_serve()
        return (time.perf_counter_ns() - began) / 1e6

    def prepare_workload(self) -> None:
        """Renew a session after other engines may have exceeded its idle lease.

        This happens before setup/warmup/timing. Mid-workload transport errors
        remain failures; commands are never blindly retried after a lost ACK.
        """
        if self._client_mode in ('application', 'embedded'):
            return
        if self._port is None:
            raise BenchError('tofu-db benchmark engine is not started')
        if self._session is not None:
            self._session.close()
        self._session = StorageV2Session(
            '127.0.0.1', self._port, _TEST_SECRET,
            owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=30.0,
        )

    def execute(self, operation: str, payload: dict, command: bool) -> None:
        if self._client_mode == 'application':
            return _application_execute(self._client, operation, payload,
                                        command, self)
        if self._client_mode == 'embedded':
            return self._embedded_execute(operation, payload, command)
        return _v2_execute(self._session, operation, payload, command, self)

    def open_session(self) -> '_V2ThreadSession':
        """One extra authenticated session for a concurrent-phase thread.

        A ``StorageV2Session`` is a single synchronous socket, so each thread
        in the concurrent phase drives its own connection; the daemon serves
        them in parallel exactly like production clients. The embedded
        runtime is Send+Sync with the GIL released around calls, so threads
        share it with the same parallelism.
        """
        if self._client_mode in ('application', 'embedded'):
            return _LegacyThreadSession(self)
        if self._port is None:
            raise BenchError('tofu-db benchmark engine is not started')
        return _V2ThreadSession(StorageV2Session(
            '127.0.0.1', self._port, _TEST_SECRET,
            owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=30.0,
        ), self)

    def _serve_log(self):
        self._log = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode='w', prefix='tofudb-perf-serve-', suffix='.log', delete=False)
        self.log_path = self._log.name
        return self._log

    def stop(self) -> None:
        if self._embedded is not None:
            try:
                self._embedded.close()
            except BaseException:
                self._write_bytes_incomplete = True
                raise
            finally:
                self._accumulate_write_bytes()
            self._embedded = None
        elif self._client_mode == 'embedded':
            self._accumulate_write_bytes()
        self._close_connections()
        if self._log is not None:
            self._log.close()
        if self._process is not None:
            self._accumulate_write_bytes()
            if self._process.stdin:
                self._process.stdin.close()
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
        for path in self._dirs:
            shutil.rmtree(path, ignore_errors=True)


class LegacyEngine:
    """The legacy Python storage authority on one backend."""

    def __init__(self, backend: str, postgres_dsn: str = '',
                 authority_dir_parent: Path | None = None):
        self.name = backend
        self._backend = backend
        self._authority_dir_parent = authority_dir_parent
        self._retry_backpressure = os.environ.get(
            'TOFUDB_PERF_RETRY_BACKPRESSURE', '') == '1'
        self.client_retries = 0
        self._postgres_dsn = postgres_dsn
        self._project_root: Path | None = None
        self._runtime = None
        self._client = None
        self._dsn_file: Path | None = None
        self._token: str | None = None
        self._write_bytes_baseline: int | None = None
        self._write_bytes_accumulated: int = 0
        self._child_pid: int | None = None

    def start(self) -> None:
        # Startup is sequential; only the child launch may see these overrides.
        # Never attach to a caller's live sidecar or publish a global runtime.
        try:
            with patch.dict(os.environ):
                os.environ.pop('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', None)
                os.environ.pop('TOFU_STORAGE_CONNECTION_FILE', None)
                # A volume comparison cannot silently relocate SQLite's WAL.
                if self._backend == 'sqlite':
                    os.environ['TOFU_STORAGE_FASTPATH'] = 'off'
                self._start_isolated()
        except BaseException:
            self.stop()
            raise

    def _start_isolated(self) -> None:
        self._project_root = Path(tempfile.mkdtemp(
            prefix=f'legacy-perf-{self._backend}-',
            dir=self._authority_dir_parent)) / uuid.uuid4().hex[:8]
        self._project_root.mkdir(parents=True, exist_ok=True)
        if self._backend == 'postgres':
            if not self._postgres_dsn:
                raise BenchError('postgres engine requires --postgres-dsn')
            os.environ['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
            handle, dsn_name = tempfile.mkstemp(prefix='tofudb-perf-dsn-')
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                stream.write(self._postgres_dsn)
            self._dsn_file = Path(dsn_name)
            os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'] = dsn_name
        os.environ.setdefault(
            'TOFU_STORAGE_TOKEN', uuid.uuid4().hex + uuid.uuid4().hex)
        self._token = os.environ['TOFU_STORAGE_TOKEN']
        self._launch_runtime()

    def _launch_runtime(self) -> None:
        from lib.storage import StorageRuntime, StorageSupervisor

        self._runtime = StorageRuntime(
            StorageSupervisor(
                project_root=self._project_root,
                backend=self._backend,
                startup_timeout=120,
            ),
            auto_restart=False,
        )
        self._runtime.start()
        self._client = self._runtime.client(write=True)
        pid = None
        try:
            status = self._runtime.status()
            pid = status.get('pid') if isinstance(status, dict) else None
        except Exception:  # noqa: BLE001 - status is best-effort telemetry
            pid = None
        self._child_pid = pid
        self._write_bytes_baseline = _pid_write_bytes(pid)

    def _accumulate_write_bytes(self) -> None:
        current = _pid_write_bytes(self._child_pid)
        if current is not None and self._write_bytes_baseline is not None:
            self._write_bytes_accumulated += max(
                0, current - self._write_bytes_baseline)
        self._write_bytes_baseline = None

    def restart_cycle(self) -> tuple[float, float]:
        """Graceful runtime stop then reopen of the SAME project root.

        Mirrors a production server bounce on existing data; the engine
        overrides are re-applied only for the child launch exactly like the
        initial start, never leaking into the parent environment.
        """
        if self._runtime is None or self._project_root is None:
            raise BenchError('legacy benchmark engine is not started')
        self._accumulate_write_bytes()
        began = time.perf_counter_ns()
        self._runtime.stop()
        shutdown_ms = (time.perf_counter_ns() - began) / 1e6
        self._runtime = None
        self._client = None
        overrides = {}
        if self._backend == 'sqlite':
            overrides['TOFU_STORAGE_FASTPATH'] = 'off'
        if self._token:
            overrides['TOFU_STORAGE_TOKEN'] = self._token
        if self._backend == 'postgres':
            if self._dsn_file is None:
                raise BenchError('postgres engine lost its disposable DSN')
            overrides['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
            overrides['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'] = str(
                self._dsn_file)
        began = time.perf_counter_ns()
        with patch.dict(os.environ, overrides):
            os.environ.pop('TOFU_STORAGE_CONNECTION_FILE', None)
            self._launch_runtime()
        reopen_ms = (time.perf_counter_ns() - began) / 1e6
        return shutdown_ms, reopen_ms

    def execute(self, operation: str, payload: dict, command: bool):
        return _application_execute(self._client, operation, payload, command)

    def crash_cycle(self) -> float:
        """SIGKILL the storage child then relaunch the runtime, timed.

        Mirrors TofuDbEngine.crash_cycle for the sidecar: the kill lands
        after the caller's acknowledged writes, so SQLite WAL recovery must
        present exactly those rows when the runtime comes back.
        """
        if self._runtime is None:
            raise BenchError('legacy benchmark engine is not started')
        pid = None
        try:
            status = self._runtime.status()
            pid = status.get('pid') if isinstance(status, dict) else None
        except Exception:  # noqa: BLE001 - status is best-effort telemetry
            pid = None
        began = time.perf_counter_ns()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pid = None
        if pid is not None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
        self._accumulate_write_bytes()
        try:
            self._runtime.stop()
        except Exception:  # noqa: BLE001 - the child is already dead
            pass
        self._runtime = None
        self._client = None
        overrides = {}
        if self._backend == 'sqlite':
            overrides['TOFU_STORAGE_FASTPATH'] = 'off'
        if self._token:
            overrides['TOFU_STORAGE_TOKEN'] = self._token
        with patch.dict(os.environ, overrides):
            os.environ.pop('TOFU_STORAGE_CONNECTION_FILE', None)
            self._launch_runtime()
        return (time.perf_counter_ns() - began) / 1e6

    def open_session(self) -> _LegacyThreadSession:
        if self._client is None:
            raise BenchError('legacy benchmark engine is not started')
        return _LegacyThreadSession(self)

    def stop(self) -> None:
        try:
            if self._runtime is not None:
                try:
                    self._accumulate_write_bytes()
                    self._runtime.stop()
                except Exception:
                    pass
        finally:
            try:
                if self._project_root is not None:
                    shutil.rmtree(self._project_root.parent, ignore_errors=True)
            finally:
                if self._dsn_file is not None:
                    self._dsn_file.unlink(missing_ok=True)


class _RawSqliteSession:
    """One connection's execute/close surface for the concurrent phases.

    WAL readers never block the writer; writers serialize through
    ``BEGIN IMMEDIATE`` plus a 30 s busy timeout, which is exactly the raw
    engine's single-writer ceiling under the swarm shape.
    """

    def __init__(self, engine: 'RawSqliteEngine', connection: sqlite3.Connection):
        self._engine = engine
        self._conn = connection

    def execute(self, operation: str, payload: dict, command: bool):
        return self._engine._execute_on(self._conn, operation, payload, command)

    def close(self) -> None:
        self._conn.close()


class RawSqliteEngine:
    """Bare in-process SQLite: the engine-floor baseline.

    Identical logical operation shapes as the other engines, executed as
    direct SQL in the harness process — no supervisor, socket, or
    serialization layer. Durability is symmetric with the sidecar and with
    ``tofudb --durability strict``: WAL journal, ``synchronous=FULL``, one
    durable commit per logical command. Answers "what does the embedded
    engine itself cost on this workload", which bounds the client-stack tax
    both production stacks pay.

    Honest limits of the reference point:

    * ``crash_cycle`` closes and reopens the in-process connection. Reports
      label this graceful control explicitly: it does not certify crash
      recovery or acknowledged durability after SIGKILL.
    * ``engine_write_bytes`` is the whole harness process's
      ``/proc/self/io`` delta since this engine started — an upper bound
      that over-includes the benchmark's own incidental writes (serve logs,
      report I/O); the engine's own WAL/DB writes dominate it.
    * Application-domain cold families with no engine meaning
      (``task_results.recovery_pending``, ``timer.active.count``,
      ``credential.list``) read real but empty tables, pricing the engine's
      point-read floor rather than any product semantics.
    """

    name = 'rawsqlite'

    def __init__(self, authority_dir_parent: Path | None = None):
        self._retry_backpressure = os.environ.get(
            'TOFUDB_PERF_RETRY_BACKPRESSURE', '') == '1'
        self.client_retries = 0
        self._authority_dir_parent = authority_dir_parent
        self._dir: Path | None = None
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._fts = False
        self._write_bytes_baseline: int | None = None
        self._write_bytes_accumulated = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            self._dir = Path(tempfile.mkdtemp(prefix='rawsqlite-perf-', dir=self._authority_dir_parent))
            self._db_path = self._dir / 'authority.db'
            self._conn = self._connect()
            self._install_schema(self._conn)
            self._write_bytes_baseline = _pid_write_bytes(os.getpid())
        except BaseException:
            self.stop()
            raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path), isolation_level=None,
            check_same_thread=False, timeout=30.0)
        conn.execute('PRAGMA journal_mode=WAL')
        # symmetric with the sidecar's FULL journal and tofudb strict mode
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    def _install_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
CREATE TABLE IF NOT EXISTS records(
    namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY(namespace, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS conversations(
    conv_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
    title TEXT NOT NULL, settings TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS conversations_by_updated
    ON conversations(user_id, updated_at DESC, conv_id);
CREATE TABLE IF NOT EXISTS turns(
    turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
    user_id INTEGER NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
    content TEXT NOT NULL, lane_id TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS turns_by_conv
    ON turns(conversation_id, created_at, turn_id);
CREATE TABLE IF NOT EXISTS task_results(
    task_id TEXT PRIMARY KEY, pending INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS timers(
    timer_id TEXT PRIMARY KEY, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(
    credential_id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL);
""")
        self._fts = conn.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE type='table' AND name='search_index'").fetchone()[0] > 0
        if not self._fts:
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE search_index"
                    " USING fts5(turn_id UNINDEXED, content)")
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False  # LIKE fallback keeps the phase measured.

    def restart_cycle(self) -> tuple[float, float]:
        """Graceful close then reopen of the SAME database file, both timed."""
        if self._conn is None or self._db_path is None:
            raise BenchError('rawsqlite benchmark engine is not started')
        began = time.perf_counter_ns()
        self._conn.close()
        shutdown_ms = (time.perf_counter_ns() - began) / 1e6
        began = time.perf_counter_ns()
        self._conn = self._connect()
        reopen_ms = (time.perf_counter_ns() - began) / 1e6
        return shutdown_ms, reopen_ms

    def crash_cycle(self) -> float:
        """Graceful reopen control, not a process-crash recovery measurement."""
        if self._conn is None or self._db_path is None:
            raise BenchError('rawsqlite benchmark engine is not started')
        began = time.perf_counter_ns()
        stale = self._conn
        self._conn = None
        try:
            stale.close()  # SQLite may checkpoint on this orderly close.
        except sqlite3.Error:
            pass
        self._conn = self._connect()
        return (time.perf_counter_ns() - began) / 1e6

    def open_session(self) -> _RawSqliteSession:
        if self._db_path is None:
            raise BenchError('rawsqlite benchmark engine is not started')
        return _RawSqliteSession(self, self._connect())

    def stop(self) -> None:
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
        finally:
            if self._dir is not None:
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None

    # ── operation dispatch ───────────────────────────────────────────────
    def execute(self, operation: str, payload: dict, command: bool):
        if self._conn is None:
            raise BenchError('rawsqlite benchmark engine is not started')
        return self._execute_on(self._conn, operation, payload, command)

    def _execute_on(self, conn: sqlite3.Connection, operation: str,
                    payload: dict, command: bool):
        """One logical op with the production-faithful bounded-retry policy.

        A failed command transaction is rolled back before any retry, so a
        retried op applies exactly once (the same idempotence the client
        stacks get from command receipts).
        """
        attempts = 6000 if self._retry_backpressure else 1
        for _attempt in range(attempts):
            try:
                return self._dispatch(conn, operation, payload, command)
            except sqlite3.OperationalError as exc:
                if 'lock' not in str(exc).lower():
                    raise BenchError(f'{operation}: {exc}') from exc
                self.client_retries += 1
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
                time.sleep(0.01)
        raise BenchError(f'{operation}: database is locked')

    @staticmethod
    def _transact(conn: sqlite3.Connection, fn):
        conn.execute('BEGIN IMMEDIATE')
        try:
            result = fn()
        except BaseException:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        conn.execute('COMMIT')
        return result

    def _dispatch(self, conn: sqlite3.Connection, operation: str,
                  payload: dict, command: bool):
        handler = getattr(self, '_op_' + operation.replace('.', '__'), None)
        if handler is None:
            raise BenchError(f'unsupported operation {operation}')
        if command:
            return self._transact(conn, lambda: handler(conn, payload))
        return handler(conn, payload)

    # ── record.* ─────────────────────────────────────────────────────────
    def _op_record__put(self, conn, payload):
        conn.execute(
            'INSERT OR REPLACE INTO records(namespace, key, value)'
            ' VALUES (?, ?, ?)',
            (payload['namespace'], payload['key'],
             json.dumps(payload['value'])))
        return {}

    def _op_record__get(self, conn, payload):
        row = conn.execute(
            'SELECT value FROM records WHERE namespace=? AND key=?',
            (payload['namespace'], payload['key'])).fetchone()
        return None if row is None else json.loads(row[0])

    def _op_record__list(self, conn, payload):
        prefix = payload.get('prefix', '')
        rows = conn.execute(
            "SELECT key, value FROM records"
            " WHERE namespace=? AND substr(key, 1, ?)=?"
            " ORDER BY key LIMIT ?",
            (payload['namespace'], len(prefix), prefix,
             payload.get('limit', 100))).fetchall()
        return [{'key': key, 'value': json.loads(value)} for key, value in rows]

    # ── conversation.* ───────────────────────────────────────────────────
    def _op_conversation__create(self, conn, payload):
        try:
            conn.execute(
                'INSERT INTO conversations'
                ' (conv_id, user_id, title, settings, created_at, updated_at)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (payload['conv_id'], payload['user_id'], payload['title'],
                 json.dumps(payload.get('settings') or {}),
                 payload['created_at'], payload['updated_at']))
        except sqlite3.IntegrityError as exc:
            raise BenchError(
                f"conversation.create: database_conflict {exc}") from exc
        return {}

    def _hydrate(self, conn, conv_id: str, window: int | None) -> list[dict]:
        if window is None:
            rows = conn.execute(
                'SELECT content, actor, created_at FROM turns'
                ' WHERE conversation_id=? ORDER BY created_at, turn_id',
                (conv_id,)).fetchall()
        else:
            rows = conn.execute(
                'SELECT content, actor, created_at FROM turns'
                ' WHERE conversation_id=?'
                ' ORDER BY created_at DESC, turn_id DESC LIMIT ?',
                (conv_id, window)).fetchall()
            rows.reverse()
        return [{'content': content, 'actor': actor, 'created_at': at}
                for content, actor, at in rows]

    def _op_conversation__list(self, conn, payload):
        rows = conn.execute(
            'SELECT conv_id, title, settings, created_at, updated_at'
            ' FROM conversations WHERE user_id=?'
            ' ORDER BY updated_at DESC, conv_id LIMIT ?',
            (payload['user_id'], payload.get('limit', 50))).fetchall()
        include = bool(payload.get('include_messages'))
        items = []
        for conv_id, title, settings, created, updated in rows:
            item = {
                'conv_id': conv_id, 'title': title,
                'settings': json.loads(settings),
                'created_at': created, 'updated_at': updated,
            }
            if include:
                item['messages'] = self._hydrate(conn, conv_id, None)
            items.append(item)
        return items

    def _op_conversation__get(self, conn, payload):
        row = conn.execute(
            'SELECT title, settings, created_at, updated_at'
            ' FROM conversations WHERE conv_id=?',
            (payload['conv_id'],)).fetchone()
        if row is None:
            raise BenchError('conversation.get: not_found')
        window = payload.get('message_window')
        count = conn.execute(
            'SELECT count(*) FROM turns WHERE conversation_id=?',
            (payload['conv_id'],)).fetchone()[0]
        title, settings, created, updated = row
        return {
            'conv_id': payload['conv_id'], 'title': title,
            'settings': json.loads(settings),
            'created_at': created, 'updated_at': updated,
            'messages': self._hydrate(conn, payload['conv_id'], window),
            'metadata': {'msg_count': int(count)},
        }

    def _op_conversation__count(self, conn, payload):
        count = conn.execute('SELECT count(*) FROM conversations').fetchone()[0]
        return {'count': int(count)}

    def _op_conversation__search(self, conn, payload):
        query = payload.get('query') or ''
        if not query.strip():
            # Raw engine indexes synchronously inside each append's commit:
            # coverage is ready by construction, never eventually consistent.
            return {'items': [], 'incomplete': False, 'next_cursor': None,
                    'coverage': 'ready', 'coverage_reasons': []}
        limit = payload.get('limit', 50)
        cursor = payload.get('cursor')
        offset = int(cursor) if cursor else 0
        if self._fts:
            rows = conn.execute(
                'SELECT t.turn_id, t.conversation_id, t.content'
                ' FROM search_index s JOIN turns t ON t.turn_id = s.turn_id'
                ' WHERE search_index MATCH ?'
                ' ORDER BY s.rank LIMIT ? OFFSET ?',
                ('"' + query.replace('"', '""') + '"',
                 limit + 1, offset)).fetchall()
        else:
            rows = conn.execute(
                "SELECT turn_id, conversation_id, content FROM turns"
                " WHERE content LIKE '%' || ? || '%' ESCAPE '\\'"
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (query.replace('\\', '\\\\').replace('%', '\\%')
                      .replace('_', '\\_'),
                 limit + 1, offset)).fetchall()
        items = [{'turn_id': turn_id, 'conversation_id': conv_id,
                  'snippet': content[:200]}
                 for turn_id, conv_id, content in rows[:limit]]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return {'items': items, 'incomplete': False,
                'next_cursor': next_cursor}

    # ── turn.* ───────────────────────────────────────────────────────────
    def _op_turn__append_settled(self, conn, payload):
        conv = payload['conversation_id']
        content = (payload.get('projection') or {}).get('content') or ''
        exists = conn.execute(
            'SELECT 1 FROM conversations WHERE conv_id=?',
            (conv,)).fetchone()
        if exists is None:
            defaults = payload.get('conversation_defaults') or {}
            if not defaults.get('allowCreate'):
                raise BenchError('turn.append_settled: conversation missing')
            conn.execute(
                'INSERT INTO conversations'
                ' (conv_id, user_id, title, settings, created_at, updated_at)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (conv, payload['user_id'], defaults.get('title', ''),
                 json.dumps(defaults.get('settings') or {}),
                 defaults.get('createdAt', payload['created_at']),
                 payload['now']))
        try:
            conn.execute(
                'INSERT INTO turns'
                ' (turn_id, conversation_id, user_id, actor, status,'
                '  content, lane_id, created_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (payload['turn_id'], conv, payload['user_id'],
                 payload['actor'], payload['status'], content,
                 payload['lane_id'], payload['created_at']))
        except sqlite3.IntegrityError as exc:
            raise BenchError(
                f'turn.append_settled: database_conflict {exc}') from exc
        if self._fts:
            conn.execute(
                'INSERT INTO search_index(turn_id, content) VALUES (?, ?)',
                (payload['turn_id'], content))
        conn.execute(
            'UPDATE conversations SET updated_at=? WHERE conv_id=?',
            (payload['now'], conv))
        return {}

    def _op_turn__sync__snapshot(self, conn, payload):
        limit = payload.get('turn_limit', 20)
        rows = conn.execute(
            'SELECT turn_id, content, actor, created_at FROM turns'
            ' WHERE conversation_id=?'
            ' ORDER BY created_at DESC, turn_id DESC LIMIT ?',
            (payload['conversation_id'], limit)).fetchall()
        rows.reverse()
        return {'turns': [
            {'turn_id': turn_id, 'content': content, 'actor': actor,
             'created_at': at} for turn_id, content, actor, at in rows]}

    def _op_turn__search__backfill(self, conn, payload):
        return {}  # synchronous indexing: no background projection to sweep

    # ── application-domain cold families (empty real tables) ─────────────
    def _op_task_results__recovery_pending(self, conn, payload):
        count = conn.execute(
            'SELECT count(*) FROM task_results WHERE pending=1').fetchone()[0]
        return {'pending': int(count)}

    def _op_timer__active__count(self, conn, payload):
        count = conn.execute(
            'SELECT count(*) FROM timers WHERE active=1').fetchone()[0]
        return {'count': int(count)}

    def _op_credential__list(self, conn, payload):
        rows = conn.execute(
            'SELECT credential_id FROM credentials LIMIT 100').fetchall()
        return [row[0] for row in rows]


# ── Workloads ────────────────────────────────────────────────────────────
# Each workload is (name, setup, operation, iterations[, runner]).  ``setup``
# runs once per engine before warmup; ``operation(i)`` returns
# (operation, payload, command) for iteration i.  The optional fifth element
# replaces ``measure_workload`` for phases that are not sequential loops
# (the concurrent phase supplies its own threading and timing).

def _record_value(i: int) -> dict:
    return {
        'n': i,
        's': f'value-{i}-' + 'x' * 64,
        'l': [True, None, i],
        'nested': {'a': i, 'b': f'string-{i}'},
    }


def _workloads(scale: dict[str, int], user: int, *, namespace: str | None = None):
    ns = namespace if namespace is not None else f'perf_{uuid.uuid4().hex[:8]}'
    conv_ts = _TS_BASE

    def conv_id(prefix: str, i: int) -> str:
        return f'{ns}_{prefix}_conv_{i:06d}'

    def key(i: int) -> str:
        return f'k/{i:06d}'

    def record_put_setup(engine):
        for i in range(scale['seed_records']):
            engine.execute('record.put', {
                'namespace': ns, 'key': key(i), 'value': _record_value(i),
            }, command=True)

    def record_put_op(i):
        return ('record.put', {
            'namespace': ns, 'key': key(scale['seed_records'] + i),
            'value': _record_value(i),
        }, True)

    def record_get_op(i):
        return ('record.get', {
            'namespace': ns, 'key': key(i % scale['seed_records']),
        }, False)

    def conv_create_op(i):
        return ('conversation.create', {
            'conv_id': conv_id('cb', i), 'user_id': user,
            'title': f'Perf conversation {i}',
            'settings': {'projectPath': f'/perf/{i}'},
            'created_at': conv_ts + i, 'updated_at': conv_ts + i,
        }, True)

    def conv_seed_setup(prefix):
        def setup(engine):
            for i in range(scale['seed_convs']):
                engine.execute('conversation.create', {
                    'conv_id': conv_id(prefix, i), 'user_id': user,
                    'title': f'Perf conversation {i}',
                    'settings': {'projectPath': f'/perf/{i}'},
                    'created_at': conv_ts + i, 'updated_at': conv_ts + i,
                }, command=True)
        return setup

    def conv_list_op(i):
        # Production sidebar payload: the catalog always passes
        # include_messages=False (lib/conversations/catalog.py). The engine
        # defaults an omitted flag to full transcript hydration, which is a
        # different (bulk) contract measured by transcript_bulk_hydration.
        return ('conversation.list', {
            'user_id': user, 'limit': 50, 'include_messages': False,
        }, False)

    def conv_get_op(i):
        # Production conversation open is a bounded message window
        # (repository.get(message_window=...)), never an unbounded history.
        return ('conversation.get', {
            'conv_id': conv_id('op', i % scale['seed_convs']),
            'user_id': user, 'message_window': 50,
        }, False)

    def make_turn_append(tag: str):
        """One (setup, op) pair with identities private to this workload.

        Every workload that appends turns must own a distinct tag: workloads
        share one authority per engine, and reused conversation/command/turn
        identities collide as database_conflict on the second seeder.
        """
        conv = f'{ns}_{tag}_turns'

        def setup(engine):
            engine.execute('conversation.create', {
                'conv_id': conv, 'user_id': user,
                'title': 'Turn append target',
                'settings': {},
                'created_at': conv_ts, 'updated_at': conv_ts,
            }, command=True)

        def op(i):
            at = _SEARCH_TS + 60_000 + i
            return ('turn.append_settled', {
                'conversation_id': conv, 'user_id': user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': (
                        f'agent round {i} '
                        + 'lorem ipsum dolor sit amet ' * 8
                    ),
                },
                'lane_id': 'main', 'command_id': f'{conv}_{i}_command',
                'turn_id': f'{conv}_turn_{i:06d}', 'created_at': at, 'now': at,
                'conversation_defaults': {
                    'allowCreate': False, 'title': 'Turn append target',
                    'createdAt': conv_ts, 'settings': {},
                },
            }, True)

        return setup, op

    turn_append_setup, turn_append_op = make_turn_append('main')
    maint_turn_setup, maint_turn_op = make_turn_append('reopen')

    search_word = f'wq{uuid.uuid4().hex[:10]}'

    def search_setup(engine):
        for i in range(scale['search_docs']):
            at = _SEARCH_TS + i * 1_000
            engine.execute('turn.append_settled', {
                'conversation_id': f'{ns}_search_{i:04d}', 'user_id': user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': (
                        f'document {i} padding text {search_word} '
                        'probe body with more words'
                    ),
                },
                'lane_id': 'main', 'command_id': f'{ns}_search_{i}_command',
                'turn_id': f'{ns}_search_turn_{i:04d}',
                'created_at': at, 'now': at,
                'conversation_defaults': {
                    'allowCreate': True, 'title': f'Search doc {i}',
                    'createdAt': _SEARCH_TS, 'settings': {},
                },
            }, command=True)
        # Both authorities index through an asynchronous projection worker.
        # The legacy engines only flip their search coverage to 'ready' after
        # an explicit global backfill sweep; without it they report 'indexing'
        # forever even once every seeded document is searchable. Schedule the
        # sweep now, then wait until the seeded documents are all visible and
        # coverage is ready. (tofu-db reports 'ready' as soon as its owner is
        # not actively indexing, so this command is a harmless no-op for it.)
        try:
            engine.execute('turn.search.backfill', {}, command=True)
        except BenchError:
            pass
        # Backfill schedules a destructive rebuild, not a resumable nudge.
        # Issue it once: repeating it can reset the cursor before an accumulated
        # corpus finishes. Coverage changes alone are not a progress witness;
        # use the existing bounded total preparation deadline for every engine.
        started = time.monotonic()
        deadline = started + 600.0 + 2.0 * scale['search_docs']
        last_state = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise BenchError(
                    f'search projection did not stabilize for {engine.name} '
                    f'within {now - started:.0f}s (last state: {last_state})')
            hits = _execute_workload_operation(engine, 'conversation.search', {
                'user_id': user, 'query': search_word,
                'limit': max(50, scale['search_docs']), 'snippet_radius': 10,
            }, command=False)
            # Observe coverage even before all seed hits become visible.
            coverage = engine.execute('conversation.search', {
                'user_id': user, 'query': '', 'page': True, 'limit': 1,
            }, command=False)
            if isinstance(coverage, dict):
                last_state = (len(hits), coverage.get('coverage'),
                              tuple(coverage.get('coverage_reasons') or ()))
                if len(hits) >= scale['search_docs'] \
                        and coverage.get('coverage') == 'ready' \
                        and coverage.get('coverage_reasons') == []:
                    engine._search_readiness_observation = {
                        'available': True, 'source': 'conversation.search',
                        'phase': 'conversation_search_setup',
                        'coverage': 'ready', 'coverage_reasons': [],
                        'seed_documents_visible': len(hits),
                        'seed_documents_expected': scale['search_docs'],
                        'observed_at_unix_ms': int(time.time() * 1000),
                    }
                    engine._search_stabilization_s = round(
                        time.monotonic() - started, 1)
                    return
            time.sleep(0.1)

    def search_op(i):
        return ('conversation.search', {
            'user_id': user, 'query': search_word,
            'limit': 20, 'snippet_radius': 10,
        }, False)

    def mixed_setup(engine):
        record_put_setup(engine)
        conv_seed_setup('mx')(engine)

    def mixed_op(i):
        slot = i % 10
        if slot < 8:
            return record_get_op(i)
        if slot == 8:
            return ('record.put', {
                'namespace': ns, 'key': f'mix/{i:06d}',
                'value': _record_value(i),
            }, True)
        return conv_list_op(i)

    def concurrent_read_runner(engine, setup, operation, iterations):
        """One writer plus four concurrent readers (the harness's real shape).

        Reader latency is the headline statistic; writer throughput/p99 carry
        the charter's "reads must not block writer" gate. Any thread failure
        is loud: the engine's phase becomes an error, never a silently
        degraded sample set.
        """
        readers = 4
        writes = scale['concurrent_writes']
        reads_each = iterations
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        record_put_setup(engine)
        conv_seed_setup('cr')(engine)
        start = threading.Event()
        errors: list[str] = []
        reader_samples: list[list[float]] = [[] for _ in range(readers)]
        writer_samples: list[float] = []

        def open_thread_session():
            opener = getattr(engine, 'open_session', None)
            return opener() if opener is not None else engine

        def close_thread_session(session) -> None:
            closer = getattr(session, 'close', None)
            if closer is not None:
                closer()

        def reader(index: int) -> None:
            session = open_thread_session()
            try:
                start.wait()
                for i in range(reads_each):
                    pick = (index + i) % 3
                    if pick == 0:
                        op, payload, command = record_get_op(i)
                    elif pick == 1:
                        op, payload, command = (
                            'conversation.get',
                            {'conv_id': conv_id('cr', i % scale['seed_convs']),
                             'user_id': user},
                            False,
                        )
                    else:
                        op, payload, command = conv_list_op(i)
                    began = time.perf_counter_ns()
                    _execute_workload_operation(session, op, payload, command)
                    reader_samples[index].append(
                        (time.perf_counter_ns() - began) / 1e6)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'reader {index}: {type(exc).__name__}: {exc}')
            finally:
                close_thread_session(session)

        def writer() -> None:
            session = open_thread_session()
            try:
                start.wait()
                for i in range(writes):
                    began = time.perf_counter_ns()
                    session.execute('record.put', {
                        'namespace': ns, 'key': f'cw/{i:06d}',
                        'value': _record_value(i),
                    }, True)
                    writer_samples.append(
                        (time.perf_counter_ns() - began) / 1e6)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'writer: {type(exc).__name__}: {exc}')
            finally:
                close_thread_session(session)

        threads = [
            threading.Thread(target=reader, args=(index,), daemon=True,
                             name=f'perf-reader-{index}')
            for index in range(readers)
        ]
        threads.append(threading.Thread(
            target=writer, daemon=True, name='perf-writer'))
        for thread in threads:
            thread.start()
        phase_started = time.perf_counter_ns()
        start.set()
        # Threads stop on their own bounded op counts; the join deadline only
        # guards a wedged engine (each op already carries a 30 s deadline).
        deadline = time.monotonic() + 120.0 + 0.1 * (
            writes + readers * reads_each)
        for thread in threads:
            remaining = deadline - time.monotonic()
            thread.join(timeout=max(1.0, remaining))
        stuck = [thread.name for thread in threads if thread.is_alive()]
        if stuck:
            errors.append('threads past join deadline: ' + ', '.join(stuck))
        if errors:
            return {'error': '; '.join(errors)}
        if len(writer_samples) != writes:
            return {'error': (
                f'writer completed {len(writer_samples)}/{writes} ops: '
                'reads blocked the writer')}
        flat = [sample for per_reader in reader_samples
                for sample in per_reader]
        if not flat:
            return {'error': 'concurrent readers produced no samples'}
        result = _latency_summary(flat)
        writer_stats = _latency_summary(writer_samples)
        phase_wall_seconds = (time.perf_counter_ns() - phase_started) / 1e9
        result.update({
            'scope': 'concurrent',
            'concurrent_readers': readers,
            'reads_completed': len(flat),
            'writer_iterations': len(writer_samples),
            # Wall-clock phase throughput, not 1/mean-latency: the latter
            # inflates concurrency whenever per-op latency varies.
            'writer_ops_per_sec': round(
                len(writer_samples) / phase_wall_seconds, 1)
                if phase_wall_seconds else 0.0,
            'writer_p50_ms': writer_stats['p50_ms'],
            'writer_p99_ms': writer_stats['p99_ms'],
        })
        return result

    def maintenance_setup(engine):
        """Seed restart-worthy state: point records plus one turn history.

        The conversation gets its own identity: reusing the turn-append
        workload's conversation collides with it inside one shared authority.
        """
        record_put_setup(engine)
        engine.execute('conversation.create', {
            'conv_id': f'{ns}_maint', 'user_id': user,
            'title': 'Maintenance reopen target',
            'settings': {},
            'created_at': conv_ts, 'updated_at': conv_ts,
        }, command=True)
        for i in range(scale['turn_writes']):
            at = _SEARCH_TS + 120_000 + i
            engine.execute('turn.append_settled', {
                'conversation_id': f'{ns}_maint', 'user_id': user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': (
                        f'maintenance round {i} '
                        + 'lorem ipsum dolor sit amet ' * 8
                    ),
                },
                'lane_id': 'main', 'command_id': f'{ns}_maint_{i}_command',
                'turn_id': f'{ns}_maint_turn_{i:06d}',
                'created_at': at, 'now': at,
                'conversation_defaults': {
                    'allowCreate': False,
                    'title': 'Maintenance reopen target',
                    'createdAt': conv_ts, 'settings': {},
                },
            }, command=True)

    def maintenance_reopen_runner(engine, setup, operation, iterations):
        """Graceful stop + reopen cycles on the seeded authority.

        The charter's lifecycle gate: reopen wall is the headline statistic
        (flat summary keys below), shutdown captures the clean catch-up
        (CONTROL publication for tofu-db, WAL truncation on SQLite close),
        and first_read prices the first query after recovery. A failed cycle
        is loud, never a dropped sample.
        """
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        if setup is not None:
            setup(engine)
        restart = getattr(engine, 'restart_cycle', None)
        if restart is None:
            return {'error': f'{engine.name} does not expose restart cycles'}
        # Cold-read families mirror the app boot warm-up set. The 2026-09-16/18
        # timeout storm showed every cold family pays the full first-read cost
        # after a restart while each later read is nearly free; track both per
        # family so cold-path optimizations are visible in this report.
        cold_families: list[tuple[str, dict, bool]] = [
            ('record.get', {'namespace': ns, 'key': key(0)}, False),
            ('record.list', {'namespace': ns, 'prefix': 'k/', 'limit': 5}, False),
            ('conversation.count', {'user_id': user}, False),
            ('conversation.list', {'user_id': user, 'limit': 1}, False),
            ('task_results.recovery_pending', {'limit': 1}, False),
            ('timer.active.count', {'user_id': user}, False),
            ('credential.list', {'owner_user_id': user, 'tenant_id': ''}, False),
        ]
        shutdowns: list[float] = []
        reopens: list[float] = []
        first_reads: list[float] = []
        cold_reads: dict[str, list[float]] = {family: [] for family, _, _ in cold_families}
        warm_reads: dict[str, list[float]] = {family: [] for family, _, _ in cold_families}
        family_errors: dict[str, str] = {}
        for _ in range(iterations):
            shutdown_ms, reopen_ms = restart()
            for family, payload, write in cold_families:
                if family in family_errors:
                    continue
                began = time.perf_counter_ns()
                try:
                    _execute_workload_operation(engine, family, payload, write)
                except BenchError as exc:
                    # An engine lacking a family is an explicit gap in the
                    # report, never a dropped sample or a failed phase.
                    family_errors[family] = str(exc)[:120]
                    continue
                cold_reads[family].append((time.perf_counter_ns() - began) / 1e6)
                began = time.perf_counter_ns()
                _execute_workload_operation(engine, family, payload, write)
                warm_reads[family].append((time.perf_counter_ns() - began) / 1e6)
            first_reads.append(cold_reads['record.get'][-1])
            shutdowns.append(shutdown_ms)
            reopens.append(reopen_ms)
        result = _latency_summary(reopens)
        result.update({
            'scope': 'maintenance',
            'rounds': iterations,
            'shutdown': _latency_summary(shutdowns),
            'first_read': _latency_summary(first_reads),
            'cold_read': {
                family: _latency_summary(samples)
                for family, samples in cold_reads.items() if samples
            },
            'warm_read': {
                family: _latency_summary(samples)
                for family, samples in warm_reads.items() if samples
            },
        })
        if family_errors:
            result['cold_read_unsupported'] = family_errors
        return result

    def swarm_write_runner(engine, setup, operation, iterations):
        """W concurrent writers (one conversation each) + R concurrent readers.

        The real swarm shape: per-conversation sequential writers, concurrent
        across conversations. Aggregate writer throughput exposes the
        single-writer authority's serialization ceiling; per-writer latency
        keeps the interactive gate honest; reader latency proves reads stay
        unblocked. Any thread failure is loud, never a dropped sample.
        """
        writers = scale['swarm_writers']
        readers = scale['swarm_readers']
        writes_each = iterations
        reads_each = scale['swarm_reads_each']
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        record_put_setup(engine)
        for writer in range(writers):
            engine.execute('conversation.create', {
                'conv_id': f'{ns}_swarm_{writer}', 'user_id': user,
                'title': f'Swarm writer {writer}', 'settings': {},
                'created_at': conv_ts, 'updated_at': conv_ts,
            }, command=True)
        start = threading.Event()
        errors: list[str] = []
        writer_samples: list[list[float]] = [[] for _ in range(writers)]
        reader_samples: list[list[float]] = [[] for _ in range(readers)]

        def open_thread_session():
            opener = getattr(engine, 'open_session', None)
            return opener() if opener is not None else engine

        def close_thread_session(session) -> None:
            closer = getattr(session, 'close', None)
            if closer is not None:
                closer()

        def writer(index: int) -> None:
            session = open_thread_session()
            conv = f'{ns}_swarm_{index}'
            try:
                start.wait()
                for i in range(writes_each):
                    at = _SEARCH_TS + 3_600_000 + i
                    began = time.perf_counter_ns()
                    session.execute('turn.append_settled', {
                        'conversation_id': conv, 'user_id': user,
                        'actor': 'human', 'status': 'completed',
                        'projection': {
                            'content': (
                                f'swarm agent {index} round {i} '
                                + 'lorem ipsum dolor sit amet ' * 8
                            ),
                        },
                        'lane_id': 'main',
                        'command_id': f'{conv}_{i}_command',
                        'turn_id': f'{conv}_turn_{i:06d}',
                        'created_at': at, 'now': at,
                        'conversation_defaults': {
                            'allowCreate': False,
                            'title': f'Swarm writer {index}',
                            'createdAt': conv_ts, 'settings': {},
                        },
                    }, True)
                    writer_samples[index].append(
                        (time.perf_counter_ns() - began) / 1e6)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'writer {index}: {type(exc).__name__}: {exc}')
            finally:
                close_thread_session(session)

        def reader(index: int) -> None:
            session = open_thread_session()
            try:
                start.wait()
                for i in range(reads_each):
                    pick = (index + i) % 3
                    if pick == 0:
                        op, payload, command = (
                            'conversation.get',
                            {'conv_id': f'{ns}_swarm_{index % writers}',
                             'user_id': user},
                            False,
                        )
                    elif pick == 1:
                        op, payload, command = conv_list_op(i)
                    else:
                        op, payload, command = record_get_op(i)
                    began = time.perf_counter_ns()
                    _execute_workload_operation(session, op, payload, command)
                    reader_samples[index].append(
                        (time.perf_counter_ns() - began) / 1e6)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'reader {index}: {type(exc).__name__}: {exc}')
            finally:
                close_thread_session(session)

        threads = [
            threading.Thread(target=writer, args=(index,), daemon=True,
                             name=f'perf-swarm-writer-{index}')
            for index in range(writers)
        ] + [
            threading.Thread(target=reader, args=(index,), daemon=True,
                             name=f'perf-swarm-reader-{index}')
            for index in range(readers)
        ]
        for thread in threads:
            thread.start()
        phase_started = time.perf_counter_ns()
        start.set()
        deadline = time.monotonic() + 180.0 + 0.2 * (
            writers * writes_each + readers * reads_each)
        for thread in threads:
            remaining = deadline - time.monotonic()
            thread.join(timeout=max(1.0, remaining))
        stuck = [thread.name for thread in threads if thread.is_alive()]
        if stuck:
            errors.append('threads past join deadline: ' + ', '.join(stuck))
        if errors:
            return {'error': '; '.join(errors)}
        flat_writers = [sample for per_writer in writer_samples
                        for sample in per_writer]
        flat_readers = [sample for per_reader in reader_samples
                        for sample in per_reader]
        if len(flat_writers) != writers * writes_each or not flat_readers:
            return {'error': (
                f'swarm phase incomplete: writers '
                f'{len(flat_writers)}/{writers * writes_each}, readers '
                f'{len(flat_readers)}/{readers * reads_each}')}
        result = _latency_summary(flat_writers)
        reader_stats = _latency_summary(flat_readers)
        phase_wall_seconds = (time.perf_counter_ns() - phase_started) / 1e9
        result.update({
            'scope': 'swarm',
            'swarm_writers': writers,
            'concurrent_readers': readers,
            'writer_iterations': len(flat_writers),
            # Headline: one writer's interactive append latency under swarm
            # pressure; wall-clock aggregate throughput exposes the
            # authority's serialization ceiling (never 1/mean-latency, which
            # inflates concurrency whenever per-op latency varies).
            'aggregate_writer_ops_per_sec': round(
                len(flat_writers) / phase_wall_seconds, 1)
                if phase_wall_seconds else 0.0,
            'writer_ops_per_sec': result['ops_per_sec'],
            'writer_p50_ms': result['p50_ms'],
            'writer_p99_ms': result['p99_ms'],
            'reader_p50_ms': reader_stats['p50_ms'],
            'reader_p99_ms': reader_stats['p99_ms'],
            'reads_completed': len(flat_readers),
        })
        return result

    def large_turn_setup(engine):
        engine.execute('conversation.create', {
            'conv_id': f'{ns}_large_turns', 'user_id': user,
            'title': 'Large turn target',
            'settings': {},
            'created_at': conv_ts, 'updated_at': conv_ts,
        }, command=True)
        for i in range(-scale.get('large_turn_history', 0), 0):
            op, payload, command = large_turn_op(i)
            engine.execute(op, payload, command)

    def large_turn_op(i):
        """A turn carrying a tool-result-class payload (256 KiB of text).

        Agent harness reality: tool results dominate turn bytes. Large
        payloads exercise carried-block spill, WAL record size, and the
        pack/segment pipeline that small turns never touch.
        """
        at = _SEARCH_TS + 7_200_000 + i
        return ('turn.append_settled', {
            'conversation_id': f'{ns}_large_turns', 'user_id': user,
            'actor': 'human', 'status': 'completed',
            'projection': {
                'content': _large_turn_content(i, scale.get('large_turn_bytes', 262_144),
                    scale.get('large_turn_pattern', 'repetitive')),
            },
            'lane_id': 'main', 'command_id': f'{ns}_large_turn_{i}_command',
            'turn_id': f'{ns}_large_turn_{i:06d}', 'created_at': at,
            'now': at,
            'conversation_defaults': {
                'allowCreate': False, 'title': 'Large turn target',
                'createdAt': conv_ts, 'settings': {},
            },
        }, True)

    def history_open_setup(engine):
        """One conversation carrying a realistic accumulated history.

        The charter's header-only ``conversation_open`` corpus cannot see
        history-scaled read paths; this phase's corpus can. The op reads
        the production open path (windowed main-lane snapshot), so any
        O(history) work behind an O(window) contract shows up linearly.
        """
        engine.execute('conversation.create', {
            'conv_id': f'{ns}_history_open', 'user_id': user,
            'title': 'Long history target', 'settings': {},
            'created_at': conv_ts, 'updated_at': conv_ts,
        }, command=True)
        for i in range(scale['history_turns']):
            at = _SEARCH_TS + 3_600_000 + i
            engine.execute('turn.append_settled', {
                'conversation_id': f'{ns}_history_open', 'user_id': user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': f'history round {i} ' + 'lorem ipsum ' * 8,
                },
                'lane_id': 'main', 'command_id': f'{ns}_hist_{i}_command',
                'turn_id': f'{ns}_hist_turn_{i:06d}', 'created_at': at,
                'now': at,
                'conversation_defaults': {
                    'allowCreate': False, 'title': 'Long history target',
                    'createdAt': conv_ts, 'settings': {},
                },
            }, command=True)

    def history_open_op(i):
        return ('turn.sync.snapshot', {
            'conversation_id': f'{ns}_history_open', 'user_id': user,
            'turn_limit': 20,
        }, False)

    def sidebar_history_setup(engine):
        """Sidebar conversations that carry real turn history.

        ``sidebar_list_page`` seeds headers only; production sidebars are
        dominated by conversations with accumulated turns, and any
        per-conversation history work inside ``conversation.list`` scales
        with the summed history instead of the page size.
        """
        for c in range(scale['sidebar_hist_convs']):
            cid = f'{ns}_sbh_{c:05d}'
            engine.execute('conversation.create', {
                'conv_id': cid, 'user_id': user,
                'title': f'History sidebar {c}', 'settings': {},
                'created_at': conv_ts + c, 'updated_at': conv_ts + c,
            }, command=True)
            for t in range(scale['sidebar_hist_turns']):
                # _TS_BASE-based timestamps keep these conversations ahead of
                # the header-only seed corpora (conv_ts + i) in recency
                # order; otherwise a full-suite run pages the header-only
                # conversations and the phase silently re-measures
                # sidebar_list_page instead of history-bearing entries.
                at = _TS_BASE + 7_900_000 + c * 10_000 + t
                engine.execute('turn.append_settled', {
                    'conversation_id': cid, 'user_id': user,
                    'actor': 'human', 'status': 'completed',
                    'projection': {'content': f'sidebar {c} turn {t}'},
                    'lane_id': 'main',
                    'command_id': f'{ns}_sbh_{c}_{t}_command',
                    'turn_id': f'{ns}_sbh_{c}_turn_{t:06d}',
                    'created_at': at, 'now': at,
                    'conversation_defaults': {
                        'allowCreate': False, 'title': f'History sidebar {c}',
                        'createdAt': conv_ts + c, 'settings': {},
                    },
                }, command=True)

    def sidebar_history_op(i):
        return ('conversation.list', {
            'user_id': user, 'limit': 50, 'include_messages': False,
        }, False)

    def hydration_setup(engine):
        for c in range(scale['sidebar_hist_convs']):
            cid = f'{ns}_hyd_{c:05d}'
            engine.execute('conversation.create', {
                'conv_id': cid, 'user_id': user,
                'title': f'Hydration sidebar {c}', 'settings': {},
                'created_at': conv_ts + c, 'updated_at': conv_ts + c,
            }, command=True)
            for t in range(scale['sidebar_hist_turns']):
                at = _TS_BASE + 8_900_000 + c * 10_000 + t
                engine.execute('turn.append_settled', {
                    'conversation_id': cid, 'user_id': user,
                    'actor': 'human', 'status': 'completed',
                    'projection': {'content': f'hydration {c} turn {t}'},
                    'lane_id': 'main',
                    'command_id': f'{ns}_hyd_{c}_{t}_command',
                    'turn_id': f'{ns}_hyd_{c}_turn_{t:06d}',
                    'created_at': at, 'now': at,
                    'conversation_defaults': {
                        'allowCreate': False, 'title': f'Hydration sidebar {c}',
                        'createdAt': conv_ts + c, 'settings': {},
                    },
                }, command=True)

    def hydration_op(i):
        # The bulk transcript path (sync/export): conversation.list's engine
        # default hydrates every entry's full main lane. Kept as its own
        # workload so the O(history-sum) contract stays visible and is never
        # conflated with the production sidebar path above.
        return ('conversation.list', {
            'user_id': user, 'limit': 50, 'include_messages': True,
        }, False)

    def crash_setup(engine):
        engine.execute('conversation.create', {
            'conv_id': f'{ns}_crash_target', 'user_id': user,
            'title': 'Crash recovery target', 'settings': {},
            'created_at': conv_ts, 'updated_at': conv_ts,
        }, command=True)

    def crash_op(i):
        at = _TS_BASE + 9_500_000 + i
        return ('turn.append_settled', {
            'conversation_id': f'{ns}_crash_target', 'user_id': user,
            'actor': 'human', 'status': 'completed',
            'projection': {'content': f'crash round {i}'},
            'lane_id': 'main', 'command_id': f'{ns}_crash_{i}_command',
            'turn_id': f'{ns}_crash_turn_{i:06d}', 'created_at': at,
            'now': at,
            'conversation_defaults': {
                'allowCreate': False, 'title': 'Crash recovery target',
                'createdAt': conv_ts, 'settings': {},
            },
        }, True)

    def crash_msg_count(engine) -> int:
        document = engine.execute('conversation.get', {
            'conv_id': f'{ns}_crash_target', 'user_id': user,
            'message_window': 1,
        }, False)
        metadata = (document or {}).get('metadata') or {}
        count = metadata.get('msg_count')
        if not isinstance(count, int):
            raise BenchError('crash phase could not read a turn count')
        return count

    def crash_recovery_runner(engine, setup, operation, iterations):
        """SIGKILL mid-load: recovery wall, first read, zero acknowledged loss.

        Crash-recovery latency is a first-class engine metric. Each round
        appends ``crash_appends`` acknowledged turns, SIGKILLs the storage
        process with no graceful catch-up, then measures the
        kill-to-serving wall, the first windowed read after recovery, exact
        acknowledged-turn visibility (zero loss), and writability after
        recovery. A wrong count fails the phase loudly.
        """
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        if setup is not None:
            setup(engine)
        crash = getattr(engine, 'crash_cycle', None)
        if crash is None:
            return {'error': f'{engine.name} does not expose crash cycles'}
        appends = scale['crash_appends']
        recovers: list[float] = []
        first_reads: list[float] = []
        expected = 0
        appended = 0
        for _ in range(iterations):
            for _ in range(appends):
                op, payload, command = operation(appended)
                _execute_workload_operation(engine, op, payload, command)
                appended += 1
            expected += appends
            if crash_msg_count(engine) != expected:
                raise BenchError(
                    f'pre-crash turn count drifted from {expected} acknowledged')
            recovers.append(crash())
            began = time.perf_counter_ns()
            visible = crash_msg_count(engine)
            first_reads.append((time.perf_counter_ns() - began) / 1e6)
            if visible != expected:
                raise BenchError(
                    f'crash recovery lost acknowledged turns: {visible} of '
                    f'{expected} visible after reopen')
            op, payload, command = operation(appended)
            _execute_workload_operation(engine, op, payload, command)
            appended += 1
            expected += 1
            if crash_msg_count(engine) != expected:
                raise BenchError('authority is not writable after crash recovery')
        result = _latency_summary(recovers)
        embedded = getattr(engine, '_client_mode', None) == 'embedded'
        method = ('connection_close_reopen' if engine.name == 'rawsqlite' else
                  'embedded_abandon_reopen' if embedded else 'process_sigkill')
        result.update({
            'scope': ('graceful_reopen_control' if engine.name == 'rawsqlite' else
                      'embedded_abandon_control' if embedded else 'crash_recovery'),
            'recovery_method': method,
            'certifies_process_crash': method == 'process_sigkill',
            'rounds': iterations,
            'crash_appends': appends,
            'acknowledged_turns_recovered': expected,
            'zero_acknowledged_loss': True,
            'first_read': _latency_summary(first_reads),
        })
        return result

    return [
        ('record_put_burst', None, record_put_op, scale['writes']),
        ('record_get_points', record_put_setup, record_get_op, scale['reads']),
        ('conversation_create_burst', None, conv_create_op, scale['writes']),
        ('sidebar_list_page', conv_seed_setup('sb'), conv_list_op,
         scale['reads']),
        ('conversation_open', conv_seed_setup('op'), conv_get_op,
         scale['reads']),
        ('turn_append_burst', turn_append_setup, turn_append_op,
         scale['turn_writes']),
        ('conversation_search', search_setup, search_op, scale['search_reads']),
        ('mixed_80_10_10', mixed_setup, mixed_op, scale['mixed']),
        ('open_windowed_long_history', history_open_setup, history_open_op,
         scale['reads']),
        ('sidebar_with_history', sidebar_history_setup, sidebar_history_op,
         scale['reads']),
        ('transcript_bulk_hydration', hydration_setup, hydration_op,
         max(10, scale['reads'] // 10)),
        ('concurrent_read', None, None, scale['concurrent_reads'],
         concurrent_read_runner),
        ('swarm_write', None, None, scale['swarm_writes_each'],
         swarm_write_runner),
        ('turn_append_large', large_turn_setup, large_turn_op,
         scale['large_turn_writes']),
        ('maintenance_reopen', maintenance_setup, None,
         scale['maintenance_rounds'], maintenance_reopen_runner),
        ('crash_recovery', crash_setup, crash_op,
         scale['maintenance_rounds'], crash_recovery_runner),
    ]


_SCALES = {
    'tiny': {
        'writes': 20, 'reads': 40, 'turn_writes': 20, 'seed_records': 20,
        'seed_convs': 20, 'search_docs': 4, 'search_reads': 20, 'mixed': 50,
        'concurrent_reads': 15, 'concurrent_writes': 15,
        'maintenance_rounds': 2,
        'swarm_writers': 3, 'swarm_readers': 2, 'swarm_writes_each': 8,
        'swarm_reads_each': 8, 'large_turn_writes': 6,
        'history_turns': 60, 'sidebar_hist_convs': 8, 'sidebar_hist_turns': 5,
        'crash_appends': 20,
    },
    'small': {
        'writes': 200, 'reads': 500, 'turn_writes': 200, 'seed_records': 500,
        'seed_convs': 100, 'search_docs': 16, 'search_reads': 100,
        'mixed': 600, 'concurrent_reads': 125, 'concurrent_writes': 150,
        'maintenance_rounds': 3,
        'swarm_writers': 4, 'swarm_readers': 2, 'swarm_writes_each': 50,
        'swarm_reads_each': 50, 'large_turn_writes': 30,
        'history_turns': 400, 'sidebar_hist_convs': 50,
        'sidebar_hist_turns': 10, 'crash_appends': 100,
    },
    'medium': {
        'writes': 1000, 'reads': 2000, 'turn_writes': 1000,
        'seed_records': 2000, 'seed_convs': 500, 'search_docs': 64,
        'search_reads': 400, 'mixed': 3000,
        'concurrent_reads': 500, 'concurrent_writes': 600,
        'maintenance_rounds': 5,
        'swarm_writers': 8, 'swarm_readers': 4, 'swarm_writes_each': 200,
        'swarm_reads_each': 200, 'large_turn_writes': 100,
        'history_turns': 1500, 'sidebar_hist_convs': 100,
        'sidebar_hist_turns': 40, 'crash_appends': 300,
    },
}


def _percentile(sorted_samples: list[float], fraction: float) -> float:
    if not sorted_samples:
        return 0.0
    index = min(
        len(sorted_samples) - 1,
        max(0, int(round(fraction * (len(sorted_samples) - 1)))),
    )
    return sorted_samples[index]


def _latency_summary(samples_ms: list[float], *, retain_samples: bool = True) -> dict:
    ordered = sorted(samples_ms)
    total_s = sum(ordered) / 1e3
    result = {
        'iterations': len(ordered),
        'p99_sample_count_sufficient': len(ordered) >= _MINIMUM_TAIL_SAMPLES,
        'p50_ms': round(_percentile(ordered, 0.50), 6),
        'p95_ms': round(_percentile(ordered, 0.95), 6),
        'p99_ms': round(_percentile(ordered, 0.99), 6),
        'mean_ms': round(statistics.fmean(ordered), 6),
        'min_ms': round(ordered[0], 6),
        'max_ms': round(ordered[-1], 6),
        'ops_per_sec': round(len(ordered) / total_s, 1) if total_s else 0.0,
    }
    if retain_samples:
        # Keep the collector's order, outside timed calls. Concurrent collectors
        # may concatenate per-worker series; this is not a global event timeline.
        result['samples_ms'] = list(samples_ms)
        result['sample_order'] = 'collector-input'
    return result


def _execute_workload_operation(engine, operation: str, payload: dict, command: bool):
    """Time one logical query, including every bounded search continuation.

    An empty scan page is progress, never a successful zero-hit benchmark.
    All engines use the same page contract and return at most the requested hits.
    """
    if operation != 'conversation.search':
        return engine.execute(operation, payload, command)
    limit = payload.get('limit', 50)
    if command or type(limit) is not int or not 1 <= limit <= 200:
        raise BenchError('invalid benchmark search request')
    items, cursor, seen = [], None, set()
    deadline = time.monotonic() + 60
    for _ in range(1024):
        if time.monotonic() >= deadline:
            raise BenchError('search continuation deadline exceeded')
        result = engine.execute(operation, {**payload, 'page': True, 'cursor': cursor,
                                            'limit': limit - len(items)}, False)
        if (not isinstance(result, dict) or not isinstance(result.get('items'), list)
                or len(result['items']) > limit - len(items)
                or type(result.get('incomplete')) is not bool):
            raise BenchError('invalid paged search benchmark response')
        items.extend(result['items'])
        cursor = result.get('next_cursor')
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor.encode()) > 8192:
                raise BenchError('invalid search benchmark cursor')
            digest = hashlib.sha256(cursor.encode()).digest()
            if digest in seen:
                raise BenchError('search benchmark cursor did not advance')
            seen.add(digest)
        if len(items) >= limit or cursor is None:
            return items
    raise BenchError('search continuation page budget exceeded')


def measure_workload(engine, setup, operation, iterations: int,
                     warmup: int = 5) -> dict:
    if iterations <= 0 or warmup < 0:
        raise BenchError('iterations must be positive and warmup nonnegative')
    prepare = getattr(engine, 'prepare_workload', None)
    if prepare is not None:
        prepare()
    if setup is not None:
        setup(engine)
    for i in range(warmup):
        op, payload, command = operation(i)
        _execute_workload_operation(engine, op, payload, command)
    samples_ms: list[float] = []
    for i in range(warmup, warmup + iterations):
        op, payload, command = operation(i)
        start = time.perf_counter_ns()
        _execute_workload_operation(engine, op, payload, command)
        samples_ms.append((time.perf_counter_ns() - start) / 1e6)
    result = _latency_summary(samples_ms)
    result['sample_order'] = 'sequential-operation-start'
    # Preserve temporal order in at most ten windows: a global percentile
    # cannot distinguish history-dependent amplification from isolated stalls.
    window_size = max(1, (iterations + 9) // 10)
    result['windows'] = [
        {'start_iteration': warmup + start,
         **_latency_summary(samples_ms[start:start + window_size], retain_samples=False)}
        for start in range(0, iterations, window_size)
    ]
    return result


# ── Binary resolution ────────────────────────────────────────────────────
def _verify_default_binary(binary: Path) -> Path:
    from serverctl_pkg.native_engine import EnginePreparationError, verify_current
    try:
        verify_current(binary, _PROJECT_ROOT)
    except EnginePreparationError as exc:
        raise BenchError('default benchmark binary is stale or unverified; build current sources '
                         'or select a historical build explicitly with --binary') from exc
    return binary


def _resolve_tofudb_binary(explicit: Path | None = None) -> Path:
    performance = os.environ.get('TOFUDB_PERF_BIN', '').strip()
    application = os.environ.get('TOFUDB_BIN', '').strip()
    if explicit is None and performance and application and Path(performance).resolve() != Path(application).resolve():
        raise BenchError('TOFUDB_PERF_BIN and TOFUDB_BIN disagree; select --binary explicitly')
    override = str(explicit) if explicit is not None else performance or application
    if override:
        binary = Path(override).resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise BenchError('selected native binary must name an executable file')
        return binary if explicit is not None else _verify_default_binary(binary)
    candidates = [
        _PROJECT_ROOT / 'packages/tofu-db/target/release/tofu-db',
        _PROJECT_ROOT / 'packages/tofu-db/target-differential/debug/tofu-db',
        _PROJECT_ROOT / 'packages/tofu-db/target/debug/tofu-db',
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return _verify_default_binary(candidate)
    raise BenchError(
        'no tofu-db binary found; build with cargo or set TOFUDB_PERF_BIN')


# ── Report ───────────────────────────────────────────────────────────────
def _footprint_report(engine, directory, workload_reports: dict | None) -> tuple[dict, dict]:
    """Path ownership comes from the existing drivers, not filename heuristics."""
    native = isinstance(engine, TofuDbEngine)
    raw = isinstance(engine, RawSqliteEngine)
    roots = ({'authority': directory, 'projection': engine._projection_dir} if native
             else {'authority_and_projection' if raw else 'local_artifacts': directory})
    started_at = int(time.time() * 1000)
    footprint = _directory_footprints(roots)
    legacy = footprint.pop('legacy')
    original = legacy['authority' if native else next(iter(roots))]
    components = footprint['components']
    if native:
        components['authority']['scope'] = 'native --data-dir; all regular files, without reachability classification'
        components['projection']['scope'] = 'native --search-projection-dir; reconstructible index, not proof of safe immediate deletion'
        scope = 'declared native authority and search projection roots only'
    else:
        # SQL indexes can share database files, and the legacy driver does not
        # retain its separately placed SQLite projection path. Never fabricate
        # a table-level or remote-server decomposition from local filenames.
        for name in ('authority', 'projection'):
            components[name] = {
                'path': None, 'status': 'not_separable' if raw else 'not_collected',
                'complete': False, 'logical_bytes': None,
                'allocated_bytes': None, 'file_count': None,
            }
        scope = ('raw SQLite directory including database, WAL and colocated indexes'
                 if raw else 'legacy project directory only; external projection/server storage not collected')
        if isinstance(engine, LegacyEngine) and engine._backend == 'postgres':
            components['authority']['status'] = 'not_local'
            scope = 'PostgreSQL harness-local artifacts only; NOT PostgreSQL server data size'
        if not raw:
            footprint['total'].update(complete=False, logical_bytes=None,
                                      allocated_bytes=None, file_count=None)
    readiness = dict(getattr(engine, '_search_readiness_observation', None) or {
        'available': False, 'reason': 'search setup readiness was not observed'})
    readiness['post_run_readiness_proven'] = False
    maintenance_run = (workload_reports or {}).get('maintenance_reopen')
    footprint.update({
        'schema_version': 1,
        'scope': scope,
        'timing': {'phase': 'post_workloads_before_shutdown',
                   'started_at_unix_ms': started_at,
                   'finished_at_unix_ms': int(time.time() * 1000),
                   'steady_state': False, 'consistent_snapshot': False},
        'accounting': {
            'logical_bytes': 'sum of st_size for distinct regular-file inodes, not user payload bytes',
            'allocated_bytes': 'sum of st_blocks * 512 when supported for every counted file; not exclusive device usage',
            'file_count': 'distinct regular-file inodes; hardlink aliases counted once',
            'excluded': ['directory metadata', 'symlink bytes and targets', 'special files',
                         'files outside declared roots', 'deleted-open files'],
            'liveness': 'not_assessed; no live/reclaimable byte classification',
        },
        'evidence': {
            'readiness': readiness,
            'maintenance': {'available': False, 'reason': 'native maintenance metrics not collected',
                            'settled': 'not_assessed'},
            'maintenance_reopen': {
                'status': ('not_run' if maintenance_run is None else
                           'error' if 'error' in maintenance_run else 'completed'),
                'rounds': maintenance_run.get('rounds') if maintenance_run else None,
                'source': f'workloads.maintenance_reopen.{engine.name}',
                'settled': 'not_assessed',
            },
        },
    })
    return footprint, original


_GC_FOOTPRINT_FIELDS = (
    'epochs_completed', 'marks_built', 'deferred_epochs', 'epoch_in_progress',
    'last_epoch_duration_ms', 'last_epoch_reclaimed_bytes', 'total_reclaimed_bytes',
    'total_reclaimed_blocks', 'total_temporary_files_removed',
    'total_orphan_segments_removed', 'total_segments_rewritten', 'total_segments_retired',
)
_MAINTENANCE_FOOTPRINT_FIELDS = {
    'maintenance': ('rounds', 'failedRounds', 'foregroundDeferrals', 'noWorkRounds',
                    'historyCompactions', 'retainedHistorySegments', 'materializedRows',
                    'materializedBytes', 'searchDirtyConversations',
                    'searchRebuiltConversations', 'searchFailedRounds',
                    'searchPageBudgetExhaustions'),
    'backgroundPacker': ('packedSegments', 'packedBytes', 'failedRounds',
                         'discardedSegments', 'discardedBytes', 'retiredWals'),
    'onlineGarbageCollector': _GC_FOOTPRINT_FIELDS,
    'searchGarbageCollector': _GC_FOOTPRINT_FIELDS,
}


def _native_resource_budget_observation(engine, *, maintenance: dict | None = None) -> dict:
    """Observe effective policy after timing; environment values are requests."""
    observation = {
        'available': False,
        'scope': 'after all workloads, before shutdown; latest runtime only',
        'requested_cache_environment': getattr(engine, '_cache_environment_requested', None),
    }
    try:
        engine.prepare_workload()  # Renew an idle direct-wire session outside timing.
        metrics = engine.execute('system.metrics', {}, False)

        if maintenance is not None:
            selected = {}
            for group, fields in _MAINTENANCE_FOOTPRINT_FIELDS.items():
                values = metrics.get(group) if isinstance(metrics, dict) else None
                if isinstance(values, dict):
                    selected[group] = {key: values[key] for key in fields
                        if type(values.get(key)) in (int, bool) and values[key] >= 0}
                    if not selected[group]:
                        del selected[group]
            maintenance.update({
                'available': bool(selected), 'source': 'system.metrics',
                'scope': 'latest runtime only, after footprint scan; cumulative counters are not reclaimable bytes',
                'observed_at_unix_ms': int(time.time() * 1000),
                'metrics': selected,
                'reason': None if selected else 'no supported maintenance counters returned',
            })
        budget = metrics.get('resourceBudget') if isinstance(metrics, dict) else None
        if not isinstance(budget, dict) or any(
                type(budget.get(name)) is not int or budget[name] < 0
                for name in ('blockCacheBytes', 'entityPageCacheBytes')):
            raise BenchError('native metrics lack valid effective cache budgets')
        observation.update(available=True, effective=budget)
    except Exception as exc:  # Missing telemetry must not erase measured workloads.
        observation['error'] = f'{type(exc).__name__}: {exc}'

        if maintenance is not None and not maintenance['available']:
            maintenance['error'] = observation['error']
    return observation


def _engine_resource_report(engine, workload_reports: dict | None = None) -> dict:
    """Post-run local file accounting and OS-level process write volume.

    ``data_size_bytes`` retains the legacy apparent path-byte sum under the
    original driver root only (native authority WITHOUT its external search
    projection; legacy project directory, NOT PostgreSQL server storage).
    It is not allocated bytes, user payload bytes or a steady-state claim.
    ``engine_write_bytes`` accumulates the existing process serving windows.
    The additive footprint records scope, gaps, allocation support and evidence.
    """
    if isinstance(engine, TofuDbEngine):
        directory = engine._data_dir
        if getattr(engine, '_client_mode', '') == 'embedded':
            # In-process authority: the harness process's own io delta (an
            # upper bound like the rawsqlite engine's).
            pid = os.getpid()
        else:
            pid = engine._process.pid if engine._process is not None else None
    elif isinstance(engine, LegacyEngine):
        directory = engine._project_root
        pid = None
        if engine._runtime is not None:
            try:
                status = engine._runtime.status()
                pid = status.get('pid') if isinstance(status, dict) else None
            except Exception:  # noqa: BLE001 - best-effort telemetry
                pid = None
    elif isinstance(engine, RawSqliteEngine):
        directory = engine._dir
        # In-process engine: the only writable process is the harness
        # itself, so the delta is an upper bound (see the class docstring).
        pid = os.getpid()
    else:
        return {'error': 'unsupported engine type'}
    footprint, original_size = _footprint_report(engine, directory, workload_reports)
    report: dict = {
        'footprint': footprint,
        'data_size_bytes_complete': original_size['complete'],
        'data_size_bytes_scope': 'legacy apparent path bytes under original driver root only; excludes external native projection',
    }
    if original_size['complete']:
        report['data_size_bytes'] = original_size['bytes']
        report['authority_directory'] = str(Path(directory).absolute())
    else:
        report['data_size_bytes_observed'] = original_size['bytes']
    from scripts.tofudb_bench import _mount_type
    for component in footprint['components'].values():
        if component['complete'] and component['path'] is not None:
            component['filesystem'] = _mount_type(Path(component['path']))

    if original_size['complete']:
        report['filesystem'] = _mount_type(Path(directory))

        report['volume_device'] = Path(directory).stat().st_dev
    # Fold completed serving windows once; add only the current live window.
    # Native report scope names in-process attribution and excluded daemon tails.
    accumulated = getattr(engine, '_write_bytes_accumulated', 0)
    current = _pid_write_bytes(pid)
    baseline = getattr(engine, '_write_bytes_baseline', None)
    live = max(0, current - baseline) if current is not None and baseline is not None else 0
    live_observed = current is not None and baseline is not None and current >= baseline
    if accumulated or live or live_observed or getattr(engine, '_write_bytes_observed', False):
        report['engine_write_bytes'] = accumulated + live
    if isinstance(engine, TofuDbEngine):
        report['engine_write_bytes_complete'] = (
            not engine._write_bytes_incomplete and (baseline is None or live_observed))
        report['engine_write_bytes_scope'] = (
            'harness-process upper bound; serving windows include embedded open, close, abandon and reopen; authority-init subprocess excluded'
            if engine._client_mode == 'embedded' else
            'daemon-process serving windows; init/startup and shutdown tail excluded')
        # Take this last: the observation must not enter latency/write-volume
        # windows or turn an ignored/clamped override into an effective value.
        report['native_resource_budget'] = _native_resource_budget_observation(
            engine, maintenance=footprint['evidence']['maintenance'])
    return report


def _operation_runtime_evidence(name, engine):
    """Observe launch/runtime policy outside every timed workload; never read a live store."""
    if name == 'tofudb':
        engine.prepare_workload()
        metrics = engine.execute('system.metrics', {}, False)
        profile = metrics.get('requestProfile', {})
        return {'wal_durability': metrics.get('walDurability'),
                'profiling': profile.get('enabled'),
                'profile_sample_every': profile.get('sampleEvery', 0)}
    if name == 'rawsqlite':
        return {'synchronous': engine._conn.execute('PRAGMA synchronous').fetchone()[0],
                'journal_mode': engine._conn.execute('PRAGMA journal_mode').fetchone()[0]}
    if name == 'sqlite':
        fastpath = engine._client.metrics().get('fastpath', {})
        # FULL is enforced by the source-pinned adapter's writer factory, not
        # a PRAGMA from an unrelated observer connection (which proves nothing).
        return {'synchronous': 2, 'synchronous_evidence': 'source-pinned writer factory',
                'fastpath_active': fastpath.get('active')}
    return {}


def _finish_operation_evidence(report, engines, before_sources, declared, before_artifacts, runtime_before):
    from benchmarks.conversation_provenance import FORMAT, capture_sources, capture_artifact

    after_sources = capture_sources()
    meta = report['measurement']
    meta['provenance'], meta['authority'], meta['runtime_policy'] = {}, {}, {}
    for name, engine in engines.items():
        embedded = getattr(engine, '_embedded_build_identity', None)
        artifact = capture_artifact(engine._binary, embedded_build_identity=embedded) if name == 'tofudb' else None
        meta['provenance'][name] = {
            'format': FORMAT, 'sources_before': before_sources, 'sources_after': after_sources,
            'declared_artifact': declared.get(name), 'artifact_before': before_artifacts.get(name),
            'artifact_after': artifact,
        }
        if name == 'tofudb':
            build = engine._build_identity
            meta['native_binary_sha256'] = before_artifacts[name]['binary_sha256']

            meta['native_client'] = engine._client_mode
            meta['authority'][name] = {
                'backend': name, 'build_identity': build,
                'checkout_source_sha256': before_sources['native_source_sha256'],
                'source_matches_checkout': build.get('source_sha256') == before_sources['native_source_sha256'],
                'client_mode': engine._client_mode,
            }
            if embedded is not None:
                meta['authority'][name].update(embedded_build_identity=embedded,
                    shared_library_sha256=before_artifacts[name]['shared_library_sha256'])
        after_runtime = _operation_runtime_evidence(name, engine)
        meta['runtime_policy'][name] = {'before': runtime_before[name], 'after': after_runtime}
    native = meta['runtime_policy'].get('tofudb', {})
    sqlite = meta['runtime_policy'].get('sqlite', {})
    raw = meta['runtime_policy'].get('rawsqlite', {})
    unprofiled = bool(native) and all(row.get('profiling') is False and row.get('profile_sample_every') == 0
                                    for row in native.values())
    meta['profiling_requested'] = False if unprofiled else None
    meta['equivalent_durability'] = (
        bool(native and sqlite and raw) and
        all(row.get('wal_durability') == 'strict' for row in native.values()) and
        all(row.get('synchronous') == 2 and row.get('fastpath_active') is False for row in sqlite.values()) and
        all(row.get('synchronous') == 2 and str(row.get('journal_mode')).lower() == 'wal' for row in raw.values()))
    report['env'] = {'native': {'profile': not unprofiled},
                     'sqlite_synchronous': sqlite.get('before', {}).get('synchronous'),
                     'sqlite_runtime': {'version': sqlite3.sqlite_version, 'python': sys.version.split()[0],
                                        'policy': sqlite}}


def _measurement_metadata(engines: dict, args=None) -> dict:
    metadata = {
        'scope': 'storage-operations-not-active-conversations',
        'performance_verdict': 'not_assessed',
        'uncovered': ['active-conversation-streaming', '1000-running-attempts',
                      'scheduled-arrival-latency', 'browser-send-feedback'],
        'benchmark_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'engine_order': list(engines),
        'python_version': sys.version.split()[0],
        'platform': sys.platform,
        'temporary_directory': tempfile.gettempdir(),
        'authority_dir_parent': (
            str(args.authority_dir_parent)
            if args is not None and args.authority_dir_parent else None),
        'latency_clock': 'perf_counter_ns',
        'throughput_basis': 'summed-request-latency',
    }
    engine = engines.get('tofudb')
    binary = getattr(engine, '_binary', None)
    if isinstance(binary, Path):
        with binary.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        metadata['tofudb_binary'] = {
            'path': str(binary.resolve()), 'sha256': digest,
            'bytes': binary.stat().st_size,
        }
        if getattr(engine, '_binary_source', None) is not None:
            metadata['tofudb_binary_source'] = str(engine._binary_source)
        identity = getattr(engine, '_build_identity', {})
        checkout_source = getattr(engine, '_checkout_source_sha256', None)
        metadata['tofudb_build_identity'] = identity
        metadata['checkout_source_sha256'] = checkout_source
        metadata['source_matches_checkout'] = bool(
            checkout_source and identity.get('source_sha256') == checkout_source)
        metadata['tofudb_client_mode'] = getattr(engine, '_client_mode', 'application')
        if getattr(engine, '_client_mode', None) == 'embedded':
            library = binary.parent / 'libtofu_db.so'
            with library.open('rb') as stream:
                library_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            embedded_identity = getattr(engine, '_embedded_build_identity', None)
            metadata['tofudb_shared_library'] = {
                'sha256': library_digest, 'bytes': library.stat().st_size,
                'build_identity': embedded_identity,
            }
            metadata['embedded_source_matches_checkout'] = bool(embedded_identity and
                embedded_identity.get('source_sha256') == checkout_source)
        metadata['benchmark_added_retries'] = 0
        metadata['equivalent_durability'] = getattr(engine, '_durability', 'strict') == 'strict'
        metadata['tofudb_durability'] = getattr(engine, '_durability', 'strict')
        metadata['tofudb_wal_flush_ms'] = getattr(engine, '_wal_flush_ms', 10)
        from tofu_db import wire as wire_client
        metadata['tofudb_wire_client'] = {
            'source_sha256': hashlib.sha256(Path(wire_client.__file__).read_bytes()).hexdigest(),
            'crc32c': 'compiled' if wire_client._NATIVE_CRC32C is not None else 'python',
            'large_text_json': 'orjson-compatible-subset' if wire_client._fast_json is not None else 'stdlib',
            'json_accelerator_version': getattr(wire_client._fast_json, '__version__', None),
        }

    return metadata


def _format_latency(milliseconds: float) -> str:
    return f'{milliseconds * 1000:.3f}us' if milliseconds < 1 else f'{milliseconds:.3f}ms'


def _print_report(report: dict) -> None:
    engines = report['engines']
    workloads = report['workloads']
    header = ['workload'] + engines
    rows = []
    for name, per_engine in workloads.items():
        row = [name]
        for engine in engines:
            stats = per_engine.get(engine)
            if stats is None:
                row.append('skipped')
            elif 'error' in stats:
                row.append(f"ERROR: {stats['error'][:40]}")
            else:
                row.append(
                    f"p50 {_format_latency(stats['p50_ms'])} "
                    f"p95 {_format_latency(stats['p95_ms'])} "
                    f"p99 {_format_latency(stats['p99_ms'])} "
                    f"{stats['ops_per_sec']:.0f} op/s")
        rows.append(row)
    widths = [max(len(r[i]) for r in [header] + rows)
              for i in range(len(header))]
    print('')
    print('  '.join(h.ljust(w) for h, w in zip(header, widths)))
    print('  '.join('-' * w for w in widths))
    for row in rows:
        print('  '.join(cell.ljust(w) for cell, w in zip(row, widths)))
    print('')
    # Concurrent-phase writer evidence: reader latency alone cannot prove the
    # "reads must not block writer" gate.
    for name, per_engine in workloads.items():
        for engine_name in engines:
            stats = per_engine.get(engine_name) or {}
            if stats.get('certifies_process_crash') is False:
                print(f'  {name} [{engine_name}] {stats["recovery_method"]} control only; '
                      'process crash recovery is unmeasured')
            if 'writer_ops_per_sec' in stats:
                line = (
                    f'  {name} [{engine_name}] writer '
                    f"{stats['writer_ops_per_sec']:.0f} op/s "
                    f"p50 {stats['writer_p50_ms']:.2f}ms "
                    f"p99 {stats['writer_p99_ms']:.2f}ms with "
                    f"{stats['concurrent_readers']} concurrent readers")
                if 'aggregate_writer_ops_per_sec' in stats:
                    line += (
                        f" | {stats['swarm_writers']} writers aggregate "
                        f"{stats['aggregate_writer_ops_per_sec']:.0f} op/s"
                        f", reader p50 {stats['reader_p50_ms']:.2f}ms"
                        f" p99 {stats['reader_p99_ms']:.2f}ms")
                print(line)
            if 'stabilization_seconds' in stats:
                print(f'  {name} [{engine_name}] search projection '
                      f"stabilized in {stats['stabilization_seconds']}s")
            if 'client_retries' in stats:
                print(f"  {name} [{engine_name}] classified-retryable "
                      f"backpressure absorbed: {stats['client_retries']} retries")
    # Maintenance-cycle evidence: the reopen headline alone hides the clean
    # shutdown catch-up and the first post-recovery query.
    for name, per_engine in workloads.items():
        for engine_name in engines:
            stats = per_engine.get(engine_name) or {}
            shutdown = stats.get('shutdown')
            first_read = stats.get('first_read')
            if shutdown and first_read:
                print(
                    f'  {name} [{engine_name}] '
                    f"shutdown p50 {shutdown['p50_ms']:.2f}ms "
                    f"max {shutdown['max_ms']:.2f}ms, "
                    f"first read p50 {first_read['p50_ms']:.2f}ms "
                    f"max {first_read['max_ms']:.2f}ms over "
                    f"{stats['rounds']} rounds")
            # Cold-vs-warm collapse per family: the storm's signature is a
            # cold family read orders of magnitude above its warm read.
            cold_read = stats.get('cold_read') or {}
            warm_read = stats.get('warm_read') or {}
            if cold_read:
                coldest = max(
                    cold_read.items(), key=lambda item: item[1]['p50_ms'])
                warm_stats = warm_read.get(coldest[0], {})
                warm_p50 = warm_stats.get('p50_ms')
                ratio = (
                    f" ({coldest[1]['p50_ms'] / warm_p50:.0f}x)"
                    if warm_p50 else '')
                print(
                    f'  {name} [{engine_name}] coldest family {coldest[0]}: '
                    f"cold p50 {coldest[1]['p50_ms']:.2f}ms vs "
                    f"warm p50 {warm_p50:.2f}ms{ratio}")
            unsupported = stats.get('cold_read_unsupported') or {}
            for family, reason in unsupported.items():
                print(f'  {name} [{engine_name}] cold-read family {family} '
                      f'unsupported: {reason}')
    # Post-run local footprint, not durable live bytes or a steady-state claim.
    resources = report.get('resources') or {}
    for engine_name in engines:
        entry = resources.get(engine_name) or {}
        if not entry:
            continue
        if 'error' in entry:
            print(f'  resources [{engine_name}] error: {entry["error"][:60]}')
            continue
        parts = []
        if 'data_size_bytes' in entry:
            parts.append(
                f"legacy apparent size {entry['data_size_bytes'] / 2**20:.1f} MiB")
        for name, component in (entry.get('footprint') or {}).get('components', {}).items():
            if component['complete']:
                allocated = component['allocated_bytes']
                allocation = (f'{allocated / 2**20:.1f} MiB allocated'
                              if allocated is not None else 'allocation unavailable')
                parts.append(f"{name} {component['logical_bytes'] / 2**20:.1f} MiB logical, "
                             f"{allocation}, {component['file_count']} files")
            else:
                parts.append(f"{name} {component['status']}")
        if 'engine_write_bytes' in entry:
            parts.append(
                f"process writes {entry['engine_write_bytes'] / 2**20:.1f} MiB")
        if parts:
            print(f'  resources [{engine_name}] ' + ', '.join(parts)
                  + ' (post-run, includes every workload phase)')
    # Speedup summary vs each legacy engine that ran.
    for baseline in engines:
        if baseline == 'tofudb' or baseline not in engines:
            continue
        print(f'tofudb speedup vs {baseline} (mean latency ratio):')
        for name, per_engine in workloads.items():
            mine = per_engine.get('tofudb')
            theirs = per_engine.get(baseline)
            if (not mine or not theirs or 'error' in mine
                    or 'error' in theirs or not mine.get('mean_ms')
                    or not theirs.get('mean_ms')):
                continue
            if mine.get('scope') != theirs.get('scope'):
                print(f'  {name:28s} incomparable measurement scopes')
                continue
            ratio = theirs['mean_ms'] / mine['mean_ms']
            marker = 'FASTER' if ratio > 1.0 else 'slower'
            print(f'  {name:28s} {ratio:6.2f}x  {marker}')
        print('')


def _reset_postgres_database(dsn: str) -> None:
    """Drop/recreate/re-migrate the disposable benchmark database.

    Hard guard: only names that are unambiguously benchmark-owned
    (``tofu_perf*``/``tofu_bench*``) are eligible; system databases and
    anything else refuse before any connection is opened.
    """
    import psycopg
    from psycopg import sql

    info = psycopg.conninfo.conninfo_to_dict(dsn)
    dbname = info.get('dbname', '')
    if (not dbname or dbname in {'postgres', 'template0', 'template1'}
            or not dbname.startswith(('tofu_perf', 'tofu_bench'))):
        raise BenchError(
            '--postgres-reset requires a clearly disposable database name '
            '(tofu_perf*/tofu_bench*), never a live authority')
    maintenance = psycopg.conninfo.make_conninfo(**{**info, 'dbname': 'postgres'})
    with psycopg.connect(maintenance, autocommit=True,
                         connect_timeout=10) as connection:
        connection.execute(
            sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                sql.Identifier(dbname)))
        connection.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(dbname)))
    from lib.storage_authority_sql.migrate import migrate_external_postgres
    migrate_external_postgres(dsn)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--large-turn-bytes', type=int, default=262144)
    parser.add_argument('--large-turn-pattern', choices=('repetitive', 'unique', 'escaped'), default='repetitive')
    parser.add_argument('--large-turn-writes', type=int)
    parser.add_argument('--large-turn-history', type=int, default=0)
    parser.add_argument('--observe-resources', action='store_true')

    parser.add_argument('--capture-provenance', action='store_true',
                        help='capture complete before/after source/artifact witnesses outside workload clocks for offline charter assessment')
    parser.add_argument('--binary', type=Path, help='explicit native executable; pinned for the whole run')
    parser.add_argument('--authority-dir-parent', type=Path, default=None,
                        help='parent directory for every disposable authority; point it at a network/FUSE mount to benchmark the degraded-filesystem case (the directory must already exist)')
    parser.add_argument('--expected-binary-sha256', default='', help='fail before measurement if the selected build differs')
    parser.add_argument('--scale', choices=sorted(_SCALES), default='small')
    parser.add_argument('--engines', default='tofudb,sqlite,rawsqlite',
                        help='comma subset of tofudb,sqlite,postgres,rawsqlite')
    parser.add_argument('--postgres-dsn', default='',
                        help='DSN for the optional postgres engine')
    parser.add_argument('--postgres-reset', action='store_true',
                        help='drop/recreate/re-migrate the disposable '
                             '--postgres-dsn database before measuring')
    parser.add_argument('--native-client', choices=('application', 'wire', 'embedded'), default='application',
                        help='production Tofu adapter by default; wire measures the raw session, embedded the in-process transport (no daemon)')
    parser.add_argument('--tofudb-durability', default='strict',
                        choices=('strict', 'normal'),
                        help='WAL durability mode for the tofu-db engine '
                             '(default strict; normal is bounded-RPO)')
    parser.add_argument('--tofudb-wal-flush-ms', type=int, default=10,
                        help='background WAL flush interval in ms when '
                             '--tofudb-durability=normal (1..1000)')
    parser.add_argument('--retry-backpressure', action='store_true',
                        help='retry classified retryable rejections with the '
                             'same command id (production-faithful), counted '
                             'and reported; default surfaces them as errors')
    parser.add_argument('--output', default='',
                        help='write the JSON report to this path')
    parser.add_argument('--workloads', default='',
                        help='comma subset of workload names (default: all)')
    args = parser.parse_args()

    selected = [name.strip() for name in args.engines.split(',') if name.strip()]
    if not selected or len(selected) != len(set(selected)):
        parser.error('--engines requires nonempty, unique engine names')
    if not 1 <= args.large_turn_bytes <= 1024 * 1024:
        parser.error('--large-turn-bytes must be within 1..1048576')
    if not 0 <= args.large_turn_history <= 2000:
        parser.error('--large-turn-history must be within 0..2000')
    if args.large_turn_writes is not None and not 1 <= args.large_turn_writes <= 10000:
        parser.error('--large-turn-writes must be within 1..10000')
    scale = dict(_SCALES[args.scale], large_turn_bytes=args.large_turn_bytes,
                 large_turn_pattern=args.large_turn_pattern, large_turn_history=args.large_turn_history)
    if args.large_turn_writes is not None:
        scale['large_turn_writes'] = args.large_turn_writes
    workloads = _workloads(scale, _OWNER_ID)
    if args.workloads:
        selected_workloads = {
            name.strip() for name in args.workloads.split(',') if name.strip()}
        unknown = selected_workloads - {name for name, *_ in workloads}
        if unknown:
            parser.error(f'unknown workloads: {sorted(unknown)}')
        workloads = [entry for entry in workloads
                     if entry[0] in selected_workloads]

    if args.retry_backpressure:
        os.environ['TOFUDB_PERF_RETRY_BACKPRESSURE'] = '1'
    if args.postgres_reset:
        if 'postgres' not in selected or not args.postgres_dsn:
            parser.error('--postgres-reset requires the postgres engine and '
                         '--postgres-dsn')
        _reset_postgres_database(args.postgres_dsn)

    before_sources = None
    declared, before_artifacts, runtime_before = {}, {}, {}
    if args.capture_provenance:
        from benchmarks.conversation_provenance import capture_sources, capture_declared_artifact, capture_artifact
        before_sources = capture_sources()

    engines: dict[str, object] = {}
    skipped: dict[str, str] = {}
    for name in selected:
        try:
            if name == 'tofudb':
                engine = TofuDbEngine(
                    _resolve_tofudb_binary(args.binary),
                    expected_sha256=args.expected_binary_sha256,
                    durability=args.tofudb_durability,
                    wal_flush_ms=args.tofudb_wal_flush_ms, client_mode=args.native_client,
                    authority_dir_parent=args.authority_dir_parent)
            elif name in ('sqlite', 'postgres'):
                engine = LegacyEngine(name, postgres_dsn=args.postgres_dsn,
                                      authority_dir_parent=args.authority_dir_parent)
            elif name == 'rawsqlite':
                engine = RawSqliteEngine(authority_dir_parent=args.authority_dir_parent)
            else:
                skipped[name] = 'unknown engine'
                continue
            if args.capture_provenance and name == 'tofudb':
                declared[name] = capture_declared_artifact(engine._binary, embedded=args.native_client == 'embedded')
            engine.start()
        except (BenchError, Exception) as exc:  # noqa: BLE001
            skipped[name] = f'{type(exc).__name__}: {exc}'
            continue
        engines[name] = engine


        if args.capture_provenance:
            try:
                before_artifacts[name] = capture_artifact(engine._binary,
                    embedded_build_identity=getattr(engine, '_embedded_build_identity', None)) if name == 'tofudb' else None
                runtime_before[name] = _operation_runtime_evidence(name, engine)
            except Exception as exc:
                # Preserve the workload error/report/teardown path; missing
                # provenance cannot later qualify as current performance.
                skipped[name + ':provenance'] = f'{type(exc).__name__}: {exc}'
    report = {
        'scale': args.scale,

        'schema': 'tofu.perf_compare.v2', 'env': {},
        'large_turn_profile': {key: value for key, value in scale.items() if key.startswith('large_turn_')},
        'cpu_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
        'engines': list(engines),
        'skipped': skipped,
        'workloads': {},
    }
    try:
        report['measurement'] = _measurement_metadata(engines, args)

        import platform
        report['measurement'].update(
            collection_id=_cid(), work_spec=scale, workload_order=[row[0] for row in workloads],
            host_identity={'id': hashlib.sha256(platform.node().encode()).hexdigest(),
                           'platform': platform.platform(), 'affinity': report['cpu_affinity']})
        for entry in workloads:
            name, setup, operation, iterations = entry[:4]
            runner = entry[4] if len(entry) > 4 else measure_workload
            per_engine: dict[str, dict] = {}
            for engine_name, engine in engines.items():
                try:
                    with _observe_resources(engine, args.observe_resources) as resources:
                        per_engine[engine_name] = runner(engine, setup, operation, iterations)
                    per_engine[engine_name].update(resources)
                    retries = getattr(engine, 'client_retries', 0)
                    if retries and 'error' not in per_engine[engine_name]:
                        per_engine[engine_name]['client_retries'] = retries
                    stabilization = getattr(
                        engine, '_search_stabilization_s', None)
                    if (stabilization is not None
                            and name == 'conversation_search'
                            and 'error' not in per_engine[engine_name]):
                        per_engine[engine_name]['stabilization_seconds'] = (
                            stabilization)
                except Exception as exc:  # noqa: BLE001
                    message = f'{type(exc).__name__}: {exc}'
                    log_path = getattr(engine, 'log_path', None)
                    if log_path:
                        message += f' (daemon log: {log_path})'
                    per_engine[engine_name] = {'error': message}
            report['workloads'][name] = per_engine
            print(f'[done] {name}', flush=True)
        report['resources'] = {}
        for engine_name, engine in engines.items():
            try:
                report['resources'][engine_name] = _engine_resource_report(
                    engine, {name: per_engine[engine_name]
                             for name, per_engine in report['workloads'].items()})
            except Exception as exc:  # noqa: BLE001
                report['resources'][engine_name] = {
                    'error': f'{type(exc).__name__}: {exc}'}
        if args.capture_provenance:
            try:
                _finish_operation_evidence(report, engines, before_sources, declared, before_artifacts, runtime_before)
            except Exception as exc:
                report['measurement']['provenance_error'] = f'{type(exc).__name__}: {exc}'
                skipped['provenance'] = report['measurement']['provenance_error']
    finally:
        for engine in engines.values():
            engine.stop()

    print('Scope: storage operation measurements; active-conversation capacity and latency NOT ASSESSED.')
    _print_report(report)
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
        print(f'report written to {args.output}')
    return int(bool(skipped) or any(
        'error' in stats
        for per_engine in report['workloads'].values()
        for stats in per_engine.values()
    ))


if __name__ == '__main__':
    sys.exit(main())
