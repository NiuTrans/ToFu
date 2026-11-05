"""Dependency-light process policies that must run before optional imports.

This module intentionally uses only the Python standard library.  Importing a
``lib.*`` helper first executes ``lib/__init__.py`` and is therefore too late
for policies that protect the very beginning of ``server.py`` / healthcheck /
pytest collection.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, urlsplit

from tofu_workers.resources import (
    SystemResourceSnapshot, _cgroup_cpu_count,
    _cgroup_memory_bytes, _host_memory_bytes,
    disk_capacity_bytes as _disk_capacity_bytes,
    measure_resources as _measure_system_resources,
)

__all__ = [
    'DeploymentConfiguration',
    'RESOURCE_BUDGET_AUTOMATIC_ENV',
    'RESOURCE_BUDGET_ENV_KEYS',
    'RESOURCE_BUDGET_POLICY_ENV',
    'RESOURCE_BUDGET_POLICY_VERSION',
    'ResourceBudgetManifest',
    'SystemResourceSnapshot',
    'deployment_resource_default',
    'distributed_preview_is_read_only',
    'enforce_deployment_configuration',
    'install_process_resource_defaults',
    'install_pymupdf_classic_policy',
    'install_runtime_resource_defaults',
    'load_deployment_configuration',
    'pid_is_alive',
    'proc_stat_fields',
    'probe_system_resources',
    'resolve_deployment_mode',
    'resource_budget_manifest',
    'resolve_resource_budget',
    'task_concurrency_hard_ceiling',
    'MAX_TASK_CONCURRENCY',
    'UNLIMITED',
    'UNLIMITED_SETTINGS',
]

# Value of every execution concurrency/admission setting: hardware (CPU count,
# memory capacity/headroom, process RSS, cgroup pressure) never limits work.
# Owner decision 2026-10-24; see docs/RESOURCE_CONTROL.md "Hardware never
# limits work" and tests/meta/test_hardware_never_limits_work.py.
UNLIMITED = sys.maxsize


_DEPLOYMENT_MODES = frozenset({'personal', 'distributed'})
# Legacy Coordinator maxConcurrent schema/catalog value. Never use this
# value to size execution or refuse admission; active work is unlimited.
MAX_TASK_CONCURRENCY = 4096
_PROCESS_ROLES = frozenset({'all', 'api', 'worker', 'scheduler'})
_REMOVED_DEPLOYMENT_ENV = (
    'TOFU_REQUIRE_PG',
    'TOFU_REPLICA_RING',
    'TOFU_STORAGE_MODE',
)
# Live tofu-db DAEMON diagnostics the Rust side reads from its own process
# env (packages/tofu-db/src/daemon.rs) — the blanket TOFU_DB_* stale-knob
# rejection below predates them (guard 2026-08-25, knobs 2026-09-06+) and was
# never meant to make them unsettable. Removed topology switches
# (TOFU_DB_BACKEND/TOFU_DB_PATH) stay fail-closed.
_DAEMON_ENV_PASSTHROUGH = frozenset({
    'TOFU_DB_FSYNC_STATS',
    # Bounded content-free Rust request diagnostics; not a storage selector.
    'TOFU_DB_REQUEST_PROFILE_EVERY',
    # Deferred-application entity write overlay switch, read by the Rust
    # engine at authority open (packages/tofu-db/src/entity.rs;
    # docs/TOFUDB_WRITE_PATH_OVERLAY.md). Opt-in per daemon while the
    # application certification gates run; not a topology switch.
    'TOFU_DB_WRITE_OVERLAY',
    # Bounded operator overrides for the overlay merge triggers
    # (packages/tofu-db/src/entity.rs; clamped in-engine).
    'TOFU_DB_OVERLAY_BUDGET_BYTES',
    'TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES',
    # Explicit WAL durability selection, validated by native_launch and
    # converted to serve arguments (lib/storage_authority_sql/native_launch.py).
    # These were registered there with full validation but never added to
    # this passthrough, so the boot gate rejected them as "removed
    # deployment configuration" — the knobs only worked on paths that
    # bypass the gate. Without them the launch probe tiers durability
    # automatically from the authority volume class.
    'TOFU_DB_DURABILITY',
    'TOFU_DB_WAL_FLUSH_INTERVAL_MS',
    # Checkpoint pack sizing: payload-segment bytes per materialization
    # batch, clamped in-engine at half the segment budget
    # (packages/tofu-db/src/engine.rs::checkpoint_materialize_batch_bytes).
    # High-headroom hosts widen it to cut pack-adoption frequency and raise
    # the effective full-catalog payload capacity; the personal default
    # stays lean.
    'TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES',
    # Deferred segment-retirement floor in milliseconds
    # (packages/tofu-db/src/payload_segment.rs::segment_retirement_delay).
    # The park-then-sweep model needs no topology input; 0 restores
    # immediate unlink for engines that must bisect reclaim behavior.
    'TOFU_DB_SEGMENT_RETIREMENT_DELAY_MS',
    # Physical tombstone compaction switch, read by the Rust engine at
    # authority open (packages/tofu-db/src/entity.rs;
    # docs/TOFUDB_TOMBSTONE_COMPACTION.md). L1 write-path reclamation plus
    # the L2 scan-driven maintenance vacuum; defaults off, opt-in per
    # daemon, data-safe to roll back (reclaimed entries stay absent, a
    # valid state on every build).
    'TOFU_DB_TOMBSTONE_RECLAIM',
})
_REPLICA_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_MIB = 1024 * 1024
from bootstrap_pkg.resource_policy_generated import (
    RESOURCE_CLASSES as _RESOURCE_CLASSES,
    RESOURCE_FALLBACKS as _RESOURCE_FALLBACKS,
    UNLIMITED_SETTINGS,
)

_RESOURCE_NAMES = tuple(_RESOURCE_CLASSES)
RESOURCE_BUDGET_ENV_KEYS = frozenset(_RESOURCE_NAMES)
RESOURCE_BUDGET_POLICY_VERSION = '2026-10-24.1'
RESOURCE_BUDGET_POLICY_ENV = 'TOFU_RESOURCE_BUDGET_POLICY_VERSION'
RESOURCE_BUDGET_AUTOMATIC_ENV = 'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS'
_RESOURCE_SNAPSHOT_CACHE: dict[tuple[str, ...], 'SystemResourceSnapshot'] = {}
_RESOURCE_SNAPSHOT_CACHE_CAPACITY = 16


class ResourceBudgetManifest(TypedDict):
    policy_version: str
    deployment_mode: str
    adaptive: bool
    probe: dict[str, object]
    defaults: dict[str, int]
    overrides: dict[str, str]
    automatic: list[str]


def _persistent_data_path(environment: Mapping[str, str]) -> Path:
    """Mirror the stdlib-visible data-layout choice without importing lib.*."""
    raw_path = (environment.get('TOFU_PROJECT_PATH') or '').strip()
    root = Path(raw_path) if raw_path else Path(__file__).resolve().parent
    explicit_data = (environment.get('TOFU_DATA_DIR') or '').strip()
    if explicit_data:
        configured = Path(os.path.abspath(explicit_data))
        return (
            configured if configured.name == 'data'
            else configured / 'data')

    layout = (environment.get('TOFU_DATA_LAYOUT') or 'auto').strip().lower()
    intree_data = root / 'data'
    if layout == 'intree':
        return intree_data

    if sys.platform.startswith('win'):
        user_base = Path(
            environment.get('LOCALAPPDATA') or os.path.expanduser('~')) / 'Tofu'
    elif sys.platform == 'darwin':
        user_base = Path(os.path.expanduser('~')) / 'Library' \
            / 'Application Support' / 'Tofu'
    else:
        user_base = Path(
            environment.get('XDG_DATA_HOME')
            or Path(os.path.expanduser('~')) / '.local' / 'share') / 'Tofu'
    per_user_data = user_base / 'data'
    if layout == 'xdg':
        return per_user_data

    # runtime_paths treats unknown values as auto: preserve populated legacy
    # installs in place, while a fresh source checkout uses the per-user root.
    try:
        with os.scandir(intree_data) as entries:
            if next(entries, None) is not None:
                return intree_data
    except OSError:
        pass
    return per_user_data


def _project_disk_bytes(environment: Mapping[str, str]) -> tuple[int | None, int | None]:
    """Observe the configured persistent volume through the shared probe."""
    return _disk_capacity_bytes(_persistent_data_path(environment))


def observe_host_memory_available_bytes() -> int | None:
    """Read only the current host memory counter; never refresh launch policy."""
    return _host_memory_bytes()[1]


def probe_system_resources(
    environment: Mapping[str, str] | None = None,
    *,
    refresh: bool = False,
) -> SystemResourceSnapshot:
    """Probe effective personal-computer resources with safe platform fallbacks.

    CPU capacity is the minimum of host count, process affinity/cpuset, and a
    finite cgroup quota. Memory capacity/headroom are likewise the minimum of
    host and cgroup views. The result is cached per resolved data path so every
    default selected during one process boot comes from the same observation.
    Cache lookup uses layout inputs only: resolving an auto layout may scan a
    remote directory, so that work belongs exclusively to a cache miss/refresh.
    """
    env = os.environ if environment is None else environment
    cache_key = tuple(str(env.get(name) or '') for name in (
        'TOFU_PROJECT_PATH', 'TOFU_DATA_DIR', 'TOFU_DATA_LAYOUT',
        'XDG_DATA_HOME', 'LOCALAPPDATA',
    )) + (os.path.expanduser('~'), os.getcwd())
    if not refresh and cache_key in _RESOURCE_SNAPSHOT_CACHE:
        return _RESOURCE_SNAPSHOT_CACHE[cache_key]

    snapshot = _measure_system_resources(
        cgroup_cpus=_cgroup_cpu_count(), host_memory=_host_memory_bytes(),
        cgroup_memory=_cgroup_memory_bytes(), disk_bytes=_project_disk_bytes(env))
    if cache_key not in _RESOURCE_SNAPSHOT_CACHE:
        while len(_RESOURCE_SNAPSHOT_CACHE) >= _RESOURCE_SNAPSHOT_CACHE_CAPACITY:
            _RESOURCE_SNAPSHOT_CACHE.pop(next(iter(_RESOURCE_SNAPSHOT_CACHE)))
    _RESOURCE_SNAPSHOT_CACHE[cache_key] = snapshot
    return snapshot


def _personal_resource_defaults(
    snapshot: SystemResourceSnapshot,
) -> dict[str, int]:
    """Size evict-only retention from the launch memory and disk observations.

    Every name returned here is a ``retention`` setting: a cache, TTL, log,
    replay window or disk budget that bounds growth and never refuses or
    delays work. CPU count never contributes. ``unlimited`` and ``fixed``
    settings never reach this function (see ``deployment_resource_default``).
    """
    capacity_known = snapshot.effective_memory_capacity_mb is not None
    capacity_mb = snapshot.effective_memory_capacity_mb or 4096
    available_mb = snapshot.effective_memory_available_mb
    # One retention unit needs 2 GiB of installed/effective capacity and at
    # least 1 GiB that is currently available. This keeps a large but busy
    # workstation from retaining caches sized for an empty dedicated server.
    memory_units = (
        max(1, (capacity_mb + 1023) // 2048) if capacity_known else 1)
    if available_mb is not None:
        memory_units = min(
            memory_units, max(1, (available_mb + 511) // 1024))
    # Working-set scales for caches whose useful size grows with how much the
    # host can keep resident. They are not execution limits: every consumer
    # of a value derived from them evicts; none waits or refuses.
    retention_scale = memory_units
    io_retention_scale = max(2, min(12, memory_units * 2))
    # storage.v1 permits one 64 MiB frame. Serialized RPC buffers stay in RAM
    # up to this threshold (room for at least one maximum request and one
    # maximum response, plus 32 MiB per memory unit) and spill to an anonymous
    # file beyond it; a frame never waits for or is refused a byte slot.
    rpc_inflight_max_mib = (
        _RESOURCE_FALLBACKS['personal'][
            'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB']
        if not capacity_known else
        max(128, min(512, memory_units * 32))
    )
    # Reconstructible work-tab/route affinity caches. Live devices, poll
    # waiters and tab leases have no admission quota.
    browser_client_registry_capacity = max(64, min(128, memory_units * 32))
    # Local MCP stdio servers are optional helper processes, but each npm/uv
    # launcher and its child can retain tens to hundreds of MiB while idle.
    # Keep a longer warm window when the personal computer has comfortable
    # capacity; reclaim aggressively on the 8 GiB reference machine.  The
    # bridge retains the discovered catalog and reconnects transparently, so
    # this changes transport residency rather than tool availability.
    if not capacity_known:
        mcp_stdio_idle_seconds = _RESOURCE_FALLBACKS['personal'][
            'TOFU_MCP_STDIO_IDLE_SECONDS']
    elif capacity_mb <= 8 * 1024 or (
            available_mb is not None and available_mb <= 4 * 1024):
        mcp_stdio_idle_seconds = 180
    elif capacity_mb <= 16 * 1024:
        mcp_stdio_idle_seconds = 300
    else:
        mcp_stdio_idle_seconds = 600
    # Serving-loop executors remain at full historical high-water thread count
    # forever unless their owner rotates them. Preserve a modest warm window;
    # smaller personal machines benefit sooner from releasing thread stacks
    # and per-thread allocator caches after a burst.
    if not capacity_known:
        executor_idle_seconds = _RESOURCE_FALLBACKS['personal'][
            'TOFU_EXECUTOR_IDLE_SECONDS']
    elif capacity_mb <= 8 * 1024 or (
            available_mb is not None and available_mb <= 4 * 1024):
        executor_idle_seconds = 300
    elif capacity_mb <= 16 * 1024:
        executor_idle_seconds = 600
    else:
        executor_idle_seconds = 1800
    # Project status/watch/summary refreshes are reconstructible and arrive in
    # short event bursts. Thread creation is negligible next to their storage
    # and optional LLM work, so keep only a short warm window on personal
    # machines while distributed replicas can favor steadier throughput.
    if not capacity_known:
        project_refresh_idle_seconds = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PROJECT_REFRESH_IDLE_SECONDS']
    elif capacity_mb <= 8 * 1024 or (
            available_mb is not None and available_mb <= 4 * 1024):
        project_refresh_idle_seconds = 30
    elif capacity_mb <= 16 * 1024:
        project_refresh_idle_seconds = 60
    else:
        project_refresh_idle_seconds = 300
    # Terminal Swarm session state is an evict-only cache; spawned agents
    # start immediately and never wait for this budget.
    swarm_session_capacity = max(2, min(128, retention_scale * 2))
    # SQLite's default cache is only 2 MiB per connection.  That is adequate
    # for bounded readers, but the sole writer repeatedly touches hot indexes
    # from every domain; evicting those pages turns a small UPSERT into random
    # filesystem reads.  Give only the writer an adaptive, hard-capped cache.
    # On the 8 GiB reference computer this is 64 MiB; a 4 GiB/2 GiB-free host
    # resolves to 32 MiB, and even a failed availability probe stays bounded.
    sqlite_writer_cache_mib = max(8, min(64, capacity_mb // 128))
    if available_mb is not None:
        sqlite_writer_cache_mib = min(
            sqlite_writer_cache_mib,
            max(8, available_mb // 64),
        )
    # A revision-keyed public Turn baseline avoids repeatedly transferring and
    # decoding the same multi-MiB writer row. Charge hydrated JSON bytes under
    # an independent Sidecar-process budget: the 8 GiB reference gets 32 MiB,
    # a 4/2 GiB host gets 16 MiB, and probe failure stays at the lean fallback.
    # The consumer also enforces entry count, idle lifetime, and a 1 GiB hard
    # ceiling for explicit distributed overrides.
    if not capacity_known:
        turn_projection_cache_mib = _RESOURCE_FALLBACKS['personal'][
            'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB']
    else:
        turn_projection_cache_mib = max(
            8, min(128, capacity_mb // 256))
        if available_mb is not None:
            turn_projection_cache_mib = min(
                turn_projection_cache_mib,
                max(8, available_mb // 128),
            )

    disk_free_mb = snapshot.disk_free_mb
    if disk_free_mb is not None and disk_free_mb < 4096:
        log_budget_mb = 64
    elif disk_free_mb is not None and disk_free_mb < 16 * 1024:
        log_budget_mb = 128
    else:
        log_budget_mb = (
            256 if disk_free_mb is not None else
            _RESOURCE_FALLBACKS['personal']['TOFU_LOG_TOTAL_BUDGET_MB'])
    storage_reserve_mb = (
        max(256, min(2048, int(snapshot.disk_total_mb * 0.01)))
        if snapshot.disk_total_mb is not None else 256)
    # Recovery copies preserve durable state but still multiply its physical
    # footprint. On the 500 GiB reference computer, reserve at least half the
    # disk for the authority, OS, browser, and ordinary user files; a shared
    # multi-petabyte mount must not turn the personal default into unbounded
    # copy retention. Probe failure falls back to one explicit, lean ceiling.
    recovery_copy_budget_mib = (
        max(4096, min(512 * 1024, int(snapshot.disk_total_mb * 0.50)))
        if snapshot.disk_total_mb is not None else
        _RESOURCE_FALLBACKS['personal'][
            'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB']
    )
    # A verified backup performs a sequential image write plus full integrity
    # and checksum reads. The old fixed 1,800-second deadline repeatedly lost
    # all progress on large network-backed authorities. Derive one finite
    # overnight window from the same recovery-copy budget at a conservative
    # effective 16 MiB/s, retaining a 30-minute floor and six-hour default cap.
    storage_backup_timeout_seconds = max(
        1800,
        min(21600, 1800 + recovery_copy_budget_mib // 16),
    )
    # A WAL rebase writes one full database image to durable storage. Bound
    # the WAL ceiling from the same launch-time disk observation so a large
    # authority does not turn a tiny fixed threshold into continuous full-copy
    # churn. Both the local WAL and its durable mirror fit inside four percent
    # of observed free space; the shipper additionally scales the effective
    # trigger to the authority size. Probe failure stays lean and explicit.
    fastpath_wal_rebase_max_mib = (
        max(64, min(16_384, int(disk_free_mb * 0.02)))
        if disk_free_mb is not None else
        _RESOURCE_FALLBACKS['personal'][
            'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB']
    )
    # Browser-authenticated files are reconstructible staging, not durable
    # user state.  Give their whole directory one percent of observed free
    # space, with a small-machine floor and a hard personal-computer ceiling.
    browser_staging_max_mib = (
        max(64, min(2048, int(disk_free_mb * 0.01)))
        if disk_free_mb is not None else
        _RESOURCE_FALLBACKS['personal']['TOFU_BROWSER_STAGING_MAX_MIB']
    )
    # Durable Request Inspector raw evidence has no TTL or silent eviction.
    # Bound the whole archive authority to one percent of the launch-time data
    # volume's available space, capped at 4 GiB; an unknown probe stays at the
    # explicit 256 MiB fallback. The writer independently preserves the
    # storage minimum-free floor on every archive commit.
    raw_archive_budget_mib = (
        max(1, min(4096, int(disk_free_mb * 0.01)))
        if disk_free_mb is not None else
        _RESOURCE_FALLBACKS['personal']['TOFU_RAW_ARCHIVE_BUDGET_MIB']
    )
    # Translation results are reconstructible API-cost caches, not durable
    # user state.  Bound the whole sharded directory from the same data-volume
    # observation: 0.25% of currently free space, with a small-machine floor
    # and a personal-computer ceiling.  The cache owner divides this exact
    # budget across all 256 hash shards, so skew cannot create unbounded disk
    # growth. Probe failure remains lean and explicit.
    translate_cache_max_mib = (
        max(32, min(512, int(disk_free_mb) // 400))
        if disk_free_mb is not None else
        _RESOURCE_FALLBACKS['personal']['TOFU_TRANSLATE_CACHE_MAX_MIB']
    )
    # Memory-list metadata is reconstructible from user-owned Markdown files.
    # Cache only parsed frontmatter (never bodies or eligibility decisions),
    # and scale both identity cardinality and estimated Python residency from
    # the one launch-time memory probe. The consumer repeats hard ceilings.
    if not capacity_known:
        memory_metadata_cache_capacity = _RESOURCE_FALLBACKS['personal'][
            'TOFU_MEMORY_METADATA_CACHE_CAPACITY']
        memory_metadata_cache_max_mib = _RESOURCE_FALLBACKS['personal'][
            'TOFU_MEMORY_METADATA_CACHE_MAX_MIB']
    else:
        memory_metadata_cache_capacity = max(
            512, min(4096, capacity_mb // 4))
        memory_metadata_cache_max_mib = max(
            4, min(32, capacity_mb // 512))
        if available_mb is not None:
            memory_metadata_cache_capacity = min(
                memory_metadata_cache_capacity,
                max(512, available_mb // 2),
            )
            memory_metadata_cache_max_mib = min(
                memory_metadata_cache_max_mib,
                max(4, available_mb // 256),
            )
    # Tree-index build rows are a material peak before they are converted to
    # compact columns. Bound the retained entries and in-memory roots from the
    # launch memory probe; a larger tree is still walked, never refused.
    if not capacity_known:
        tree_index_max_entries = _RESOURCE_FALLBACKS['personal'][
            'TOFU_TREE_INDEX_MAX_ENTRIES']
        tree_index_mem_roots = _RESOURCE_FALLBACKS['personal'][
            'TOFU_TREE_INDEX_MEM_ROOTS']
    else:
        tree_index_max_entries = max(
            50_000, min(600_000, capacity_mb * 50))
        if available_mb is not None:
            tree_index_max_entries = min(
                tree_index_max_entries,
                max(50_000, available_mb * 100),
            )
        if capacity_mb <= 8 * 1024 or (
                available_mb is not None and available_mb <= 4 * 1024):
            tree_index_mem_roots = 2
        elif capacity_mb <= 16 * 1024:
            tree_index_mem_roots = 3
        else:
            tree_index_mem_roots = 4
    return {
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': rpc_inflight_max_mib,
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': sqlite_writer_cache_mib,
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': turn_projection_cache_mib,
        'TOFU_MODEL_REPLAY_CACHE_MAX_MIB': min(64, turn_projection_cache_mib * 2),
        'TOFU_BROWSER_STAGING_MAX_MIB': browser_staging_max_mib,
        'TOFU_RAW_ARCHIVE_BUDGET_MIB': raw_archive_budget_mib,
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': (
            browser_client_registry_capacity),
        # Only digests and integer counts are retained, never prompt text.
        # Scale with launch memory so concurrent tasks can keep a small stable
        # schema/prompt working set without an unbounded cache.
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY': max(
            64, min(1024, retention_scale * 64)),
        # Provider-reported prompt totals make the next round cheaper and more
        # accurate, but a one-shot conversation may never be looked up again.
        # Retain only the recent useful working set; eviction safely falls back
        # to the next local counter tier.
        'TOFU_USAGE_CACHE_CAPACITY': max(
            128, min(2048, retention_scale * 64)),
        # The single-process throttle retains exact sliding-window timestamps.
        # Bound identity cardinality from launch memory; its owner derives one
        # finite aggregate event envelope from this knob.
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY': max(
            512, min(4096, retention_scale * 256)),
        # Tool Search repeatedly tokenizes a task-stable catalog plus short
        # model queries. Cache only that economic working set; the gateway
        # separately refuses to retain oversized strings as LRU keys.
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY': max(
            512, min(4096, retention_scale * 256)),
        # A root task keeps reusable and streaming-prefetched tool receipts in
        # process memory.  Bound that optimization independently of task TTL:
        # the 8 GiB reference gets 128 entries, probe failure stays at 64, and
        # larger personal hosts stop at 256.  The consumer retains a separate
        # 1,024-entry hard ceiling for explicit operator overrides.
        'TOFU_TOOL_RESULT_CACHE_CAPACITY': max(
            64, min(256, retention_scale * 32)),
        # Durable task-result + event replay makes terminal chat dictionaries
        # reconstructible. Keep a short late-poller window on personal hosts
        # instead of pinning up to several MiB of event objects for one hour;
        # larger workstations retain modestly more warmth, never over 30 min.
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': max(
            600, min(1800, retention_scale * 150)),
        'TOFU_TASK_INSPECTOR_CACHE_MAX_MIB': (
            max(4, min(65536, capacity_mb // 256, available_mb // 64))
            if capacity_known and available_mb is not None else 16),
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY': (
            memory_metadata_cache_capacity),
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB': memory_metadata_cache_max_mib,
        # Full paper source is useful only while a user asks successive
        # questions about a small active set. Each entry is independently
        # capped at one million characters by the Paper contract, so this
        # probe-derived cardinality also gives the cache a deterministic
        # resident envelope. The consumer repeats a lower hard ceiling.
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': max(
            1, min(8, (retention_scale + 1) // 2)),
        # Reconstructible build artifacts. Logical retained payload; leave
        # room for encoding, WAL and backups.
        'TOFU_PAPER_BUILD_CACHE_MAX_MIB': (
            max(64, min(512, int(disk_free_mb * 0.005)))
            if disk_free_mb is not None else 256),
        'TOFU_PAPER_BUILD_CACHE_RECORDS': max(16, min(32, retention_scale * 4)),
        'TOFU_TRANSLATE_CACHE_MAX_MIB': translate_cache_max_mib,
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS': 60,
        'TOFU_MCP_DISPLAY_CACHE_ENTRIES': max(
            256, min(2000, io_retention_scale * 128)),
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS': 60,
        'TOFU_SWARM_SESSION_CAPACITY': swarm_session_capacity,
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': project_refresh_idle_seconds,
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY': max(
            32, min(256, io_retention_scale * 16)),
        'TOFU_COMMAND_SNAPSHOT_MAX_ENTRIES': max(
            5000, min(20000, io_retention_scale * 2500)),
        'TOFU_COMMAND_SNAPSHOT_MAX_SECONDS': max(
            1, min(5, io_retention_scale)),
        'TOFU_TREE_INDEX_MAX_ENTRIES': tree_index_max_entries,
        'TOFU_TREE_INDEX_MEM_ROOTS': tree_index_mem_roots,
        # Retention ceiling only; live free bytes/inodes are checked on the
        # chosen cache volume, which may differ from this launch data volume.
        'TOFU_TREE_INDEX_DISK_MAX_MIB': _RESOURCE_FALLBACKS['personal'][
            'TOFU_TREE_INDEX_DISK_MAX_MIB'],
        # Content retention is bounded on the actual selected cache volume.
        # Never size or refuse file/search work from the launch data volume.
        **{name: _RESOURCE_FALLBACKS['personal'][name] for name in (
            'TOFU_CONTENT_SHADOW_MAX_FILE_BYTES',
            'TOFU_CONTENT_SHADOW_ROOT_MAX_FILES',
            'TOFU_CONTENT_SHADOW_ROOT_MAX_BYTES',
            'TOFU_CONTENT_SHADOW_GLOBAL_MAX_BYTES',
        )},
        'TOFU_MCP_STDIO_IDLE_SECONDS': mcp_stdio_idle_seconds,
        'TOFU_EXECUTOR_IDLE_SECONDS': executor_idle_seconds,
        'TOFU_LOG_TOTAL_BUDGET_MB': log_budget_mb,
        'TOFU_STORAGE_MIN_FREE_BYTES': storage_reserve_mb * _MIB,
        'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB': recovery_copy_budget_mib,
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': (
            storage_backup_timeout_seconds),
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': (
            fastpath_wal_rebase_max_mib),
        'TOFU_ATTEMPT_EVENT_TTL_DAYS': 1,
    }


def deployment_resource_default(
    name: str,
    environment: Mapping[str, str] | None = None,
    *,
    snapshot: SystemResourceSnapshot | None = None,
) -> int:
    """Return one launch default according to its contract class.

    ``unlimited`` is always ``UNLIMITED`` and ``fixed`` is the contract
    constant for the deployment mode; neither consults the hardware probe.
    Only personal-mode ``retention`` follows the cached launch probe.
    """
    resource_class = _RESOURCE_CLASSES.get(name)
    if resource_class is None:
        raise KeyError(f'unknown deployment resource: {name}')
    if resource_class == 'unlimited':
        return UNLIMITED
    env = os.environ if environment is None else environment
    mode = (env.get('TOFU_DEPLOYMENT_MODE') or 'personal').strip().lower()
    fallback_mode = 'distributed' if mode == 'distributed' else 'personal'
    if fallback_mode == 'distributed' or resource_class == 'fixed':
        return int(_RESOURCE_FALLBACKS[fallback_mode][name])
    observed = snapshot or probe_system_resources(env)
    return int(_personal_resource_defaults(observed)[name])


def task_concurrency_hard_ceiling(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return a legacy schema/catalog value, never an execution admission limit.

    Existing callers may decode historical Coordinator receipts with this
    constant. New grants omit maxConcurrent; active task counts, CPU, memory
    and process RSS never gate work (docs/RESOURCE_CONTROL.md).
    """
    return MAX_TASK_CONCURRENCY


