"""Reproducible performance benchmark: Tofu-DB vs the legacy SQLite sidecar.

This harness is the launch-gate performance evidence for Tofu-DB as the
database for agent harnesses.  It replays identical *semantic-operation*
workloads — agent-harness shaped — against both authorities:

* the pre-authority engine — a real ``tofu-db serve`` process speaking the
  storage.v2 wire protocol (``scripts/tofudb_v2_client.py``), and
* the live authority — a real SQLite-backed Sidecar
  (``lib.storage`` client/service contract, as in ``tests._chat_sidecar``),

and records machine-readable JSON results: ops/s, p50/p99/max latency,
daemon RSS / fd count / on-disk footprint, plus exact environment witnesses
(git commit, probe-derived resource budget, CPU/memory observed, mount type).

Methodology discipline (same spirit as certify-filesystem observations):

* fixed seed (``--seed``, default 20260904) derives every payload;
* each cell runs a warmup phase before measurement;
* scale factors are explicit, bounded, and recorded per cell;
* comparisons are only ever between identical semantic workloads — the same
  operation, payload shape, and size class driven through both drivers;
* every workload is bounded and all fixture data is removed after the run
  unless ``--keep-data`` is passed.

This harness MEASURES ONLY: it never modifies engine code, semantic modules,
or contracts.  Engine-level bottlenecks observed here belong in the report.

Usage::

    python3 scripts/tofudb_bench.py --profile smoke --mount native
    python3 scripts/tofudb_bench.py --profile full --mount both \
        --output benchmarks/tofudb_bench_full.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.tofudb_v2_client import (  # noqa: E402
    StorageV2Error,
    StorageV2Session,
)

# ── Constants ────────────────────────────────────────────────────────────
_DEFAULT_SEED = 20260904
_TEST_SECRET = b'tofudb-bench-secret-00000000000001'  # 34 ASCII bytes
_OWNER_ID = 11
_TENANT_ID = 7
_TURN_TS = 1_800_000_000_000
_CONV_TS = 1_700_000_000_000
# request deadlines generous enough for the 64 MiB checkpoint cells
_RPC_DEADLINE_S = 180.0
# latency sample reservoir cap per cell (keeps JSON + memory bounded)
_MAX_LATENCY_SAMPLES = 200_000

_CARGO_CANDIDATES = (
    Path(
        '/path/to/your/data'
        '.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/cargo'
    ),
    Path(
        '/path/to/your/data'
        '.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/cargo'
    ),
    Path(shutil.which('cargo') or '/nonexistent'),
)

KIB = 1024
MIB = 1024 * 1024


# ── Environment witnesses ────────────────────────────────────────────────
def _mount_type(path: Path) -> str:
    """Return the filesystem type for the longest /proc/mounts prefix."""
    target = os.path.realpath(str(path))
    best_mount = ''
    best_type = 'unknown'
    try:
        with open('/proc/mounts', encoding='utf-8') as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point = parts[1].replace('\\040', ' ')
                if target.startswith(mount_point) and len(mount_point) > len(best_mount):
                    best_mount = mount_point
                    best_type = parts[2]
    except OSError:
        pass
    return best_type


def _memory_total_bytes() -> int | None:
    try:
        with open('/proc/meminfo', encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=_PROJECT_ROOT,
            check=True, capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'


# ── Process / directory sampling ─────────────────────────────────────────
def _proc_rss_bytes(pid: int) -> int | None:
    try:
        with open(f'/proc/{pid}/status', encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _proc_fd_count(pid: int) -> int | None:
    try:
        return len(os.listdir(f'/proc/{pid}/fd'))
    except OSError:
        return None


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# ── Deterministic content ────────────────────────────────────────────────
_CONTENT_BLOCK_CACHE: dict[str, str] = {}
_CONTENT_CACHE_LOCK = threading.Lock()


def _content_block(label: str) -> str:
    """64 KiB deterministic printable block derived from the seed label."""
    with _CONTENT_CACHE_LOCK:
        block = _CONTENT_BLOCK_CACHE.get(label)
        if block is None:
            digest = hashlib.sha256(f'tofudb-bench:{label}'.encode()).hexdigest()
            block = (digest * 1024)[:64 * KIB]
            _CONTENT_BLOCK_CACHE[label] = block
        return block


def sized_content(label: str, size_bytes: int) -> str:
    """Deterministic ASCII document of exactly ``size_bytes`` characters."""
    block = _content_block(label)
    repeats = size_bytes // len(block) + 1
    return (block * repeats)[:size_bytes]


# ── Drivers ──────────────────────────────────────────────────────────────
@dataclass
class Outcome:
    ok: bool
    value: Any = None
    error_code: str = ''
    error_message: str = ''


class TofuDbDriver:
    """One storage.v2 connection; create one per worker thread."""

    def __init__(self, host: str, port: int, timeout: float = _RPC_DEADLINE_S):
        self._session = StorageV2Session(
            host, port, _TEST_SECRET,
            owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=timeout,
        )

    def execute(
        self, operation: str, payload: dict[str, Any], command_id: str | None,
    ) -> Outcome:
        deadline = int(time.time() * 1000) + int(_RPC_DEADLINE_S * 1000)
        try:
            response = self._session.request(
                operation, payload,
                command_id=command_id, deadline_unix_ms=deadline,
            )
        except StorageV2Error as exc:
            return Outcome(ok=False, error_code=exc.code,
                           error_message=exc.message)
        except (OSError, ValueError) as exc:
            return Outcome(ok=False, error_code=f'client:{type(exc).__name__}',
                           error_message=str(exc)[:200])
        if response.status != 0:
            return Outcome(ok=False,
                           error_code=response.error_code or 'unknown',
                           error_message=(response.error_message or '')[:200])
        return Outcome(ok=True, value=response.json())

    def close(self) -> None:
        self._session.close()


class LegacyDriver:
    """Drives the real Python storage authority (thread-safe client)."""

    def __init__(self):
        from lib.storage import get_storage_client

        self._client = get_storage_client(write=True)

    def execute(
        self, operation: str, payload: dict[str, Any], command_id: str | None,
    ) -> Outcome:
        from lib.storage.errors import StorageError

        try:
            if command_id is None:
                result = self._client.query(
                    operation, payload, deadline=_RPC_DEADLINE_S)
            else:
                result = self._client.command(
                    operation, payload, command_id, deadline=_RPC_DEADLINE_S)
        except StorageError as exc:
            return Outcome(ok=False, error_code=exc.code,
                           error_message=str(exc)[:200])
        except (OSError, ValueError) as exc:
            return Outcome(ok=False, error_code=f'client:{type(exc).__name__}',
                           error_message=str(exc)[:200])
        return Outcome(ok=True, value=result)

    def close(self) -> None:
        pass


# ── Authority lifecycle ──────────────────────────────────────────────────
def _resolve_tofudb_binary() -> Path:
    override = os.environ.get('TOFUDB_BENCH_BIN', '').strip() or os.environ.get(
        'TOFUDB_DIFFERENTIAL_BIN', '').strip()
    if override:
        binary = Path(override)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        raise RuntimeError(f'TOFUDB_BENCH_BIN is not executable: {override}')
    manifest = _PROJECT_ROOT / 'packages/tofu-db/Cargo.toml'
    cargo = next((path for path in _CARGO_CANDIDATES if path.is_file()), None)
    if cargo is not None:
        target = _PROJECT_ROOT / 'packages/tofu-db/target-bench'
        env = dict(os.environ)
        env.setdefault(
            'CARGO_HOME',
            '/path/to/your/data',
        )
        env.setdefault(
            'RUSTUP_HOME',
            '/path/to/your/data',
        )
        env['PATH'] = f'{cargo.parent}:{env.get("PATH", "")}'
        env['CARGO_TARGET_DIR'] = str(target)
        try:
            subprocess.run(
                [str(cargo), 'build', '--manifest-path', str(manifest)],
                check=True, capture_output=True, text=True, timeout=1800,
                env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        else:
            built = target / 'debug/tofu-db'
            if built.is_file():
                return built
    for candidate in (
        _PROJECT_ROOT / 'packages/tofu-db/target-bench/debug/tofu-db',
        _PROJECT_ROOT / 'packages/tofu-db/target-differential/debug/tofu-db',
        _PROJECT_ROOT / 'packages/tofu-db/target/debug/tofu-db',
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        'no tofu-db binary available (set TOFUDB_BENCH_BIN or install cargo)')


class TofuDbAuthority:
    """One fresh-authority ``tofu-db serve`` rooted at a chosen directory."""

    kind = 'tofudb'

    def __init__(self, root: Path):
        self.data_dir = root / 'tofudb-data'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        search_parent = root / 'tofudb-search-parent'
        search_parent.mkdir(parents=True, exist_ok=True)
        # The daemon initializes a missing projection authority; an existing
        # empty directory is treated as invalid/corrupt.
        self._search_projection_dir = search_parent / 'projection'
        self._process: subprocess.Popen | None = None
        self.readiness: dict[str, Any] = {}
        self._port = 0
        binary = _resolve_tofudb_binary()
        self.binary = str(binary)
        subprocess.run(
            [str(binary), 'init', '--data-dir', str(self.data_dir)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        env = dict(os.environ)
        env['TOFU_STORAGE_TOKEN'] = _TEST_SECRET.decode('ascii')
        self._process = subprocess.Popen(
            [
                str(binary), 'serve',
                '--data-dir', str(self.data_dir),
                '--owner-id', str(_OWNER_ID),
                '--tenant-id', str(_TENANT_ID),
                '--search-projection-dir', str(self._search_projection_dir),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        readiness_line = self._process.stdout.readline().strip()
        if not readiness_line:
            raise RuntimeError('tofu-db serve exited before readiness')
        self.readiness = json.loads(readiness_line)
        self._port = int(self.readiness['port'])

    @property
    def pid(self) -> int:
        assert self._process is not None
        return self._process.pid

    @property
    def dirt_root(self) -> Path:
        return self.data_dir

    def driver_factory(self) -> Callable[[], Any]:
        return lambda: TofuDbDriver('127.0.0.1', self._port)

    def witness(self) -> dict[str, Any]:
        return {
            'binary': self.binary,
            'readiness': self.readiness,
        }

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.stdin:
            self._process.stdin.close()  # EOF is the supervised shutdown signal
        try:
            self._process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        self._process = None


class LegacyAuthority:
    """One real SQLite-backed Sidecar rooted at a chosen directory."""

    kind = 'legacy_sqlite_sidecar'

    def __init__(self, root: Path):
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage.service import install_runtime_for_test

        self._root = root / 'legacy-sidecar' / uuid.uuid4().hex[:8]
        self._root.mkdir(parents=True, exist_ok=True)
        self._runtime = StorageRuntime(
            StorageSupervisor(
                project_root=self._root, backend='sqlite', startup_timeout=120),
            auto_restart=False,
        )
        self._install = install_runtime_for_test
        self._install(self._runtime)
        self._runtime.start()

    @property
    def pid(self) -> int | None:
        status = self._runtime.status()
        pid = status.get('pid')
        return int(pid) if pid else None

    @property
    def dirt_root(self) -> Path:
        return self._root

    def driver_factory(self) -> Callable[[], Any]:
        return LegacyDriver

    def witness(self) -> dict[str, Any]:
        status = dict(self._runtime.status())
        status.pop('token', None)
        return {'backend': 'sqlite', 'status': status}

    def stop(self) -> None:
        try:
            self._runtime.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask results
            pass
        self._install(None)


# ── Measurement helpers ──────────────────────────────────────────────────
@dataclass
class CellResult:
    name: str
    workload: str
    authority: str
    mount: str
    params: dict[str, Any]
    measured_ops: int = 0
    warmup_ops: int = 0
    wall_s: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    error_samples: list[dict[str, str]] = field(default_factory=list)
    rss_bytes: dict[str, int | None] = field(default_factory=dict)
    fd_count_max: int | None = None
    data_bytes_end: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)

        def pct(q: float) -> float | None:
            if not lat:
                return None
            index = min(len(lat) - 1, int(q * len(lat)))
            return round(lat[index], 3)

        return {
            'name': self.name,
            'workload': self.workload,
            'authority': self.authority,
            'mount': self.mount,
            'params': self.params,
            'warmup_ops': self.warmup_ops,
            'measured_ops': self.measured_ops,
            'wall_s': round(self.wall_s, 6),
            'ops_per_s': (
                round(self.measured_ops / self.wall_s, 2)
                if self.wall_s > 0 else None
            ),
            'latency_ms': {
                'p50': pct(0.50),
                'p99': pct(0.99),
                'max': round(lat[-1], 3) if lat else None,
            },
            'errors': self.errors,
            'error_samples': self.error_samples,
            'rss_bytes': self.rss_bytes,
            'fd_count_max': self.fd_count_max,
            'data_bytes_end': self.data_bytes_end,
            'notes': self.notes,
        }


def _record_error(cell: CellResult, outcome: Outcome) -> None:
    code = outcome.error_code or 'unknown'
    cell.errors[code] = cell.errors.get(code, 0) + 1
    if len(cell.error_samples) < 3 and outcome.error_message:
        cell.error_samples.append(
            {'code': code, 'message': outcome.error_message[:200]})


class _Sampler:
    """Background RSS/fd sampler for one authority process."""

    def __init__(self, pid: int | None, interval_s: float = 0.25):
        self._pid = pid
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rss_samples: list[int] = []
        self.fd_samples: list[int] = []

    def __enter__(self) -> '_Sampler':
        if self._pid is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            rss = _proc_rss_bytes(self._pid)
            if rss is not None:
                self.rss_samples.append(rss)
            fds = _proc_fd_count(self._pid)
            if fds is not None:
                self.fd_samples.append(fds)
            self._stop.wait(self._interval)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def run_cell(
    name: str,
    workload: str,
    authority: Any,
    mount: str,
    params: dict[str, Any],
    body: Callable[[CellResult, Callable[[], Any]], None],
    *,
    sample: bool = True,
) -> CellResult:
    """Run one measured cell against one authority and collect witnesses."""
    cell = CellResult(
        name=name, workload=workload, authority=authority.kind,
        mount=mount, params=params,
    )
    pid = authority.pid
    rss_start = _proc_rss_bytes(pid) if pid else None
    with _Sampler(pid) as sampler:
        body(cell, authority.driver_factory())
    rss_end = _proc_rss_bytes(pid) if pid else None
    if sample and sampler.rss_samples:
        cell.rss_bytes = {
            'start': rss_start,
            'max': max(sampler.rss_samples),
            'end': rss_end,
        }
    if sampler.fd_samples:
        cell.fd_count_max = max(sampler.fd_samples)
    cell.data_bytes_end = _dir_bytes(authority.dirt_root)
    return cell


def measure_sequential(
    cell: CellResult,
    driver: Any,
    operations: list[tuple[str, dict[str, Any], str | None]],
    warmup: int,
) -> None:
    """Measure one-op-at-a-time latency over a prebuilt operation list."""
    latencies: list[float] = []
    measured = 0
    for index, (operation, payload, command_id) in enumerate(operations):
        started = time.perf_counter()
        outcome = driver.execute(operation, payload, command_id)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not outcome.ok:
            _record_error(cell, outcome)
            continue
        if index < warmup:
            cell.warmup_ops += 1
            continue
        measured += 1
        if len(latencies) < _MAX_LATENCY_SAMPLES:
            latencies.append(elapsed_ms)
    cell.latencies_ms = latencies
    cell.measured_ops = measured


def measure_concurrent(
    cell: CellResult,
    driver_factory: Callable[[], Any],
    per_thread: Callable[[Any, int, int], int],
    writers: int,
    ops_per_writer: int,
    warmup_per_writer: int,
) -> None:
    """Measure aggregate throughput of ``writers`` concurrent worker threads.

    ``per_thread(driver, writer_index, ops_per_writer)`` returns the number of
    successful measured ops; errors are reported through ``cell.errors`` via
    the closure.  Wall time spans the join of all workers, so ops/s is true
    aggregate throughput (the group-commit observable).
    """
    lock = threading.Lock()
    latencies: list[float] = []

    def worker(writer_index: int) -> int:
        driver = driver_factory()
        local_lat: list[float] = []
        try:
            for op_index in range(ops_per_writer + warmup_per_writer):
                started = time.perf_counter()
                outcome = per_thread(driver, writer_index, op_index)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if isinstance(outcome, Outcome):
                    if not outcome.ok:
                        with lock:
                            _record_error(cell, outcome)
                        continue
                if op_index >= warmup_per_writer:
                    local_lat.append(elapsed_ms)
            return len(local_lat)
        finally:
            driver.close()
            with lock:
                if len(latencies) < _MAX_LATENCY_SAMPLES:
                    take = min(
                        len(local_lat), _MAX_LATENCY_SAMPLES - len(latencies))
                    latencies.extend(local_lat[:take])
                cell.warmup_ops += warmup_per_writer

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(worker, index) for index in range(writers)]
        cell.measured_ops = sum(future.result() for future in futures)
    cell.wall_s = time.perf_counter() - started
    cell.latencies_ms = latencies


# ── Payload builders (identical semantic operations on both authorities) ──
def _turn_append_payload(
    conv: str, turn: str, ordinal: int, content: str,
) -> dict[str, Any]:
    at = _TURN_TS + 1000 * (ordinal + 1)
    return {
        'conversation_id': conv,
        'user_id': _OWNER_ID,
        'actor': 'assistant',
        'status': 'completed',
        'projection': {'content': content},
        'lane_id': 'main',
        'command_id': f'{turn}_command',
        'turn_id': turn,
        'created_at': at,
        'now': at,
        'conversation_defaults': {
            'allowCreate': True,
            'title': 'Bench',
            'createdAt': _TURN_TS,
            'settings': {'theme': 'dark'},
        },
    }


def _conversation_create_payload(conv: str, ordinal: int) -> dict[str, Any]:
    return {
        'conv_id': conv,
        'user_id': _OWNER_ID,
        'title': f'Bench conversation {ordinal}',
        'created_at': _CONV_TS + ordinal,
        'updated_at': _CONV_TS + ordinal,
    }


def _event_append_payload(task: str, sequence: int, text: str) -> dict[str, Any]:
    return {
        'task_id': task,
        'sequence': sequence,
        'event': {'type': 'token_delta', 'text': text},
    }


# ── Workloads ────────────────────────────────────────────────────────────
def workload_turn_ingest(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, size_label: str, content_bytes: int, writers: int, ops_per_writer: int,
    warmup_per_writer: int, seed_label: str,
) -> None:
    """1. turn.append_settled — the hot write path of every agent run."""
    content = sized_content(f'turn:{size_label}', content_bytes)
    run_id = f'{seed_label}-{uuid.uuid4().hex[:6]}'
    if writers == 1:
        conv = f'bench-turn-{size_label}-seq-{run_id}'
        operations = [
            (
                'turn.append_settled',
                _turn_append_payload(conv, f'{conv}_t{index}', index, content),
                f'bench-turn-{size_label}-{run_id}-{index}',
            )
            for index in range(ops_per_writer + warmup_per_writer)
        ]
        measure_sequential(
            cell, driver_factory(), operations, warmup_per_writer)
        cell.wall_s = sum(cell.latencies_ms) / 1000.0
        return

    convs = [
        f'bench-turn-{size_label}-w{writer}-{run_id}'
        for writer in range(writers)
    ]

    def per_thread(driver: Any, writer: int, op_index: int) -> Outcome:
        conv = convs[writer]
        return driver.execute(
            'turn.append_settled',
            _turn_append_payload(
                conv, f'{conv}_t{op_index}', op_index, content),
            f'bench-turn-{size_label}-{run_id}-w{writer}-{op_index}',
        )

    measure_concurrent(
        cell, driver_factory, per_thread, writers, ops_per_writer,
        warmup_per_writer,
    )


def workload_turn_size_ceiling_probe(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, probe_sizes: list[int], seed_label: str,
) -> None:
    """1b. Evidence probe: where does one large turn stop being accepted?

    Rejections here are the OBSERVATION, not harness errors: each probe
    size's outcome code is recorded under params.  Both authorities are
    probed with byte-identical documents.
    """
    driver = driver_factory()
    for size in probe_sizes:
        conv = f'bench-probe-{size}-{seed_label}-{uuid.uuid4().hex[:6]}'
        content = sized_content(f'probe:{size}', size)
        started = time.perf_counter()
        outcome = driver.execute(
            'turn.append_settled',
            _turn_append_payload(conv, f'{conv}_t0', 0, content),
            f'bench-probe-{size}-{seed_label}')
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        cell.params[f'probe_{size}_bytes'] = (
            'ok' if outcome.ok else f'rejected:{outcome.error_code}')
        cell.measured_ops += 1
        cell.latencies_ms.append(elapsed_ms)
    cell.wall_s = sum(cell.latencies_ms) / 1000.0
    cell.notes.append(
        'rejection codes are the observation: legacy bound is '
        'MAX_DECODED_RECEIPT_BYTES=4MiB (receipt_codec.py), tofu-db bound '
        'is MAX_TRANSACTION_IR_LITERAL_BYTES=8MiB over the whole '
        'transaction literal (engine.rs)')


def workload_event_append_single(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, ops: int, warmup: int, seed_label: str,
) -> None:
    """2a. event.append single — per-token streaming hot path."""
    task = f'bench-event-{seed_label}-{uuid.uuid4().hex[:8]}'
    text = sized_content(f'event:{seed_label}', 64)
    operations = [
        (
            'event.append',
            _event_append_payload(task, index, text),
            f'bench-event-single-{seed_label}-{index}',
        )
        for index in range(ops + warmup)
    ]
    measure_sequential(cell, driver_factory(), operations, warmup)
    cell.wall_s = sum(cell.latencies_ms) / 1000.0


def workload_event_append_batch(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, batches: int, batch_size: int, warmup: int, seed_label: str,
) -> None:
    """2b. event.append_batch(N) — burst ingest of one agent step."""
    task = f'bench-event-batch-{seed_label}-{uuid.uuid4().hex[:8]}'
    text = sized_content(f'eventbatch:{seed_label}', 64)
    operations = []
    sequence = 0
    for batch in range(batches + warmup):
        events = []
        for _ in range(batch_size):
            events.append({
                'task_id': task,
                'sequence': sequence,
                'event': {'type': 'token_delta', 'text': text},
            })
            sequence += 1
        operations.append(
            ('event.append_batch', {'events': events},
             f'bench-event-batch-{seed_label}-{batch}'))
    driver = driver_factory()
    measure_sequential(cell, driver, operations, warmup)
    cell.wall_s = sum(cell.latencies_ms) / 1000.0
    cell.params['events_written'] = sequence
    cell.params['events_per_s'] = (
        round(cell.measured_ops * batch_size / cell.wall_s, 2)
        if cell.wall_s > 0 else None
    )


def workload_event_list_tail(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, seed_events: int, scans: int, limit: int, warmup: int,
    seed_label: str,
) -> None:
    """2c. event.list tail scans over a pre-seeded stream."""
    task = f'bench-event-tail-{seed_label}-{uuid.uuid4().hex[:8]}'
    driver = driver_factory()
    text = sized_content(f'eventtail:{seed_label}', 64)
    for sequence in range(seed_events):
        outcome = driver.execute(
            'event.append', _event_append_payload(task, sequence, text),
            f'bench-tail-seed-{seed_label}-{sequence}')
        if not outcome.ok:
            _record_error(cell, outcome)
            return
    operations = []
    for scan in range(scans + warmup):
        after = max(-1, seed_events - limit - 1 - (scan % 7))
        operations.append(
            ('event.list',
             {'task_id': task, 'after_sequence': after, 'limit': limit},
             None))
    measure_sequential(cell, driver, operations, warmup)
    cell.wall_s = sum(cell.latencies_ms) / 1000.0


def workload_queue_claim(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, rounds: int, warmup: int, seed_label: str,
) -> None:
    """3a. queue enqueue + worker claim (dequeue) + finalize rounds."""
    conv = f'bench-queue-{seed_label}-{uuid.uuid4().hex[:8]}'
    driver = driver_factory()
    outcome = driver.execute(
        'conversation.create', _conversation_create_payload(conv, 0),
        f'bench-queue-conv-{seed_label}')
    if not outcome.ok:
        _record_error(cell, outcome)
        cell.notes.append('conversation.create seed failed; cell aborted')
        return
    latencies: list[float] = []
    for round_index in range(rounds + warmup):
        queue_id = f'{conv}_q{round_index}'
        started = time.perf_counter()
        enqueue = driver.execute('queue.enqueue', {
            'conv_id': conv, 'user_id': _OWNER_ID, 'queue_id': queue_id,
            'message': {'text': 'bench', '_msgId': f'msg-{queue_id}'},
            'config': {'model': 'm'}, 'kind': 'real', 'priority': 10,
            'created_at_ms': round_index + 1,
        }, f'bench-queue-enq-{seed_label}-{round_index}')
        if not enqueue.ok:
            _record_error(cell, enqueue)
            continue
        claim = driver.execute('queue.dequeue', {
            'conv_id': conv, 'user_id': _OWNER_ID,
            'now_ms': 100 + round_index, 'lease_ms': 60_000,
        }, f'bench-queue-deq-{seed_label}-{round_index}')
        if not claim.ok:
            _record_error(cell, claim)
            continue
        finalize = driver.execute('queue.finalize', {
            'conv_id': conv, 'user_id': _OWNER_ID, 'queue_id': queue_id,
        }, f'bench-queue-fin-{seed_label}-{round_index}')
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not finalize.ok:
            _record_error(cell, finalize)
            continue
        if round_index < warmup:
            cell.warmup_ops += 1
            continue
        cell.measured_ops += 1
        if len(latencies) < _MAX_LATENCY_SAMPLES:
            latencies.append(elapsed_ms)
    cell.latencies_ms = latencies
    cell.wall_s = sum(latencies) / 1000.0
    cell.notes.append('one op = enqueue + dequeue(claim) + finalize round')


def workload_worker_job_cycle(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, rounds: int, warmup: int, seed_label: str,
) -> None:
    """3b. worker_job enqueue / claim_next / complete cycles."""
    namespace = f'benchwj{seed_label}{uuid.uuid4().hex[:6]}'
    now0 = 1_000_000
    driver = driver_factory()
    latencies: list[float] = []
    for round_index in range(rounds + warmup):
        task_id = f'{namespace}_j{round_index}'
        started = time.perf_counter()
        enqueue = driver.execute('worker_job.enqueue', {
            'task_id': task_id, 'user_id': _OWNER_ID, 'tenant_id': namespace,
            'task_kind': f'{namespace}_kind',
            'payload': {'round': round_index},
            'idempotency_key': f'{namespace}_k{round_index}',
            'priority': 100, 'now_ms': now0, 'available_at_ms': now0,
        }, f'bench-wj-enq-{seed_label}-{round_index}')
        if not enqueue.ok:
            _record_error(cell, enqueue)
            continue
        claim = driver.execute('worker_job.claim_next', {
            'worker_id': f'{namespace}_w1',
            'task_kinds': [f'{namespace}_kind'],
            'now_ms': now0, 'lease_ms': 30_000,
        }, f'bench-wj-claim-{seed_label}-{round_index}')
        if not claim.ok:
            _record_error(cell, claim)
            continue
        job = claim.value or {}
        fencing_token = job.get('fencingToken', 1)
        complete = driver.execute('worker_job.complete', {
            'task_id': task_id, 'worker_id': f'{namespace}_w1',
            'fencing_token': fencing_token, 'now_ms': now0 + 1_000,
            'terminal_status': 'succeeded',
        }, f'bench-wj-done-{seed_label}-{round_index}')
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not complete.ok:
            _record_error(cell, complete)
            continue
        if round_index < warmup:
            cell.warmup_ops += 1
            continue
        cell.measured_ops += 1
        if len(latencies) < _MAX_LATENCY_SAMPLES:
            latencies.append(elapsed_ms)
    cell.latencies_ms = latencies
    cell.wall_s = sum(latencies) / 1000.0
    cell.notes.append('one op = enqueue + claim_next + complete cycle')


def workload_task_results(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, size_label: str, content_bytes: int, writes: int, warmup: int,
    seed_label: str,
) -> None:
    """4. task_results.checkpoint writes + terminal replay_get reads."""
    driver = driver_factory()
    content = sized_content(f'taskresult:{size_label}', content_bytes)
    latencies: list[float] = []
    keys: list[str] = []
    for write in range(writes + warmup):
        key = f'bench-tr-{seed_label}-{uuid.uuid4().hex[:8]}-{write}'
        started = time.perf_counter()
        outcome = driver.execute('task_results.checkpoint', {
            'key': key,
            'value': {
                'user_id': _OWNER_ID, 'task_id': key, 'status': 'completed',
                'content': content,
            },
            'expected_version': 0,
        }, f'bench-tr-put-{seed_label}-{size_label}-{write}')
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not outcome.ok:
            _record_error(cell, outcome)
            continue
        keys.append(key)
        if write < warmup:
            cell.warmup_ops += 1
            continue
        cell.measured_ops += 1
        if len(latencies) < _MAX_LATENCY_SAMPLES:
            latencies.append(elapsed_ms)
    # terminal replay reads (include the heavy terminal payload)
    for key in keys[warmup:]:
        started = time.perf_counter()
        outcome = driver.execute('task_results.replay_get', {
            'key': key, 'user_id': _OWNER_ID,
            'include_terminal_payload': True,
        }, None)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not outcome.ok:
            _record_error(cell, outcome)
            continue
        cell.measured_ops += 1
        if len(latencies) < _MAX_LATENCY_SAMPLES:
            latencies.append(elapsed_ms)
    cell.latencies_ms = latencies
    cell.wall_s = sum(latencies) / 1000.0
    cell.notes.append(
        'measured ops mix checkpoint writes (first half) and '
        'replay_get terminal reads (second half)')


def workload_read_planes(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, conversations: int, turns_per_conv: int, pages: int, warmup: int,
    seed_label: str,
) -> None:
    """5. conversation.list pages + turn.list_delta + credential.authenticate."""
    driver = driver_factory()
    prefix = f'bench-rp-{seed_label}-{uuid.uuid4().hex[:8]}'
    conv_ids: list[str] = []
    content = sized_content(f'readplane:{seed_label}', 256)
    for ordinal in range(conversations):
        conv = f'{prefix}_c{ordinal}'
        outcome = driver.execute(
            'conversation.create', _conversation_create_payload(conv, ordinal),
            f'bench-rp-conv-{seed_label}-{ordinal}')
        if not outcome.ok:
            _record_error(cell, outcome)
            return
        conv_ids.append(conv)
        for turn in range(turns_per_conv):
            outcome = driver.execute(
                'turn.append_settled',
                _turn_append_payload(conv, f'{conv}_t{turn}', turn, content),
                f'bench-rp-turn-{seed_label}-{ordinal}-{turn}')
            if not outcome.ok:
                _record_error(cell, outcome)
                return
    secret_hash = hashlib.sha256(
        f'bench-cred-{seed_label}'.encode()).hexdigest()
    credential = driver.execute('credential.create', {
        'owner_user_id': _OWNER_ID, 'tenant_id': 'personal',
        'credential_id': f'{prefix}_cred',
        'account_user_id': '', 'name': 'Bench key', 'prefix': 'tf_bench_',
        'secret_hash': secret_hash, 'scopes': ['read'],
        'rate_limit_rpm': 60_000, 'rate_limit_tpd': 10_000_000,
        'created_at': 0.0, 'expires_at': 32_503_680_000, 'metadata': {},
    }, f'bench-rp-cred-{seed_label}')
    if not credential.ok:
        _record_error(cell, credential)
        cell.notes.append('credential.create seed failed; auth cell skipped')

    latencies: dict[str, list[float]] = {
        'conversation.list': [], 'turn.list_delta': [],
        'credential.authenticate': [],
    }

    def phase(operation: str, payload: dict[str, Any], command_id: str | None,
              index: int) -> None:
        started = time.perf_counter()
        outcome = driver.execute(operation, payload, command_id)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not outcome.ok:
            _record_error(cell, outcome)
            return
        if index < warmup:
            cell.warmup_ops += 1
            return
        cell.measured_ops += 1
        samples = latencies[operation]
        if len(samples) < _MAX_LATENCY_SAMPLES:
            samples.append(elapsed_ms)

    for page in range(pages + warmup):
        ids = [conv_ids[(page + offset) % len(conv_ids)]
               for offset in range(min(10, len(conv_ids)))]
        phase('conversation.list', {
            'user_id': _OWNER_ID, 'ids': ids,
            'order_by': 'updated_at_desc', 'include_messages': False,
        }, None, page)
    for scan in range(pages + warmup):
        conv = conv_ids[scan % len(conv_ids)]
        phase('turn.list_delta', {
            'conversation_id': conv, 'user_id': _OWNER_ID,
            'since_ms': _TURN_TS + 1000 * (scan % (turns_per_conv + 1)),
        }, None, scan)
    if credential.ok:
        for auth in range(pages + warmup):
            phase('credential.authenticate', {
                'secret_hash': secret_hash, 'now': 1 + auth,
            }, f'bench-rp-auth-{seed_label}-{auth}', auth)

    flat: list[float] = []
    for operation, samples in latencies.items():
        flat.extend(samples)
        if samples:
            ordered = sorted(samples)
            cell.params[f'p50_ms:{operation}'] = round(
                ordered[len(ordered) // 2], 3)
            cell.params[f'p99_ms:{operation}'] = round(
                ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))], 3)
    cell.latencies_ms = flat
    cell.wall_s = sum(flat) / 1000.0
    cell.notes.append(
        'mixed read-plane ops; per-operation p50/p99 recorded under params')


def workload_group_commit(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, writers: int, ops_per_writer: int, warmup_per_writer: int,
    seed_label: str,
) -> None:
    """6. Group-commit scaling: concurrent event.append writers."""
    text = sized_content(f'groupcommit:{seed_label}', 64)

    def per_thread(driver: Any, writer: int, op_index: int) -> Outcome:
        task = f'bench-gc-{seed_label}-w{writer}'
        return driver.execute(
            'event.append', _event_append_payload(task, op_index, text),
            f'bench-gc-{seed_label}-w{writer}-{op_index}',
        )

    measure_concurrent(
        cell, driver_factory, per_thread, writers, ops_per_writer,
        warmup_per_writer,
    )


def workload_footprint(
    cell: CellResult, driver_factory: Callable[[], Any],
    *, duration_s: float, writers: int, seed_label: str,
) -> None:
    """7. Sustained mixed load; RSS/fd/dirt witnesses come from run_cell."""
    content = sized_content(f'footprint:{seed_label}', KIB)
    stop_at = time.monotonic() + duration_s
    counters = [0] * writers
    lock = threading.Lock()

    def worker(writer: int) -> None:
        driver = driver_factory()
        conv = f'bench-fp-{seed_label}-w{writer}-{uuid.uuid4().hex[:6]}'
        task = f'bench-fp-ev-{seed_label}-w{writer}'
        try:
            while time.monotonic() < stop_at:
                index = counters[writer]
                outcome = driver.execute(
                    'turn.append_settled',
                    _turn_append_payload(
                        conv, f'{conv}_t{index}', index, content),
                    f'bench-fp-turn-{seed_label}-w{writer}-{index}')
                if not outcome.ok:
                    with lock:
                        _record_error(cell, outcome)
                    return
                outcome = driver.execute(
                    'event.append',
                    _event_append_payload(task, index, 'x' * 32),
                    f'bench-fp-ev-{seed_label}-w{writer}-{index}')
                if not outcome.ok:
                    with lock:
                        _record_error(cell, outcome)
                    return
                counters[writer] += 1
                with lock:
                    cell.measured_ops += 2
        finally:
            driver.close()

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(worker, index) for index in range(writers)]
        for future in futures:
            future.result()
    cell.wall_s = time.perf_counter() - started
    cell.notes.append(
        'sustained turn.append_settled(1KiB) + event.append mixed load; '
        'rss_bytes/fd_count_max/data_bytes_end are the cell output')


# ── Suite definition ─────────────────────────────────────────────────────
# Scale factors: (profile -> params).  Every cell is bounded; fixture data is
# removed after each authority finishes its cells unless --keep-data.

def _suite(profile: str) -> list[dict[str, Any]]:
    smoke = profile == 'smoke'
    factor = 1 if smoke else 10

    def cells() -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        # 1. turn ingestion
        turn_sizes = [
            ('small', 4 * KIB, 30 * factor, 1),
            ('256KiB-blob-spill', 256 * KIB, 6 * factor, 1),
            ('2MiB-shared-ceiling', 1_500_000, max(2, factor // 2), 1),
        ]
        for label, size, ops, _ in turn_sizes:
            plan.append({
                'name': f'turn_ingest/{label}/sequential',
                'workload': 'turn_ingest',
                'fn': workload_turn_ingest,
                'params': {
                    'size_label': label, 'content_bytes': size,
                    'writers': 1, 'ops_per_writer': ops,
                    'warmup_per_writer': max(1, ops // 10),
                },
            })
        # Concurrent op counts stay bounded even in the full profile: at
        # 64-way the measured engines sustain tens of ops/s, so 100+
        # ops/writer would push one cell past several minutes without
        # adding evidence.
        concurrent_factor = 1 if smoke else 3
        for writers in (16, 64):
            for label, size, ops in (
                    ('small', 4 * KIB, 10 * concurrent_factor),
                    ('256KiB-blob-spill', 256 * KIB, 3 * concurrent_factor)):
                plan.append({
                    'name': f'turn_ingest/{label}/concurrent-{writers}',
                    'workload': 'turn_ingest',
                    'fn': workload_turn_ingest,
                    'params': {
                        'size_label': label, 'content_bytes': size,
                        'writers': writers, 'ops_per_writer': ops,
                        'warmup_per_writer': 1,
                    },
                })
            # 1.5 MiB concurrency is 16-way only: larger documents exceed
            # the legacy receipt echo budget (4 MiB decoded, request +
            # response), so no shared larger class exists; the probe cell
            # documents each authority's own ceiling.  64-way would push
            # ~96 MiB of in-flight client payload per op round, fine, but
            # op counts stay bounded.
            # personal-host budget this suite measures within.
            if writers == 16:
                plan.append({
                    'name': 'turn_ingest/2MiB-shared-ceiling/concurrent-16',
                    'workload': 'turn_ingest',
                    'params': {
                        'size_label': '2MiB-shared-ceiling',
                        'content_bytes': 1_500_000,
                        'writers': 16,
                        'ops_per_writer': max(1, concurrent_factor // 2),
                        'warmup_per_writer': 0,
                    },
                    'fn': workload_turn_ingest,
                })
        plan.append({
            'name': 'turn_ingest/size_ceiling_probe',
            'workload': 'turn_ingest',
            'fn': workload_turn_size_ceiling_probe,
            'params': {'probe_sizes': [1_500_000, 3_500_000, 8_000_000]},
        })
        # 2. event streams
        plan.append({
            'name': 'event_stream/append_single',
            'workload': 'event_stream',
            'fn': workload_event_append_single,
            'params': {'ops': 50 * factor, 'warmup': 5},
        })
        plan.append({
            'name': 'event_stream/append_batch_500',
            'workload': 'event_stream',
            'fn': workload_event_append_batch,
            'params': {
                'batches': 4 * factor, 'batch_size': 500, 'warmup': 1,
            },
        })
        plan.append({
            'name': 'event_stream/list_tail_scan',
            'workload': 'event_stream',
            'fn': workload_event_list_tail,
            'params': {
                'seed_events': 200 * factor, 'scans': 20 * factor,
                'limit': 50, 'warmup': 3,
            },
        })
        # 3. queue / claim
        plan.append({
            'name': 'queue_claim/enqueue_claim_finalize',
            'workload': 'queue_claim',
            'fn': workload_queue_claim,
            'params': {'rounds': 20 * factor, 'warmup': 3},
        })
        plan.append({
            'name': 'worker_job/enqueue_claim_complete',
            'workload': 'queue_claim',
            'fn': workload_worker_job_cycle,
            'params': {'rounds': 20 * factor, 'warmup': 3},
        })
        # 4. task checkpoints (bounded: <= ~200 MiB per authority per cell)
        for label, size, writes in (
                ('1MiB', MIB, 5 * factor),
                ('16MiB', 16 * MIB, max(2, factor // 2)),
                ('64MiB', 66_000_000, 2 if smoke else 3)):
            plan.append({
                'name': f'task_results/checkpoint_replay/{label}',
                'workload': 'task_results',
                'fn': workload_task_results,
                'params': {
                    'size_label': label, 'content_bytes': size,
                    'writes': writes, 'warmup': 1,
                },
            })
        # 5. read planes
        plan.append({
            'name': 'read_planes/list_delta_auth',
            'workload': 'read_planes',
            'fn': workload_read_planes,
            'params': {
                'conversations': 10 if smoke else 50,
                'turns_per_conv': 4,
                'pages': 20 * factor, 'warmup': 3,
            },
        })
        # 6. group-commit scaling
        for writers in ((1, 8) if smoke else (1, 8, 32, 64)):
            plan.append({
                'name': f'group_commit/writers-{writers}',
                'workload': 'group_commit',
                'fn': workload_group_commit,
                'params': {
                    'writers': writers,
                    'ops_per_writer': 40 * concurrent_factor
                    // max(1, writers // 8),
                    'warmup_per_writer': 2,
                },
            })
        # 7. resource footprint
        plan.append({
            'name': 'footprint/sustained_mixed',
            'workload': 'footprint',
            'fn': workload_footprint,
            'params': {
                'duration_s': 3.0 if smoke else 15.0,
                'writers': 4 if smoke else 8,
            },
        })
        return plan

    return cells()


# ── Runner ───────────────────────────────────────────────────────────────
def _run_authority_cells(
    authority: Any,
    mount: str,
    plan: list[dict[str, Any]],
    seed: int,
    only: set[str] | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in plan:
        if only and spec['workload'] not in only and spec['name'] not in only:
            continue
        params = dict(spec['params'])
        params['seed_label'] = f'{seed}-{mount}'

        def body(cell: CellResult, factory: Callable[[], Any],
                 _spec: dict[str, Any] = spec, _params: dict[str, Any] = params,
                 ) -> None:
            _spec['fn'](cell, factory, **_params)

        recorded = dict(params)
        recorded.pop('seed_label', None)
        cell = run_cell(
            spec['name'], spec['workload'], authority, mount, recorded, body)
        results.append(cell.to_json())
        status = 'ok' if not cell.errors else f'errors={cell.errors}'
        print(
            f'  [{authority.kind}/{mount}] {cell.name}: '
            f'{cell.measured_ops} ops in {cell.wall_s:.2f}s '
            f'({cell.to_json()["ops_per_s"]} ops/s) {status}',
            flush=True,
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--profile', choices=('smoke', 'full'),
                        default='smoke')
    parser.add_argument('--mount', choices=('native', 'fuse', 'both'),
                        default='native')
    parser.add_argument(
        '--fuse-root', default=None,
        help='directory for FUSE-resident authorities '
             '(default: a temp dir under the repository worktree)')
    parser.add_argument('--seed', type=int, default=_DEFAULT_SEED)
    parser.add_argument('--output', default=None)
    parser.add_argument('--workload', action='append', default=None,
                        help='restrict to a workload/cell name (repeatable)')
    parser.add_argument('--keep-data', action='store_true')
    parser.add_argument('--authority', choices=('tofudb', 'legacy', 'both'),
                        default='both')
    args = parser.parse_args(argv)

    mounts = ['native', 'fuse'] if args.mount == 'both' else [args.mount]
    only = set(args.workload) if args.workload else None
    plan = _suite(args.profile)

    report: dict[str, Any] = {
        'suite': 'tofudb-bench/v1',
        'seed': args.seed,
        'profile': args.profile,
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'git_commit': _git_commit(),
        'environment': {
            'python': platform.python_version(),
            'platform': platform.platform(),
            'cpu_count': os.cpu_count(),
            'memory_total_bytes': _memory_total_bytes(),
            'mounts': {},
        },
        'cells': [],
    }

    for mount in mounts:
        if mount == 'native':
            root = Path(tempfile.mkdtemp(prefix='tofudb-bench-native-'))
        else:
            fuse_base = (
                Path(args.fuse_root) if args.fuse_root else _PROJECT_ROOT)
            root = Path(tempfile.mkdtemp(
                prefix='tofudb-bench-fuse-', dir=fuse_base))
        report['environment']['mounts'][mount] = {
            'path': str(root),
            'fstype': _mount_type(root),
        }
        print(f'== mount={mount} root={root} '
              f'fstype={report["environment"]["mounts"][mount]["fstype"]}',
              flush=True)
        try:
            authorities: list[Any] = []
            if args.authority in ('tofudb', 'both'):
                authorities.append(TofuDbAuthority(root))
            if args.authority in ('legacy', 'both'):
                authorities.append(LegacyAuthority(root))
            for authority in authorities:
                if authority.kind == 'tofudb':
                    report['environment']['tofudb'] = authority.witness()
                else:
                    report['environment'].setdefault(
                        'legacy', authority.witness())
                print(f'-- authority={authority.kind} pid={authority.pid}',
                      flush=True)
                try:
                    report['cells'].extend(_run_authority_cells(
                        authority, mount, plan, args.seed, only))
                finally:
                    authority.stop()
        finally:
            if not args.keep_data:
                shutil.rmtree(root, ignore_errors=True)

    output = args.output
    if output is None:
        output = str(_PROJECT_ROOT / 'benchmarks' / (
            f'tofudb_bench_{args.profile}_'
            f'{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}.json'))
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + '\n')
    print(f'wrote {output_path} ({len(report["cells"])} cells)', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
