"""Dependency-light process policies that must run before optional imports.

This module intentionally uses only the Python standard library.  Importing a
``lib.*`` helper first executes ``lib/__init__.py`` and is therefore too late
for policies that protect the very beginning of ``server.py`` / healthcheck /
pytest collection.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import parse_qs, urlsplit

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
    'probe_system_resources',
    'resolve_deployment_mode',
    'resource_budget_manifest',
    'resolve_resource_budget',
    'task_concurrency_hard_ceiling',
]


_DEPLOYMENT_MODES = frozenset({'personal', 'distributed'})
_PROCESS_ROLES = frozenset({'all', 'api', 'worker', 'scheduler'})
_REMOVED_DEPLOYMENT_ENV = (
    'TOFU_REQUIRE_PG',
    'TOFU_REPLICA_RING',
    'TOFU_STORAGE_MODE',
)
_REPLICA_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_MIB = 1024 * 1024
_RESOURCE_FALLBACKS = {
    'personal': {
        'TOFU_MALLOC_ARENA_MAX': 1,
        'TOFU_STORAGE_RPC_CAPACITY': 2,
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': 128,
        'TOFU_STORAGE_SQLITE_READ_POOL': 2,
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': 8,
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': 32,
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': 16,
        'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB': 512,
        'TOFU_BROWSER_STAGING_MAX_MIB': 256,
        'TOFU_RAW_ARCHIVE_BUDGET_MIB': 256,
        'TOFU_BROWSER_POLL_MAX_INFLIGHT': 8,
        'TOFU_BROWSER_POLL_MAX_WAITERS': 8,
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': 64,
        'TOFU_BROWSER_SESSION_LEASE_CAPACITY': 64,
        'TOFU_BROWSER_POLL_BODY_MAX_MIB': 32,
        'TOFU_MAX_SSE_PER_PRINCIPAL': 12,
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB': 64,
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB': 64,
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY': 128,
        'TOFU_USAGE_CACHE_CAPACITY': 128,
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY': 512,
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY': 512,
        'TOFU_TOOL_RESULT_CACHE_CAPACITY': 64,
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': 600,
        'TOFU_TIMER_LIVE_CAP': 8,
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY': 512,
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB': 4,
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': 1,
        'TOFU_TRANSLATE_CACHE_MAX_MIB': 128,
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS': 4,
        'TOFU_TRANSLATE_WORKERS': 1,
        'TOFU_TRANSLATE_QUEUE_CAPACITY': 4,
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS': 60,
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS': 2,
        'TOFU_PRODUCTION_LLM_FANOUT': 1,
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS': 4,
        'TOFU_PRODUCTION_IMAGE_FANOUT': 1,
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS': 4,
        'TOFU_PRODUCTION_TTS_FANOUT': 1,
        'TOFU_PDF_PROCESSES': 1,
        'TOFU_PDF_PARSE_CAPACITY': 3,
        'TOFU_PDF_MAX_PAGES': 256,
        'TOFU_PDF_MAX_TEXT_MIB': 2,
        'TOFU_PDF_PARSE_TIMEOUT': 300,
        'TOFU_PDF_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_VLM_TASK_WORKERS': 1,
        'TOFU_PDF_VLM_QUEUE_CAPACITY': 2,
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_VLM_CALL_WORKERS': 1,
        'TOFU_PDF_VLM_MAX_PAGES': 64,
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS': 1920,
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS': 4,
        'TOFU_KNOWLEDGE_ENRICH_WORKERS': 1,
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY': 4,
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS': 60,
        'TOFU_SWARM_GLOBAL_WORKERS': 1,
        'TOFU_SWARM_MAX_PARALLEL': 1,
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE': 2,
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION': 6,
        'TOFU_SWARM_MAX_RETRIES': 1,
        'TOFU_SWARM_SESSION_CAPACITY': 2,
        'TOFU_CONTROL_RPC_WORKERS': 4,
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY': 16,
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': 60,
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY': 64,
        'TOFU_TREE_INDEX_WALK_JOBS': 2,
        'TOFU_TREE_INDEX_MAX_ENTRIES': 100_000,
        'TOFU_TREE_INDEX_MEM_ROOTS': 2,
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE': 2,
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY': 8,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS': 32,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS': 30,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS': 256,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS': 1,
        'TOFU_MAX_INFLIGHT_TASKS': 1,
        'TOFU_TASK_RSS_RESERVE_MB': 1024,
        'TOFU_SYNC_WORKERS': 2,
        'TOFU_AGENT_WORKERS': 1,
        'TOOL_MAX_PARALLEL_WORKERS': 1,
        'TOFU_NUMERIC_THREADS': 1,
        'TOFU_MCP_CRED_PROBE_WORKERS': 1,
        'TOFU_MCP_STDIO_IDLE_SECONDS': 300,
        'TOFU_EXECUTOR_IDLE_SECONDS': 600,
        'TOFU_LOG_TOTAL_BUDGET_MB': 128,
        'TOFU_STORAGE_MIN_FREE_BYTES': 256 * _MIB,
        'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB': 64 * 1024,
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': 5896,
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': 512,
        'TOFU_ATTEMPT_EVENT_TTL_DAYS': 1,
        'TOFU_PROCESS_RSS_RELIEF_MB': 1024,
        'TOFU_PROCESS_RSS_RECYCLE_MB': 1536,
    },
    'distributed': {
        'TOFU_MALLOC_ARENA_MAX': 8,
        'TOFU_STORAGE_RPC_CAPACITY': 64,
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': 1024,
        'TOFU_STORAGE_SQLITE_READ_POOL': 16,
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': 128,
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': 64,
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': 256,
        'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB': 1024,
        'TOFU_BROWSER_STAGING_MAX_MIB': 4096,
        'TOFU_RAW_ARCHIVE_BUDGET_MIB': 4096,
        'TOFU_BROWSER_POLL_MAX_INFLIGHT': 128,
        'TOFU_BROWSER_POLL_MAX_WAITERS': 128,
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': 2048,
        'TOFU_BROWSER_SESSION_LEASE_CAPACITY': 2048,
        'TOFU_BROWSER_POLL_BODY_MAX_MIB': 64,
        'TOFU_MAX_SSE_PER_PRINCIPAL': 64,
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB': 128,
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB': 128,
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY': 1024,
        'TOFU_USAGE_CACHE_CAPACITY': 4096,
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY': 4096,
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY': 4096,
        'TOFU_TOOL_RESULT_CACHE_CAPACITY': 512,
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': 3600,
        'TOFU_TIMER_LIVE_CAP': 64,
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY': 8192,
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB': 64,
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': 8,
        'TOFU_TRANSLATE_CACHE_MAX_MIB': 1024,
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS': 16,
        'TOFU_TRANSLATE_WORKERS': 16,
        'TOFU_TRANSLATE_QUEUE_CAPACITY': 128,
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS': 600,
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS': 8,
        'TOFU_PRODUCTION_LLM_FANOUT': 4,
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS': 16,
        'TOFU_PRODUCTION_IMAGE_FANOUT': 4,
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS': 16,
        'TOFU_PRODUCTION_TTS_FANOUT': 4,
        'TOFU_PDF_PROCESSES': 4,
        'TOFU_PDF_PARSE_CAPACITY': 16,
        'TOFU_PDF_MAX_PAGES': 2048,
        'TOFU_PDF_MAX_TEXT_MIB': 16,
        'TOFU_PDF_PARSE_TIMEOUT': 3600,
        'TOFU_PDF_WORKER_IDLE_SECONDS': 600,
        'TOFU_PDF_VLM_TASK_WORKERS': 4,
        'TOFU_PDF_VLM_QUEUE_CAPACITY': 32,
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS': 600,
        'TOFU_PDF_VLM_CALL_WORKERS': 8,
        'TOFU_PDF_VLM_MAX_PAGES': 512,
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS': 14_400,
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS': 16,
        'TOFU_KNOWLEDGE_ENRICH_WORKERS': 8,
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY': 128,
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS': 600,
        'TOFU_SWARM_GLOBAL_WORKERS': 16,
        'TOFU_SWARM_MAX_PARALLEL': 8,
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE': 16,
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION': 64,
        'TOFU_SWARM_MAX_RETRIES': 2,
        'TOFU_SWARM_SESSION_CAPACITY': 32,
        'TOFU_CONTROL_RPC_WORKERS': 32,
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY': 512,
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': 600,
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY': 512,
        'TOFU_TREE_INDEX_WALK_JOBS': 16,
        'TOFU_TREE_INDEX_MAX_ENTRIES': 600_000,
        'TOFU_TREE_INDEX_MEM_ROOTS': 4,
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE': 32,
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY': 64,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS': 256,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS': 60,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS': 256,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS': 1,
        'TOFU_MAX_INFLIGHT_TASKS': 16,
        'TOFU_TASK_RSS_RESERVE_MB': 512,
        'TOFU_SYNC_WORKERS': 16,
        'TOFU_AGENT_WORKERS': 16,
        'TOOL_MAX_PARALLEL_WORKERS': 8,
        'TOFU_NUMERIC_THREADS': 4,
        'TOFU_MCP_CRED_PROBE_WORKERS': 8,
        'TOFU_MCP_STDIO_IDLE_SECONDS': 1800,
        'TOFU_EXECUTOR_IDLE_SECONDS': 3600,
        'TOFU_LOG_TOTAL_BUDGET_MB': 512,
        'TOFU_STORAGE_MIN_FREE_BYTES': 1024 * _MIB,
        'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB': 1024 * 1024,
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': 21600,
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': 16_384,
        'TOFU_ATTEMPT_EVENT_TTL_DAYS': 7,
        'TOFU_PROCESS_RSS_RELIEF_MB': 4096,
        'TOFU_PROCESS_RSS_RECYCLE_MB': 8192,
    },
}

_RESOURCE_NAMES = tuple(_RESOURCE_FALLBACKS['personal'])
RESOURCE_BUDGET_ENV_KEYS = frozenset(_RESOURCE_NAMES)
RESOURCE_BUDGET_POLICY_VERSION = '2026-08-31.2'
RESOURCE_BUDGET_POLICY_ENV = 'TOFU_RESOURCE_BUDGET_POLICY_VERSION'
RESOURCE_BUDGET_AUTOMATIC_ENV = 'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS'
_RESOURCE_SNAPSHOT_CACHE: dict[str, 'SystemResourceSnapshot'] = {}


class ResourceBudgetManifest(TypedDict):
    policy_version: str
    deployment_mode: str
    adaptive: bool
    probe: dict[str, object]
    defaults: dict[str, int]
    overrides: dict[str, str]
    automatic: list[str]


@dataclass(frozen=True, slots=True)
class SystemResourceSnapshot:
    """One bounded, dependency-free view of resources visible to this process.

    Values use MiB and logical CPUs. ``None`` means that the platform did not
    expose a trustworthy value; budget derivation then uses the conservative
    personal fallback instead of guessing from a server-sized host value.
    """

    host_cpu_count: int
    affinity_cpu_count: int | None
    cgroup_cpu_count: int | None
    effective_cpu_count: int
    host_memory_total_mb: int | None
    host_memory_available_mb: int | None
    cgroup_memory_limit_mb: int | None
    cgroup_memory_current_mb: int | None
    effective_memory_capacity_mb: int | None
    effective_memory_available_mb: int | None
    disk_total_mb: int | None
    disk_free_mb: int | None

    def as_dict(self) -> dict[str, int | None]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }


def _read_first_text(paths: tuple[str, ...]) -> str | None:
    for raw_path in paths:
        try:
            return Path(raw_path).read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            continue
    return None


def _finite_positive_bytes(raw: str | None) -> int | None:
    try:
        value = int(raw or '')
    except (TypeError, ValueError, OverflowError):
        return None
    # cgroup v1 represents unlimited memory with a huge page-aligned sentinel.
    return value if 0 < value < (1 << 60) else None


def _decode_mount_path(value: str) -> str:
    for encoded, decoded in (
        ('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
        value = value.replace(encoded, decoded)
    return value


def _cgroup_file_paths(
    controller: str,
    filename: str,
    fallbacks: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve this process's v1/v2 cgroup file before fixed legacy paths."""
    memberships: list[tuple[bool, str]] = []
    membership_text = _read_first_text(('/proc/self/cgroup',)) or ''
    for line in membership_text.splitlines():
        fields = line.split(':', 2)
        if len(fields) != 3:
            continue
        controllers = {item for item in fields[1].split(',') if item}
        if not controllers or controller in controllers:
            memberships.append((not controllers, fields[2] or '/'))

    resolved: list[str] = []
    mount_text = _read_first_text(('/proc/self/mountinfo',)) or ''
    for line in mount_text.splitlines():
        before, separator, after = line.partition(' - ')
        if not separator:
            continue
        mount_fields = before.split()
        fs_fields = after.split()
        if len(mount_fields) < 5 or len(fs_fields) < 3:
            continue
        filesystem = fs_fields[0]
        mount_controllers = set(fs_fields[2].split(','))
        for unified, membership in memberships:
            if unified != (filesystem == 'cgroup2'):
                continue
            if not unified and (
                    filesystem != 'cgroup'
                    or controller not in mount_controllers):
                continue
            root = PurePosixPath(_decode_mount_path(mount_fields[3]))
            member = PurePosixPath(membership)
            try:
                relative = member.relative_to(root)
            except ValueError:
                continue
            mountpoint = Path(_decode_mount_path(mount_fields[4]))
            resolved.append(str(mountpoint / Path(str(relative)) / filename))
    return tuple(dict.fromkeys([*resolved, *fallbacks]))