def resolve_resource_budget(
    name: str,
    environment: Mapping[str, str] | None = None,
    *,
    minimum: int = 1,
    maximum: int,
    snapshot: SystemResourceSnapshot | None = None,
) -> int:
    """Resolve an operator override against its class default and hard cap.

    Resident queues and worker registries must never interpret malformed,
    zero, or huge environment values as "unbounded".  Keeping this resolution
    beside the launch-time resource manifest gives every consumer the same
    fallback and makes the hard ceiling explicit at its call site.
    """
    if maximum < minimum:
        raise ValueError('maximum resource budget must be >= minimum')
    env = os.environ if environment is None else environment
    if name not in RESOURCE_BUDGET_ENV_KEYS:
        raise KeyError(f'unknown deployment resource: {name}')
    # A valid operator override — including the adaptive value materialized at
    # process launch — needs only hard-ceiling enforcement. Do not perform the
    # filesystem/cgroup default probe merely to evaluate the unused fallback.
    try:
        raw_value = env.get(name, '')
        value = int(raw_value) if str(raw_value).strip() else 0
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value > 0:
        return max(int(minimum), min(int(maximum), value))

    default = deployment_resource_default(
        name, env, snapshot=snapshot)
    return max(int(minimum), min(int(maximum), default))


def storage_backup_timeout_seconds(
    environment: Mapping[str, str] | None = None,
    *,
    snapshot: SystemResourceSnapshot | None = None,
) -> int:
    """Return the one bounded full-backup deadline for every entry point."""
    return resolve_resource_budget(
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS',
        environment,
        minimum=1800,
        maximum=86400,
        snapshot=snapshot,
    )


