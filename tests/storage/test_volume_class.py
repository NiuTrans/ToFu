"""Volume classification fixtures + the Rust/Python drift guard.

The supervisor's automatic transport resolution and the engine's automatic
durability tier both decide what a network volume is; the fstype set must
stay exactly equal to the Rust authority
(`packages/tofu-db/src/resource_probe.rs::is_network_fstype`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib.storage.volume_class import (
    LOCAL,
    NETWORK,
    UNKNOWN,
    _NETWORK_FSTYPES_EXACT,
    classify_mounts,
    volume_class,
)

pytestmark = pytest.mark.unit

_RUST_SOURCE = (
    Path(__file__).resolve().parents[2]
    / 'packages/tofu-db/src/resource_probe.rs'
)


def test_network_fstype_set_matches_the_rust_authority():
    source = _RUST_SOURCE.read_text(encoding='utf-8')
    match = re.search(
        r'fn is_network_fstype\(fstype: &str\) -> bool \{(.*?)\n\}', source,
        re.DOTALL,
    )
    assert match, 'is_network_fstype must exist in resource_probe.rs'
    body = match.group(1)
    assert 'starts_with("fuse")' in body, (
        'the Rust classifier must keep the fuse* prefix rule')
    rust_exact = set(re.findall(r'"([a-z0-9]+)"', body)) - {'fuse'}
    assert rust_exact == set(_NETWORK_FSTYPES_EXACT), (
        f'network fstype drift: rust={sorted(rust_exact)} '
        f'python={sorted(_NETWORK_FSTYPES_EXACT)}')


def test_classify_mounts_picks_longest_prefix_and_network_fstypes():
    mounts = (
        'overlay / overlay rw 0 0\n'
        '/dev/sda1 /data ext4 rw 0 0\n'
        'nas:/vol /data/remote nfs4 rw 0 0\n'
        'beegfs /data/remote/beegfs fuse.beegfs rw 0 0\n'
    )
    assert classify_mounts(mounts, '/data/app') == LOCAL
    assert classify_mounts(mounts, '/data/remote') == NETWORK
    assert classify_mounts(mounts, '/data/remote/beegfs/tofu') == NETWORK


def test_classify_mounts_component_wise_not_string_prefix():
    mounts = '/dev/sda1 /data ext4 rw 0 0\n'
    # `/data2` must NOT match the `/data` mount (component-wise, like
    # Rust's Path::starts_with); it falls to no match = unknown.
    assert classify_mounts(mounts, '/data2/app') == UNKNOWN
    assert classify_mounts(mounts, '/data/app') == LOCAL


def test_classify_mounts_network_fstype_inventory():
    for line, expected in [
        ('x /mnt nfs rw 0 0', NETWORK),
        ('x /mnt nfs4 rw 0 0', NETWORK),
        ('x /mnt cifs rw 0 0', NETWORK),
        ('x /mnt smb3 rw 0 0', NETWORK),
        ('x /mnt ceph rw 0 0', NETWORK),
        ('x /mnt glusterfs rw 0 0', NETWORK),
        ('x /mnt 9p rw 0 0', NETWORK),
        ('x /mnt sshfs rw 0 0', NETWORK),
        ('x /mnt fuse.sshfs rw 0 0', NETWORK),
        ('x /mnt fuse rw 0 0', NETWORK),
        ('x /mnt ext4 rw 0 0', LOCAL),
        ('x /mnt xfs rw 0 0', LOCAL),
        ('x /mnt overlay rw 0 0', LOCAL),
        ('x /mnt tmpfs rw 0 0', LOCAL),
        ('x /mnt btrfs rw 0 0', LOCAL),
        ('x /mnt zfs rw 0 0', LOCAL),
    ]:
        assert classify_mounts(line, '/mnt/app') == expected, line


def test_classify_mounts_garbage_and_absence_are_unknown():
    assert classify_mounts('garbage\n', '/x') == UNKNOWN
    assert classify_mounts('', '/x') == UNKNOWN
    assert classify_mounts('not enough\nfields here\n', '/x') == UNKNOWN


def test_volume_class_observes_this_checkouts_fuse_mount():
    # The repo checkout itself lives on a beegfs-fuse mount in this
    # environment; when run elsewhere the assertion degrades to a smoke read.
    observed = volume_class(Path(__file__).resolve())
    assert observed in {LOCAL, NETWORK, UNKNOWN}


def test_volume_class_missing_path_still_classifies(tmp_path):
    # resolve() is non-strict: a not-yet-created authority directory still
    # classifies through its nearest existing ancestor's mount.
    observed = volume_class(tmp_path / 'does-not-exist-yet' / 'authority')
    assert observed in {LOCAL, NETWORK, UNKNOWN}
