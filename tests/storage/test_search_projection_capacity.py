"""Automatic search-projection placement: capacity admission, yielding and reclaim."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from lib.storage_authority_sql import native_fastpath_auto as auto
from lib.storage_authority_sql import projection_paths as paths
from serverctl_pkg import local_placement as placement

fcntl = pytest.importorskip('fcntl')

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != 'posix', reason='automatic placement is POSIX-only'),
]

GIB = 1024 ** 3
FILES_PER_SHARD = 50
# _native_tree samples four shards, so the census sees four times this many files.
TREE_FILES = 4 * FILES_PER_SHARD


class Volume:
    """statvfs and disk_usage views of one local volume the test controls."""

    def __init__(self):
        self.healthy()

    def healthy(self):
        self.files, self.free_files = 1_000_000, 900_000
        self.total, self.free = 500 * GIB, 400 * GIB

    def starved(self, free_files):
        # The volume the copy occupies: 1,000 inodes, a 5% (50-inode) reserve.
        self.files, self.free_files = 1_000, free_files

    def statvfs(self, _path):
        return SimpleNamespace(f_files=self.files, f_favail=self.free_files)

    def disk_usage(self, _path):
        return SimpleNamespace(total=self.total, free=self.free, used=self.total - self.free)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    (data / 'tofudb-authority').mkdir(parents=True)
    cache = tmp_path / 'cache'
    monkeypatch.setattr(placement, 'index_cache_placement', lambda *a, **k: placement.Placement(
        True, placement.PURPOSE_INDEXES, 'auto', 'selected', root=str(cache)))
    volume = Volume()
    monkeypatch.setattr(os, 'statvfs', volume.statvfs)
    monkeypatch.setattr(shutil, 'disk_usage', volume.disk_usage)
    candidate = paths.resolve_search_projection_paths(data, environ={}).directory
    return SimpleNamespace(data=data.resolve(), cache=cache, volume=volume, candidate=candidate,
                           legacy=(data / 'projections' / 'tofudb-search').resolve())


def _native_tree(directory: Path, files_per_shard: int = FILES_PER_SHARD) -> Path:
    for shard in ('00', '40', '80', 'c0'):
        blocks = directory / 'blocks' / shard
        blocks.mkdir(parents=True, exist_ok=True)
        for index in range(files_per_shard):
            (blocks / f'{shard}{index:062x}.blk').write_bytes(b'x' * 64)
    (directory / 'LOCK').touch()
    (directory / 'CONTROL').write_bytes(b'c' * 64)
    return directory


def _local_copy(layout, files_per_shard: int = FILES_PER_SHARD) -> Path:
    placement.ensure_private_directory(layout.cache)
    placement.ensure_private_directory(layout.candidate.parent)
    return _native_tree(layout.candidate, files_per_shard)


def _launch(layout) -> Path:
    return paths.prepare_search_projection_paths(layout.data, environ={}).directory


def _placement_state(layout) -> dict:
    return auto.search_projection_placement_state(layout.data)


def _remove_now(retired):
    for path in retired:
        shutil.rmtree(path)


def test_a_local_copy_that_no_longer_fits_yields_to_the_data_volume_projection(layout):
    copy = _local_copy(layout)
    layout.volume.starved(free_files=30)
    assert _launch(layout) == layout.legacy
    assert copy.is_dir(), 'the launch never deletes; reclaim runs off the launch path'
    state = _placement_state(layout)
    assert (state['lastLaunch']['selected'], state['lastLaunch']['reason']) == (
        False, 'capacity-critical')
    assert state['reclaim']['pending'] == [str(copy)]


def test_a_fitting_local_copy_keeps_serving_without_a_launch_path_scan(layout, monkeypatch):
    _local_copy(layout)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('the launch path never walks a projection')

    monkeypatch.setattr(auto, 'take_census', forbidden)
    assert _launch(layout) == layout.candidate
    state = _placement_state(layout)
    assert state['lastLaunch']['selected'] and not state['reclaim'].get('pending')


def test_a_copy_under_pressure_keeps_serving_so_native_gc_can_reclaim_it(layout):
    copy = _local_copy(layout)
    assert _launch(layout) == copy
    assert auto.maintain_search_projection(layout.data, str(copy))['capacity']['state'] == 'ok'
    relieved = []
    # 60 spare inodes above the 50-inode reserve: under half the copy's 200 files,
    # over a fifth of them.
    layout.volume.starved(free_files=110)
    result = auto.maintain_search_projection(
        layout.data, str(copy), relieve=lambda path: relieved.append(path) or {})
    assert result['capacity']['state'] == 'pressure' and relieved == [copy]
    assert _launch(layout) == copy


def test_a_fresh_installation_places_its_projection_locally(layout):
    assert _launch(layout) == layout.candidate
    assert not layout.candidate.exists(), 'native open initializes the absent leaf'


def test_a_new_local_copy_waits_for_a_measured_data_volume_projection(layout):
    _native_tree(layout.legacy)
    assert _launch(layout) == layout.legacy
    assert _placement_state(layout)['lastLaunch']['reason'] == 'census-pending'

    measured = auto.maintain_search_projection(layout.data, str(layout.legacy))
    assert measured['census']['looseBlockFiles'] == TREE_FILES
    # A rebuild needs the measured files plus equal growth room above the reserve.
    layout.volume.starved(free_files=2 * TREE_FILES + 49)
    assert _launch(layout) == layout.legacy
    assert _placement_state(layout)['lastLaunch']['reason'] == 'insufficient-capacity'
    layout.volume.healthy()
    assert _launch(layout) == layout.candidate
    assert layout.legacy.is_dir(), 'the data-volume projection is preserved'


def test_a_serving_copy_that_stops_fitting_moves_at_the_next_launch(layout):
    copy = _local_copy(layout)
    assert _launch(layout) == layout.candidate
    relieved = []
    layout.volume.starved(free_files=30)
    result = auto.maintain_search_projection(
        layout.data, str(copy), relieve=lambda path: relieved.append(path) or {})
    assert result['capacity']['state'] == 'critical' and relieved == [copy]
    assert copy.is_dir(), 'a serving copy is never removed'
    resolved = paths.resolve_search_projection_paths(layout.data, environ={})
    assert resolved.directory == copy, 'resolution stays a pure policy read'
    assert paths.automatic_placement_decision(layout.data, resolved, launch=False)['selected'] is False
    assert _launch(layout) == layout.legacy


def test_a_yielded_copy_is_reclaimed_only_once_its_engine_lock_is_free(layout):
    copy = _local_copy(layout, files_per_shard=5)
    layout.volume.starved(free_files=10)
    assert _launch(layout) == layout.legacy
    holder = os.open(copy / 'LOCK', os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auto.maintain_search_projection(layout.data, str(layout.legacy), remove=_remove_now)
        assert (copy / 'CONTROL').is_file(), 'a copy an engine still holds is never touched'
        assert _placement_state(layout)['reclaim']['pending'] == [str(copy)]
    finally:
        os.close(holder)
    auto.maintain_search_projection(layout.data, str(layout.legacy), remove=_remove_now)
    assert not copy.exists() and list(copy.parent.iterdir()) == []
    assert _placement_state(layout)['reclaim']['pending'] == []


def test_reclaim_refuses_anything_but_this_authoritys_yielded_copy(layout, tmp_path):
    _native_tree(layout.legacy)
    placement.ensure_private_directory(layout.cache)
    placement.ensure_private_directory(layout.candidate.parent)
    foreign = _native_tree(layout.candidate.parent / ('ab' * 32))
    outside = _native_tree(tmp_path / 'outside')
    layout.candidate.symlink_to(outside, target_is_directory=True)
    state_path = auto.state_path(layout.data)
    state_path.write_text(json.dumps({
        'format': auto.STATE_FORMAT,
        'searchProjection': {'candidate': str(layout.candidate), 'legacy': str(layout.legacy),
                             'selected': False, 'reason': 'capacity-critical'},
        'searchProjectionReclaim': {'pending': [
            str(layout.legacy), str(foreign), str(layout.candidate), str(tmp_path / 'outside')]},
    }))
    auto.maintain_search_projection(layout.data, str(layout.legacy), remove=_remove_now)
    for tree in (layout.legacy, foreign, outside):
        assert (tree / 'CONTROL').is_file()
    assert layout.candidate.is_symlink()


def test_an_interrupted_removal_resumes_and_spares_other_installations(layout):
    copy = _local_copy(layout, files_per_shard=2)
    layout.volume.starved(free_files=10)
    assert _launch(layout) == layout.legacy
    leftover = _native_tree(copy.parent / f'{copy.name}.retired-{"0" * 32}', files_per_shard=1)
    other = _native_tree(copy.parent / f'{"ab" * 32}.retired-{"0" * 32}', files_per_shard=1)
    auto.maintain_search_projection(layout.data, str(layout.legacy), remove=_remove_now)
    assert not copy.exists() and not leftover.exists()
    assert (other / 'CONTROL').is_file()


def test_the_panel_reports_where_the_next_launch_serves(layout, monkeypatch):
    from lib.database_panel import probe

    _local_copy(layout)
    layout.volume.starved(free_files=30)
    assert _launch(layout) == layout.legacy
    monkeypatch.setattr(probe, 'data_root', lambda: str(layout.data))
    state = probe.search_projection_state(
        'tofudb', {'search_projection_directory': str(layout.legacy)})
    assert state['directory'] == state['pendingDirectory'] == str(layout.legacy)
    assert not state['restartRequired']
    last = _placement_state(layout)['lastLaunch']
    assert (last['selected'], last['reason']) == (False, 'capacity-critical')


def test_the_panel_reports_a_pending_yield_as_a_restart(layout, monkeypatch):
    from lib.database_panel import probe

    copy = _local_copy(layout)
    assert _launch(layout) == copy
    layout.volume.starved(free_files=30)
    monkeypatch.setattr(probe, 'data_root', lambda: str(layout.data))
    state = probe.search_projection_state('tofudb', {'search_projection_directory': str(copy)})
    assert state['directory'] == str(copy)
    assert state['pendingDirectory'] == str(layout.legacy) and state['restartRequired']
    assert _placement_state(layout)['lastLaunch']['selected'], 'status never records a decision'