def resource_budget_manifest(
    environment: Mapping[str, str] | None = None,
    *,
    snapshot: SystemResourceSnapshot | None = None,
) -> ResourceBudgetManifest:
    """Return the probe evidence and all resulting zero-config budgets."""
    env = os.environ if environment is None else environment
    observed = snapshot or probe_system_resources(env)
    mode = (env.get('TOFU_DEPLOYMENT_MODE') or 'personal').strip().lower()
    automatic = (
        _automatic_resource_names(env)
        if env.get(RESOURCE_BUDGET_POLICY_ENV) == RESOURCE_BUDGET_POLICY_VERSION
        else set()
    )
    probe: dict[str, object] = {
        **observed.as_dict(),
        'data_path': str(_persistent_data_path(env)),
    }
    return {
        'policy_version': RESOURCE_BUDGET_POLICY_VERSION,
        'deployment_mode': mode,
        'adaptive': mode != 'distributed',
        'probe': probe,
        'defaults': {
            name: deployment_resource_default(
                name, env, snapshot=observed)
            for name in _RESOURCE_NAMES
        },
        'overrides': {
            name: str(env.get(name)).strip()
            for name in _RESOURCE_NAMES
            if env.get(name) not in (None, '') and name not in automatic
        },
        'automatic': sorted(automatic),
    }


