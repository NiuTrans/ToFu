"""Shared stdlib CPU, memory and volume observations for optional worker budgets.

The application owns launch-snapshot caching and its data-volume selection.
Standalone callers provide their own working volume and keep the same cgroup,
affinity and headroom semantics. No sampler, worker or model is started here.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MIB = 1024 * 1024


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


def measure_resources(*, cgroup_cpus: int | None,
                      host_memory: tuple[int | None, int | None],
                      cgroup_memory: tuple[int | None, int | None],
                      disk_bytes: tuple[int | None, int | None]) -> SystemResourceSnapshot:
    """Combine explicit observations using the common capacity semantics."""
    try:
        host_cpus = max(1, int(os.cpu_count() or 1))
    except (TypeError, ValueError, OverflowError):
        host_cpus = 1
    try:
        affinity_cpus = max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError, TypeError, ValueError):
        affinity_cpus = None
    effective_cpus = min(
        value for value in (host_cpus, affinity_cpus, cgroup_cpus)
        if value is not None)

    host_total, host_available = host_memory
    cgroup_limit, cgroup_current = cgroup_memory
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
    disk_total, disk_free = disk_bytes

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
    return snapshot


def disk_capacity_bytes(volume_path: str | Path) -> tuple[int | None, int | None]:
    """Observe the selected existing volume, walking only missing parents."""
    path = Path(volume_path)
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


def probe_resources(volume_path: str | Path) -> SystemResourceSnapshot:
    """Take one uncached observation; the caller owns snapshot lifetime."""
    return measure_resources(
        cgroup_cpus=_cgroup_cpu_count(), host_memory=_host_memory_bytes(),
        cgroup_memory=_cgroup_memory_bytes(), disk_bytes=disk_capacity_bytes(volume_path))
