"""Folder-picker scans remain shallow and explicitly bounded."""

from __future__ import annotations

import os

import orjson
import pytest


pytestmark = pytest.mark.unit


def test_browse_directory_bounds_nested_fuse_metadata_reads(
    tmp_path, monkeypatch,
):
    from lib.project_mod import tools

    for directory_index in range(15):
        directory = tmp_path / f'dir-{directory_index:02d}'
        directory.mkdir()
        for file_index in range(2):
            (directory / f'file-{file_index}.py').write_text(
                'pass\n', encoding='utf-8')
    for file_index in range(3):
        (tmp_path / f'root-{file_index}.txt').write_text(
            'root\n', encoding='utf-8')

    monkeypatch.setattr(tools, '_PROJECT_BROWSE_MAX_DIRS', 10)
    monkeypatch.setattr(tools, '_PROJECT_BROWSE_DETAIL_DIRS', 2)
    monkeypatch.setattr(tools, '_PROJECT_BROWSE_DETAIL_ENTRIES', 4)
    real_scandir = os.scandir
    scanned_paths = []

    def tracked_scandir(path):
        scanned_paths.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(tools.os, 'scandir', tracked_scandir)

    result = tools.browse_directory(str(tmp_path))

    assert result['truncated'] is True
    assert len(result['dirs']) == 10
    assert result['dirs'] == sorted(
        result['dirs'], key=lambda item: item['name'].lower())
    assert len(scanned_paths) == 3  # current directory + only two child details
    assert sum(item['itemCount'] for item in result['dirs']) <= 4
    assert sum(not item['detailsDeferred'] for item in result['dirs']) == 2
    assert all(item['hasCode'] for item in result['dirs'][:2])
    assert all(item['detailsDeferred'] for item in result['dirs'][2:])


def test_browse_directory_bounds_total_entries_and_response_bytes(
    tmp_path, monkeypatch,
):
    from lib.project_mod import tools

    class _Entry:
        def __init__(self, index):
            self.name = f'directory-{index:04d}-' + ('x' * 80)
            self.path = str(tmp_path / self.name)

        def is_dir(self, *, follow_symlinks):
            assert follow_symlinks is False
            return True

        def is_file(self, *, follow_symlinks):  # pragma: no cover
            raise AssertionError('directory was reclassified as a file')

    class _Entries:
        def __enter__(self):
            return iter(_Entry(index) for index in range(100_000))

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(tools.os.path, 'isdir', lambda _path: True)
    monkeypatch.setattr(tools.os, 'scandir', lambda _path: _Entries())
    monkeypatch.setattr(tools, '_PROJECT_BROWSE_MAX_ENTRIES', 40)
    monkeypatch.setattr(tools, '_PROJECT_BROWSE_MAX_DIRS', 1000)
    monkeypatch.setattr(tools, '_PROJECT_BROWSE_RESPONSE_BYTES', 4096)

    result = tools.browse_directory(str(tmp_path))

    assert result['truncated'] is True
    assert result['scannedEntries'] <= 40
    assert len(orjson.dumps(result)) <= 4096 + 1024