def _recorded_automatic_names(environment: Mapping[str, str]) -> set[str]:
    """Return every Tofu setting name the provenance marker attributes to Tofu."""
    raw = str(environment.get(RESOURCE_BUDGET_AUTOMATIC_ENV) or '')
    return {
        name for name in raw.split(',')
        if name.startswith(('TOFU_', 'TOOL_'))
    }


def _automatic_resource_names(environment: Mapping[str, str]) -> set[str]:
    """Parse the internal provenance marker without widening public knobs."""
    return _recorded_automatic_names(environment) & RESOURCE_BUDGET_ENV_KEYS


def _prepare_resource_budget_environment(
    environment: MutableMapping[str, str],
) -> set[str]:
    """Discard only defaults attributed to an older policy generation.

    Explicit settings are never inferred from their numeric value.  A value is
    replaceable only when a prior Tofu generation wrote its name into the
    internal provenance marker.  This lets an in-place exec adopt a new policy
    without laundering generated values into permanent operator overrides.
    The marker proves authorship even for names the current policy no longer
    catalogs, so retired automatic values are discarded too.
    """
    previous_policy = str(
        environment.get(RESOURCE_BUDGET_POLICY_ENV) or '').strip()
    if not previous_policy:
        return set()
    if previous_policy == RESOURCE_BUDGET_POLICY_VERSION:
        return _automatic_resource_names(environment)
    recorded = _recorded_automatic_names(environment)
    previous_allocator = environment.get('TOFU_MALLOC_ARENA_MAX')
    for name in recorded:
        environment.pop(name, None)
    if 'TOFU_MALLOC_ARENA_MAX' in recorded \
            and environment.get('MALLOC_ARENA_MAX') == previous_allocator:
        environment.pop('MALLOC_ARENA_MAX', None)
    environment.pop(RESOURCE_BUDGET_AUTOMATIC_ENV, None)
    return set()


