"""Automatic index acceleration never moves durable state or writes on status."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverctl_pkg import local_placement as placement
from tests.server.test_local_placement import (
    _choose, _layout, _mountinfo, _usage, durable_tmp as durable_tmp,
)

pytestmark = pytest.mark.unit


def test_index_selection_is_read_only_and_allows_noexec(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    result = _choose(source, local, environment, placement.PURPOSE_INDEXES,
                     mountinfo_text=_mountinfo(source, local, local_options='rw,noexec'),
                     create=False)
    assert result.selected
    assert result.root == str(local / 'home/.cache/tofu/indexes')
    assert not Path(result.root).exists()


def test_insufficient_speedup_tries_next_candidate(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    slow = local / 'slow-cache'
    slow.mkdir()
    environment['XDG_CACHE_HOME'] = str(slow)
    result = _choose(source, local, environment, placement.PURPOSE_INDEXES,
                     latency_probe=lambda path: 400 if path.startswith(str(source)) else (
                         300 if path.startswith(str(slow)) else 5), create=False)
    assert result.selected
    assert result.root == str(local / 'home/.cache/tofu/indexes')


@pytest.mark.parametrize('kind', ['tmpfs', 'nfs4', 'unknownfs'])
def test_index_auto_refuses_nonlocal_or_volatile_targets(tmp_path, durable_tmp, kind):
    source, local, environment = _layout(tmp_path)
    result = _choose(source, local, environment, placement.PURPOSE_INDEXES,
                     mountinfo_text=_mountinfo(source, local, local_fs=kind), create=False)
    assert not result.selected


def test_index_decision_is_stable_and_does_not_create_directories(tmp_path, monkeypatch):
    calls = []
    def choose(source, purpose, **kwargs):
        calls.append((source, kwargs))
        return placement.Placement(True, purpose, 'auto', 'selected', root=str(tmp_path / 'cache'))
    monkeypatch.setattr(placement, 'choose_local_root', choose)
    placement._index_placement.cache_clear()
    try:
        first = placement.index_cache_placement(tmp_path, environ={})
        second = placement.index_cache_placement(tmp_path, environ={})
        assert first == second and len(calls) == 1
        assert calls[0][1]['create'] is False
        assert not (tmp_path / 'cache').exists()
        placement.index_cache_placement(tmp_path, environ={'TOFU_INDEX_FASTPATH': 'off'})
        assert len(calls) == 2
    finally:
        placement._index_placement.cache_clear()


def test_small_disk_reserve_is_useful_without_excluding_small_disks(tmp_path, monkeypatch):
    gib = 1024 ** 3
    monkeypatch.setattr(placement, 'has_free_inodes', lambda path, required=0: True)
    assert placement.reserve_bytes(gib) == gib // 4
    assert placement.has_free_space(tmp_path, gib // 8, disk_usage=_usage(gib // 2, gib))
    assert not placement.has_free_space(tmp_path, gib // 2, disk_usage=_usage(gib // 2, gib))
    assert placement.reserve_bytes(100_000 * gib) == 8 * gib
    assert not placement.has_free_space(tmp_path, disk_usage=_usage(0, 0))


@pytest.mark.parametrize(('total', 'free', 'admitted'), [(1000, 0, False), (1000, 20, False),
                                                      (1000, 500, True), (0, 0, True)])
def test_inode_pressure_is_independent_of_free_bytes(tmp_path, monkeypatch, total, free, admitted):
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_files=total, f_favail=free))
    assert placement.has_free_space(tmp_path, disk_usage=_usage()) is admitted


# A 1000-inode volume keeps the 128-inode floor of the shared reserve.
@pytest.mark.parametrize(('required', 'admitted'), [(0, True), (492, True), (493, False)])
def test_inode_demand_must_fit_above_the_reserve(tmp_path, monkeypatch, required, admitted):
    # Byte room alone admitted a tree with more files than the volume had
    # inodes left; the copy then failed part-way with ENOSPC.
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_files=1000, f_favail=620))
    assert placement.has_free_inodes(tmp_path, required) is admitted
    assert placement.has_free_space(tmp_path, required_inodes=required,
                                    disk_usage=_usage()) is admitted


def test_symlink_cache_root_is_never_adopted(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    root = local / 'home/.cache/tofu/indexes'
    root.parent.mkdir(parents=True)
    root.symlink_to(source, target_is_directory=True)
    result = _choose(source, local, environment, placement.PURPOSE_INDEXES, create=False)
    assert not result.selected
