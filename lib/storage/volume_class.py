"""Launch-time volume classification for the storage transport decision.

Mirrors the engine's own classifier
(``packages/tofu-db/src/resource_probe.rs::classify_mounts`` and
``is_network_fstype``): the supervisor's automatic transport resolution and
the engine's automatic durability tier must agree on what a network volume
is, so a drift-guard test (``tests/storage/test_volume_class.py``) pins this
fstype set to the Rust source. Observation gaps — a non-Linux platform, an
unreadable or overlong mount table, no matching mount entry — answer
``UNKNOWN``, which both sides treat as the always-safe local tier (strict
durability, embedded transport eligible): misclassifying a network mount as
local costs latency, never correctness.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath

LOCAL = 'local'
NETWORK = 'network'
UNKNOWN = 'unknown'

# Keep exactly equal to the `matches!(...)` arm of
# `resource_probe.rs::is_network_fstype` (the drift-guard test enforces it).
_NETWORK_FSTYPES_EXACT = frozenset({
    'nfs', 'nfs4', 'cifs', 'smbfs', 'smb3', 'ceph', 'glusterfs', '9p', 'sshfs',
})

_MAX_MOUNTS_BYTES = 64 * 1024


def _is_network_fstype(fstype: str) -> bool:
    return fstype.startswith('fuse') or fstype in _NETWORK_FSTYPES_EXACT


def classify_mounts(mounts_text: str, canonical_path: str) -> str:
    """Longest-mount-prefix classification over one mount-table document;
    pure so every edge case is a deterministic unit fixture."""
    best: tuple[int, str] | None = None
    canonical = PurePath(canonical_path)
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        target, fstype = fields[1], fields[2]
        mount = PurePath(target)
        if canonical.is_relative_to(mount) and (
                best is None or len(target) > best[0]):
            best = (len(target), fstype)
    if best is None:
        return UNKNOWN
    return NETWORK if _is_network_fstype(best[1]) else LOCAL


def volume_class(path: Path) -> str:
    """Classify the volume hosting ``path``; ``UNKNOWN`` on any gap."""
    mounts = Path('/proc/mounts')
    if os.name != 'posix' or not mounts.is_file():
        return UNKNOWN
    try:
        canonical = Path(path).resolve()
        with mounts.open('rb') as handle:
            raw = handle.read(_MAX_MOUNTS_BYTES + 1)
        if len(raw) > _MAX_MOUNTS_BYTES:
            return UNKNOWN
        text = raw.decode('utf-8')
    except (OSError, UnicodeError):
        return UNKNOWN
    return classify_mounts(text, str(canonical))