def _write_resource_budget_provenance(
    environment: MutableMapping[str, str],
    automatic: set[str],
) -> None:
    environment[RESOURCE_BUDGET_POLICY_ENV] = RESOURCE_BUDGET_POLICY_VERSION
    environment[RESOURCE_BUDGET_AUTOMATIC_ENV] = ','.join(sorted(automatic))


def install_runtime_resource_defaults(
    environment: MutableMapping[str, str] | None = None,
    *,
    snapshot: SystemResourceSnapshot | None = None,
) -> ResourceBudgetManifest:
    """Materialize one boot-time snapshot of fixed and retention defaults."""
    env = os.environ if environment is None else environment
    automatic = _prepare_resource_budget_environment(env)
    observed = snapshot or probe_system_resources(env, refresh=True)
    manifest = resource_budget_manifest(env, snapshot=observed)
    for name, default_value in manifest['defaults'].items():
        if name in UNLIMITED_SETTINGS:
            # Absence already means UNLIMITED; a materialized sys.maxsize
            # would only leak into every child environment.
            continue
        if name == 'TOFU_MALLOC_ARENA_MAX':
            # Native allocator policy is meaningful only in the parent before
            # exec; install_process_resource_defaults owns that translation.
            continue
        if not str(env.get(name) or '').strip():
            env[name] = str(default_value)
            automatic.add(name)
    _write_resource_budget_provenance(env, automatic)
    manifest['automatic'] = sorted(automatic)
    return manifest


