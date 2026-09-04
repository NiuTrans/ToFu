"""Fast-path authority: a measured-local write front with a durable shadow.

2026-08-20 root fix for the recurring "Storage writer acquisition timed out"
class.  The durable authority historically lived directly on the deployment's
persistent filesystem (a network mount in the flagship deployment), so every
commit paid that mount's fsync latency — 45ms healthy, SECONDS under cgroup
memory pressure — and the single writer's throughput ceiling collapsed to
1/fsync_latency.  The fast path relocates the WRITE FRONT to a measured-fast
local filesystem while the durable truth remains on the persistent data dir
as a continuously-shipped shadow (snapshot + WAL prefix segments).

Design invariants (deployment-topology agnostic by construction):

1. FAIL-CLOSED ACTIVATION, OPT-IN.  The mode defaults to ``off`` and only an
   explicit ``TOFU_STORAGE_FASTPATH=auto|required`` enables probing (the
   2026-08-20 incident: auto-activation adopted a certification test's
   authority from the shared /tmp front and served 3 test conversations while
   the real 424 GiB authority sat untouched).  Even when enabled, activation
   requires EVERY probe to pass: candidate dir creatable/writable; candidate
   on a DIFFERENT filesystem than the data dir (same-device relocation is
   pointless); scratch WAL recovery semantics verified on the candidate;
   sufficient free space; and a measured commit-latency benchmark showing a
   decisive improvement over the data dir.  Any doubt → the authority stays
   on the data dir, exactly as before.  Deployments without a usable local
   disk (permissions, topology) simply never activate.
2. THE SHADOW IS ALWAYS CONSISTENT.  Only the shipper checkpoints the local
   WAL (autocheckpoint disabled, close-checkpoint disabled where the driver
   allows), so each shadow generation's WAL is a byte-prefix of the local
   WAL since that generation's snapshot.  Shipping appends at frame
   boundaries, fsyncs, then durably replaces the manifest.  Recovery =
   snapshot + native SQLite WAL replay — semantics SQLite itself guarantees.
3. BOUNDED, OBSERVABLE RPO.  Commit acknowledgement never waits on the
   network filesystem; a local-disk loss forfeits at most the unshipped tail
   (ship lag, exported in metrics).  A local disk that SURVIVES a crash loses
   nothing: boot reconciliation forward-ships the tail before serving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import statistics
import tempfile
import threading
import time
from typing import Any

from lib.log import get_logger
from lib.storage.startup_control import StartupProgressCallback
from lib.storage_sidecar.durability import (
    fsync_directory, fsync_file, write_json_durable,
)
from lib.storage_sidecar.reclaim_policy import copy_capacity_requirement


logger = get_logger('tofu.storage.sidecar.fastpath')

MODE_OFF = 'off'
MODE_AUTO = 'auto'
MODE_REQUIRED = 'required'

SHADOW_DIRNAME = 'fastpath-shadow'
LOCAL_MANIFEST_NAME = 'tofu-fastpath.json'
SHADOW_MANIFEST_NAME = 'manifest.json'
SNAPSHOT_NAME = 'snapshot.sqlite3'
SHADOW_WAL_NAME = 'shadow.wal'

# Activation thresholds.  The benchmark demands a DECISIVE win — a marginal
# one does not justify the operational surface; the speedup threshold is the
# data-dir median divided by the candidate median.  Operators/tests may tune
# it via TOFU_STORAGE_FASTPATH_MIN_SPEEDUP (0 = activate on any measurable
# win — the benchmark is still recorded honestly either way).
_BENCHMARK_WRITES = 16
_BENCHMARK_PAYLOAD_BYTES = 4096
_DEFAULT_MIN_SPEEDUP = 3.0
_MIN_FREE_BYTES = 2 * 1024 ** 3
_WAL_FRAME_HEADER_BYTES = 24
_SEED_STATE_VERSION = 2
_SEED_STATE_SUFFIX = '.seed-state.json'
_SEED_PROVENANCE_SUFFIX = '.seed-provenance.json'
_SEED_COPY_CHECKPOINT_BYTES = 256 * 1024 ** 2
_SEED_COPY_BUFFER_BYTES = 8 * 1024 ** 2
_STARTUP_PROGRESS_INTERVAL_SECONDS = 1.0
_STARTUP_HEARTBEAT_SECONDS = 5.0
_FINGERPRINT_FULL_HASH_MAX_BYTES = 1024 ** 2
_FINGERPRINT_SAMPLE_BYTES = 16 * 1024
_FINGERPRINT_SAMPLE_COUNT = 32


def _min_speedup(environ: Any) -> float:
    raw = str(environ.get('TOFU_STORAGE_FASTPATH_MIN_SPEEDUP') or '').strip()
    if not raw:
        return _DEFAULT_MIN_SPEEDUP
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError('invalid TOFU_STORAGE_FASTPATH_MIN_SPEEDUP') from exc
    if not 0.0 <= value <= 1000.0:
        raise RuntimeError('TOFU_STORAGE_FASTPATH_MIN_SPEEDUP out of bounds')
    return value


@dataclass(slots=True)
class FastpathDecision:
    active: bool
    reason: str
    mode: str
    local_dir: Path | None = None
    shadow_dir: Path | None = None
    benchmark: dict[str, Any] = field(default_factory=dict)


def _mode_from_environment(environ: Any) -> str:
    # Default OFF — relocation is opt-in.  The 2026-08-20 incident proved
    # auto-by-default unsafe: any sidecar on the host (test harnesses
    # included) activated on the same uid-shared /tmp front, and a later
    # production boot adopted that foreign authority.  Operators re-enable
    # with an explicit TOFU_STORAGE_FASTPATH=auto|required.
    raw = str(environ.get('TOFU_STORAGE_FASTPATH') or MODE_OFF).strip().lower()
    if raw not in {MODE_OFF, MODE_AUTO, MODE_REQUIRED}:
        raise RuntimeError('TOFU_STORAGE_FASTPATH must be off, auto, or required')
    return raw


def _data_dir_key(data_dir: Path) -> str:
    """Stable per-data-dir key namespacing the shared front locations.

    The auto candidates live in per-user locations (XDG state, /tmp) that
    every deployment on the host would otherwise SHARE — the exact
    2026-08-20 incident vector, where a certification test's front was
    adopted by the production sidecar.  Keying by the resolved data dir
    makes cross-deployment adoption structurally impossible.
    """
    try:
        resolved = str(data_dir.resolve())
    except OSError:
        resolved = str(data_dir)
    return hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:12]


def _candidate_dirs(data_dir: Path, environ: Any) -> list[Path]:
    """Ordered fast-path candidates, most explicit first.

    Only paths an operator or the platform clearly owns locally are
    considered; the probes below — never this list — decide usability, so a
    missing/unwritable/shared candidate costs one skipped entry.
    """
    candidates: list[Path] = []
    explicit = str(environ.get('TOFU_STORAGE_FASTPATH_DIR') or '').strip()
    if explicit:
        # An explicit operator choice skips the same-device short-circuit
        # below: the measured benchmark remains the honest gate (a same-
        # device candidate measures ~1x and simply never activates).
        candidates.append(Path(explicit))
        return candidates
    key = _data_dir_key(data_dir)
    state_home = str(environ.get('XDG_STATE_HOME') or '').strip()
    home = Path.home()
    if state_home:
        candidates.append(Path(state_home) / 'tofu-fastpath' / key)
    elif str(home) not in {'', '/'}:
        candidates.append(home / '.local' / 'state' / 'tofu-fastpath' / key)
    candidates.append(
        Path(tempfile.gettempdir()) / f'tofu-fastpath-{os.getuid()}-{key}')
    # Never consider the data dir's own filesystem hierarchy: relocation
    # within the same mount is pure overhead.
    return [c for c in candidates if not _same_filesystem_root(c, data_dir)]


def _same_filesystem_root(candidate: Path, data_dir: Path) -> bool:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(data_dir.resolve())
        return True
    except (ValueError, OSError):
        return False


def _same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True  # doubt → treat as same, probe fails closed later


def _probe_writable_wal_semantics(directory: Path) -> None:
    """Prove create/write/fsync/WAL-recovery semantics on the candidate.

    Mirrors SQLiteBackend._scratch_recovery_preflight: a committed row must
    survive a close/reopen with integrity intact — the minimal contract the
    shipper's replay-based recovery relies on.
    """
    probe = directory / '.fastpath-probe.sqlite3'
    for suffix in ('', '-wal', '-shm'):
        probe.with_name(probe.name + suffix).unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(probe, isolation_level=None)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('CREATE TABLE probe(value TEXT NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT INTO probe(value) VALUES (?)', ('committed',))
        connection.commit()
        connection.close()
        reopened = sqlite3.connect(probe)
        row = reopened.execute('SELECT value FROM probe').fetchone()
        reopened.close()
        if row != ('committed',):
            raise RuntimeError('candidate WAL recovery probe failed')
    finally:
        for suffix in ('', '-wal', '-shm'):
            probe.with_name(probe.name + suffix).unlink(missing_ok=True)


def _benchmark_fsync_ms(directory: Path, writes: int = _BENCHMARK_WRITES,
                        payload_bytes: int = _BENCHMARK_PAYLOAD_BYTES) -> float:
    """Median write+fsync latency — the quantity commit latency rides on."""
    probe = directory / '.fastpath-benchmark.bin'
    payload = os.urandom(payload_bytes)
    samples: list[float] = []
    try:
        with probe.open('wb') as stream:
            for _ in range(writes):
                start = time.perf_counter()
                stream.seek(0)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        probe.unlink(missing_ok=True)
    return statistics.median(samples)


def _copy_required_free_bytes(source_bytes: int) -> int:
    return copy_capacity_requirement(
        source_bytes,
        minimum_free_bytes=_MIN_FREE_BYTES,
    )['required_free_bytes']


def _classic_source_bytes(classic_db: Path) -> int:
    total = classic_db.stat().st_size
    classic_wal = classic_db.with_name(classic_db.name + '-wal')
    if classic_wal.is_file():
        total += classic_wal.stat().st_size
    return total


def _require_copy_capacity(
    destination_dir: Path,
    source_bytes: int,
    *,
    purpose: str,
    reusable_bytes: int = 0,
) -> None:
    required = _copy_required_free_bytes(source_bytes)
    free = shutil.disk_usage(destination_dir).free
    reusable = max(0, int(reusable_bytes))
    if free + reusable < required:
        raise RuntimeError(
            f'{purpose} needs {required} free bytes but {destination_dir} has '
            f'{free} free and {reusable} in owned resumable seed files; '
            'refusing to fill the local filesystem')


def _seed_paths(local_db: Path) -> tuple[Path, Path, Path]:
    temporary = local_db.with_name(local_db.name + '.seed-tmp')
    temporary_wal = temporary.with_name(temporary.name + '-wal')
    state_path = local_db.with_name(local_db.name + _SEED_STATE_SUFFIX)
    return temporary, temporary_wal, state_path


def _owned_seed_temporary_bytes(local_db: Path) -> int:
    """Return reclaimable bytes from exact private seed artifact names."""
    temporary, temporary_wal, _state_path = _seed_paths(local_db)
    total = 0
    for path in (temporary, temporary_wal):
        try:
            status = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode):
            total += max(0, int(status.st_size))
    return total


def decide(data_dir: Path, *, environ: Any = os.environ,
           benchmark: Any = _benchmark_fsync_ms) -> FastpathDecision:
    """Probe every candidate and decide where the write front lives.

    Pure decision logic (filesystem probes aside): no authority bytes move
    here — that is ``reconcile``'s job, and only after ``active`` is True.
    """
    mode = _mode_from_environment(environ)
    if mode == MODE_OFF:
        return FastpathDecision(False, 'disabled by TOFU_STORAGE_FASTPATH=off', mode)
    if not data_dir.is_dir():
        return FastpathDecision(False, 'data dir missing', mode)

    min_speedup = _min_speedup(environ)
    explicit_raw = str(environ.get('TOFU_STORAGE_FASTPATH_DIR') or '').strip()
    explicit = Path(explicit_raw) if explicit_raw else None
    data_median_ms: float | None = None
    best_bench: dict[str, Any] = {}
    for candidate in _candidate_dirs(data_dir, environ):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.info('[fastpath] %s not creatable: %s', candidate, exc)
            continue
        if candidate != explicit and _same_device(candidate, data_dir):
            logger.info('[fastpath] %s shares the data dir filesystem — skipped',
                        candidate)
            continue
        try:
            _probe_writable_wal_semantics(candidate)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            logger.info('[fastpath] %s failed the WAL semantics probe: %s',
                        candidate, exc)
            continue
        free = shutil.disk_usage(candidate).free
        local_db = candidate / 'tofu.db'
        classic_db = data_dir / 'tofu.db'
        reusable_seed_bytes = (
            _owned_seed_temporary_bytes(local_db)
            if not local_db.exists() and classic_db.is_file()
            else 0
        )
        effective_free = free + reusable_seed_bytes
        if effective_free < _MIN_FREE_BYTES:
            logger.info('[fastpath] %s has only %.1f GiB free — skipped',
                        candidate, effective_free / 1024 ** 3)
            continue
        # First activation seeds byte-for-byte before SQLite can compact.  A
        # fixed 2 GiB check allowed a large classic authority to fill the local
        # filesystem mid-copy.  Reject that topology during the decision, then
        # repeat the check at the copy boundary to close the TOCTOU window.
        if not local_db.is_file() and classic_db.is_file():
            required = _copy_required_free_bytes(
                _classic_source_bytes(classic_db))
            if effective_free < required:
                logger.info(
                    '[fastpath] %s has %.1f GiB usable (including owned seed '
                    'progress) but first activation '
                    'requires %.1f GiB for the classic authority — skipped',
                    candidate, effective_free / 1024 ** 3,
                    required / 1024 ** 3)
                continue
        if data_median_ms is None:
            data_median_ms = benchmark(data_dir)
        candidate_median_ms = benchmark(candidate)
        speedup = (data_median_ms / candidate_median_ms
                   if candidate_median_ms > 0 else float('inf'))
        bench = {
            'data_dir_median_fsync_ms': round(data_median_ms, 3),
            'candidate_median_fsync_ms': round(candidate_median_ms, 3),
            'speedup': round(speedup, 2),
        }
        if not best_bench or bench['speedup'] > best_bench['speedup']:
            best_bench = bench
        if speedup < min_speedup:
            logger.info('[fastpath] %s measured %.2fx the data dir fsync '
                        '(need %.1fx) — skipped', candidate, speedup,
                        min_speedup)
            continue
        logger.info('[fastpath] ACTIVATED on %s: fsync %.2fms vs data dir '
                    '%.2fms (%.1fx, required %.1fx)', candidate,
                    candidate_median_ms, data_median_ms, speedup, min_speedup)
        return FastpathDecision(
            True, 'activated', mode, local_dir=candidate,
            shadow_dir=data_dir / SHADOW_DIRNAME, benchmark=bench)

    reason = 'no candidate passed every probe'
    if mode == MODE_REQUIRED:
        raise RuntimeError(
            f'TOFU_STORAGE_FASTPATH=required but {reason}; refusing to boot '
            'a slow authority the operator explicitly rejected')
    logger.info('[fastpath] %s — authority stays on the data dir', reason)
    return FastpathDecision(
        False, reason, mode,
        benchmark=best_bench or ({'data_dir_median_fsync_ms':
                                  round(data_median_ms, 3)}
                                 if data_median_ms is not None else {}))


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    import json
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        # Corrupt is NOT absent: a manifest that cannot be parsed disables
        # the lineage guards keyed on it (uuid cross-check, shadow_dir
        # identity).  That must be loud, not a silent None.
        logger.warning('[fastpath] manifest %s exists but is unreadable (%s) '
                       '— treating as absent; lineage guards keyed on it are '
                       'disabled for this boot', path, exc)
        return None
    if not isinstance(value, dict):
        logger.warning('[fastpath] manifest %s is not a JSON object — treating '
                       'as absent', path)
        return None
    return value


def read_local_manifest(local_dir: Path) -> dict[str, Any] | None:
    return _load_json(local_dir / LOCAL_MANIFEST_NAME)


def read_shadow_manifest(shadow_dir: Path) -> dict[str, Any] | None:
    return _load_json(shadow_dir / SHADOW_MANIFEST_NAME)


def write_local_manifest(local_dir: Path, payload: dict[str, Any]) -> None:
    write_json_durable(local_dir / LOCAL_MANIFEST_NAME, payload)


def write_shadow_manifest(shadow_dir: Path, payload: dict[str, Any]) -> None:
    write_json_durable(shadow_dir / SHADOW_MANIFEST_NAME, payload)


def shadow_paths(shadow_dir: Path) -> tuple[Path, Path]:
    return shadow_dir / SNAPSHOT_NAME, shadow_dir / SHADOW_WAL_NAME


def local_front_matches_shadow(local_db: Path, data_dir: Path) -> bool:
    """Whether one local ``tofu.db`` proves lineage to this data directory.

    This is a read-only discovery predicate for diagnostics. A filename match
    alone is never enough: both manifests must name the same authority UUID and
    the local manifest must point at this deployment's exact shadow directory.
    """
    local_db = Path(local_db).expanduser()
    data_dir = Path(data_dir).expanduser()
    if local_db.name != 'tofu.db' or not local_db.is_file():
        return False
    shadow_dir = data_dir / SHADOW_DIRNAME
    local_manifest = read_local_manifest(local_db.parent)
    shadow_manifest = read_shadow_manifest(shadow_dir)
    if not local_manifest or not shadow_manifest:
        return False
    local_uuid = str(local_manifest.get('authority_uuid') or '')
    shadow_uuid = str(shadow_manifest.get('authority_uuid') or '')
    if not local_uuid or local_uuid != shadow_uuid:
        return False
    recorded_shadow = str(local_manifest.get('shadow_dir') or '').strip()
    if not recorded_shadow:
        return False
    try:
        return Path(recorded_shadow).expanduser().resolve() == shadow_dir.resolve()
    except OSError:
        return False


def matching_local_fronts(
    data_dir: Path,
    *,
    environ: Any = os.environ,
) -> list[Path]:
    """Return bounded fastpath candidates whose manifests prove ownership.

    Candidate enumeration is the same deployment-keyed policy used at startup,
    but this function performs no mkdir, benchmark, mutation, or recovery. It
    exists so offline readers do not duplicate private fastpath layout rules.
    """
    matches: list[Path] = []
    seen: set[str] = set()
    for candidate in _candidate_dirs(Path(data_dir), environ):
        local_db = candidate / 'tofu.db'
        if not local_front_matches_shadow(local_db, Path(data_dir)):
            continue
        try:
            key = str(local_db.resolve())
        except OSError:
            key = str(local_db)
        if key not in seen:
            seen.add(key)
            matches.append(local_db)
    return matches


def _bounded_content_witness(path: Path, size: int) -> str:
    """Hash all small sources or bounded, deterministic large-file samples."""
    digest = hashlib.sha256()
    digest.update(str(size).encode('ascii'))
    with path.open('rb') as stream:
        if size <= _FINGERPRINT_FULL_HASH_MAX_BYTES:
            while payload := stream.read(_FINGERPRINT_SAMPLE_BYTES):
                digest.update(payload)
            return digest.hexdigest()
        last_offset = max(0, size - _FINGERPRINT_SAMPLE_BYTES)
        offsets = {
            last_offset * index // (_FINGERPRINT_SAMPLE_COUNT - 1)
            for index in range(_FINGERPRINT_SAMPLE_COUNT)
        }
        for offset in sorted(offsets):
            stream.seek(offset)
            digest.update(offset.to_bytes(8, 'big'))
            digest.update(stream.read(_FINGERPRINT_SAMPLE_BYTES))
    return digest.hexdigest()


def _source_fingerprint(path: Path) -> dict[str, Any]:
    status = path.stat()
    witness = _bounded_content_witness(path, int(status.st_size))
    final_status = path.stat()
    before = (
        status.st_dev, status.st_ino, status.st_size,
        status.st_mtime_ns, status.st_ctime_ns,
    )
    after = (
        final_status.st_dev, final_status.st_ino, final_status.st_size,
        final_status.st_mtime_ns, final_status.st_ctime_ns,
    )
    if before != after:
        raise RuntimeError(
            f'fastpath source changed while fingerprinting {path.name}')
    return {
        'path': str(path.resolve()),
        'device': int(final_status.st_dev),
        'inode': int(final_status.st_ino),
        'size': int(final_status.st_size),
        'mtime_ns': int(final_status.st_mtime_ns),
        'ctime_ns': int(final_status.st_ctime_ns),
        'content_witness_sha256': witness,
    }


def _classic_source_fingerprint(classic_db: Path) -> dict[str, Any]:
    classic_wal = classic_db.with_name(classic_db.name + '-wal')
    wal_fingerprint = None
    if classic_wal.is_file() and classic_wal.stat().st_size > 0:
        wal_fingerprint = _source_fingerprint(classic_wal)
    return {
        'database': _source_fingerprint(classic_db),
        'wal': wal_fingerprint,
    }


def _load_seed_state(state_path: Path) -> dict[str, Any] | None:
    try:
        state_status = state_path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(state_status.st_mode):
        logger.warning(
            '[fastpath] resumable seed state %s is not a regular file; '
            'discarding only the private seed artifacts', state_path)
        return None
    try:
        value = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        logger.warning(
            '[fastpath] resumable seed state %s is unreadable (%s); '
            'discarding only the private partial copy', state_path, exc)
        return None
    return value if isinstance(value, dict) else None


def _write_seed_state(state_path: Path, state: dict[str, Any]) -> None:
    # ``write_json_durable`` intentionally uses O_EXCL for its replacement.
    # A SIGKILL may leave that exact private replacement behind; it is never a
    # committed checkpoint and must not block the next durable update.
    state_path.with_name(state_path.name + '.new').unlink(missing_ok=True)
    write_json_durable(state_path, state)


def _remove_seed_state(state_path: Path) -> None:
    changed = False
    for path in (state_path, state_path.with_name(state_path.name + '.new')):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        else:
            changed = True
    if changed:
        fsync_directory(state_path.parent)


def _seed_source_sizes(source: dict[str, Any]) -> tuple[int, int]:
    database = source.get('database')
    wal = source.get('wal')
    if not isinstance(database, dict):
        raise RuntimeError('fastpath seed source fingerprint is invalid')
    database_bytes = int(database.get('size', -1))
    wal_bytes = int(wal.get('size', -1)) if isinstance(wal, dict) else 0
    if database_bytes < 0 or wal_bytes < 0:
        raise RuntimeError('fastpath seed source size is invalid')
    return database_bytes, wal_bytes


def _validated_seed_resume(
    state: dict[str, Any] | None,
    source: dict[str, Any],
    temporary: Path,
    temporary_wal: Path,
) -> tuple[int, int] | None:
    if (not state
            or state.get('version') != _SEED_STATE_VERSION
            or state.get('source') != source):
        return None
    database_size, wal_size = _seed_source_sizes(source)
    database_bytes = state.get('database_bytes')
    wal_bytes = state.get('wal_bytes')
    if (isinstance(database_bytes, bool)
            or not isinstance(database_bytes, int)
            or isinstance(wal_bytes, bool)
            or not isinstance(wal_bytes, int)
            or not 0 <= database_bytes <= database_size
            or not 0 <= wal_bytes <= wal_size):
        return None
    for path, durable_bytes in (
        (temporary, database_bytes),
        (temporary_wal, wal_bytes),
    ):
        try:
            status = path.lstat()
        except FileNotFoundError:
            if durable_bytes == 0:
                continue
            return None
        if not stat.S_ISREG(status.st_mode) or status.st_size < durable_bytes:
            return None
    return database_bytes, wal_bytes


def _clear_seed_temporary(local_db: Path) -> None:
    temporary, temporary_wal, state_path = _seed_paths(local_db)
    changed = False
    for path in (
        temporary,
        temporary_wal,
        state_path,
        state_path.with_name(state_path.name + '.new'),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        else:
            changed = True
    if changed:
        fsync_directory(local_db.parent)


def _copy_file_checkpointed(
    source: Path,
    destination: Path,
    *,
    expected_bytes: int,
    durable_bytes: int,
    checkpoint: Any,
    progress: Any = None,
) -> None:
    """Copy from one durable offset, fsyncing bounded progress checkpoints."""
    mode = 'r+b' if destination.exists() else 'w+b'
    with source.open('rb') as source_stream, destination.open(mode) as target:
        if target.seek(0, os.SEEK_END) < durable_bytes:
            raise RuntimeError('resumable seed file is shorter than its checkpoint')
        target.truncate(durable_bytes)
        offset = durable_bytes
        target.seek(offset)
        use_sendfile = hasattr(os, 'sendfile')
        unsupported_sendfile_errors = {
            errno.EINVAL,
            errno.ENOSYS,
            errno.EXDEV,
            getattr(errno, 'EOPNOTSUPP', errno.EINVAL),
            getattr(errno, 'ENOTSOCK', errno.EINVAL),
        }
        while offset < expected_bytes:
            checkpoint_start = offset
            checkpoint_end = min(
                expected_bytes, offset + _SEED_COPY_CHECKPOINT_BYTES)
            while offset < checkpoint_end:
                remaining = min(
                    _SEED_COPY_BUFFER_BYTES,
                    checkpoint_end - offset,
                )
                copied = 0
                if use_sendfile:
                    try:
                        copied = os.sendfile(
                            target.fileno(), source_stream.fileno(),
                            offset, remaining)
                    except InterruptedError:
                        continue
                    except OSError as exc:
                        if exc.errno not in unsupported_sendfile_errors:
                            raise
                        use_sendfile = False
                        continue
                else:
                    source_stream.seek(offset)
                    target.seek(offset)
                    payload = source_stream.read(
                        min(_SEED_COPY_BUFFER_BYTES, remaining))
                    if payload:
                        copied = target.write(payload)
                if copied <= 0:
                    raise RuntimeError(
                        f'fastpath seed source ended at {offset} of '
                        f'{expected_bytes} bytes')
                offset += copied
                if progress is not None:
                    progress(offset)
            target.flush()
            os.fsync(target.fileno())
            # This is one-shot migration/snapshot I/O, not a useful database
            # cache warm-up. Release only the completed, now-clean range so an
            # 8 GiB personal machine does not retain tens of GiB of source and
            # destination page cache while copying a large authority.
            try:
                advice = os.POSIX_FADV_DONTNEED
                length = offset - checkpoint_start
                os.posix_fadvise(
                    source_stream.fileno(), checkpoint_start, length, advice)
                os.posix_fadvise(
                    target.fileno(), checkpoint_start, length, advice)
            except (AttributeError, OSError):
                # Non-POSIX runtimes and filesystems that reject the advisory
                # hint retain identical copy and durability semantics.
                pass
            checkpoint(offset)


def _run_with_startup_heartbeat(
    operation,
    *,
    phase: str,
    startup_progress: StartupProgressCallback | None,
) -> None:
    """Run one opaque recovery step with liveness inside the hard deadline."""
    if startup_progress is None:
        operation()
        return
    startup_progress(phase, 0, 0)
    finished = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(
        target=run,
        name='storage-startup-recovery',
        daemon=True,
    )
    worker.start()
    while not finished.wait(_STARTUP_HEARTBEAT_SECONDS):
        startup_progress(phase, 0, 0, heartbeat=True)
    worker.join()
    if errors:
        raise errors[0]


def _copy_progress_reporter(
    startup_progress: StartupProgressCallback | None,
    *,
    phase: str,
    initial_bytes: int,
    total_bytes: int,
):
    """Return a time-bounded reporter without flooding the control pipe."""
    if startup_progress is None:
        return None
    startup_progress(phase, initial_bytes, total_bytes)
    last_reported_at = time.monotonic()
    last_reported_bytes = initial_bytes

    def report(completed_bytes: int) -> None:
        nonlocal last_reported_at, last_reported_bytes
        if completed_bytes < last_reported_bytes:
            raise RuntimeError('startup copy progress moved backwards')
        now = time.monotonic()
        if (completed_bytes != total_bytes
                and now - last_reported_at
                < _STARTUP_PROGRESS_INTERVAL_SECONDS):
            return
        startup_progress(phase, completed_bytes, total_bytes)
        last_reported_at = now
        last_reported_bytes = completed_bytes

    return report


def _completed_seed_is_recoverable(
    local_db: Path,
    classic_db: Path,
) -> bool:
    """Recognize only an atomically installed copy backed by our checkpoint."""
    _temporary, _temporary_wal, state_path = _seed_paths(local_db)
    state = _load_seed_state(state_path)
    if not state or state.get('phase') not in {'copy_complete', 'installed'}:
        return False
    try:
        source = _classic_source_fingerprint(classic_db)
        database_bytes, wal_bytes = _seed_source_sizes(source)
    except (OSError, RuntimeError):
        return False
    if (state.get('source') != source
            or state.get('database_bytes') != database_bytes
            or state.get('wal_bytes') != wal_bytes):
        return False
    try:
        local_status = local_db.stat()
    except OSError:
        return False
    if (not stat.S_ISREG(local_status.st_mode)
            or local_status.st_size != database_bytes):
        return False
    final_wal = local_db.with_name(local_db.name + '-wal')
    if wal_bytes == 0:
        return not final_wal.exists()
    try:
        wal_status = final_wal.stat()
    except OSError:
        return False
    return stat.S_ISREG(wal_status.st_mode) and wal_status.st_size == wal_bytes


def _publish_seed_lineage(
    local_db: Path,
    shadow_dir: Path,
) -> dict[str, Any]:
    """Make an installed classic seed recognizable before SQLite opens it."""
    _temporary, _temporary_wal, state_path = _seed_paths(local_db)
    seed_state = _load_seed_state(state_path)
    payload = {
        # The adapter replaces this provisional value with the canonical row
        # before readiness.  No shadow exists during first activation, so an
        # empty value cannot be mistaken for a competing durable lineage.
        'authority_uuid': '',
        'shadow_dir': str(shadow_dir),
        'seeded_from': 'classic',
    }
    write_local_manifest(local_db.parent, payload)
    if (seed_state is not None
            and seed_state.get('phase') in {'copy_complete', 'installed'}):
        provenance_path = local_db.with_name(
            local_db.name + _SEED_PROVENANCE_SUFFIX)
        try:
            provenance_path.with_name(
                provenance_path.name + '.new').unlink(missing_ok=True)
            write_json_durable(provenance_path, {
                'version': _SEED_STATE_VERSION,
                'shadow_dir': str(shadow_dir),
                'source': seed_state.get('source'),
                'installed': _source_fingerprint(local_db),
            })
        except OSError as exc:
            # Provenance only enables a zero-copy first shadow. The local
            # authority lineage manifest above is the correctness boundary;
            # losing this optional hint merely falls back to a sequential
            # snapshot and must not revoke a valid seed.
            logger.warning(
                '[fastpath] could not persist classic-seed provenance; '
                'first shadow will use a sequential snapshot: %s', exc)
    _remove_seed_state(state_path)
    return payload


def verified_classic_seed_provenance(
    local_db: Path,
    classic_db: Path,
    shadow_dir: Path,
) -> bool:
    """Verify that unchanged installed bytes still equal the classic seed."""
    provenance_path = local_db.with_name(
        local_db.name + _SEED_PROVENANCE_SUFFIX)
    try:
        status = provenance_path.lstat()
        if not stat.S_ISREG(status.st_mode):
            return False
        payload = json.loads(provenance_path.read_text(encoding='utf-8'))
        if (not isinstance(payload, dict)
                or payload.get('version') != _SEED_STATE_VERSION
                or payload.get('shadow_dir') != str(shadow_dir)):
            return False
        return (
            payload.get('source') == _classic_source_fingerprint(classic_db)
            and payload.get('installed') == _source_fingerprint(local_db)
        )
    except (OSError, ValueError, RuntimeError):
        return False


def clear_classic_seed_provenance(local_db: Path) -> None:
    """Retire the one-use zero-copy hint after a shadow exists."""
    provenance_path = local_db.with_name(
        local_db.name + _SEED_PROVENANCE_SUFFIX)
    changed = False
    for path in (
        provenance_path,
        provenance_path.with_name(provenance_path.name + '.new'),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        else:
            changed = True
    if changed:
        fsync_directory(local_db.parent)


def reconcile(
    decision: FastpathDecision,
    classic_db: Path,
    *,
    startup_progress: StartupProgressCallback | None = None,
) -> Path:
    """Make the local authority current and return the path to open.

    Two-way, fail-closed:

    * local lost, shadow present  → restore snapshot + WAL prefix, let
      SQLite's own replay roll forward (bounded RPO: the unshipped tail).
    * local present               → it is authoritative (possibly ahead of
      the shadow by the crash tail, which the shipper forward-ships once
      running).  A uuid mismatch against the shadow means two authorities
      diverged — never guess: refuse to pick, keep the classic path and
      scream.
    * neither, but a classic pre-fastpath authority exists → seed the local
      front from it byte-for-byte (we own the boot; nothing else is running).
    """
    assert decision.local_dir is not None and decision.shadow_dir is not None
    local_dir = decision.local_dir
    shadow_dir = decision.shadow_dir
    local_db = local_dir / 'tofu.db'
    local_manifest = read_local_manifest(local_dir)
    shadow_manifest = read_shadow_manifest(shadow_dir)

    if local_db.exists():
        # DATA-DIR IDENTITY GUARD.  The front is only trustworthy when its
        # manifest names THIS deployment's shadow dir.  A mismatch means a
        # foreign authority (e.g. a test harness sharing an explicit
        # TOFU_STORAGE_FASTPATH_DIR with production); a missing manifest
        # means the lineage is unverifiable.  Never serve either: quarantine
        # the bytes for forensics and rebuild below from the durable shadow
        # or the classic authority.  (2026-08-20 incident: the production
        # sidecar adopted a certification test's front and served it.)
        recorded = str((local_manifest or {}).get('shadow_dir') or '')
        if recorded != str(shadow_dir):
            if (local_manifest is None
                    and _completed_seed_is_recoverable(local_db, classic_db)):
                logger.warning(
                    '[fastpath] recovering lineage publication after an '
                    'interrupted classic seed install')
                local_manifest = _publish_seed_lineage(local_db, shadow_dir)
            else:
                logger.critical(
                    '[fastpath] local front belongs to %s, not %s — '
                    'quarantining the foreign/unverifiable authority; '
                    'rebuilding from the shadow or classic authority',
                    recorded or '<unknown>', shadow_dir)
                _quarantine_foreign_front(local_db)
                local_manifest = None

    if local_manifest and shadow_manifest:
        if local_manifest.get('authority_uuid') != shadow_manifest.get(
                'authority_uuid'):
            # Split-brain: the local front and the durable shadow belong to
            # different authority lineages.  Picking one silently could lose
            # committed user data; stay on the classic path and force an
            # operator decision (docs/TRB-fastpath.md).
            logger.critical(
                '[fastpath] authority uuid mismatch (local=%s shadow=%s) — '
                'refusing to guess; falling back to the classic data-dir '
                'authority until an operator reconciles %s',
                local_manifest.get('authority_uuid'),
                shadow_manifest.get('authority_uuid'), shadow_dir)
            return classic_db

    if not local_db.exists():
        snapshot, shadow_wal = shadow_paths(shadow_dir)
        if shadow_manifest and snapshot.is_file():
            _restore_from_shadow(
                local_db,
                snapshot,
                shadow_wal,
                shadow_manifest,
                startup_progress=startup_progress,
            )
        elif classic_db.is_file():
            try:
                _seed_from_classic(
                    local_db,
                    classic_db,
                    retain_completion_state=True,
                    startup_progress=startup_progress,
                )
                local_manifest = _publish_seed_lineage(local_db, shadow_dir)
            except BrokenPipeError:
                # The parent control channel is the child authority's owner.
                # Preserve the last durable seed checkpoint for the next boot
                # and exit; treating owner loss as an ordinary auto-mode copy
                # failure would erase the very resume state that prevents the
                # restart loop.
                raise
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                # With no shadow this is first activation: the classic file is
                # still the current durable authority. Auto mode may safely
                # remain there; required mode explicitly rejects that fallback.
                if (local_db.exists()
                        and _completed_seed_is_recoverable(
                            local_db, classic_db)):
                    logger.error(
                        '[fastpath] classic seed bytes installed but lineage '
                        'publication did not finish (%s); preserving the '
                        'completion checkpoint for the next boot', exc)
                    if decision.mode == MODE_REQUIRED:
                        raise
                    return classic_db
                _clear_seed_temporary(local_db)
                if not local_db.exists():
                    _unlink_stale_wal_sidecars(local_db)
                if local_db.exists():
                    _quarantine_foreign_front(local_db)
                if decision.mode == MODE_REQUIRED:
                    raise
                logger.error(
                    '[fastpath] first-activation seed failed (%s); auto mode '
                    'is staying on the current classic authority', exc)
                return classic_db
        # else: fresh deployment — the backend's schema init creates it.

    return local_db


def _quarantine_foreign_front(local_db: Path) -> None:
    """Move a foreign/unverifiable front aside — evidence kept, never served.

    The rename is same-directory (atomic); a concurrently-running owner of
    the front keeps its open file handles and is unaffected.
    """
    stamp = time.strftime('%Y%m%d-%H%M%S')
    quarantine = local_db.with_name(f'{local_db.name}.foreign-{stamp}')
    for suffix in ('', '-wal', '-shm'):
        source = local_db.with_name(local_db.name + suffix)
        if source.exists():
            os.replace(source, quarantine.with_name(quarantine.name + suffix))
    fsync_directory(local_db.parent)


def _restore_from_shadow(
    local_db: Path,
    snapshot: Path,
    shadow_wal: Path,
    shadow_manifest: dict[str, Any],
    *,
    startup_progress: StartupProgressCallback | None = None,
) -> None:
    logger.info('[fastpath] restoring the local front from the durable '
                'shadow (generation=%s)', shadow_manifest.get('generation'))
    local_db.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = snapshot.stat().st_size
    if shadow_wal.is_file():
        source_bytes += shadow_wal.stat().st_size
    _require_copy_capacity(
        local_db.parent, source_bytes, purpose='fastpath shadow restore')
    temporary = local_db.with_name(local_db.name + '.restore-tmp')
    temporary_wal = temporary.with_name(temporary.name + '-wal')
    final_wal = local_db.with_name(local_db.name + '-wal')
    _unlink_stale_wal_sidecars(local_db)
    total_bytes = source_bytes
    snapshot_bytes = snapshot.stat().st_size
    report_copy_progress = _copy_progress_reporter(
        startup_progress,
        phase='fastpath.shadow_restore.copy',
        initial_bytes=0,
        total_bytes=total_bytes,
    )
    _copy_file_checkpointed(
        snapshot,
        temporary,
        expected_bytes=snapshot_bytes,
        durable_bytes=0,
        checkpoint=lambda _copied: None,
        progress=report_copy_progress,
    )
    if shadow_wal.is_file() and shadow_wal.stat().st_size > 0:
        shadow_wal_bytes = shadow_wal.stat().st_size
        _copy_file_checkpointed(
            shadow_wal,
            temporary_wal,
            expected_bytes=shadow_wal_bytes,
            durable_bytes=0,
            checkpoint=lambda _copied: None,
            progress=(
                lambda copied: report_copy_progress(snapshot_bytes + copied)
                if report_copy_progress is not None else None
            ),
        )
        fsync_file(temporary_wal)
    fsync_file(temporary)
    os.replace(temporary, local_db)
    if temporary_wal.is_file():
        # The WAL must land under the FINAL database name — a WAL orphaned
        # beside the temp name is never replayed (that exact naming bug
        # silently dropped the shipped tail on restore).
        os.replace(temporary_wal, final_wal)
    fsync_directory(local_db.parent)
    # Verify the restored front opens and replays cleanly before serving.
    # Native SQLite WAL replay has no byte counter, so it uses an explicit
    # heartbeat while remaining bounded by the parent's immutable hard limit.
    def verify_restored_front() -> None:
        connection = sqlite3.connect(local_db, isolation_level=None)
        try:
            connection.execute('PRAGMA busy_timeout=30000')
            row = connection.execute(
                "SELECT meta_value FROM storage_meta WHERE meta_key = ?",
                ('authority_uuid',)).fetchone()
            if (shadow_manifest.get('authority_uuid')
                    and (not row
                         or row[0] != shadow_manifest['authority_uuid'])):
                raise RuntimeError(
                    'restored shadow failed the authority uuid check')
        finally:
            connection.close()

    _run_with_startup_heartbeat(
        verify_restored_front,
        phase='fastpath.shadow_restore.verify',
        startup_progress=startup_progress,
    )


def _unlink_stale_wal_sidecars(local_db: Path) -> None:
    """Remove a pre-existing ``-wal``/``-shm`` beside the install target.

    A leftover WAL belongs to a DIFFERENT salt/generation; SQLite would
    replay it onto the freshly-installed image on first open, corrupting it
    with frames from an abandoned lineage.  Deleting is safe: the image
    being installed carries (or replaces) every committed byte.
    """
    for suffix in ('-wal', '-shm'):
        stale = local_db.with_name(local_db.name + suffix)
        try:
            stale.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f'cannot remove stale {stale.name} before installing the '
                f'front: {exc}') from exc


def _seed_from_classic(
    local_db: Path,
    classic_db: Path,
    *,
    retain_completion_state: bool = False,
    startup_progress: StartupProgressCallback | None = None,
) -> None:
    logger.info('[fastpath] seeding the local front from the classic '
                'authority %s', classic_db)
    local_db.parent.mkdir(parents=True, exist_ok=True)
    classic_wal = classic_db.with_name(classic_db.name + '-wal')
    source = _classic_source_fingerprint(classic_db)
    database_size, wal_size = _seed_source_sizes(source)
    temporary, temporary_wal, state_path = _seed_paths(local_db)
    reusable_bytes = _owned_seed_temporary_bytes(local_db)
    _require_copy_capacity(
        local_db.parent, database_size + wal_size,
        purpose='fastpath classic seed', reusable_bytes=reusable_bytes)
    final_wal = local_db.with_name(local_db.name + '-wal')
    _unlink_stale_wal_sidecars(local_db)

    state = _load_seed_state(state_path)
    resume = _validated_seed_resume(
        state, source, temporary, temporary_wal)
    if resume is None:
        _clear_seed_temporary(local_db)
        state = {
            'version': _SEED_STATE_VERSION,
            'source': source,
            'phase': 'copying',
            'database_bytes': 0,
            'wal_bytes': 0,
        }
        _write_seed_state(state_path, state)
        database_bytes = 0
        wal_bytes = 0
    else:
        database_bytes, wal_bytes = resume
        assert state is not None
        logger.warning(
            '[fastpath] resuming classic seed at database=%d/%d bytes, '
            'wal=%d/%d bytes', database_bytes, database_size,
            wal_bytes, wal_size)

    report_stride = 4 * 1024 ** 3
    last_reported = database_bytes + wal_bytes
    total_bytes = database_size + wal_size
    report_copy_progress = _copy_progress_reporter(
        startup_progress,
        phase='fastpath.classic_seed.copy',
        initial_bytes=database_bytes + wal_bytes,
        total_bytes=total_bytes,
    )

    def checkpoint_database(copied_bytes: int) -> None:
        nonlocal last_reported
        state['database_bytes'] = copied_bytes
        state['phase'] = 'copying_database'
        _write_seed_state(state_path, state)
        total_copied = copied_bytes + int(state['wal_bytes'])
        if (total_copied - last_reported >= report_stride
                or total_copied == database_size + wal_size):
            logger.info(
                '[fastpath] classic seed durable progress %.1f/%.1f GiB',
                total_copied / 1024 ** 3,
                (database_size + wal_size) / 1024 ** 3)
            last_reported = total_copied

    def checkpoint_wal(copied_bytes: int) -> None:
        nonlocal last_reported
        state['wal_bytes'] = copied_bytes
        state['phase'] = 'copying_wal'
        _write_seed_state(state_path, state)
        total_copied = int(state['database_bytes']) + copied_bytes
        if (total_copied - last_reported >= report_stride
                or total_copied == database_size + wal_size):
            logger.info(
                '[fastpath] classic seed durable progress %.1f/%.1f GiB',
                total_copied / 1024 ** 3,
                (database_size + wal_size) / 1024 ** 3)
            last_reported = total_copied

    _copy_file_checkpointed(
        classic_db,
        temporary,
        expected_bytes=database_size,
        durable_bytes=database_bytes,
        checkpoint=checkpoint_database,
        progress=(
            lambda copied: report_copy_progress(copied + wal_bytes)
            if report_copy_progress is not None else None
        ),
    )
    if wal_size > 0:
        _copy_file_checkpointed(
            classic_wal,
            temporary_wal,
            expected_bytes=wal_size,
            durable_bytes=wal_bytes,
            checkpoint=checkpoint_wal,
            progress=(
                lambda copied: report_copy_progress(database_size + copied)
                if report_copy_progress is not None else None
            ),
        )
    else:
        temporary_wal.unlink(missing_ok=True)

    if _classic_source_fingerprint(classic_db) != source:
        raise RuntimeError(
            'classic authority changed during fastpath seed; refusing install')
    state['phase'] = 'copy_complete'
    _write_seed_state(state_path, state)

    if temporary_wal.is_file():
        # Same naming rule as _restore_from_shadow: the WAL must land under
        # the FINAL database name.  An orphaned ``*.seed-tmp-wal`` is never
        # replayed — the classic authority's committed-but-uncheckpointed
        # tail would silently vanish on first open of the seeded front.
        os.replace(temporary_wal, final_wal)
    else:
        final_wal.unlink(missing_ok=True)
    os.replace(temporary, local_db)
    fsync_directory(local_db.parent)
    state['phase'] = 'installed'
    _write_seed_state(state_path, state)
    if not retain_completion_state:
        _remove_seed_state(state_path)


__all__ = [
    'FastpathDecision', 'SHADOW_DIRNAME', 'clear_classic_seed_provenance',
    'decide', 'reconcile',
    'local_front_matches_shadow', 'matching_local_fronts',
    'read_local_manifest', 'read_shadow_manifest', 'shadow_paths',
    'verified_classic_seed_provenance', 'write_local_manifest',
    'write_shadow_manifest',
]