def _cgroup_cpu_count() -> int | None:
    v2 = _read_first_text(_cgroup_file_paths(
        'cpu', 'cpu.max', ('/sys/fs/cgroup/cpu.max',)))
    if v2:
        fields = v2.split()
        if len(fields) >= 2 and fields[0] != 'max':
            try:
                quota, period = int(fields[0]), int(fields[1])
                if quota > 0 and period > 0:
                    return max(1, quota // period)
            except (TypeError, ValueError, OverflowError):
                pass
    quota = _finite_positive_bytes(_read_first_text(_cgroup_file_paths(
        'cpu', 'cpu.cfs_quota_us', (
        '/sys/fs/cgroup/cpu/cpu.cfs_quota_us',
        '/sys/fs/cgroup/cpu.cfs_quota_us',
    ))))
    period = _finite_positive_bytes(_read_first_text(_cgroup_file_paths(
        'cpu', 'cpu.cfs_period_us', (
        '/sys/fs/cgroup/cpu/cpu.cfs_period_us',
        '/sys/fs/cgroup/cpu.cfs_period_us',
    ))))
    if quota is None or period is None:
        return None
    return max(1, quota // period)


def _host_memory_bytes() -> tuple[int | None, int | None]:
    """Return host total/available memory without importing psutil."""
    total = None
    available = None
    text = _read_first_text(('/proc/meminfo',))
    if text:
        values: dict[str, int] = {}
        for line in text.splitlines():
            name, separator, raw_value = line.partition(':')
            if not separator:
                continue
            fields = raw_value.split()
            try:
                values[name] = int(fields[0]) * 1024
            except (IndexError, TypeError, ValueError, OverflowError):
                continue
        total = values.get('MemTotal')
        available = values.get('MemAvailable')

    def _sysconf_bytes(page_name: str) -> int | None:
        try:
            pages = int(os.sysconf(page_name))
            page_size = int(os.sysconf('SC_PAGE_SIZE'))
        except (AttributeError, OSError, TypeError, ValueError, OverflowError):
            return None
        value = pages * page_size
        return value if value > 0 else None

    total = total or _sysconf_bytes('SC_PHYS_PAGES')
    available = available or _sysconf_bytes('SC_AVPHYS_PAGES')
    if os.name == 'nt' and (total is None or available is None):
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ('length', ctypes.c_ulong),
                    ('memory_load', ctypes.c_ulong),
                    ('total_physical', ctypes.c_ulonglong),
                    ('available_physical', ctypes.c_ulonglong),
                    ('total_page_file', ctypes.c_ulonglong),
                    ('available_page_file', ctypes.c_ulonglong),
                    ('total_virtual', ctypes.c_ulonglong),
                    ('available_virtual', ctypes.c_ulonglong),
                    ('available_extended_virtual', ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = total or int(status.total_physical)
                available = available or int(status.available_physical)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return total, available


def _cgroup_memory_bytes() -> tuple[int | None, int | None]:
    v2_limit = _read_first_text(_cgroup_file_paths(
        'memory', 'memory.max', ('/sys/fs/cgroup/memory.max',)))
    v1_limit = _read_first_text(_cgroup_file_paths(
        'memory', 'memory.limit_in_bytes', (
            '/sys/fs/cgroup/memory/memory.limit_in_bytes',)))
    limit = _finite_positive_bytes(v2_limit or v1_limit)
    v2_current = _read_first_text(_cgroup_file_paths(
        'memory', 'memory.current', ('/sys/fs/cgroup/memory.current',)))
    v1_current = _read_first_text(_cgroup_file_paths(
        'memory', 'memory.usage_in_bytes', (
            '/sys/fs/cgroup/memory/memory.usage_in_bytes',)))
    current = _finite_positive_bytes(v2_current or v1_current)
    return limit, current


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


def _project_disk_bytes(
    environment: Mapping[str, str],
) -> tuple[int | None, int | None]:
    data_path = _persistent_data_path(environment)

    # Persistent data is commonly a separate Docker/NAS mount; probing the
    # source/overlay filesystem would report capacity the SQLite authority
    # cannot actually use.
    path = data_path
    while True:
        try:
            usage = shutil.disk_usage(path)
            return int(usage.total), int(usage.free)
        except (FileNotFoundError, NotADirectoryError):
            parent = path.parent
            if parent == path:
                break
            path = parent
        except (OSError, TypeError, ValueError, OverflowError):
            break
    return None, None


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
    """
    env = os.environ if environment is None else environment
    cache_key = str(_persistent_data_path(env))
    if not refresh and cache_key in _RESOURCE_SNAPSHOT_CACHE:
        return _RESOURCE_SNAPSHOT_CACHE[cache_key]

    try:
        host_cpus = max(1, int(os.cpu_count() or 1))
    except (TypeError, ValueError, OverflowError):
        host_cpus = 1
    try:
        affinity_cpus = max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError, TypeError, ValueError):
        affinity_cpus = None
    cgroup_cpus = _cgroup_cpu_count()
    effective_cpus = min(
        value for value in (host_cpus, affinity_cpus, cgroup_cpus)
        if value is not None)

    host_total, host_available = _host_memory_bytes()
    cgroup_limit, cgroup_current = _cgroup_memory_bytes()
    capacity_candidates = [
        value for value in (host_total, cgroup_limit)
        if value is not None]
    effective_capacity = min(capacity_candidates) if capacity_candidates else None
    cgroup_is_explicit_budget = bool(
        cgroup_limit is not None
        and (host_total is None or cgroup_limit < host_total * 0.90))
    cgroup_available = (
        max(0, cgroup_limit - cgroup_current)
        if cgroup_is_explicit_budget and cgroup_current is not None else
        cgroup_limit if cgroup_is_explicit_budget else None)
    available_candidates = [
        value for value in (host_available, cgroup_available)
        if value is not None]
    effective_available = min(available_candidates) if available_candidates else None
    if effective_capacity is not None and effective_available is not None:
        effective_available = min(effective_available, effective_capacity)
    disk_total, disk_free = _project_disk_bytes(env)

    def _mb(value: int | None) -> int | None:
        return max(0, value // _MIB) if value is not None else None

    snapshot = SystemResourceSnapshot(
        host_cpu_count=host_cpus,
        affinity_cpu_count=affinity_cpus,
        cgroup_cpu_count=cgroup_cpus,
        effective_cpu_count=max(1, effective_cpus),
        host_memory_total_mb=_mb(host_total),
        host_memory_available_mb=_mb(host_available),
        cgroup_memory_limit_mb=_mb(cgroup_limit),
        cgroup_memory_current_mb=_mb(cgroup_current),
        effective_memory_capacity_mb=_mb(effective_capacity),
        effective_memory_available_mb=_mb(effective_available),
        disk_total_mb=_mb(disk_total),
        disk_free_mb=_mb(disk_free),
    )
    _RESOURCE_SNAPSHOT_CACHE[cache_key] = snapshot
    return snapshot


def _task_slots_from_rss_budget(
    hard_rss_mb: int,
    task_reserve_mb: int,
) -> int:
    """Return task slots that fit beside the non-task process working set."""
    if hard_rss_mb <= 0:
        return 256
    reserve = max(64, int(task_reserve_mb))
    # Imports, route state, storage clients, terminal settlement, and transient
    # response copies need a quarter of the process budget (at least 512 MiB)
    # even with zero active agents.  Tasks consume only the remainder.
    process_baseline_mb = max(512, int(hard_rss_mb) // 4)
    task_budget_mb = max(0, int(hard_rss_mb) - process_baseline_mb)
    return max(1, task_budget_mb // reserve)


def _personal_resource_defaults(
    snapshot: SystemResourceSnapshot,
) -> dict[str, int]:
    """Derive useful concurrency while preserving OS/browser headroom."""
    cpus = max(1, snapshot.effective_cpu_count)
    capacity_known = snapshot.effective_memory_capacity_mb is not None
    capacity_mb = snapshot.effective_memory_capacity_mb or 4096
    available_mb = snapshot.effective_memory_available_mb
    # One concurrency unit needs 2 GiB of installed/effective capacity and at
    # least 1 GiB that is currently available. This prevents a large but busy
    # workstation from being treated like an empty dedicated server.
    memory_units = (
        max(1, (capacity_mb + 1023) // 2048) if capacity_known else 1)
    if available_mb is not None:
        memory_units = min(
            memory_units, max(1, (available_mb + 511) // 1024))

    cgroup_is_explicit_budget = bool(
        snapshot.cgroup_memory_limit_mb
        and snapshot.host_memory_total_mb
        and snapshot.cgroup_memory_limit_mb
        < snapshot.host_memory_total_mb * 0.90)
    hard_fraction = 0.70 if cgroup_is_explicit_budget else 0.375
    hard_floor_mb = min(1536, max(768, int(capacity_mb * 0.50)))
    # Scale the worker envelope on actual servers instead of pinning every
    # personal deployment to the old 6 GiB workstation ceiling.  The fraction
    # still reserves most host memory for the OS/browser (or 30% of an explicit
    # application cgroup), while the absolute 64 GiB ceiling prevents a huge
    # host probe from creating an unbounded single-process budget.
    hard_rss_mb = max(
        hard_floor_mb, min(64 * 1024, int(capacity_mb * hard_fraction)))
    if available_mb is not None:
        hard_rss_mb = min(
            hard_rss_mb, max(hard_floor_mb, int(available_mb * 0.75)))
    soft_target_mb = (
        int(capacity_mb * 0.50)
        if cgroup_is_explicit_budget else
        int(hard_rss_mb * (2.0 / 3.0)))
    if available_mb is not None:
        soft_target_mb = min(soft_target_mb, int(available_mb * 0.50))
    soft_rss_mb = max(512, soft_target_mb)
    if hard_rss_mb - soft_rss_mb < 256:
        soft_rss_mb = max(512, hard_rss_mb - 256)

    # A 3 GiB worker on the 8 GiB reference computer historically sustains
    # four ordinary tasks with a 512 MiB live-state envelope. Larger worker
    # envelopes reserve 1 GiB per root so task concurrency scales only with the
    # memory the process is actually allowed to retain.
    task_rss_reserve_mb = (
        512 if capacity_known and hard_rss_mb <= 3072 else
        _RESOURCE_FALLBACKS['personal']['TOFU_TASK_RSS_RESERVE_MB'])
    rss_task_units = _task_slots_from_rss_budget(
        hard_rss_mb, task_rss_reserve_mb)
    # The absolute 64-GiB worker envelope minus its 25% process baseline fits
    # at most 48 default 1-GiB root tasks.  Align this CPU/capacity ceiling to
    # that measured RSS boundary so a large personal server is not stranded at
    # an unrelated 32-task cap; smaller hosts remain constrained first by CPU,
    # current memory headroom, and ``rss_task_units`` below.
    general_parallelism = max(1, min(48, cpus, memory_units))
    task_parallelism = max(
        1, min(general_parallelism, rss_task_units))
    # This is a PER-TASK fan-out pool. Letting it grow with root-task count
    # multiplies threads quadratically on a large server, so retain a separate
    # four-way ceiling while root concurrency consumes the wider host budget.
    tool_parallelism = max(1, min(4, cpus, memory_units))
    io_parallelism = max(2, min(12, cpus * 2, memory_units * 2))
    # storage.v1 permits one 64 MiB frame so historical slot-only admission
    # exposed ``rpc_capacity * 64 MiB`` of serialized buffers. Give each
    # launch-probed memory unit 32 MiB while retaining room for at least one
    # maximum request and one maximum response. The Sidecar consumes this as
    # one process-wide weighted budget, independently of handler count.
    rpc_inflight_max_mib = (
        _RESOURCE_FALLBACKS['personal'][
            'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB']
        if not capacity_known else
        max(128, min(512, memory_units * 32))
    )
    sync_workers = max(2, min(16, cpus * 2, memory_units * 2))
    # Browser polls are async and normally retain only one small coroutine per
    # installed device.  Keep enough headroom for several personal computers
    # and result-flush overlap, while bounding malicious unique-client churn.
    # The request-body cap below is an independent multiplier, so the inflight
    # ceiling deliberately grows more slowly than the general sync pool.
    browser_poll_max_inflight = max(8, min(16, sync_workers * 2))
    browser_poll_max_waiters = browser_poll_max_inflight
    browser_client_registry_capacity = max(
        64, min(256, browser_poll_max_waiters * 8))
    if not capacity_known:
        browser_poll_body_max_mib = _RESOURCE_FALLBACKS['personal'][
            'TOFU_BROWSER_POLL_BODY_MAX_MIB']
        max_sse_per_principal = _RESOURCE_FALLBACKS['personal'][
            'TOFU_MAX_SSE_PER_PRINCIPAL']
    else:
        browser_poll_body_max_mib = max(16, min(32, capacity_mb // 256))
        if available_mb is not None:
            browser_poll_body_max_mib = min(
                browser_poll_body_max_mib,
                max(16, available_mb // 128),
            )
        max_sse_per_principal = max(
            8, min(24, general_parallelism * 3))
    numeric_threads = max(
        1, min(4, cpus, max(1, (memory_units + 1) // 2)))
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
    # VLM PDF transcription retains compressed source bytes and rendered page
    # images, then fans out paid model calls. Bound pages before rendering and
    # grant a second whole-document worker only when the launch probe shows
    # substantial task headroom. Thirty seconds per admitted page gives the
    # task deadline a conservative finite envelope across all batches.
    if not capacity_known:
        vlm_max_pages = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PDF_VLM_MAX_PAGES']
    else:
        vlm_memory_mb = min(
            capacity_mb,
            available_mb if available_mb is not None else capacity_mb,
        )
        if vlm_memory_mb <= 4096:
            vlm_max_pages = 64
        elif vlm_memory_mb <= 8192:
            vlm_max_pages = 128
        elif vlm_memory_mb <= 16 * 1024:
            vlm_max_pages = 192
        else:
            vlm_max_pages = 256
    vlm_task_timeout_seconds = max(
        1800, min(7200, vlm_max_pages * 30))
    # Classic extraction retains the compressed source, parser-native state,
    # Markdown output, and an IPC copy when it runs in the process pool. Keep
    # the 8 GiB reference machine at one worker / three unfinished documents,
    # while larger personal machines grow only to two workers. Page and text
    # ceilings bound CPU and durable/context amplification independently of the
    # historical 200 MiB compressed-input gate.
    if not capacity_known:
        classic_pdf_processes = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PDF_PROCESSES']
        classic_pdf_capacity = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PDF_PARSE_CAPACITY']
        classic_pdf_max_pages = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PDF_MAX_PAGES']
        classic_pdf_max_text_mib = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PDF_MAX_TEXT_MIB']
        classic_pdf_timeout = _RESOURCE_FALLBACKS['personal'][
            'TOFU_PDF_PARSE_TIMEOUT']
    else:
        classic_pdf_processes = 2 if task_parallelism >= 16 else 1
        classic_pdf_capacity = max(3, classic_pdf_processes * 3)
        # Classic extraction does not render a page-image batch or retain paid
        # call inputs. Scale its page budget separately from VLM: installed
        # capacity grants one page per 16 MiB and current headroom grants one
        # per 8 MiB. Thus the 8/4 GiB reference machine keeps 512 pages while
        # a 4/2 GiB host falls back to 256.
        classic_pdf_max_pages = max(
            256, min(1024, capacity_mb // 16))
        if available_mb is not None:
            classic_pdf_max_pages = min(
                classic_pdf_max_pages,
                max(256, available_mb // 8),
            )
        classic_pdf_max_text_mib = max(
            2, min(8, (classic_pdf_max_pages + 127) // 128))
        classic_pdf_timeout = max(
            300, min(1800, classic_pdf_max_pages * 2))
    # Swarm agents outlive the root turn and historically created one private
    # thread/API pool per conversation. Derive both the process-wide expensive
    # execution ceiling and each session's smaller share from the root-task
    # envelope, then bound accepted waves/results separately. On the 8 GiB
    # reference host this is two executing agents, four agents per wave, and
    # twelve total agents in one live session.
    swarm_global_workers = max(
        1, min(4, max(1, (task_parallelism + 1) // 2)))
    swarm_max_agents_per_wave = max(
        2, min(8, swarm_global_workers * 2))
    swarm_max_agents_per_session = max(
        6, min(24, swarm_max_agents_per_wave * 3))
    swarm_session_capacity = max(
        2, min(8, task_parallelism))
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
    # Conversation search is a disposable local projection, not durable
    # authority. Keep it useful on the 500 GB reference machine without
    # letting a pathological transcript corpus consume the volume: at most
    # two percent of currently free space, with a lean fallback when the
    # launch probe cannot establish a trustworthy capacity.
    search_projection_max_mib = (
        max(128, min(4096, int(disk_free_mb * 0.02)))
        if disk_free_mb is not None else
        _RESOURCE_FALLBACKS['personal'][
            'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB']
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
    # Python bytecode is reconstructible and useful only as a small local
    # acceleration layer.  Scale its hard process-wide ceiling from the same
    # launch-time disk observation as every other zero-config disk budget;
    # the runtime additionally verifies capacity on the actual cache volume.
    if disk_free_mb is not None and disk_free_mb < 4096:
        python_cache_max_mib = 16
    elif disk_free_mb is not None and disk_free_mb < 16 * 1024:
        python_cache_max_mib = 32
    else:
        python_cache_max_mib = (
            64 if disk_free_mb is not None else
            _RESOURCE_FALLBACKS['personal']['TOFU_RUN_PYTHON_CACHE_MAX_MIB'])
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
    # Control RPC executes bounded, read-only filesystem requests away from
    # the event loop.  It shares the launch-time I/O parallelism observation,
    # but stays below the general storage pool so several browser tabs cannot
    # manufacture an unbounded set of blocked filesystem threads.
    control_rpc_workers = max(2, min(8, io_parallelism))
    # Tree walks are metadata-heavy on the network/FUSE filesystems this
    # index exists to protect. More than eight concurrent scandir jobs slowed
    # the retained project in measurement, while the build-row objects remain
    # a material peak before they are converted to compact columns. Give all
    # concurrent roots one shared launch-probed scan and retention budget.
    tree_index_walk_jobs = max(2, min(8, io_parallelism))
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
        'TOFU_MALLOC_ARENA_MAX': min(4, numeric_threads),
        'TOFU_STORAGE_RPC_CAPACITY': io_parallelism,
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': rpc_inflight_max_mib,
        'TOFU_STORAGE_SQLITE_READ_POOL': io_parallelism,
        # One SQLite writer serializes all domains. RPC admission already
        # bounds simultaneously live callers; keep a small second envelope
        # for internal event/maintenance producers without retaining an
        # arbitrary number of timed-out operation closures during slow I/O.
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': max(
            8, min(64, io_parallelism * 2)),
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': sqlite_writer_cache_mib,
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': turn_projection_cache_mib,
        'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB': search_projection_max_mib,
        'TOFU_BROWSER_STAGING_MAX_MIB': browser_staging_max_mib,
        'TOFU_RAW_ARCHIVE_BUDGET_MIB': raw_archive_budget_mib,
        'TOFU_BROWSER_POLL_MAX_INFLIGHT': browser_poll_max_inflight,
        'TOFU_BROWSER_POLL_MAX_WAITERS': browser_poll_max_waiters,
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': (
            browser_client_registry_capacity),
        # Browser pages and adapters retain one small owner/device lease each.
        # Use the device-cardinality budget as the upper envelope, but expire
        # all timed leases through one shared sweeper rather than multiplying
        # resident Timer threads by this value.
        'TOFU_BROWSER_SESSION_LEASE_CAPACITY': (
            browser_client_registry_capacity),
        'TOFU_BROWSER_POLL_BODY_MAX_MIB': browser_poll_body_max_mib,
        # SSE sockets and their proxy buffers are resident resources. Scale
        # modestly with useful local concurrency and preserve a finite launch
        # default even when CPU/memory probes fail.
        'TOFU_MAX_SSE_PER_PRINCIPAL': max_sse_per_principal,
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB': python_cache_max_mib,
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB': python_cache_max_mib,
        # Only digests and integer counts are retained, never prompt text.
        # Scale with useful task parallelism so each active task can keep a
        # small stable schema/prompt working set without an unbounded cache.
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY': max(
            64, min(1024, task_parallelism * 64)),
        # Provider-reported prompt totals make the next round cheaper and more
        # accurate, but a one-shot conversation may never be looked up again.
        # Retain only the recent useful working set; eviction safely falls back
        # to the next local counter tier.
        'TOFU_USAGE_CACHE_CAPACITY': max(
            128, min(2048, task_parallelism * 64)),
        # The single-process throttle retains exact sliding-window timestamps.
        # Bound identity cardinality from the same useful task concurrency;
        # its owner derives one finite aggregate event envelope from this knob.
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY': max(
            512, min(4096, task_parallelism * 256)),
        # Tool Search repeatedly tokenizes a task-stable catalog plus short
        # model queries. Cache only that economic working set; the gateway
        # separately refuses to retain oversized strings as LRU keys.
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY': max(
            512, min(4096, task_parallelism * 256)),
        # A root task keeps reusable and streaming-prefetched tool receipts in
        # process memory.  Bound that optimization independently of task TTL:
        # the 8 GiB reference gets 128 entries, probe failure stays at 64, and
        # larger personal hosts stop at 256.  The consumer retains a separate
        # 1,024-entry hard ceiling for explicit operator overrides.
        'TOFU_TOOL_RESULT_CACHE_CAPACITY': max(
            64, min(256, task_parallelism * 32)),
        # Durable task-result + event replay makes terminal chat dictionaries
        # reconstructible. Keep a short late-poller window on personal hosts
        # instead of pinning up to several MiB of event objects for one hour;
        # larger workstations retain modestly more warmth, never over 30 min.
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': max(
            600, min(1800, task_parallelism * 150)),
        # Timer watchers currently own one sleeping daemon thread apiece.
        # Bound both durable admission and the live registry from this one
        # launch-time probe; future coordinator/queue implementations can
        # retain the same observable product budget.
        'TOFU_TIMER_LIVE_CAP': max(
            8, min(16, task_parallelism * 2)),
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY': (
            memory_metadata_cache_capacity),
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB': memory_metadata_cache_max_mib,
        # Full paper source is useful only while a user asks successive
        # questions about a small active set. Each entry is independently
        # capped at one million characters by the Paper contract, so this
        # probe-derived cardinality also gives the cache a deterministic
        # resident envelope. The consumer repeats a lower hard ceiling.
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': max(
            1, min(8, (task_parallelism + 1) // 2)),
        'TOFU_TRANSLATE_CACHE_MAX_MIB': translate_cache_max_mib,
        # Optional translation enrichment must not turn a provider-wide
        # capacity incident into hundreds of paid transport attempts. Scale
        # enough to sample several keys/models, then stop independently of the
        # longer wall-clock deadline. Interactive task dispatch stays uncapped.
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS': max(
            4, min(8, task_parallelism * 2)),
        # Whole-turn, explicit text, PPTX, and paper translation share this
        # one optional-work lane. Keep provider concurrency below ordinary
        # root-task concurrency and retain only a finite owner-fair backlog.
        'TOFU_TRANSLATE_WORKERS': max(
            1, min(2, task_parallelism)),
        'TOFU_TRANSLATE_QUEUE_CAPACITY': max(
            4, min(32, task_parallelism * 4)),
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS': 60,
        # Reconstructible metadata and maintenance enrichments are lower value
        # than an explicit production deliverable. Two actual 429 responses
        # can sample a second slot without inheriting production's 4..8-call
        # personal allowance; attended Agent dispatch remains uncapped here.
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS': 2,
        # Independent long-production calls share one per-job fan-out budget.
        # It never exceeds two on a personal computer; root-task admission is
        # the separate process-wide multiplier.
        'TOFU_PRODUCTION_LLM_FANOUT': max(
            1, min(2, task_parallelism)),
        # Paid long-production calls must not inherit the interactive
        # dispatcher's intentionally unbounded 429 cycling. Larger admitted
        # machines may sample more provider slots, still under a hard caller
        # ceiling.
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS': max(
            4, min(8, task_parallelism * 2)),
        # Generated image replies retain base64 plus decoded pixels. Keep the
        # per-job HTTP fan-out independently observable from text-model calls.
        'TOFU_PRODUCTION_IMAGE_FANOUT': max(
            1, min(2, task_parallelism)),
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS': max(
            4, min(8, task_parallelism * 2)),
        # TTS is HTTP-bound but each response retains audio bytes until ordered
        # assembly. Two personal lanes shorten long narration without letting
        # one task own the process or provider.
        'TOFU_PRODUCTION_TTS_FANOUT': max(
            1, min(2, task_parallelism)),
        'TOFU_PDF_PROCESSES': classic_pdf_processes,
        'TOFU_PDF_PARSE_CAPACITY': classic_pdf_capacity,
        'TOFU_PDF_MAX_PAGES': classic_pdf_max_pages,
        'TOFU_PDF_MAX_TEXT_MIB': classic_pdf_max_text_mib,
        'TOFU_PDF_PARSE_TIMEOUT': classic_pdf_timeout,
        'TOFU_PDF_WORKER_IDLE_SECONDS': 60,
        # One owner-fair lane bounds retained source PDFs. Page-level calls
        # have a second, lower ceiling, so neither job count nor a large PDF
        # recreates an unbounded thread/API fan-out inside an admitted job.
        'TOFU_PDF_VLM_TASK_WORKERS': (
            2 if task_parallelism >= 16 else 1),
        'TOFU_PDF_VLM_QUEUE_CAPACITY': max(
            2, min(8, max(1, task_parallelism // 2))),
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_VLM_CALL_WORKERS': max(
            1, min(4, max(1, task_parallelism // 2))),
        'TOFU_PDF_VLM_MAX_PAGES': vlm_max_pages,
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS': vlm_task_timeout_seconds,
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS': max(
            4, min(8, task_parallelism * 2)),
        # Visual descriptions are optional, paid, and backed by durable asset
        # claims. One process-wide owner-fair scheduler therefore retains only
        # owner IDs, executes one asset per owner turn, and keeps personal-host
        # provider concurrency below general foreground task concurrency.
        'TOFU_KNOWLEDGE_ENRICH_WORKERS': (
            2 if task_parallelism >= 16 else 1),
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY': max(
            4, min(32, task_parallelism * 4)),
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS': 60,
        # Every session retains a private scheduler for dependency state, but
        # expensive SubAgent execution also crosses one process-wide,
        # owner-fair gate. These caps bound thread/API fan-out, accepted model
        # work, retries, and live conversation registries independently.
        'TOFU_SWARM_GLOBAL_WORKERS': swarm_global_workers,
        'TOFU_SWARM_MAX_PARALLEL': swarm_global_workers,
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE': swarm_max_agents_per_wave,
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION': (
            swarm_max_agents_per_session),
        'TOFU_SWARM_MAX_RETRIES': 1,
        'TOFU_SWARM_SESSION_CAPACITY': swarm_session_capacity,
        'TOFU_CONTROL_RPC_WORKERS': control_rpc_workers,
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY': max(
            16, min(128, io_parallelism * 8)),
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': project_refresh_idle_seconds,
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY': max(
            32, min(256, io_parallelism * 16)),
        'TOFU_TREE_INDEX_WALK_JOBS': tree_index_walk_jobs,
        'TOFU_TREE_INDEX_MAX_ENTRIES': tree_index_max_entries,
        'TOFU_TREE_INDEX_MEM_ROOTS': tree_index_mem_roots,
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE': max(
            2, min(16, task_parallelism * 2)),
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY': max(
            8, min(32, task_parallelism * 4)),
        # Preview enrichment must never grow with the lifetime of a Turn.
        # The terminal deliverable is outside this per-accumulator allowance.
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS': 32,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS': 30,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS': 256,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS': 1,
        'TOFU_MAX_INFLIGHT_TASKS': task_parallelism,
        'TOFU_TASK_RSS_RESERVE_MB': task_rss_reserve_mb,
        'TOFU_SYNC_WORKERS': sync_workers,
        'TOFU_AGENT_WORKERS': task_parallelism,
        'TOOL_MAX_PARALLEL_WORKERS': tool_parallelism,
        'TOFU_NUMERIC_THREADS': numeric_threads,
        # Credential probes have no foreground cancellation owner. Defer a
        # diagnostic when these finite slots are busy; the maintenance sweep
        # retries it without delaying connection or retaining another thread.
        'TOFU_MCP_CRED_PROBE_WORKERS': max(
            1, min(4, max(1, task_parallelism // 2))),
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
        'TOFU_PROCESS_RSS_RELIEF_MB': soft_rss_mb,
        'TOFU_PROCESS_RSS_RECYCLE_MB': hard_rss_mb,
    }


def deployment_resource_default(
    name: str,
    environment: Mapping[str, str] | None = None,
    *,
    snapshot: SystemResourceSnapshot | None = None,
) -> int:
    """Return one probed personal default or stable distributed fallback."""
    env = os.environ if environment is None else environment
    mode = (env.get('TOFU_DEPLOYMENT_MODE') or 'personal').strip().lower()
    profile = 'distributed' if mode == 'distributed' else 'personal'
    try:
        if profile == 'distributed':
            return int(_RESOURCE_FALLBACKS[profile][name])
        observed = snapshot or probe_system_resources(env)
        return int(_personal_resource_defaults(observed)[name])
    except KeyError as exc:
        raise KeyError(f'unknown deployment resource: {name}') from exc


def task_concurrency_hard_ceiling(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return the personal task ceiling implied by the worker RSS budget.

    Operators may raise task concurrency, but not independently of the process
    hard ceiling that the lifecycle manager enforces.  Raising both remains an
    explicit supported override. Distributed replicas retain their external
    deployment admission contract.
    """
    env = os.environ if environment is None else environment
    mode = (env.get('TOFU_DEPLOYMENT_MODE') or 'personal').strip().lower()
    if mode == 'distributed':
        return 256

    def _positive_mb(name: str) -> int:
        default = deployment_resource_default(name, env)
        try:
            value = int(float(str(env.get(name) or default)))
        except (TypeError, ValueError, OverflowError):
            value = default
        return value

    hard_rss_mb = _positive_mb('TOFU_PROCESS_RSS_RECYCLE_MB')
    if hard_rss_mb <= 0:
        return 256
    task_reserve_mb = max(
        64, _positive_mb('TOFU_TASK_RSS_RESERVE_MB'))
    return min(
        256,
        _task_slots_from_rss_budget(hard_rss_mb, task_reserve_mb),
    )


def resolve_resource_budget(
    name: str,
    environment: Mapping[str, str] | None = None,
    *,
    minimum: int = 1,
    maximum: int,
    snapshot: SystemResourceSnapshot | None = None,
) -> int:
    """Resolve an operator override against one probed default and hard cap.

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


def _automatic_resource_names(environment: Mapping[str, str]) -> set[str]:
    """Parse the internal provenance marker without widening public knobs."""
    raw = str(environment.get(RESOURCE_BUDGET_AUTOMATIC_ENV) or '')
    return {
        name for name in raw.split(',')
        if name in RESOURCE_BUDGET_ENV_KEYS
    }


def _prepare_resource_budget_environment(
    environment: MutableMapping[str, str],
) -> set[str]:
    """Discard only defaults attributed to an older policy generation.

    Explicit settings are never inferred from their numeric value.  A value is
    replaceable only when a prior Tofu generation wrote its name into the
    internal provenance marker.  This lets an in-place exec adopt a new policy
    without laundering generated values into permanent operator overrides.
    """
    previous_policy = str(
        environment.get(RESOURCE_BUDGET_POLICY_ENV) or '').strip()
    automatic = _automatic_resource_names(environment) if previous_policy else set()
    if previous_policy and previous_policy != RESOURCE_BUDGET_POLICY_VERSION:
        previous_allocator = environment.get('TOFU_MALLOC_ARENA_MAX')
        for name in automatic:
            environment.pop(name, None)
        if 'TOFU_MALLOC_ARENA_MAX' in automatic \
                and environment.get('MALLOC_ARENA_MAX') == previous_allocator:
            environment.pop('MALLOC_ARENA_MAX', None)
        automatic.clear()
        environment.pop(RESOURCE_BUDGET_AUTOMATIC_ENV, None)
    return automatic


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
    """Materialize one boot-time adaptive snapshot into otherwise-empty knobs."""
    env = os.environ if environment is None else environment
    automatic = _prepare_resource_budget_environment(env)
    observed = snapshot or probe_system_resources(env, refresh=True)
    manifest = resource_budget_manifest(env, snapshot=observed)
    for name, default_value in manifest['defaults'].items():
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
    """Install one probed resource snapshot before starting a new process.

    glibc otherwise creates up to several allocator arenas per CPU. Tofu has
    multiple bounded thread pools, so a high-core host can retain dozens of
    mostly-empty 64 MiB arenas after large RPC/tool payloads have been freed.
    The setting must exist *before* the Python child starts; setting it inside
    that child is too late. All source/desktop launchers and the storage
    supervisor therefore call this one function while constructing child env.
    It also materializes every otherwise-empty adaptive default into that child
    environment, so the server and its later Sidecar agree on one boot-time
    observation instead of independently reacting to a transient fluctuation.

    Personal mode optimizes for an 8 GiB shared computer. Distributed mode
    keeps a larger default for concurrent replicas, and either profile remains
    explicitly overridable through one bounded Tofu setting.
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
    postgres_dsn: str = field(default='', repr=False)
    redis_url: str = field(default='', repr=False)

    @property
    def storage_backend(self) -> str:
        return 'postgres' if self.mode == 'distributed' else 'sqlite'


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
        return DeploymentConfiguration(
            mode=mode,
            process_role=role,
            replica_id=None,
            postgres_dsn_file=None,
            redis_url_file=None,
            distributed_preview_read_only=False,
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
