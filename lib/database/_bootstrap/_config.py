"""Managed PostgreSQL tuning config — code-managed postgresql.conf block.

Historically the durability + sizing settings (max_connections, wal_level,
fsync, …) were appended to postgresql.conf MANUALLY, once, under a
"# ── ChatUI Custom Config ──" header. Nothing in the codebase maintained
them, so bumping the app-side TOFU_DB_MAX_CONNS did NOT raise PG's own
max_connections ceiling, and durability settings could silently drift.

This module makes the config code-managed: every owned-PG startup rewrites a
single delimited block (idempotently). PG reads the LAST occurrence of a
setting in the file, so appending our block also overrides any older manual
entries above it.

Extracted from the monolithic ``_bootstrap.py`` (facade-preserving split).
"""

import os
import subprocess
import sys

from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.database._pg_ownership import _find_pg_binary

logger = get_logger(__name__)

_PROJECT_ENV_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', '.env'))


# ─────────────────────────────────────────────────────────────────────
#  Managed PostgreSQL tuning block
# ─────────────────────────────────────────────────────────────────────

_MANAGED_BLOCK_BEGIN = '# ── Tofu managed config (auto-generated; do not edit) BEGIN ──'
_MANAGED_BLOCK_END = '# ── Tofu managed config END ──'


def _project_config_value(name, default=None):
    """Read a managed-PG knob with the project ``.env`` as authority.

    ``server.py`` and ``bootstrap.py`` load ``.env`` before importing the
    database stack, but maintenance scripts import this module directly.  The
    old environment-only read therefore made those scripts rewrite the NEXT
    PostgreSQL restart config with defaults (observed: explicit 200 silently
    became 96).  Managed on-disk config must have one answer regardless of the
    entry point that happened to import it.

    Modern ``TOFU_*`` wins over its legacy ``CHATUI_*`` alias, matching
    :func:`getenv_compat`.  If the file has neither, normal process-environment
    precedence is preserved for advanced/ephemeral deployments.
    """
    file_values = {}
    try:
        if os.path.isfile(_PROJECT_ENV_PATH):
            with open(_PROJECT_ENV_PATH, encoding='utf-8') as env_file:
                for raw_line in env_file:
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, _, value = line.partition('=')
                    file_values[key.strip()] = value.strip()
    except OSError as exc:
        logger.debug('[PG-Config] could not read project .env: %s', exc)
    if name in file_values:
        return file_values[name]
    if name.startswith('TOFU_'):
        legacy = 'CHATUI_' + name[len('TOFU_'):]
        if legacy in file_values:
            return file_values[legacy]
    return getenv_compat(name, default=default)


