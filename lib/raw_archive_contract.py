"""Machine-readable scalar limits shared by raw-archive client and storage.

Raw archive bytes remain owned by ``lib.raw_archive`` and the storage sidecar.
This leaf contains only their wire-safe numeric ceiling so filesystem probes
and protocol validation cannot drift on very large or virtual volumes.
"""

RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES = 1_000_000_000_000_000

__all__ = ['RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES']
