"""Continuous WAL shipper: the fast-path front's durable shadow keeper.

See fastpath.py for the design invariants.  This module owns the runtime
loop: after every front commit (or every tick, whichever first), copy the
local WAL's unsent frame-aligned prefix to the shadow on the durable data
dir, fsync, and (throttled) replace the manifest.  Only this shipper ever
checkpoints the local authority, which is what makes the shadow a strict
byte-prefix mirror; when the WAL reaches the proactive rebase trigger the
shipper runs a snapshot cycle (ship-to-end → TRUNCATE checkpoint on the
writer → stable sequential DB image + concurrent WAL prefix → manifest generation
bump). The trigger reserves one sixteenth of the bounded WAL budget for writes
that race the shipper's checkpoint. At every physical commit, a separate hard
admission fence observes the local WAL; consuming that reserve prevents any
later transaction from starting until the raw checkpoint creates headroom.
Already-started commit work remains atomic, but new work receives a typed
retryable refusal instead of growing the recovery tail until a filesystem
fills. A verified
first activation reuses the immutable classic DB through a same-filesystem hard
link instead of allocating/copying it twice.

Large replacement images checkpoint one bounded resumable prefix every
256 MiB. The witness binds authority UUID, base generation, and a sampled
source fingerprint; a timeout/restart resumes only that exact immutable image,
while any source change restarts at byte zero. The WAL prefix is always recopied
and the prior published snapshot/WAL pair remains untouched throughout the
resumable copy.

Crash honesty: the shadow WAL is valid by construction (every shipped frame
keeps SQLite's cumulative checksum chain; a torn tail frame fails the chain
and replay simply stops there), and the resume offset derives from the
shadow WAL's real size — the manifest is advisory.  RPO is the unshipped
tail, exported as ``ship_lag_bytes``/``last_ship_age_s``.
"""

from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
from typing import Any, Callable

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar import fastpath
from lib.storage_sidecar.durability import fsync_directory, write_json_durable


logger = get_logger('tofu.storage.sidecar.shipper')

_WAL_HEADER_BYTES = 32
_COPY_CHUNK_BYTES = 4 * 1024 ** 2
_MIB = 1024 ** 2
_MIN_WAL_REBASE_BYTES = 64 * _MIB
_DEFAULT_WAL_REBASE_MAX_BYTES = 512 * _MIB
_WAL_REBASE_DATABASE_FRACTION = 4
_WAL_REBASE_FREE_SPACE_DENOMINATOR = 50
_WAL_REBASE_HEADROOM_DENOMINATOR = 16
_MANIFEST_MIN_INTERVAL_S = 2.0
_MANIFEST_MIN_BYTES = 8 * 1024 ** 2
_MAX_STALE_ARTIFACTS_PER_START = 64
_REBASE_STATE_VERSION = 1
_REBASE_SNAPSHOT_SUFFIX = '.rebase-tmp'
_REBASE_WAL_SUFFIX = '.rebase-tmp'
_REBASE_STATE_SUFFIX = '.rebase-state.json'
_PRIVATE_TEMP_PATTERN = re.compile(
    r'^(?:snapshot\.sqlite3\.tmp-(\d+)(?:-(?:journal|wal|shm))?'
    r'|shadow\.wal\.tmp-(\d+)'
    r'|snapshot\.sqlite3\.seed-link-(\d+)'
    r'|shadow\.wal\.seed-copy-(\d+))$')


class _SnapshotCancelled(RuntimeError):
    """Cooperative stop for a reconstructible snapshot copy."""

def _link_snapshot_via_same_dir_rename(source: Path, destination: Path) -> bool:
    """Hard-link ``source`` to ``destination`` on same-dir-link filesystems.

    BeeGFS-class filesystems accept hard links only within one directory
    (cross-directory ``os.link`` fails with EPERM/EOPNOTSUPP even though both
    paths share a filesystem). Linking beside the source and then atomically
    renaming that second name across directories stays zero-copy, so
    budget-neutral verified-backup rotation remains possible there. Returns
    False when the filesystem rejects either step, leaving ``destination``
    absent so the caller can fall back to its copy/refusal path.
    """
    sibling = source.with_name(
        f'{source.name}.backup-link-{os.getpid()}-{time.time_ns()}')
    try:
        os.link(source, sibling)
    except OSError:
        return False
    try:
        fsync_directory(source.parent)
        os.replace(sibling, destination)
        fsync_directory(destination.parent)
    except OSError:
        try:
            sibling.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def adaptive_wal_rebase_budget(
    database_bytes: int,
    maximum_bytes: int,
) -> int:
    """Bound full-copy frequency while keeping WAL recovery/storage finite."""
    maximum = max(_MIN_WAL_REBASE_BYTES, int(maximum_bytes))
    authority_scaled = max(
        _MIN_WAL_REBASE_BYTES,
        max(0, int(database_bytes)) // _WAL_REBASE_DATABASE_FRACTION,
    )
    return min(authority_scaled, maximum)


def filesystem_wal_rebase_maximum(
    configured_maximum_bytes: int,
    *,
    local_free_bytes: int | None,
    shadow_free_bytes: int | None,
) -> int:
    """Constrain one WAL by both filesystems observed at shipper start.

    The launch manifest probes the durable data volume. Fastpath can place its
    write front on a different, smaller local filesystem, so the final TOCTOU
    check applies the same two-percent per-WAL envelope to both volumes. A
    failed filesystem probe contributes no new cap; the already-bounded launch
    configuration remains authoritative in that degraded case.
    """
    candidates = [max(_MIN_WAL_REBASE_BYTES, int(configured_maximum_bytes))]
    for free_bytes in (local_free_bytes, shadow_free_bytes):
        if (isinstance(free_bytes, bool)
                or not isinstance(free_bytes, int)
                or free_bytes < 0):
            continue
        candidates.append(max(
            _MIN_WAL_REBASE_BYTES,
            free_bytes // _WAL_REBASE_FREE_SPACE_DENOMINATOR,
        ))
    return min(candidates)