def install_process_resource_defaults(
    environment: MutableMapping[str, str] | None = None,
    *,
    snapshot: SystemResourceSnapshot | None = None,
) -> dict[str, int]:
    """Install one launch resource snapshot before starting a new process.

    glibc otherwise creates up to several allocator arenas per CPU. Tofu runs
    many threads, so a high-core host can retain dozens of mostly-empty
    64 MiB arenas after large RPC/tool payloads have been freed.
    The setting must exist *before* the Python child starts; setting it inside
    that child is too late. All source/desktop launchers and the storage
    supervisor therefore call this one function while constructing child env.
    It also materializes every otherwise-empty fixed and retention default
    into that child environment, so the server and its later Sidecar agree on
    one boot-time observation instead of independently reacting to a
    transient fluctuation.

    The arena count is the fixed contract value in both deployment modes and
    remains explicitly overridable through one bounded Tofu setting.
    """
    env = os.environ if environment is None else environment
    manifest = install_runtime_resource_defaults(env, snapshot=snapshot)
    automatic = _automatic_resource_names(env)
    configured_allocator = (
        env.get('TOFU_MALLOC_ARENA_MAX', '').strip()
        or env.get('MALLOC_ARENA_MAX', '').strip()
    )
    defaults = manifest['defaults']
    default_arenas = int(defaults['TOFU_MALLOC_ARENA_MAX'])
    try:
        arenas = (
            int(configured_allocator)
            if configured_allocator else default_arenas)
    except (TypeError, ValueError, OverflowError):
        arenas = default_arenas
    arenas = max(1, min(64, arenas))
    env['TOFU_MALLOC_ARENA_MAX'] = str(arenas)
    env['MALLOC_ARENA_MAX'] = str(arenas)
    if not configured_allocator:
        automatic.add('TOFU_MALLOC_ARENA_MAX')
    _write_resource_budget_provenance(env, automatic)
    return {'malloc_arena_max': arenas}