def _bounded_env_int(name, default, minimum, maximum):
    """Read an integer setting without letting a bad env brick startup."""
    raw = _project_config_value(name, default=str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[DB] Invalid %s=%r; using %d', name, raw, default)
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def _detect_memory_limit_mb():
    """Best-effort effective memory limit (cgroup first, host fallback)."""
    candidates = []
    for path in ('/sys/fs/cgroup/memory.max',
                 '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        try:
            with open(path, encoding='ascii') as f:
                raw = f.read().strip()
            if raw and raw != 'max':
                value = int(raw)
                # cgroup-v1 commonly exposes a near-LONG_MAX sentinel for
                # "unlimited"; ignore it rather than sizing against exabytes.
                if 256 * 1024 * 1024 <= value < (1 << 60):
                    candidates.append(value // (1024 * 1024))
        except (OSError, TypeError, ValueError) as exc:
            logger.debug('[PG-Config] memory-limit probe %s unavailable: %s',
                         path, exc)
    try:
        pages = int(os.sysconf('SC_PHYS_PAGES'))
        page_size = int(os.sysconf('SC_PAGE_SIZE'))
        if pages > 0 and page_size > 0:
            candidates.append((pages * page_size) // (1024 * 1024))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        logger.debug('[PG-Config] host-memory sysconf probe unavailable: %s',
                     exc)
    return min(candidates) if candidates else 8192


def _memory_tuning_mb(total_mb=None):
    """Conservative PG memory settings for a personal server.

    ``shared_buffers`` is real reserved memory, so cap it at 2 GiB and only
    use 1/16 of the effective machine/cgroup limit. ``effective_cache_size``
    is a planner hint (not an allocation) and can describe more of the OS page
    cache. Individual env overrides keep advanced installs fully controllable.
    """
    detected = _detect_memory_limit_mb() if total_mb is None else int(total_mb)
    budget = _bounded_env_int(
        'TOFU_PG_MEMORY_BUDGET_MB', detected, 1024, 1024 * 1024)
    shared_default = max(256, min(2048, budget // 16))
    shared = _bounded_env_int(
        'TOFU_PG_SHARED_BUFFERS_MB', shared_default, 128, 16384)
    cache_default = max(shared * 2, min(16384, budget // 4))
    effective_cache = _bounded_env_int(
        'TOFU_PG_EFFECTIVE_CACHE_MB', cache_default, shared, 262144)
    maintenance_default = max(128, min(512, budget // 128))
    maintenance = _bounded_env_int(
        'TOFU_PG_MAINTENANCE_WORK_MEM_MB', maintenance_default, 64, 4096)
    work_mem = _bounded_env_int('TOFU_PG_WORK_MEM_MB', 8, 1, 256)
    return {
        'shared_buffers': shared,
        'effective_cache_size': effective_cache,
        'maintenance_work_mem': maintenance,
        'work_mem': work_mem,
    }


# PG server max sits above the app semaphore (personal-server default 64), so
# the application remains the binding constraint while admin/migration slots
# retain headroom. Avoid the old 200-backend default: it allowed a thread-local
# leak to consume substantial RAM before PG applied backpressure.
_APP_CONN_CEILING = _bounded_env_int(
    'TOFU_DB_MAX_CONNS', 64, minimum=4, maximum=4096)
_MANAGED_PG_MAX_CONNECTIONS = _bounded_env_int(
    'TOFU_PG_MAX_CONNECTIONS', max(96, _APP_CONN_CEILING + 24),
    minimum=32, maximum=4096)


def _tier_b_enabled():
    """True when Tier B (WAL archive + PITR) is opted in."""
    return _project_config_value(
        'TOFU_DB_TIER_B', default='0').lower() in ('1', 'true', 'yes')


def _pgdata_is_resolved_primary(pgdata):
    """True when *pgdata* is the LOCAL primary the split resolves to (post-flip).

    Tier B archiving must engage ONLY against the local primary — never the
    legacy FUSE cluster while resolution is still on legacy (pre-seed). Writing
    archive_mode into the soon-to-be-retired legacy cluster would waste FUSE
    writes and muddy the §3a WAL-tail timestamp.
    """
    try:
        from lib.database.db_paths import resolve_pgdata_dir
        from lib.runtime_paths import data_root
        return os.path.abspath(pgdata) == os.path.abspath(resolve_pgdata_dir(data_root()))
    except Exception as e:
        logger.debug('[DB] primary-resolution probe failed: %s', e)
        return False


def _build_managed_pg_config(archive_enabled=False):
    """Return the body (settings only) of the managed postgresql.conf block.

    Args:
        archive_enabled: When True, emit Tier B ``archive_mode=on`` +
            ``archive_command``. Caller passes this only for the resolved LOCAL
            primary when TOFU_DB_TIER_B is opted in (never the legacy cluster).

    Durability is deliberately kept SAFE (fsync + synchronous_commit on,
    full_page_writes on) — the cluster lives on a shared FUSE mount where a
    torn page on crash would corrupt the whole cluster, exactly the failure
    that produced the 'lost conversations' incident. ``wal_level=replica``
    (up from the old ``minimal``) is what makes PITR / base-backup-based
    recovery possible, so a future corrupt primary is recoverable to a
    point-in-time instead of needing a data-losing ``pg_resetwal -f``.
    """
    memory = _memory_tuning_mb()
    return [
        f'max_connections = {_MANAGED_PG_MAX_CONNECTIONS}',
        'superuser_reserved_connections = 10',
        # Server-side backstop for leaked transactions: PG kills any backend
        # left 'idle in transaction' past this. Matched to the app-side idle
        # reaper (TOFU_DB_IDLE_RELEASE_S, default 120s) so a connection parked
        # mid-transaction by a long-lived worker is reclaimed even though the
        # app-side reaper deliberately skips non-IDLE connections.
        'idle_in_transaction_session_timeout = 120s',
        # ── Durability (do NOT relax on a FUSE-mounted cluster) ──
        'fsync = on',
        'synchronous_commit = on',
        'full_page_writes = on',
        # ── WAL: replica level enables base-backup + PITR recovery ──
        'wal_level = replica',
        'max_wal_senders = 10',
        'wal_compression = on',
        # The live workload spent almost every 5-minute checkpoint window
        # writing. Fewer, well-spread checkpoints cut FUSE fsync churn without
        # relaxing commit durability.
        'checkpoint_timeout = 15min',
        'max_wal_size = 4GB',
        'min_wal_size = 512MB',
        'checkpoint_completion_target = 0.9',
        # Backend writer hit its page cap ~180k times in the measured cluster;
        # let it smooth dirty-page eviction before foreground backends must do
        # the write themselves.
        'bgwriter_lru_maxpages = 1000',
        'bgwriter_lru_multiplier = 4.0',
        # Short OLTP queries dominate; LLVM JIT setup is pure overhead there.
        'jit = off',
        # Enables pg_stat_io/statement diagnosis with negligible overhead on
        # modern Linux, important for distinguishing DB work from FUSE stalls.
        'track_io_timing = on',
        # ── Bounded, privacy-preserving native logs ──
        # pg_ctl's ``-l logs/postgresql.log`` redirects stderr to one append-
        # only file.  It reached 2.68 GiB in the audited personal install, and
        # a failed huge JSON statement placed full conversation text in it.
        # Once the collector starts, stderr becomes a tiny bootstrap log and
        # PostgreSQL owns an hourly, week-cyclic family under pgdata/log/.
        'logging_collector = on',
        "log_directory = 'log'",
        "log_filename = 'postgresql-%a-%H.log'",
        'log_rotation_age = 1h',
        'log_rotation_size = 0',
        'log_truncate_on_rotation = on',
        'log_file_mode = 0600',
        # Preserve ERROR/FATAL metadata while refusing to echo the SQL text or
        # bound parameter values.  Application slow-query logs identify the
        # operation shape without copying user conversations to disk.
        'log_statement = none',
        'log_min_error_statement = panic',
        'log_parameter_max_length_on_error = 0',
        # Connection churn once dominated this log and adds no signal for the
        # single-user pooled server. Lock waits remain available through app
        # diagnostics and pg_stat views when an incident is investigated.
        'log_connections = off',
        'log_disconnections = off',
        # ── Adaptive memory sizing (bounded for small/new installations) ──
        f"shared_buffers = {memory['shared_buffers']}MB",
        f"effective_cache_size = {memory['effective_cache_size']}MB",
        f"work_mem = {memory['work_mem']}MB",
        f"maintenance_work_mem = {memory['maintenance_work_mem']}MB",
    ] + ([
        # ── Tier B: continuous WAL archiving to the DolphinFS durability
        # target (only on the resolved local primary; see B1/B2). The shim is
        # FUSE-stall-safe (hard timeout, non-zero-to-retain). Invoked as a
        # module so it inherits our env/paths and takes no shell string.
        'archive_mode = on',
        "archive_command = '%s -m lib.database.wal_archive archive %%p %%f'"
        % (sys.executable or 'python3'),
    ] if archive_enabled else [])


def _ensure_managed_pg_config(pgdata):
    """Idempotently write the managed tuning block into postgresql.conf.

    Returns:
        bool: True if the on-disk config CHANGED (caller should restart PG
        for ``max_connections`` / ``wal_level`` — which need a restart — to
        take effect), False if it was already up to date or on error.
    """
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return False

    # Tier B archiving engages ONLY when opted in AND this pgdata is the
    # resolved local primary (never the legacy cluster pre-flip).
    archive_enabled = _tier_b_enabled() and _pgdata_is_resolved_primary(pgdata)
    settings = _build_managed_pg_config(archive_enabled=archive_enabled)
    block_lines = [_MANAGED_BLOCK_BEGIN, *settings, _MANAGED_BLOCK_END]
    new_block = '\n'.join(block_lines) + '\n'

    try:
        with open(conf_path, encoding='utf-8') as f:
            content = f.read()

        import re
        # Strip any prior managed block (between the BEGIN/END markers).
        pattern = re.compile(
            re.escape(_MANAGED_BLOCK_BEGIN) + r'.*?' + re.escape(_MANAGED_BLOCK_END) + r'\n?',
            re.DOTALL)
        stripped = pattern.sub('', content)

        desired = stripped.rstrip('\n') + '\n\n' + new_block
        if desired == content:
            logger.debug('[DB] Managed PG config already current — no change')
            return False

        # Write atomically (temp file + replace) so a crash mid-write can't
        # leave a truncated postgresql.conf that bricks startup.
        tmp_path = conf_path + '.tofu.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(desired)
        os.replace(tmp_path, conf_path)
        logger.info('[DB] Wrote managed PG config block '
                    '(max_connections=%d, wal_level=replica, fsync=on) — '
                    'restart required for connection/WAL settings',
                    _MANAGED_PG_MAX_CONNECTIONS)
        return True
    except Exception as e:
        logger.warning('[DB] Could not write managed PG config block: %s', e)
        return False


def _restart_local_pg(pgdata, base_dir, *, maintenance_approved=False):
    """Restart owned PG only behind an explicit maintenance-window token.

    This helper used to be reachable after any managed-config text change,
    including import-time discovery and health recovery. Keep the low-level
    operation for an intentional admin flow, but make the unsafe historical
    two-positional-argument call fail closed. Best-effort: on execution
    failure the running PG keeps its previous (still-valid) config.

    Returns:
        bool: True on a successful restart.
    """
    if maintenance_approved is not True:
        logger.error(
            '[DB] Refusing disruptive PostgreSQL restart without explicit '
            'maintenance approval')
        return False
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    try:
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
             'restart', '-m', 'fast', '-w', '-t', '30'],
            capture_output=True, text=True, timeout=45
        )
        if result.returncode != 0:
            logger.error('[DB] pg_ctl restart (for managed config) failed: %s',
                         (result.stderr or '').strip()[:300])
            return False
        logger.info('[DB] Restarted local PG to apply managed config')
        return True
    except Exception as e:
        logger.error('[DB] pg_ctl restart raised: %s', e, exc_info=True)
        return False