def proactive_wal_rebase_trigger(write_pressure_bytes: int) -> int:
    """Start checkpointing with a fixed fraction of WAL admission in hand."""
    pressure = max(_MIB, int(write_pressure_bytes))
    return pressure - max(1, pressure // _WAL_REBASE_HEADROOM_DENOMINATOR)


def _filesystem_free_bytes(path: Path) -> int | None:
    try:
        return max(0, int(shutil.disk_usage(path).free))
    except OSError:
        return None


class WalShipper:
    def __init__(
        self,
        local_db: Path,
        shadow_dir: Path,
        *,
        authority_uuid: str,
        checkpoint_fn: Callable[[], None],
        checkpoint_deadline_fn: Callable[[float], None] | None = None,
        wal_budget_bytes: int | None = None,
        wal_budget_max_bytes: int = _DEFAULT_WAL_REBASE_MAX_BYTES,
        tick_s: float = 1.0,
    ) -> None:
        self._local_db = local_db
        self._local_wal = local_db.with_name(local_db.name + '-wal')
        self._shadow_dir = shadow_dir
        self._classic_db = shadow_dir.parent / local_db.name
        self._snapshot, self._shadow_wal = fastpath.shadow_paths(shadow_dir)
        self._rebase_snapshot = self._snapshot.with_name(
            self._snapshot.name + _REBASE_SNAPSHOT_SUFFIX)
        self._rebase_wal = self._shadow_wal.with_name(
            self._shadow_wal.name + _REBASE_WAL_SUFFIX)
        self._rebase_state = self._snapshot.with_name(
            self._snapshot.name + _REBASE_STATE_SUFFIX)
        self._authority_uuid = authority_uuid
        self._checkpoint_fn = checkpoint_fn
        self._checkpoint_deadline_fn = checkpoint_deadline_fn
        try:
            self._database_bytes_at_start = self._local_db.stat().st_size
        except OSError:
            self._database_bytes_at_start = 0
        if wal_budget_bytes is None:
            shadow_probe = (
                self._shadow_dir
                if self._shadow_dir.exists()
                else self._shadow_dir.parent
            )
            wal_budget_max_bytes = filesystem_wal_rebase_maximum(
                wal_budget_max_bytes,
                local_free_bytes=_filesystem_free_bytes(
                    self._local_db.parent),
                shadow_free_bytes=_filesystem_free_bytes(shadow_probe),
            )
            self._wal_budget_bytes = adaptive_wal_rebase_budget(
                self._database_bytes_at_start,
                wal_budget_max_bytes,
            )
        else:
            # Explicit constructor values are a deterministic test/operator
            # seam and remain authoritative.
            self._wal_budget_bytes = max(_MIB, int(wal_budget_bytes))
        self._wal_rebase_trigger_bytes = proactive_wal_rebase_trigger(
            self._wal_budget_bytes
        )
        self._tick_s = max(0.1, float(tick_s))
        self._wake = threading.Event()
        self._stop = False
        self._state_lock = threading.Lock()
        # Serializes ship passes: the tick loop, stop()'s final ship, and
        # operator-triggered passes must never interleave copies at the same
        # offsets.
        self._pass_lock = threading.Lock()
        self._generation = 0
        self._wal_shipped = 0  # frame-aligned bytes of the shadow WAL
        self._last_manifest_at = 0.0
        self._last_manifest_bytes = 0
        self._snapshot_recovery_point_at: float | None = None
        # True only when appending the current local WAL to the published
        # shadow is valid. A generation mismatch must preserve the old,
        # self-consistent snapshot+WAL until the replacement snapshot commits.
        self._shadow_wal_matches_local = False
        self._thread = threading.Thread(
            target=self._run, name='storage-wal-shipper', daemon=True)
        self._last_ship_wall: float | None = None
        # Set by _resume_or_snapshot: the first ship pass re-bases the shadow
        # instead of resuming.  Deferred to the shipper thread because the
        # initial snapshot of a large authority can take minutes and must not
        # block the supervisor's bounded startup window.
        self._needs_snapshot = False
        self._rebase_active = False
        self._write_pressure_active = False
        self.metrics: dict[str, Any] = {
            'ships': 0, 'bytes_shipped': 0, 'snapshots': 0,
            'ship_failures': 0, 'ship_lag_bytes': 0,
            'snapshot_progress_bytes': 0,
            'snapshot_database_bytes_copied': 0,
            'snapshot_wal_bytes_copied': 0,
            'snapshot_resume_count': 0,
            'snapshot_resumed_bytes': 0,
            'stale_artifacts_reclaimed': 0,
            'stale_artifact_bytes_reclaimed': 0,
            'wal_rebase_budget_bytes': self._wal_budget_bytes,
            'wal_rebase_trigger_bytes': self._wal_rebase_trigger_bytes,
            'wal_write_pressure_bytes': self._wal_budget_bytes,
            'rebase_active': False,
            'write_pressure_active': False,
            'write_pressure_activations': 0,
            'write_pressure_rejections': 0,
            'write_pressure_observation_failures': 0,
            'local_wal_bytes': 0,
            'wal_write_headroom_bytes': self._wal_budget_bytes,
        }

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        self._shadow_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_artifacts()
        self._resume_or_snapshot()
        self._observe_current_write_pressure()
        logger.info(
            '[shipper] WAL rebase budget %.1f MiB for %.1f GiB authority',
            self._wal_budget_bytes / _MIB,
            self._database_bytes_at_start / 1024 ** 3,
        )
        self._thread.start()

    def notify_commit(self) -> None:
        """Writer-thread hook: a commit landed on the front's WAL."""
        # One local stat per PHYSICAL commit (group commit amortizes logical
        # jobs) closes the interval between crossing the resource watermark
        # and the shipper thread beginning its next pass.
        self._observe_current_write_pressure()
        self._wake.set()

    def _observe_current_write_pressure(self) -> None:
        try:
            local_wal_bytes = self._local_wal_size()
        except OSError as exc:
            with self._state_lock:
                if not self._write_pressure_active:
                    self.metrics['write_pressure_activations'] += 1
                self._write_pressure_active = True
                self.metrics['write_pressure_active'] = True
                self.metrics['write_pressure_observation_failures'] += 1
            logger.warning(
                '[shipper] local WAL size observation failed; writes remain '
                'fenced until a trustworthy observation succeeds: %s', exc)
            return
        self._observe_write_pressure(local_wal_bytes)

    def _observe_write_pressure(self, local_wal_bytes: int) -> None:
        pressure = max(0, int(local_wal_bytes)) >= self._wal_budget_bytes
        with self._state_lock:
            if pressure and not self._write_pressure_active:
                self.metrics['write_pressure_activations'] += 1
            self._write_pressure_active = pressure
            self.metrics['write_pressure_active'] = pressure
            self.metrics['local_wal_bytes'] = max(0, int(local_wal_bytes))
            self.metrics['wal_write_headroom_bytes'] = max(
                0, self._wal_budget_bytes - max(0, int(local_wal_bytes)))

    def assert_write_admitted(self) -> None:
        """Refuse not-yet-started writes after a rebase WAL uses its budget.

        The writer checks this before queue retention and immediately before
        BEGIN. A transaction or group-commit segment already in flight remains
        atomic, so the WAL may exceed the threshold by that one bounded
        segment; later work cannot compound the overshoot.
        """
        with self._state_lock:
            if not self._write_pressure_active:
                return
            self.metrics['write_pressure_rejections'] += 1
        raise StorageError(
            'database_busy',
            'Fastpath WAL write-pressure threshold reached; '
            'retry after shadow maintenance advances',
            True,
            250,
        )

    def _set_rebase_active(self, active: bool) -> None:
        with self._state_lock:
            self._rebase_active = bool(active)
            self.metrics['rebase_active'] = self._rebase_active
        self._observe_current_write_pressure()

    def pin_checkpointed_snapshot_for_backup(
        self,
        destination: Path,
        *,
        deadline_at: float,
        require_hardlink: bool = False,
    ) -> dict[str, Any]:
        """Create one stable, standalone backup image under the ship lock.

        A newly started rebase checkpoints every commit acknowledged before
        this call into the database image. A resumed rebase retains its first
        checkpoint as the honest recovery boundary; callers receive that time
        rather than mistaking eventual publication time for data freshness.
        Commits accepted during the sequential copy remain in the new WAL and
        intentionally belong to the next backup. The published image is then
        hard-linked on the same filesystem or copied across devices. A caller
        performing budget-neutral verified-backup rotation sets
        ``require_hardlink`` so an unsupported link fails before allocating a
        second full image.
        """
        remaining_s = max(0.0, float(deadline_at) - time.monotonic())
        if remaining_s <= 0 or not self._pass_lock.acquire(timeout=remaining_s):
            raise TimeoutError('SQLite backup deadline expired waiting for shipper')
        try:
            self._snapshot_cycle(deadline_at=deadline_at)
            if time.monotonic() >= deadline_at:
                raise TimeoutError('SQLite backup deadline expired after snapshot')
            source_bytes = self._snapshot.stat().st_size
            strategy = 'hardlink'
            try:
                os.link(self._snapshot, destination)
            except OSError as exc:
                unsupported = {
                    errno.EXDEV,
                    errno.EPERM,
                    errno.EOPNOTSUPP,
                    getattr(errno, 'ENOTSUP', errno.EOPNOTSUPP),
                }
                if exc.errno not in unsupported:
                    raise
                linked_via_rename = False
                if exc.errno != errno.EXDEV:
                    linked_via_rename = _link_snapshot_via_same_dir_rename(
                        self._snapshot, destination)
                if not linked_via_rename:
                    if require_hardlink:
                        raise StorageError(
                            'database_unavailable',
                            'SQLite backup budget rotation requires a '
                            'same-filesystem hard link; refusing full-copy fallback',
                            retryable=False,
                        ) from exc
                    strategy = 'sequential-copy'

                    def enforce_deadline(_copied_bytes: int) -> None:
                        if time.monotonic() >= deadline_at:
                            raise TimeoutError(
                                'SQLite backup deadline expired during snapshot copy')

                    fastpath._copy_file_checkpointed(
                        self._snapshot,
                        destination,
                        expected_bytes=source_bytes,
                        durable_bytes=0,
                        checkpoint=lambda _copied: None,
                        progress=enforce_deadline,
                    )
                else:
                    strategy = 'hardlink-rename'
            return {
                'generation': self._generation,
                'bytes': source_bytes,
                'copy_strategy': strategy,
                'recovery_point_at': self._snapshot_recovery_point_at,
            }
        finally:
            self._pass_lock.release()

    def stop(self, *, timeout_s: float = 30.0) -> None:
        with self._state_lock:
            self._stop = True
        self._wake.set()
        self._thread.join(timeout=max(1.0, timeout_s))
        if not self._thread.is_alive():
            if self._needs_snapshot:
                # Never restart an interrupted full snapshot during shutdown.
                # The previous snapshot+WAL (or pre-fastpath classic) remains
                # the durable recovery point; next boot re-bases it.
                logger.info('[shipper] pending snapshot deferred to next boot')
            else:
                try:
                    self._ship_pass(final=True)
                except Exception as exc:
                    # Broad on purpose: the final pass is best-effort — boot
                    # reconciliation owns the tail.
                    logger.warning('[shipper] final ship failed (boot '
                                   'reconciliation owns the tail): %s', exc)
        else:
            # The thread is mid-pass (e.g. a first snapshot cycle on a large
            # authority).  Skipping the final ship is safe ONLY because boot
            # reconciliation forward-ships a surviving local front — but a
            # silent skip hid exactly the flush stop() exists for.
            logger.warning('[shipper] stop(): shipper thread still alive '
                           'after %.0fs — final ship deferred to boot '
                           'reconciliation (ship_lag_bytes=%s)',
                           max(1.0, timeout_s),
                           self.metrics.get('ship_lag_bytes'))

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            result = dict(self.metrics)
        result['generation'] = self._generation
        result['wal_shipped_bytes'] = self._wal_shipped
        result['last_ship_age_s'] = (
            round(time.time() - self._last_ship_wall, 3)
            if self._last_ship_wall is not None else None)
        return result

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _cleanup_stale_artifacts(self) -> None:
        """Reclaim only exact private temp files whose creating PID is dead."""
        checked_pids: dict[int, bool] = {}
        matched = 0
        reclaimed_files = 0
        reclaimed_bytes = 0
        try:
            entries = os.scandir(self._shadow_dir)
        except OSError as exc:
            logger.debug('[shipper] stale-artifact scan unavailable: %s', exc)
            return
        with entries:
            for entry in entries:
                match = _PRIVATE_TEMP_PATTERN.fullmatch(entry.name)
                if match is None:
                    continue
                matched += 1
                if matched > _MAX_STALE_ARTIFACTS_PER_START:
                    logger.warning(
                        '[shipper] stale-artifact cleanup reached its %d-file '
                        'startup bound; later boots will continue',
                        _MAX_STALE_ARTIFACTS_PER_START)
                    break
                pid = int(next(value for value in match.groups() if value))
                if pid not in checked_pids:
                    checked_pids[pid] = self._pid_is_alive(pid)
                alive = checked_pids[pid]
                if alive:
                    continue
                path = self._shadow_dir / entry.name
                try:
                    status = path.lstat()
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning(
                        '[shipper] could not reclaim stale artifact %s: %s',
                        entry.name, exc)
                    continue
                reclaimed_files += 1
                reclaimed_bytes += max(0, int(status.st_size))
        if reclaimed_files:
            fsync_directory(self._shadow_dir)
            self.metrics['stale_artifacts_reclaimed'] += reclaimed_files
            self.metrics['stale_artifact_bytes_reclaimed'] += reclaimed_bytes
            logger.info(
                '[shipper] reclaimed %d stale private artifact(s), %.1f MiB',
                reclaimed_files, reclaimed_bytes / 1024 ** 2)

    def _read_rebase_state(self) -> dict[str, Any] | None:
        """Read one exact private resume witness without following links."""
        try:
            status = self._rebase_state.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning('[shipper] rebase state unavailable: %s', exc)
            return None
        if not stat.S_ISREG(status.st_mode):
            logger.warning('[shipper] rebase state is not a regular file')
            return None
        try:
            value = json.loads(self._rebase_state.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning('[shipper] rebase state is unreadable: %s', exc)
            return None
        return value if isinstance(value, dict) else None

    def _clear_rebase_progress(self) -> None:
        """Remove only the bounded reconstructible rebase working set."""
        changed = False
        for path in (
            self._rebase_snapshot,
            self._rebase_snapshot.with_name(
                self._rebase_snapshot.name + '-journal'),
            self._rebase_snapshot.with_name(
                self._rebase_snapshot.name + '-wal'),
            self._rebase_snapshot.with_name(
                self._rebase_snapshot.name + '-shm'),
            self._rebase_wal,
            self._rebase_state,
            self._rebase_state.with_name(self._rebase_state.name + '.new'),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            else:
                changed = True
        if changed:
            fsync_directory(self._shadow_dir)
        with self._state_lock:
            self.metrics['snapshot_progress_bytes'] = 0

    def _validated_rebase_resume(
        self,
    ) -> tuple[dict[str, Any], int, float] | None:
        """Return source identity and durable offset for one safe resume."""
        state = self._read_rebase_state()
        if state is None:
            return None
        durable_bytes = state.get('database_bytes')
        recovery_point_at = state.get('recovery_point_at')
        if (state.get('version') != _REBASE_STATE_VERSION
                or state.get('authority_uuid') != self._authority_uuid
                or state.get('base_generation') != self._generation
                or not isinstance(state.get('source'), dict)
                or isinstance(durable_bytes, bool)
                or not isinstance(durable_bytes, int)
                or isinstance(recovery_point_at, bool)
                or not isinstance(recovery_point_at, (int, float))
                or not math.isfinite(float(recovery_point_at))
                or float(recovery_point_at) <= 0):
            return None
        try:
            source_identity = fastpath._source_fingerprint(self._local_db)
        except (OSError, RuntimeError) as exc:
            logger.warning('[shipper] cannot validate rebase source: %s', exc)
            return None
        if state['source'] != source_identity:
            logger.info('[shipper] rebase source changed; restarting at byte zero')
            return None
        source_bytes = int(source_identity['size'])
        if not 0 <= durable_bytes <= source_bytes:
            return None
        try:
            temporary_status = self._rebase_snapshot.lstat()
        except FileNotFoundError:
            if durable_bytes:
                return None
        except OSError:
            return None
        else:
            if (not stat.S_ISREG(temporary_status.st_mode)
                    or temporary_status.st_size < durable_bytes):
                return None
        return source_identity, durable_bytes, float(recovery_point_at)

    def _write_rebase_state(
        self,
        source_identity: dict[str, Any],
        durable_bytes: int,
        *,
        phase: str,
        recovery_point_at: float | None = None,
    ) -> None:
        # ``write_json_durable`` uses O_EXCL for its private replacement. A
        # crash may leave only that uncommitted replacement behind; it has no
        # authority and must not permanently block the next state checkpoint.
        self._rebase_state.with_name(
            self._rebase_state.name + '.new').unlink(missing_ok=True)
        boundary = recovery_point_at
        if boundary is None:
            previous = self._read_rebase_state() or {}
            candidate = previous.get('recovery_point_at')
            boundary = (
                float(candidate)
                if (not isinstance(candidate, bool)
                    and isinstance(candidate, (int, float))
                    and math.isfinite(float(candidate))
                    and float(candidate) > 0)
                else time.time()
            )
        write_json_durable(self._rebase_state, {
            'version': _REBASE_STATE_VERSION,
            'authority_uuid': self._authority_uuid,
            'base_generation': self._generation,
            'target_generation': self._generation + 1,
            'source': source_identity,
            'database_bytes': max(0, int(durable_bytes)),
            'phase': phase,
            'recovery_point_at': float(boundary),
            'updated_at': time.time(),
        })
        with self._state_lock:
            self.metrics['snapshot_progress_bytes'] = max(
                0, int(durable_bytes))

    def _require_rebase_capacity(
        self,
        source_bytes: int,
        *,
        durable_bytes: int = 0,
    ) -> None:
        """Admit one bounded image while crediting owned resumable progress."""
        try:
            reusable_bytes = 0
            try:
                temporary_status = self._rebase_snapshot.lstat()
            except OSError:
                temporary_status = None
            if (temporary_status is not None
                    and stat.S_ISREG(temporary_status.st_mode)):
                allocated_blocks = getattr(
                    temporary_status, 'st_blocks', None)
                allocated_bytes = (
                    max(0, int(allocated_blocks)) * 512
                    if allocated_blocks is not None
                    else max(0, int(temporary_status.st_size))
                )
                reusable_bytes = min(
                    max(0, int(durable_bytes)),
                    max(0, int(temporary_status.st_size)),
                    allocated_bytes,
                )
            fastpath._require_copy_capacity(
                self._shadow_dir,
                source_bytes,
                purpose='fastpath shadow rebase',
                reusable_bytes=reusable_bytes,
            )
        except RuntimeError as exc:
            raise StorageError(
                'database_unavailable', str(exc), retryable=False) from exc

    # -------------------------------------------------------------- runtime

    def _run(self) -> None:
        while True:
            self._wake.wait(self._tick_s)
            self._wake.clear()
            with self._state_lock:
                if self._stop:
                    return
            try:
                self._ship_pass()
            except _SnapshotCancelled:
                logger.info('[shipper] snapshot cancelled for shutdown')
                return
            except Exception as exc:
                # The shadow lagging must never kill the write front — that
                # inversion is the very failure this design removes.  Lag is
                # observable via metrics; boot reconciliation is the backstop.
                self.metrics['ship_failures'] += 1
                logger.warning('[shipper] ship pass failed: %s', exc)

    def _local_wal_size(self) -> int:
        try:
            return self._local_wal.stat().st_size
        except FileNotFoundError:
            return 0

    def _frame_size(self) -> int | None:
        """24 + page_size from the local WAL header; None when no WAL."""
        try:
            with self._local_wal.open('rb') as stream:
                header = stream.read(_WAL_HEADER_BYTES)
        except OSError:
            return None
        if len(header) < _WAL_HEADER_BYTES:
            return None
        page_size = int.from_bytes(header[8:12], 'big') or 1024
        return 24 + page_size

    @staticmethod
    def _floor_frame(offset: int, frame_size: int) -> int:
        if offset <= _WAL_HEADER_BYTES:
            return _WAL_HEADER_BYTES if offset >= _WAL_HEADER_BYTES else 0
        frames = (offset - _WAL_HEADER_BYTES) // frame_size
        return _WAL_HEADER_BYTES + frames * frame_size

    def _resume_or_snapshot(self) -> None:
        manifest = fastpath.read_shadow_manifest(self._shadow_dir)
        if manifest is None:
            manifest = self._bootstrap_classic_seed_shadow()
        self._generation = int(manifest.get('generation') or 0) if manifest else 0
        if manifest:
            candidate = manifest.get('recovery_point_at')
            if (not isinstance(candidate, bool)
                    and isinstance(candidate, (int, float))
                    and math.isfinite(float(candidate))
                    and float(candidate) > 0):
                self._snapshot_recovery_point_at = float(candidate)
        rebase_state = self._read_rebase_state()
        if (rebase_state is not None
                and rebase_state.get('version') == _REBASE_STATE_VERSION
                and rebase_state.get('authority_uuid') == self._authority_uuid
                and rebase_state.get('base_generation') == self._generation):
            durable_bytes = rebase_state.get('database_bytes')
            progress = (
                durable_bytes
                if isinstance(durable_bytes, int)
                and not isinstance(durable_bytes, bool)
                and durable_bytes >= 0
                else 0
            )
            with self._state_lock:
                self.metrics['snapshot_progress_bytes'] = progress
            self._shadow_wal_matches_local = False
            self._needs_snapshot = True
            self._set_rebase_active(True)
            logger.warning(
                '[shipper] resumable snapshot generation=%d found at %.1f GiB; '
                'validation deferred to the shipper thread',
                self._generation + 1,
                progress / 1024 ** 3,
            )
            return
        # A state file from an already-published generation, or an incomplete
        # state/temp pair, has no authority. Keep at most one resumable copy.
        self._clear_rebase_progress()
        local_size = self._local_wal_size()
        frame_size = self._frame_size()
        if manifest and manifest.get('classic_seed_base_no_wal'):
            # The classic DB image is the exact checkpointed seed base and had
            # no WAL. The front's first WAL generation can therefore be shipped
            # from byte zero without manufacturing another 82 GiB snapshot.
            self._wal_shipped = 0
            self._shadow_wal_matches_local = True
            logger.info(
                '[shipper] adopted zero-copy classic seed snapshot '
                '(generation=%d); WAL starts at offset 0', self._generation)
            return
        if manifest and frame_size is not None and local_size >= _WAL_HEADER_BYTES:
            shadow_size = (self._shadow_wal.stat().st_size
                           if self._shadow_wal.is_file() else 0)
            if self._wal_headers_match():
                # Ground truth is the shadow's real size (a torn tail from a
                # crashed copy is overwritten by the next pass).
                self._wal_shipped = self._floor_frame(
                    min(shadow_size, local_size), frame_size)
                self._shadow_wal_matches_local = True
                logger.info('[shipper] resumed at generation=%d offset=%d',
                            self._generation, self._wal_shipped)
                return
            logger.info('[shipper] WAL generation changed (checkpoint '
                        'landed while down) — snapshot cycle')
        self._needs_snapshot = True
        logger.warning('[shipper] initial/re-basing snapshot deferred to the '
                       'shipper thread; until it completes the durable copy '
                       'is the pre-fastpath classic authority (bounded first-'
                       'activation window — see docs/TRB-fastpath.md)')

    def _bootstrap_classic_seed_shadow(self) -> dict[str, Any] | None:
        """Publish the immutable classic seed as generation 1 via hard link.

        First activation already copied ``data/tofu.db`` into the local front.
        Duplicating those same tens of GiB back onto the data filesystem is
        pure write amplification. A one-use, fingerprinted seed provenance
        proves both files are still the installed pair; a same-filesystem hard
        link then creates the durable snapshot in O(1) space and metadata I/O.
        Any unsupported topology or changed byte identity falls back to the
        ordinary sequential snapshot without weakening lineage checks.
        """
        if self._snapshot.exists():
            return None
        if not fastpath.verified_classic_seed_provenance(
                self._local_db, self._classic_db, self._shadow_dir):
            return None

        temporary = self._snapshot.with_name(
            self._snapshot.name + f'.seed-link-{os.getpid()}')
        temporary_wal = self._shadow_wal.with_name(
            self._shadow_wal.name + f'.seed-copy-{os.getpid()}')
        for path in (temporary, temporary_wal):
            path.unlink(missing_ok=True)
        try:
            os.link(self._classic_db, temporary)
            classic_wal = self._classic_db.with_name(
                self._classic_db.name + '-wal')
            wal_bytes = (
                classic_wal.stat().st_size
                if classic_wal.is_file() else 0
            )
            if wal_bytes > 0:
                fastpath._copy_file_checkpointed(
                    classic_wal,
                    temporary_wal,
                    expected_bytes=wal_bytes,
                    durable_bytes=0,
                    checkpoint=lambda _copied: None,
                )
            os.replace(temporary, self._snapshot)
            if temporary_wal.is_file():
                os.replace(temporary_wal, self._shadow_wal)
            else:
                self._shadow_wal.unlink(missing_ok=True)
            fsync_directory(self._shadow_dir)
            fastpath.write_shadow_manifest(self._shadow_dir, {
                'format': 'tofu.fastpath-shadow.v1',
                'authority_uuid': self._authority_uuid,
                'generation': 1,
                'wal_shipped_bytes': wal_bytes,
                'snapshot_bytes': self._snapshot.stat().st_size,
                'classic_seed_base_no_wal': wal_bytes == 0,
                'recovery_point_at': time.time(),
                'updated_at': time.time(),
            })
            fastpath.clear_classic_seed_provenance(self._local_db)
            self.metrics['snapshots'] += 1
            logger.info(
                '[shipper] zero-copy classic seed snapshot published '
                '(%d bytes, wal=%d bytes)',
                self._snapshot.stat().st_size, wal_bytes)
            return fastpath.read_shadow_manifest(self._shadow_dir)
        except (OSError, RuntimeError) as exc:
            logger.warning(
                '[shipper] zero-copy classic seed snapshot unavailable (%s); '
                'falling back to a sequential snapshot', exc)
            return None
        finally:
            for path in (temporary, temporary_wal):
                path.unlink(missing_ok=True)

    def _wal_headers_match(self) -> bool:
        try:
            with self._local_wal.open('rb') as stream:
                local_header = stream.read(_WAL_HEADER_BYTES)
            with self._shadow_wal.open('rb') as stream:
                shadow_header = stream.read(_WAL_HEADER_BYTES)
        except OSError:
            return False
        # Salt-1 and the checksum seed identify a WAL generation.
        return (len(local_header) == _WAL_HEADER_BYTES
                and local_header[16:24] == shadow_header[16:24])

    def _ship_pass(self, *, final: bool = False) -> None:
        with self._pass_lock:
            self._ship_pass_locked(final=final)

    def _ship_pass_locked(self, *, final: bool = False) -> None:
        if self._needs_snapshot:
            try:
                self._snapshot_cycle()
            except Exception:
                # Retry on the next tick — the shadow simply lags until then.
                self._needs_snapshot = True
                raise
            self._needs_snapshot = False
            return
        frame_size = self._frame_size()
        if frame_size is None:
            self.metrics['ship_lag_bytes'] = 0
            return
        local_size = self._local_wal_size()
        target = self._floor_frame(local_size, frame_size)
        if target < self._wal_shipped:
            # The WAL shrank under us — a checkpoint escaped the shipper.
            # Never guess at bytes: full snapshot cycle re-bases the shadow.
            logger.warning('[shipper] local WAL shrank (%d < %d) — '
                           'snapshot cycle', target, self._wal_shipped)
            self._shadow_wal_matches_local = False
            self._snapshot_cycle()
            return
        if target > self._wal_shipped:
            shipped_now = target - self._wal_shipped
            self._copy_range(self._wal_shipped, target)
            self._wal_shipped = target
            self.metrics['ships'] += 1
            self.metrics['bytes_shipped'] += shipped_now
            self._last_ship_wall = time.time()
        # The manifest is advisory (the shadow WAL's real size is ground
        # truth), but it must still track the durable state within the
        # throttle window — a pass with no new bytes may be the one whose
        # interval expired since the last ship.
        if self._wal_shipped != self._last_manifest_bytes:
            self._maybe_write_manifest(force=final)
        lag = local_size - self._wal_shipped
        self.metrics['ship_lag_bytes'] = lag
        if not final and local_size >= self._wal_rebase_trigger_bytes:
            self._snapshot_cycle()

    def _copy_range(self, start: int, end: int) -> None:
        self._copy_range_to(self._shadow_wal, start, end)

    def _copy_range_to(
        self,
        destination_path: Path,
        start: int,
        end: int,
        *,
        bytes_copied_sink: Callable[[int], None] | None = None,
    ) -> None:
        with self._local_wal.open('rb') as source:
            source.seek(start)
            mode = 'r+b' if destination_path.exists() else 'wb'
            with destination_path.open(mode) as destination:
                destination.seek(start)
                remaining = end - start
                while remaining > 0:
                    chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        copied = end - start - remaining
                        raise RuntimeError(
                            f'local WAL ended at {start + copied} of {end} '
                            'bytes during shadow copy')
                    destination.write(chunk)
                    remaining -= len(chunk)
                    if bytes_copied_sink is not None:
                        bytes_copied_sink(len(chunk))
                # A crashed prior copy may have left a non-frame-aligned tail.
                # The requested prefix is authoritative; discard everything
                # beyond it before publishing durability.
                destination.truncate(end)
                destination.flush()
                os.fsync(destination.fileno())

    def _maybe_write_manifest(self, *, force: bool = False) -> None:
        now = time.monotonic()
        shipped_since = self._wal_shipped - self._last_manifest_bytes
        if not force and (
                now - self._last_manifest_at < _MANIFEST_MIN_INTERVAL_S
                and shipped_since < _MANIFEST_MIN_BYTES):
            return
        self._write_manifest()

    def _write_manifest(self) -> None:
        payload = {
            'format': 'tofu.fastpath-shadow.v1',
            'authority_uuid': self._authority_uuid,
            'generation': self._generation,
            'wal_shipped_bytes': self._wal_shipped,
            'snapshot_bytes': (self._snapshot.stat().st_size
                               if self._snapshot.is_file() else 0),
            'updated_at': time.time(),
        }
        if self._snapshot_recovery_point_at is not None:
            payload['recovery_point_at'] = self._snapshot_recovery_point_at
        fastpath.write_shadow_manifest(self._shadow_dir, payload)
        self._last_manifest_at = time.monotonic()
        self._last_manifest_bytes = self._wal_shipped

    def _snapshot_cycle(self, *, deadline_at: float | None = None) -> None:
        """Re-base from one stable DB image plus its concurrent WAL tail.

        Once the writer completes ``wal_checkpoint(TRUNCATE)``, normal WAL-mode
        commits append only to a new WAL; the database file itself is immutable
        until another checkpoint.  This shipper is the sole checkpoint owner.
        A bounded sequential copy is therefore a consistent snapshot and avoids
        SQLite backup's page-at-a-time writes to the network filesystem.  The
        latter repeatedly restarted under live writes and held an 82 GiB first
        snapshot at ~646 MiB indefinitely.
        """
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TimeoutError('SQLite backup deadline expired before snapshot')
        # Keep a SQLite connection open while the raw image is copied. During
        # bounded shutdown it prevents the copy descriptor from becoming the
        # only remaining owner while another connection's last-close checkpoint
        # mutates the database underneath it.
        source_guard = sqlite3.connect(self._local_db, isolation_level=None)
        published = False
        try:
            source_guard.execute('PRAGMA busy_timeout=30000')
            source_guard.execute('PRAGMA query_only=ON')
            setter = getattr(source_guard, 'setconfig', None)
            no_close_checkpoint = getattr(
                sqlite3, 'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE', None)
            if setter is not None and no_close_checkpoint is not None:
                setter(no_close_checkpoint, 1)

            resume = self._validated_rebase_resume()
            if resume is None:
                self._set_rebase_active(False)
                self._clear_rebase_progress()
                self._require_rebase_capacity(
                    self._local_db.stat().st_size + self._local_wal_size())
                frame_size = self._frame_size()
                if frame_size is not None and self._shadow_wal_matches_local:
                    target = self._floor_frame(
                        self._local_wal_size(), frame_size)
                    if target > self._wal_shipped:
                        self._copy_range(self._wal_shipped, target)
                        self._wal_shipped = target
                # The checkpoint MUST run on the writer connection
                # (single-writer authority); checkpoint_fn is the backend's
                # maintenance-lane hop. It makes the DB image immutable while
                # all later commits accumulate in a fresh WAL generation.
                if (deadline_at is not None
                        and self._checkpoint_deadline_fn is not None):
                    self._checkpoint_deadline_fn(deadline_at)
                else:
                    self._checkpoint_fn()
                self._set_rebase_active(True)
                recovery_point_at = time.time()
                self._shadow_wal_matches_local = False
                self._needs_snapshot = True
                source_identity = fastpath._source_fingerprint(self._local_db)
                # Close the capacity TOCTOU window after the checkpoint has
                # folded every pre-existing WAL page into the exact DB image.
                self._require_rebase_capacity(int(source_identity['size']))
                durable_bytes = 0
                self._write_rebase_state(
                    source_identity,
                    durable_bytes,
                    phase='copying_database',
                    recovery_point_at=recovery_point_at,
                )
            else:
                source_identity, durable_bytes, recovery_point_at = resume
                self._set_rebase_active(True)
                self._require_rebase_capacity(
                    int(source_identity['size']),
                    durable_bytes=durable_bytes,
                )
                self._shadow_wal_matches_local = False
                self._needs_snapshot = True
                with self._state_lock:
                    self.metrics['snapshot_resume_count'] += 1
                    self.metrics['snapshot_resumed_bytes'] += durable_bytes
                    self.metrics['snapshot_progress_bytes'] = durable_bytes
                logger.warning(
                    '[shipper] resuming snapshot generation=%d at %.1f/%.1f GiB',
                    self._generation + 1,
                    durable_bytes / 1024 ** 3,
                    int(source_identity['size']) / 1024 ** 3,
                )

            source_bytes = int(source_identity['size'])
            database_bytes_accounted = durable_bytes

            def account_database_copy(copied_bytes: int) -> None:
                nonlocal database_bytes_accounted
                delta = max(0, int(copied_bytes) - database_bytes_accounted)
                if delta:
                    with self._state_lock:
                        self.metrics['snapshot_database_bytes_copied'] += delta
                    database_bytes_accounted = int(copied_bytes)

            def cancel_snapshot_copy(_copied_bytes: int) -> None:
                account_database_copy(_copied_bytes)
                with self._state_lock:
                    stopping = self._stop
                if stopping:
                    raise _SnapshotCancelled(
                        'snapshot interrupted by Sidecar shutdown')
                if deadline_at is not None and time.monotonic() >= deadline_at:
                    raise TimeoutError(
                        'SQLite backup deadline expired during shadow snapshot')

            def checkpoint_snapshot_copy(copied_bytes: int) -> None:
                self._write_rebase_state(
                    source_identity,
                    copied_bytes,
                    phase='copying_database',
                    recovery_point_at=recovery_point_at,
                )

            fastpath._copy_file_checkpointed(
                self._local_db,
                self._rebase_snapshot,
                expected_bytes=source_bytes,
                durable_bytes=durable_bytes,
                checkpoint=checkpoint_snapshot_copy,
                progress=cancel_snapshot_copy,
            )
            self._write_rebase_state(
                source_identity,
                source_bytes,
                phase='database_complete',
                recovery_point_at=recovery_point_at,
            )
            final_source_identity = fastpath._source_fingerprint(self._local_db)
            if final_source_identity != source_identity:
                raise RuntimeError(
                    'fastpath database image changed during shadow snapshot; '
                    'refusing to publish mixed bytes')

            # Capture every complete frame committed while the DB image copied.
            # Later frames remain ordinary observable ship lag and are handled
            # by the next one-second pass.
            new_frame_size = self._frame_size()
            wal_target = 0
            if new_frame_size is not None:
                wal_target = self._floor_frame(
                    self._local_wal_size(), new_frame_size)
            # The WAL can grow for hours while a large DB prefix is copied.
            # Recheck the complete pair at the publication boundary so that
            # the cheap tail cannot unexpectedly fill the shadow filesystem.
            self._require_rebase_capacity(
                source_bytes + wal_target,
                durable_bytes=source_bytes,
            )
            self._rebase_wal.unlink(missing_ok=True)
            if wal_target > 0:
                def account_wal_copy(copied_bytes: int) -> None:
                    with self._state_lock:
                        self.metrics['snapshot_wal_bytes_copied'] += max(
                            0, int(copied_bytes))

                self._copy_range_to(
                    self._rebase_wal,
                    0,
                    wal_target,
                    bytes_copied_sink=account_wal_copy,
                )

            os.replace(self._rebase_snapshot, self._snapshot)
            if self._rebase_wal.is_file():
                os.replace(self._rebase_wal, self._shadow_wal)
            else:
                self._shadow_wal.unlink(missing_ok=True)
            self._generation += 1
            self._snapshot_recovery_point_at = recovery_point_at
            self._wal_shipped = wal_target
            self._shadow_wal_matches_local = True
            if wal_target:
                self.metrics['ships'] += 1
                self.metrics['bytes_shipped'] += wal_target
                self._last_ship_wall = time.time()
            self.metrics['ship_lag_bytes'] = max(
                0, self._local_wal_size() - wal_target)
            self._write_manifest()
            fsync_directory(self._shadow_dir)
            fastpath.clear_classic_seed_provenance(self._local_db)
            self.metrics['snapshots'] += 1
            self._needs_snapshot = False
            self._set_rebase_active(False)
            published = True
        finally:
            source_guard.close()
            if published:
                self._clear_rebase_progress()
            else:
                # The database image is resumable from its last fsynced state
                # witness. The WAL prefix is cheap relative to that image and
                # intentionally recopied, so an interrupted tail cannot be
                # mistaken for a durable frame boundary.
                try:
                    self._rebase_wal.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(
                        '[shipper] could not clear partial rebase WAL: %s', exc)
        logger.info('[shipper] snapshot generation=%d complete (%d bytes)',
                    self._generation, self._snapshot.stat().st_size)


__all__ = [
    'WalShipper',
    'adaptive_wal_rebase_budget',
    'filesystem_wal_rebase_maximum',
    'proactive_wal_rebase_trigger',
]