@dataclass(frozen=True, slots=True)
class DeploymentConfiguration:
    """Validated process topology; secret values are excluded from repr."""

    mode: str
    process_role: str
    replica_id: str | None
    postgres_dsn_file: Path | None
    redis_url_file: Path | None
    distributed_preview_read_only: bool
    personal_storage_backend: str = 'sqlite'
    # Transport between the application and the selected native authority:
    # 'daemon' spawns `tofu-db serve`; 'embedded' runs the same engine
    # in-process (docs/STORAGE_TOFUDB.md); 'auto' (default) resolves at
    # launch from the authority volume class — embedded everywhere except
    # observed network mounts, where the engine's Normal durability tier
    # requires the daemon (lib/storage/volume_class.py). Meaningful only
    # with the tofudb backend; never a second authority.
    tofudb_transport: str = 'auto'
    postgres_dsn: str = field(default='', repr=False)
    redis_url: str = field(default='', repr=False)

    @property
    def storage_backend(self) -> str:
        # Distributed mode is authoritative on PostgreSQL; a personal
        # deployment stays on SQLite unless it explicitly opts into the native
        # Tofu-DB backend (storage.v2 direct, no Python sidecar).
        if self.mode == 'distributed':
            return 'postgres'
        return self.personal_storage_backend


def _read_secret_file(environment: dict[str, str], name: str) -> tuple[Path, str]:
    raw_path = environment.get(name, '').strip()
    if not raw_path:
        raise RuntimeError(f'{name} is required in distributed mode')
    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError(f'{name} must be an absolute secret-file path')
    try:
        stat = path.stat()
    except OSError as exc:
        raise RuntimeError(f'{name} is not readable') from exc
    if not path.is_file() or not 0 < stat.st_size <= 16 * 1024:
        raise RuntimeError(f'{name} must contain a 1..16384 byte secret')
    try:
        value = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f'{name} is not readable UTF-8') from exc
    if not value or '\x00' in value:
        raise RuntimeError(f'{name} contains an invalid secret')
    return path.resolve(), value


def _postgres_tls_is_verified(dsn: str) -> bool:
    if dsn.startswith(('postgres://', 'postgresql://')):
        values = parse_qs(urlsplit(dsn).query).get('sslmode', ())
        return bool(values and values[-1].lower() == 'verify-full')
    match = re.search(
        r'(?:^|\s)sslmode\s*=\s*["\']?([^\s"\']+)', dsn,
        flags=re.IGNORECASE,
    )
    return bool(match and match.group(1).lower() == 'verify-full')


