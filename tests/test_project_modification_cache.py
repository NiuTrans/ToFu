"""Bounded project undo-cache contracts; disk remains the authority."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_undo_cache_evicts_lru_and_reloads_from_authority(
    monkeypatch,
    tmp_path,
):
    from lib.project_mod import modifications

    directories = [tmp_path / name for name in ('a', 'b', 'c')]
    for directory in directories:
        directory.mkdir()
    loads: list[str] = []

    def load(session_dir):
        loads.append(session_dir)
        return [{'session': session_dir}]

    monkeypatch.setattr(modifications, '_MODS_CACHE_CAPACITY', 2)
    monkeypatch.setattr(modifications, '_load_from_disk', load)
    monkeypatch.setattr(modifications, '_clean_stale_tmp', lambda _path: None)
    with modifications._lock:
        modifications._mods_cache.clear()
        try:
            for directory in directories:
                modifications._cache_get(str(directory))

            assert list(modifications._mods_cache) == [
                str(directories[1]), str(directories[2]),
            ]
            assert modifications.modification_cache_snapshot() == {
                'entries': 2,
                'capacity': 2,
                'pendingRecords': 2,
            }

            # The evicted record is reconstructed from disk and becomes newest.
            modifications._cache_get(str(directories[0]))
            assert loads.count(str(directories[0])) == 2
            assert list(modifications._mods_cache) == [
                str(directories[2]), str(directories[0]),
            ]
        finally:
            modifications._mods_cache.clear()
