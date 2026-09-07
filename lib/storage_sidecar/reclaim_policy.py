"""Pure SQLite reclaim/copy sizing policy shared by runtime and offline tools.

Entry points consume integer page/byte facts only. They perform no I/O and
know no authority path, keeping the online writer, deep-clean CLI, and local
fast-path capacity gates on one machine-testable boundary.
"""

from __future__ import annotations


BULK_FREELIST_MIN_PAGES = 1_048_576
BULK_FREELIST_MIN_RATIO = 0.25
COPY_RESERVE_MIN_BYTES = 1024 ** 3
COPY_RESERVE_MAX_BYTES = 8 * 1024 ** 3

# Online page relocation is permitted only when the storage topology is known
# to keep one 4 KiB SQLite operation local to the host.  Network and generic
# userspace filesystems can turn even ``incremental_vacuum(1)`` into an
# uninterruptible remote metadata/page operation; SQLite cannot observe a
# Python watchdog until that call returns.  Container overlay and memory
# filesystems are host-local from this latency perspective, even though they
# have different durability semantics (which the authority preflight owns).
ONLINE_RECLAIM_STORAGE_CLASSES = frozenset({
    'local-block',
    'memory-filesystem',
    'container-overlay',
})


def online_reclaim_allowed(storage_class: str) -> bool:
    """Return whether SQLite may enter its writer for automatic page moves."""
    return str(storage_class or '').strip().lower() in (
        ONLINE_RECLAIM_STORAGE_CLASSES
    )


def requires_offline_compaction(
    freelist_pages: int,
    page_count: int,
) -> bool:
    free = max(0, int(freelist_pages))
    total = max(0, int(page_count))
    return (
        free >= BULK_FREELIST_MIN_PAGES
        and free / max(1, total) >= BULK_FREELIST_MIN_RATIO
    )


def copy_capacity_requirement(
    source_bytes: int,
    *,
    minimum_free_bytes: int = 0,
) -> dict[str, int]:
    """Return bounded reserve and total free bytes required for one copy."""
    source = max(0, int(source_bytes))
    reserve = max(
        COPY_RESERVE_MIN_BYTES,
        min(COPY_RESERVE_MAX_BYTES, source // 20),
    )
    return {
        'source_bytes': source,
        'reserve_bytes': reserve,
        'required_free_bytes': max(
            max(0, int(minimum_free_bytes)), source + reserve),
    }


__all__ = [
    'BULK_FREELIST_MIN_PAGES',
    'BULK_FREELIST_MIN_RATIO',
    'COPY_RESERVE_MAX_BYTES',
    'COPY_RESERVE_MIN_BYTES',
    'ONLINE_RECLAIM_STORAGE_CLASSES',
    'copy_capacity_requirement',
    'online_reclaim_allowed',
    'requires_offline_compaction',
]