def resolve_deployment_mode(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve only the public topology mode for dependency-boundary gates."""
    env = os.environ if environment is None else environment
    mode = (env.get('TOFU_DEPLOYMENT_MODE') or 'personal').strip().lower()
    if mode not in _DEPLOYMENT_MODES:
        raise RuntimeError(
            'TOFU_DEPLOYMENT_MODE must be exactly personal or distributed')
    return mode


def distributed_preview_is_read_only(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether this process is inside the temporary read-only preview."""
    env = os.environ if environment is None else environment
    return (
        resolve_deployment_mode(env) == 'distributed'
        and env.get('TOFU_DISTRIBUTED_PREVIEW_MODE', '').strip().lower()
        == 'read-only'
    )


def load_deployment_configuration(
    environment: dict[str, str] | None = None,
    *,
    allow_test_backend_override: bool = False,
) -> DeploymentConfiguration:
    """Parse and validate the public deployment contract without side effects."""
    env = dict(os.environ if environment is None else environment)
    mode = resolve_deployment_mode(env)
    role = (env.get('TOFU_PROCESS_ROLE') or 'all').strip().lower()
    if role not in _PROCESS_ROLES:
        raise RuntimeError(
            'TOFU_PROCESS_ROLE must be all, api, worker, or scheduler')

    removed = sorted(
        name for name, value in env.items()
        if str(value or '').strip()
        and name not in _DAEMON_ENV_PASSTHROUGH
        and (
            name in _REMOVED_DEPLOYMENT_ENV
            or name.startswith('TOFU_DB_')
            or name.startswith('CHATUI_')
        )
    )
    if removed:
        raise RuntimeError(
            'removed deployment configuration is set: ' + ', '.join(removed))

    if mode == 'personal':
        if role != 'all':
            raise RuntimeError('personal mode requires TOFU_PROCESS_ROLE=all')
        unexpected = [
            name for name in (
                'TOFU_DISTRIBUTED_PREVIEW_MODE',
                'TOFU_POSTGRES_DSN_FILE', 'TOFU_REDIS_URL_FILE',
                'TOFU_REPLICA_ID',
            ) if env.get(name, '').strip()
        ]
        if unexpected:
            raise RuntimeError(
                'distributed-only configuration is set in personal mode: '
                + ', '.join(unexpected))
        personal_storage_backend = (
            env.get('TOFU_PERSONAL_STORAGE_BACKEND', '').strip().lower()
        ) or 'sqlite'
        if personal_storage_backend not in {'sqlite', 'tofudb'}:
            raise RuntimeError(
                'TOFU_PERSONAL_STORAGE_BACKEND must be sqlite or tofudb')
        tofudb_transport = (
            env.get('TOFUDB_TRANSPORT', '').strip().lower()
        ) or 'auto'
        if tofudb_transport not in {'auto', 'daemon', 'embedded'}:
            raise RuntimeError(
                'TOFUDB_TRANSPORT must be auto, daemon or embedded')
        if tofudb_transport == 'embedded' and personal_storage_backend != 'tofudb':
            raise RuntimeError(
                'TOFUDB_TRANSPORT=embedded requires '
                'TOFU_PERSONAL_STORAGE_BACKEND=tofudb')
        return DeploymentConfiguration(
            mode=mode,
            process_role=role,
            replica_id=None,
            postgres_dsn_file=None,
            redis_url_file=None,
            distributed_preview_read_only=False,
            personal_storage_backend=personal_storage_backend,
            tofudb_transport=tofudb_transport,
        )

    preview_mode = env.get('TOFU_DISTRIBUTED_PREVIEW_MODE', '').strip().lower()
    if preview_mode != 'read-only':
        raise RuntimeError(
            'distributed mode is an operator-gated preview and requires '
            'TOFU_DISTRIBUTED_PREVIEW_MODE=read-only')
    replica_id = env.get('TOFU_REPLICA_ID', '').strip()
    if not _REPLICA_ID.fullmatch(replica_id):
        raise RuntimeError(
            'distributed mode requires a valid unique TOFU_REPLICA_ID')
    dsn_path, postgres_dsn = _read_secret_file(env, 'TOFU_POSTGRES_DSN_FILE')
    redis_path, redis_url = _read_secret_file(env, 'TOFU_REDIS_URL_FILE')
    if not _postgres_tls_is_verified(postgres_dsn):
        raise RuntimeError(
            'distributed PostgreSQL requires sslmode=verify-full')
    if not redis_url.lower().startswith('rediss://'):
        raise RuntimeError('distributed Redis requires a rediss:// URL')
    return DeploymentConfiguration(
        mode=mode,
        process_role=role,
        replica_id=replica_id,
        postgres_dsn_file=dsn_path,
        redis_url_file=redis_path,
        distributed_preview_read_only=True,
        postgres_dsn=postgres_dsn,
        redis_url=redis_url,
    )


def enforce_deployment_configuration() -> DeploymentConfiguration:
    """Fatal production boot gate for topology, secrets, and removed flags."""
    # Storage project-root override is test-only authority used by this
    # repository's isolated sidecar fixtures. It may select a private backend
    # without reopening the public legacy configuration contract.
    test_override = os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1'
    return load_deployment_configuration(
        allow_test_backend_override=test_override)


def _env_true(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }


def install_pymupdf_classic_policy() -> bool:
    """Keep pymupdf4llm on its supported classic Markdown implementation.

    pymupdf4llm automatically activates the optional ``pymupdf.layout``
    backend merely because that module is installed.  In Tofu's supported
    dependency set the backend is not usable: its OCR adapter expects the old
    ``RapidOCR.text_detector`` API, while current RapidOCR exposes
    ``text_det``.  Tofu and tofu-search consequently call the classic
    ``helpers.pymupdf_rag`` implementation explicitly.

    Letting the unused layout backend activate is still expensive: it creates
    ONNX sessions at import time, retaining a host-sized native thread pool and
    tens of MiB before a PDF is opened.  Marking the optional submodule as
    unavailable makes pymupdf4llm select its own documented classic fallback;
    PyMuPDF and pymupdf4llm remain available.

    Set ``TOFU_ENABLE_PYMUPDF_LAYOUT=1`` to opt back into the upstream layout
    backend for controlled compatibility experiments.  The policy is
    idempotent.  It deliberately refuses to replace a backend that was already
    imported, because mutating a live module would be unsafe (and its ONNX
    sessions would already exist).

    Returns True when the classic policy is active, False when explicitly
    opted out or installed too late.
    """
    if _env_true('TOFU_ENABLE_PYMUPDF_LAYOUT'):
        return False

    module_name = 'pymupdf.layout'
    if module_name in sys.modules:
        return sys.modules[module_name] is None

    # A None entry is Python's standard import blocker: ``import
    # pymupdf.layout`` raises ModuleNotFoundError, which pymupdf4llm already
    # catches to select ``use_layout(False)``.
    sys.modules[module_name] = None
    return True


# ── Process liveness: the one rule shared by the lifecycle manager and app ──
# The Supervisor (which must not import ``lib``), the storage sidecar,
# packaged installs and standalone scripts all apply this verdict; callers
# never probe ``os.kill(pid, 0)`` themselves.

def proc_stat_fields(pid: int) -> list[str] | None:
    """Linux /proc/<pid>/stat fields after comm; index 0 is field 3 (state)."""
    try:
        raw = Path(f'/proc/{int(pid)}/stat').read_bytes()
    except (OSError, ValueError, TypeError):
        return None
    fields = raw[raw.rfind(b')') + 2:].decode('ascii', 'replace').split()
    return fields if len(fields) > 19 else None


def pid_is_alive(pid: int) -> bool:
    """Whether *pid* names a process that has not finished exiting.

    Signal 0 still reaches an exited child that its parent has not reaped, so
    a zombie whose last thread is gone is dead. A zombie leader whose other
    threads still run keeps its descriptors and locks and is alive, as is a
    process that exists but refuses our signal. POSIX only: on Windows
    ``os.kill`` with signal 0 is not a probe.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except (OSError, OverflowError):
        return False
    fields = proc_stat_fields(pid)
    if fields is None or fields[0] not in ('Z', 'X'):
        return True
    return not (fields[17].isdigit() and int(fields[17]) <= 1)
