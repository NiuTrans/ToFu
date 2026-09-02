"""Continuous WAL shipper: the fast-path front's durable shadow keeper.

See fastpath.py for the design invariants.  This module owns the runtime
loop: after every front commit (or every tick, whichever first), copy the
local WAL's unsent frame-aligned prefix to the shadow on the durable data
dir, fsync, and (throttled) replace the manifest.  Only this shipper ever
checkpoints the local authority, which is what makes the shadow a strict
byte-prefix mirror; when the WAL outgrows the budget the shipper runs a
snapshot cycle (ship-to-end → TRUNCATE checkpoint on the writer → stable
sequential DB image + concurrent WAL prefix → manifest generation
bump). A verified first activation reuses the immutable classic DB through a
same-filesystem hard link instead of allocating/copying it twice.

Crash honesty: the shadow WAL is valid by construction (every shipped frame
keeps SQLite's cumulative checksum chain; a torn tail frame fails the chain
and replay simply stops there), and the resume offset derives from the
shadow WAL's real size — the manifest is advisory.  RPO is the unshipped
tail, exported as ``ship_lag_bytes``/``last_ship_age_s``.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable

from lib.log import get_logger
from lib.storage_sidecar import fastpath
from lib.storage_sidecar.durability import fsync_directory


logger = get_logger('tofu.storage.sidecar.shipper')

_WAL_HEADER_BYTES = 32
_COPY_CHUNK_BYTES = 4 * 1024 ** 2
_MIB = 1024 ** 2
_MIN_WAL_REBASE_BYTES = 64 * _MIB
_DEFAULT_WAL_REBASE_MAX_BYTES = 512 * _MIB
_WAL_REBASE_DATABASE_FRACTION = 8
_MANIFEST_MIN_INTERVAL_S = 2.0
_MANIFEST_MIN_BYTES = 8 * 1024 ** 2
_MAX_STALE_ARTIFACTS_PER_START = 64
_PRIVATE_TEMP_PATTERN = re.compile(
    r'^(?:snapshot\.sqlite3\.tmp-(\d+)(?:-(?:journal|wal|shm))?'
    r'|shadow\.wal\.tmp-(\d+)'
    r'|snapshot\.sqlite3\.seed-link-(\d+)'
    r'|shadow\.wal\.seed-copy-(\d+))$')


class _SnapshotCancelled(RuntimeError):
    """Cooperative stop for a reconstructible snapshot copy."""


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
        self._authority_uuid = authority_uuid
        self._checkpoint_fn = checkpoint_fn
        self._checkpoint_deadline_fn = checkpoint_deadline_fn
        try:
            self._database_bytes_at_start = self._local_db.stat().st_size
        except OSError:
            self._database_bytes_at_start = 0
        if wal_budget_bytes is None:
            self._wal_budget_bytes = adaptive_wal_rebase_budget(
                self._database_bytes_at_start,
                wal_budget_max_bytes,
            )
        else:
            # Explicit constructor values are a deterministic test/operator
            # seam and remain authoritative.
            self._wal_budget_bytes = max(_MIB, int(wal_budget_bytes))
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
        self.metrics: dict[str, Any] = {
            'ships': 0, 'bytes_shipped': 0, 'snapshots': 0,
            'ship_failures': 0, 'ship_lag_bytes': 0,
            'stale_artifacts_reclaimed': 0,
            'stale_artifact_bytes_reclaimed': 0,
            'wal_rebase_budget_bytes': self._wal_budget_bytes,
        }

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        self._shadow_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_artifacts()
        self._resume_or_snapshot()
        logger.info(
            '[shipper] WAL rebase budget %.1f MiB for %.1f GiB authority',
            self._wal_budget_bytes / _MIB,
            self._database_bytes_at_start / 1024 ** 3,
        )
        self._thread.start()

    def notify_commit(self) -> None:
        """Writer-thread hook: a commit landed on the front's WAL."""
        self._wake.set()

    def pin_checkpointed_snapshot_for_backup(
        self,
        destination: Path,
        *,
        deadline_at: float,
    ) -> dict[str, Any]:
        """Create one stable, standalone backup image under the ship lock.

        A forced rebase checkpoints every commit acknowledged before this call
        into the database image. Commits accepted during the sequential shadow
        copy remain in the new WAL and intentionally belong to the next backup.
        The published image is then hard-linked when the backup destination is
        on the same filesystem, or copied sequentially across devices.
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
            return {
                'generation': self._generation,
                'bytes': source_bytes,
                'copy_strategy': strategy,
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
        except OSError:
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
        if not final and local_size >= self._wal_budget_bytes:
            self._snapshot_cycle()

    def _copy_range(self, start: int, end: int) -> None:
        self._copy_range_to(self._shadow_wal, start, end)

    def _copy_range_to(self, destination_path: Path,
                       start: int, end: int) -> None:
        with self._local_wal.open('rb') as source:
            source.seek(start)
            mode = 'r+b' if destination_path.exists() else 'wb'
            with destination_path.open(mode) as destination:
                destination.seek(start)
                remaining = end - start
                while remaining > 0:
                    chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    destination.write(chunk)
                    remaining -= len(chunk)
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
        fastpath.write_shadow_manifest(self._shadow_dir, {
            'format': 'tofu.fastpath-shadow.v1',
            'authority_uuid': self._authority_uuid,
            'generation': self._generation,
            'wal_shipped_bytes': self._wal_shipped,
            'snapshot_bytes': (self._snapshot.stat().st_size
                               if self._snapshot.is_file() else 0),
            'updated_at': time.time(),
        })
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
        frame_size = self._frame_size()
        if frame_size is not None and self._shadow_wal_matches_local:
            target = self._floor_frame(self._local_wal_size(), frame_size)
            if target > self._wal_shipped:
                self._copy_range(self._wal_shipped, target)
                self._wal_shipped = target
        # The checkpoint MUST run on the writer connection (single-writer
        # authority); checkpoint_fn is the backend's maintenance-lane hop.
        if deadline_at is not None and self._checkpoint_deadline_fn is not None:
            self._checkpoint_deadline_fn(deadline_at)
        else:
            self._checkpoint_fn()
        # The checkpoint starts a new local WAL generation. Until a replacement
        # snapshot is atomically published, the old durable pair remains valid
        # but must never receive bytes from that new generation. Any later
        # failure therefore leaves an explicit rebase pending.
        self._shadow_wal_matches_local = False
        self._needs_snapshot = True
        temporary = self._snapshot.with_name(
            self._snapshot.name + f'.tmp-{os.getpid()}')
        temporary_wal = self._shadow_wal.with_name(
            self._shadow_wal.name + f'.tmp-{os.getpid()}')
        for path in (
            temporary,
            temporary.with_name(temporary.name + '-journal'),
            temporary.with_name(temporary.name + '-wal'),
            temporary.with_name(temporary.name + '-shm'),
            temporary_wal,
        ):
            path.unlink(missing_ok=True)

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

            source_identity = fastpath._source_fingerprint(self._local_db)
            source_bytes = int(source_identity['size'])

            def cancel_snapshot_copy(_copied_bytes: int) -> None:
                with self._state_lock:
                    stopping = self._stop
                if stopping:
                    raise _SnapshotCancelled(
                        'snapshot interrupted by Sidecar shutdown')
                if deadline_at is not None and time.monotonic() >= deadline_at:
                    raise TimeoutError(
                        'SQLite backup deadline expired during shadow snapshot')

            fastpath._copy_file_checkpointed(
                self._local_db,
                temporary,
                expected_bytes=source_bytes,
                durable_bytes=0,
                checkpoint=lambda _copied: None,
                progress=cancel_snapshot_copy,
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
            if wal_target > 0:
                self._copy_range_to(temporary_wal, 0, wal_target)

            os.replace(temporary, self._snapshot)
            if temporary_wal.is_file():
                os.replace(temporary_wal, self._shadow_wal)
            else:
                self._shadow_wal.unlink(missing_ok=True)
            self._generation += 1
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
            published = True
        finally:
            source_guard.close()
            if not published:
                for path in (temporary, temporary_wal):
                    path.unlink(missing_ok=True)
        logger.info('[shipper] snapshot generation=%d complete (%d bytes)',
                    self._generation, self._snapshot.stat().st_size)


__all__ = ['WalShipper', 'adaptive_wal_rebase_budget']
