"""Fail-closed sidecar configuration; paths are derived inside the process."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import hashlib
import os
from pathlib import Path
import tempfile

from runtime_guards import (
    deployment_resource_default,
    load_deployment_configuration,
)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, '')
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'invalid {name}') from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f'{name} must be between {minimum} and {maximum}')
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, '')
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'invalid {name}') from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f'{name} must be between {minimum} and {maximum}')
    return value


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    project_root: Path
    data_dir: Path
    logs_dir: Path
    backend: str
    deployment_mode: str
    process_role: str
    replica_id: str | None
    token: str
    sqlite_path: Path
    postgres_dsn: str = field(repr=False)
    redis_url: str = field(repr=False)
    allow_schema_migration: bool
    read_pool_size: int
    write_pool_size: int
    rpc_capacity: int = 8
    acquire_timeout_s: float = 2.0
    transaction_timeout_s: float = 5.0
    sqlite_writer_cache_mib: int = 32
    fastpath_wal_rebase_max_mib: int = 512
    idle_trim_rss_mib: int = 256
    idle_trim_cooldown_s: float = 300.0
    turn_search_projection_dir: Path = field(default_factory=Path)
    turn_search_projection_max_mib: int = 512
    turn_search_backfill_delay_s: float = 60.0
    writer_stall_grace_s: float = 15.0
    writer_hard_kill_s: float = 60.0
    idle_lifetime_s: float = 60.0
    max_lifetime_s: float = 900.0
    distributed_preview_read_only: bool = False
    logical_shadow_mode: str = 'off'
    logical_shadow_dir: Path | None = None
    logical_outbox_max_bytes: int = 128 * 1024 * 1024
    logical_shadow_max_bytes: int = 512 * 1024 * 1024
    logical_shadow_segment_bytes: int = 64 * 1024 * 1024
    logical_record_max_bytes: int = 4 * 1024 * 1024
    logical_publish_batch_size: int = 32
    logical_shadow_access: str = 'owner'

    def __post_init__(self) -> None:
        # Direct construction is a supported test/tool seam. Never let its
        # omitted reconstructible-cache path resolve to the caller's current
        # working directory: that leaks projection databases into source trees
        # and lets otherwise isolated authority fixtures share one cache.
        if self.turn_search_projection_dir == Path():
            object.__setattr__(
                self,
                'turn_search_projection_dir',
                (self.data_dir / 'projections').resolve(),
            )

    @classmethod
    def from_environment(cls) -> 'SidecarConfig':
        allow_test_override = (
            os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1')
        deployment = load_deployment_configuration(
            allow_test_backend_override=allow_test_override)
        test_backend = os.environ.get('TOFU_STORAGE_TEST_BACKEND', '').strip().lower()
        if test_backend:
            if not allow_test_override:
                raise RuntimeError(
                    'TOFU_STORAGE_TEST_BACKEND requires explicit test authority')
            if test_backend not in {'sqlite', 'postgres'}:
                raise RuntimeError(
                    'TOFU_STORAGE_TEST_BACKEND must be sqlite or postgres')
            backend = test_backend
        else:
            backend = deployment.storage_backend

        postgres_dsn = deployment.postgres_dsn
        if backend == 'postgres' and not postgres_dsn:
            # Repository contract tests may exercise the PostgreSQL adapter
            # while the parent process remains in personal mode.  Keep that
            # authority private and file-based so no production environment
            # regains a plaintext DSN or legacy backend selector.
            test_dsn_file = os.environ.get(
                'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', '').strip()
            if not allow_test_override or not test_dsn_file:
                raise RuntimeError(
                    'PostgreSQL test backend requires '
                    'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE')
            test_dsn_path = Path(test_dsn_file)
            if not test_dsn_path.is_absolute() or not test_dsn_path.is_file():
                raise RuntimeError(
                    'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE must be an absolute '
                    'readable file')
            try:
                if not 0 < test_dsn_path.stat().st_size <= 16 * 1024:
                    raise RuntimeError(
                        'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE has invalid size')
                postgres_dsn = test_dsn_path.read_text(
                    encoding='utf-8').strip()
            except (OSError, UnicodeError) as exc:
                raise RuntimeError(
                    'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE is not readable') from exc
            if not postgres_dsn or '\x00' in postgres_dsn:
                raise RuntimeError(
                    'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE contains an invalid DSN')
        token = os.environ.get('TOFU_STORAGE_TOKEN', '')
        if len(token) < 32:
            raise RuntimeError('TOFU_STORAGE_TOKEN is missing or too short')

        override = os.environ.get('TOFU_STORAGE_PROJECT_ROOT', '').strip()
        if override:
            if os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') != '1':
                raise RuntimeError('project-root override requires explicit test authority')
            project_root = Path(override).resolve()
            data_dir = (project_root / 'data').resolve()
            logs_dir = (project_root / 'logs').resolve()
        else:
            from lib.runtime_paths import data_root, logs_root

            data_dir = Path(data_root()).resolve()
            logs_dir = Path(logs_root()).resolve()
            project_root = data_dir.parent
            if logs_dir.parent != project_root:
                raise RuntimeError(
                    'runtime data and log roots must share one authority root')
        for path in (data_dir, logs_dir):
            try:
                path.relative_to(project_root)
            except ValueError as exc:
                raise RuntimeError('persistent storage escaped the project root') from exc
            path.mkdir(parents=True, exist_ok=True)

        projection_override = os.environ.get(
            'TOFU_TURN_SEARCH_PROJECTION_DIR', '').strip()
        if projection_override:
            projection_dir = Path(projection_override)
            if not projection_dir.is_absolute():
                raise RuntimeError(
                    'TOFU_TURN_SEARCH_PROJECTION_DIR must be absolute')
            projection_dir = projection_dir.resolve()
        elif allow_test_override:
            # Test authorities already live in disposable, worker-isolated
            # roots. Keeping the projection there makes cleanup deterministic.
            projection_dir = (data_dir / 'projections').resolve()
        else:
            # This database is reconstructible. Put it on the host-local cache
            # path instead of beside a potentially remote/FUSE authority.
            # Namespace by authority path so two Tofu installations owned by
            # the same uid can never adopt each other's projection.
            authority_key = hashlib.sha256(
                str(data_dir).encode('utf-8')).hexdigest()[:16]
            uid = getattr(os, 'getuid', lambda: 0)()
            projection_dir = (
                Path(tempfile.gettempdir())
                / f'tofu-projections-{uid}'
                / authority_key
            ).resolve()

        if backend == 'sqlite':
            read_pool_size = _bounded_int(
                'TOFU_STORAGE_SQLITE_READ_POOL',
                deployment_resource_default(
                    'TOFU_STORAGE_SQLITE_READ_POOL', os.environ),
                1,
                64,
            )
            write_pool_size = 1
        else:
            read_pool_size = _bounded_int('TOFU_STORAGE_PG_READ_POOL', 32, 1, 256)
            write_pool_size = _bounded_int('TOFU_STORAGE_PG_WRITE_POOL', 16, 1, 128)
        # One socket maps to one request thread. The historical fixed 256-slot
        # ceiling let a personal Sidecar retain scores of glibc arenas even
        # though SQLite has one writer and a small read pool. Keep the personal
        # envelope aligned with useful backend concurrency; distributed
        # replicas retain a larger, explicitly overridable budget.
        rpc_capacity = _bounded_int(
            'TOFU_STORAGE_RPC_CAPACITY',
            deployment_resource_default(
                'TOFU_STORAGE_RPC_CAPACITY', os.environ),
            2,
            256,
        )
        sqlite_writer_cache_mib = _bounded_int(
            'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB',
            deployment_resource_default(
                'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB', os.environ),
            8,
            256,
        )
        # The writer cache is already derived from the launch-time memory
        # probe.  Scale the Sidecar's idle heap-return threshold from that same
        # observable budget instead of assuming the process owns the host.
        idle_trim_default_mib = (
            max(1024, min(4096, sqlite_writer_cache_mib * 16))
            if deployment.mode == 'distributed'
            else max(128, min(512, sqlite_writer_cache_mib * 8))
        )
        idle_trim_rss_mib = _bounded_int(
            'TOFU_STORAGE_IDLE_TRIM_RSS_MIB',
            idle_trim_default_mib,
            64,
            16 * 1024,
        )
        writer_stall_grace_s = _bounded_float(
            'TOFU_STORAGE_WRITER_STALL_GRACE_S', 15.0, 1.0, 120.0)
        writer_hard_kill_s = _bounded_float(
            'TOFU_STORAGE_WRITER_HARD_KILL_S', 60.0, 5.0, 600.0)
        if writer_hard_kill_s <= writer_stall_grace_s:
            raise RuntimeError(
                'TOFU_STORAGE_WRITER_HARD_KILL_S must be greater than '
                'TOFU_STORAGE_WRITER_STALL_GRACE_S')
        logical_shadow_mode = str(os.environ.get(
            'TOFU_STORAGE_LOGICAL_SHADOW', 'off')).strip().lower()
        if logical_shadow_mode not in {'off', 'auto', 'required'}:
            raise RuntimeError(
                'TOFU_STORAGE_LOGICAL_SHADOW must be off, auto, or required')
        logical_shadow_access = str(os.environ.get(
            'TOFU_STORAGE_LOGICAL_ACCESS', 'owner')).strip().lower()
        if logical_shadow_access not in {'owner', 'group'}:
            raise RuntimeError(
                'TOFU_STORAGE_LOGICAL_ACCESS must be owner or group')
        logical_shadow_dir_raw = str(os.environ.get(
            'TOFU_STORAGE_LOGICAL_SHADOW_DIR', '')).strip()
        logical_shadow_dir = None
        if logical_shadow_dir_raw:
            logical_shadow_dir = Path(logical_shadow_dir_raw)
            if not logical_shadow_dir.is_absolute():
                raise RuntimeError(
                    'TOFU_STORAGE_LOGICAL_SHADOW_DIR must be absolute')
            logical_shadow_dir = logical_shadow_dir.resolve()
        log_budget_mib = deployment_resource_default(
            'TOFU_LOG_TOTAL_BUDGET_MB', os.environ)
        logical_outbox_mib = _bounded_int(
            'TOFU_STORAGE_LOGICAL_OUTBOX_MAX_MIB',
            max(32, min(512, log_budget_mib)),
            16,
            8192,
        )
        logical_shadow_mib = _bounded_int(
            'TOFU_STORAGE_LOGICAL_SHADOW_MAX_MIB',
            max(128, min(4096, logical_outbox_mib * 4)),
            64,
            32768,
        )
        logical_segment_mib = _bounded_int(
            'TOFU_STORAGE_LOGICAL_SEGMENT_MIB', 64, 8, 1024)
        logical_record_mib = _bounded_int(
            'TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB', 4, 1, 64)
        if logical_record_mib * 2 > logical_segment_mib:
            raise RuntimeError(
                'TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB must be at most half '
                'TOFU_STORAGE_LOGICAL_SEGMENT_MIB')
        if logical_segment_mib > logical_shadow_mib:
            raise RuntimeError(
                'TOFU_STORAGE_LOGICAL_SEGMENT_MIB must not exceed '
                'TOFU_STORAGE_LOGICAL_SHADOW_MAX_MIB')
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            logs_dir=logs_dir,
            backend=backend,
            deployment_mode=deployment.mode,
            process_role=deployment.process_role,
            replica_id=deployment.replica_id,
            token=token,
            sqlite_path=data_dir / 'tofu.db',
            postgres_dsn=postgres_dsn,
            redis_url=deployment.redis_url,
            allow_schema_migration=bool(test_backend),
            read_pool_size=read_pool_size,
            write_pool_size=write_pool_size,
            rpc_capacity=rpc_capacity,
            # Bulk maintenance (e.g. conversation imports of multi-MiB
            # transcripts) cannot commit inside the 5s user-latency watchdog;
            # operators running a migration window may raise it explicitly.
            transaction_timeout_s=_bounded_float(
                'TOFU_STORAGE_TRANSACTION_TIMEOUT_S', 5.0, 1.0, 120.0),
            sqlite_writer_cache_mib=sqlite_writer_cache_mib,
            fastpath_wal_rebase_max_mib=_bounded_int(
                'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB',
                deployment_resource_default(
                    'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', os.environ),
                64,
                8192,
            ),
            idle_trim_rss_mib=idle_trim_rss_mib,
            idle_trim_cooldown_s=_bounded_float(
                'TOFU_STORAGE_IDLE_TRIM_COOLDOWN_SECONDS',
                300.0,
                30.0,
                3600.0,
            ),
            turn_search_projection_dir=projection_dir,
            turn_search_projection_max_mib=_bounded_int(
                'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB',
                deployment_resource_default(
                    'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB', os.environ),
                128,
                16 * 1024,
            ),
            turn_search_backfill_delay_s=_bounded_float(
                'TOFU_TURN_SEARCH_BACKFILL_DELAY_S', 60.0, 0.0, 3600.0),
            writer_stall_grace_s=writer_stall_grace_s,
            writer_hard_kill_s=writer_hard_kill_s,
            distributed_preview_read_only=(
                deployment.distributed_preview_read_only),
            logical_shadow_mode=logical_shadow_mode,
            logical_shadow_dir=logical_shadow_dir,
            logical_outbox_max_bytes=logical_outbox_mib * 1024 * 1024,
            logical_shadow_max_bytes=logical_shadow_mib * 1024 * 1024,
            logical_shadow_segment_bytes=logical_segment_mib * 1024 * 1024,
            logical_record_max_bytes=logical_record_mib * 1024 * 1024,
            logical_publish_batch_size=_bounded_int(
                'TOFU_STORAGE_LOGICAL_PUBLISH_BATCH', 32, 1, 256),
            logical_shadow_access=logical_shadow_access,
        )


__all__ = ['SidecarConfig']
